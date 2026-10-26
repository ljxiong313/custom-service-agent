"""Chroma 向量存储封装（PersistentClient，data/chroma/）。

约定：collection "knowledge" 的文档 id = Chunk 表主键字符串，元数据带
file_id/file_name/enabled，实现「DB 为准、向量可重建」的弱一致模型：
- 删除文件 → delete by ids；禁用文件 → update metadata（检索侧过滤）。
embedding 走 core/models/manager（OpenAI 兼容），仅在这里被调用。
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache

from app.config import BACKEND_ROOT, get_settings

EMBED_BATCH = 10  # 单次 embedding 请求条数（服务端上限 20，留一半余量防长文本超限）


@dataclass
class VectorHit:
    chunk_id: int
    score: float          # 相似度（越大越相似，由距离换算）
    file_id: int
    file_name: str


@lru_cache(maxsize=1)
def _collection():
    """进程内复用一个 collection 句柄。

    旧实现每次调用都 new 一个 PersistentClient：一次 743 chunk 的重入库要开几十次
    SQLite 连接，且多个 client 同时持同一目录存在写者冲突风险。
    """
    import chromadb

    # 配置文件里写 chroma_path: "" 时 get_dotted 会返回空串而不是默认值，
    # 而 yaml 注释承诺"留空 = data/chroma/"——必须 or 回落，否则会开到一个空目录
    path = get_settings().get_dotted("rag.chroma_path", "") or str(BACKEND_ROOT / "data" / "chroma")
    client = chromadb.PersistentClient(path=path)
    return client.get_or_create_collection("knowledge", metadata={"hnsw:space": "cosine"})


def reset_collection_cache() -> None:
    """换库路径（测试用临时目录、脚本改配置）后必须调用，否则复用旧句柄。"""
    _collection.cache_clear()


def _embed(texts: list[str]) -> list[list[float]]:
    from app.core.models.manager import get_embeddings

    return get_embeddings().embed_documents(texts)


def add_chunks(chunks: list[tuple[int, str, int, str]],
               progress: Callable[[int, int], None] | None = None) -> None:
    """chunks: [(chunk_id, text, file_id, file_name)]

    显式传 embeddings——若让 Chroma 用默认 embedding function，
    会静默下载本地 ONNX 模型且与查询侧向量空间不一致（写入/查询必须同一模型）。

    分批发送：百炼兼容模式单次 embedding 请求上限 20 条（超了报
    "batch size is invalid"），大文件一次整批送会直接降级成"仅关键词检索"。
    progress(已完成条数, 总条数) 供任务表写进度。
    """
    if not chunks:
        return
    col = _collection()
    total = len(chunks)
    for i in range(0, total, EMBED_BATCH):
        batch = chunks[i:i + EMBED_BATCH]
        col.add(ids=[str(c[0]) for c in batch],
                documents=[c[1] for c in batch],
                embeddings=_embed([c[1] for c in batch]),
                metadatas=[{"file_id": c[2], "file_name": c[3], "enabled": True} for c in batch])
        if progress:
            progress(min(i + EMBED_BATCH, total), total)


def remove_chunks(chunk_ids: list[int]) -> None:
    if chunk_ids:
        _collection().delete(ids=[str(i) for i in chunk_ids])


def has_chunks(chunk_ids: list[int]) -> set[int]:
    """回传其中真正有向量的 chunk_id（管理页用来区分「已降级为仅关键词检索」的切片）。

    Chroma 不可用（未配置 embedding/目录损坏）时返回空集——只影响展示，不影响检索。
    """
    if not chunk_ids:
        return set()
    try:
        got = _collection().get(ids=[str(i) for i in chunk_ids])
    except Exception as e:
        from loguru import logger

        logger.warning("向量存在性检查失败: {}", e)
        return set()
    return {int(i) for i in got["ids"]}


def set_file_enabled(file_id: int, enabled: bool) -> None:
    col = _collection()
    got = col.get(where={"file_id": file_id})
    if got["ids"]:
        col.update(ids=got["ids"], metadatas=[{"enabled": enabled}] * len(got["ids"]))


def query(text: str, top_k: int = 20) -> list[VectorHit]:
    """向量召回（仅 enabled 文件）。距离→相似度换算：sim = 1 - cosine_distance。

    embedding 服务不可用（未配置 key/网络故障）时返回 []——handler 侧 BM25 兜底，
    检索链路整体可用，精度降级但不中断。
    """
    col = _collection()
    if col.count() == 0:
        return []
    try:
        emb = _embed([text])
    except Exception as e:
        from loguru import logger

        logger.warning("向量召回跳过（embedding 不可用，BM25 兜底）: {}", e)
        return []
    res = col.query(query_embeddings=emb, n_results=min(top_k, col.count()),
                    where={"enabled": True})
    hits = []
    for cid, dist, meta in zip(res["ids"][0], res["distances"][0], res["metadatas"][0],
                               strict=True):
        hits.append(VectorHit(chunk_id=int(cid), score=1.0 - dist,
                              file_id=int(meta["file_id"]), file_name=str(meta["file_name"])))
    return hits
