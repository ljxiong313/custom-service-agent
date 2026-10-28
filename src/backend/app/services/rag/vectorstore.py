"""Chroma 向量存储封装（PersistentClient，data/chroma/）。

约定：collection "knowledge" 的文档 id = Chunk 表主键字符串，元数据带
file_id/file_name/enabled，实现「DB 为准、向量可重建」的弱一致模型：
- 删除文件 → delete by ids；禁用文件 → update metadata（检索侧过滤）。
embedding 走 core/models/manager（OpenAI 兼容），仅在这里被调用。

写入侧的吞吐参数与失败口径（B1-9）：
- 批大小来自配置 rag.embedding_batch_size（默认 10，硬上限 20 = dashscope 兼容模式
  单请求上限）；代码里不再写死。
- 每批失败按 rerank 已有的重试语义退避重试（复用常数，不新造一套策略）。
- 重试仍失败的批次**不静默跳过**：其余批次继续（让尽可能多的切片拿到向量），
  最后抛出 EmbeddingBatchError，由 ingest/reindex 把错误文案写进
  KnowledgeFile.error 与 IngestJob.error——"只落 BM25"的降级保留，但必须显式可见。
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache

from loguru import logger

from app.config import BACKEND_ROOT, get_settings

EMBED_BATCH_DEFAULT = 10   # 单次 embedding 请求条数（留一半余量防长文本超限）
EMBED_BATCH_MIN = 1
EMBED_BATCH_MAX = 20       # 百炼兼容模式单次请求硬上限，超了直接报 "batch size is invalid"
_BATCH_WARNED = False
_BATCH_WARN_LOCK = threading.Lock()
# embedding 的重试语义与 rerank 完全一致（同样只重试网络/5xx/429 这类"再试一次可能就好"的
# 错误），所以直接复用它的常数而不是在本地再抄一份数字，避免两条链路调参时漂移。
EMBED_MAX_ATTEMPTS = 2
EMBED_RETRY_DELAY_SECONDS = 0.5


class EmbeddingBatchError(RuntimeError):
    """若干批次重试后仍失败：受影响切片只有 BM25 关键词召回，没有向量。

    message 里带"失败批次/受影响切片/最后一错"三段，直接可以落进 KnowledgeFile.error
    与 IngestJob.error 给运营看（旧行为是首个失败就中断整批，剩下的切片静默没有向量）。
    """

    def __init__(self, message: str, *, failed_batches: list[int], failed_chunk_ids: list[int],
                 total_batches: int) -> None:
        super().__init__(message)
        self.failed_batches = failed_batches
        self.failed_chunk_ids = failed_chunk_ids
        self.total_batches = total_batches


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


def embedding_batch_size() -> int:
    """实际使用的批大小（已夹紧到 [1, 20]）。

    get_dotted 只在"键不存在"时给默认值，写空值/写成字符串都得自己兜底
    （同 manager._llm_int_setting 的教训）。超过服务端硬上限就夹紧，并在第一次读到
    非法值时 warn 一次——启动即告警，不会每个批次刷屏。
    """
    global _BATCH_WARNED
    raw = get_settings().get_dotted("rag.embedding_batch_size", EMBED_BATCH_DEFAULT)
    value: int | None
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        value = EMBED_BATCH_DEFAULT
    else:
        try:
            value = int(float(raw))
        except (TypeError, ValueError):
            logger.warning("配置 rag.embedding_batch_size={!r} 不是数字，按默认值 {} 处理",
                           raw, EMBED_BATCH_DEFAULT)
            value = EMBED_BATCH_DEFAULT
    clamped = max(EMBED_BATCH_MIN, min(value, EMBED_BATCH_MAX))
    if clamped != value:
        with _BATCH_WARN_LOCK:
            first_time = not _BATCH_WARNED
            _BATCH_WARNED = True
        if first_time:
            logger.warning("rag.embedding_batch_size={} 超出可用区间 [{}, {}]，按 {} 执行"
                           "（后续同类告警不再重复）",
                           value, EMBED_BATCH_MIN, EMBED_BATCH_MAX, clamped)
    return clamped


def validate_startup_config() -> int:
    """启动自检入口：把批大小读一遍（配错/超上限就在这里夹紧并 warn 一次）。

    main.py 的 lifespan 不在我的改动范围内，所以提供显式函数供其调用；就算没人调用，
    embedding_batch_size() 也会在第一次外呼前自己 warn 一次——告警不依赖调用点。
    """
    return embedding_batch_size()


def _embed(texts: list[str]) -> list[list[float]]:
    from app.core.models.manager import get_embeddings, model_id, resolve_name
    from app.services import usage

    # embedding 接口不回报 usage_metadata：按输入文本本地粗估（track 会自动标 estimated=True）。
    # 异常照常上抛，调用方的降级分支（BM25 兜底 / 跳过向量）不受影响。
    with usage.track("embedding", provider=resolve_name(), model=model_id("embedding")) as st:
        st["inputs"] = list(texts)
        return get_embeddings().embed_documents(texts)


def _embed_with_retry(texts: list[str], *, batch_no: int, total_batches: int) -> list[list[float]]:
    """一批文本向量化，网络/5xx/429 类故障退避重试（口径同 rerank._post_with_retry）。

    重试仍失败就把最后一个错误原样上抛，由 add_chunks 汇总成 EmbeddingBatchError。
    未配置 key（ModelNotConfiguredError）属于"重试没有意义"的配置错误：openai SDK 的
    max_retries 不会重试本地抛出的参数错误，这里最多按常数再试一次就放弃，不拖慢入库。
    """
    last_error: Exception | None = None
    for attempt in range(EMBED_MAX_ATTEMPTS):
        if attempt:
            time.sleep(EMBED_RETRY_DELAY_SECONDS * (2 ** (attempt - 1)))
        try:
            return _embed(texts)
        except Exception as e:  # noqa: BLE001 - 上游是 HTTP/SDK 异常族，逐次记日志后重试
            last_error = e
            logger.warning("embedding 批次 {}/{} 失败（第 {}/{} 次）: {}",
                           batch_no, total_batches, attempt + 1, EMBED_MAX_ATTEMPTS, e)
    assert last_error is not None
    raise last_error


def add_chunks(chunks: list[tuple[int, str, int, str]],
               progress: Callable[[int, int], None] | None = None) -> None:
    """chunks: [(chunk_id, text, file_id, file_name)]

    显式传 embeddings——若让 Chroma 用默认 embedding function，
    会静默下载本地 ONNX 模型且与查询侧向量空间不一致（写入/查询必须同一模型）。

    分批发送：批大小见 embedding_batch_size()（默认 10，上限 20），大文件整批送会被
    百炼 "batch size is invalid" 直接拒掉，降级成"仅关键词检索"。
    progress(已完成条数, 总条数) 供任务表写进度——失败批次同样计入已完成，否则进度条
    停在半路看起来像卡死。

    全部批次成功才正常返回；有批次重试后仍失败时抛 EmbeddingBatchError（带失败批次与
    受影响切片 id），错误文案由上层写进 KnowledgeFile.error / IngestJob.error。
    """
    if not chunks:
        return
    col = _collection()
    total = len(chunks)
    batch = embedding_batch_size()
    total_batches = (total + batch - 1) // batch
    done_batches = 0
    failed_batches: list[int] = []
    failed_chunk_ids: list[int] = []
    last_error: Exception | None = None

    for i, start in enumerate(range(0, total, batch)):
        group = chunks[start:start + batch]
        try:
            vectors = _embed_with_retry([c[1] for c in group], batch_no=i + 1,
                                        total_batches=total_batches)
            col.add(ids=[str(c[0]) for c in group],
                    documents=[c[1] for c in group],
                    embeddings=vectors,
                    metadatas=[{"file_id": c[2], "file_name": c[3], "enabled": True}
                               for c in group])
        except Exception as e:  # noqa: BLE001 - 单批失败不再中断整个文件：后面的切片还能拿到向量
            failed_batches.append(i + 1)
            failed_chunk_ids.extend(c[0] for c in group)
            last_error = e
            logger.warning("向量化批次 {}/{} 最终失败（{} 条降级为仅关键词检索）: {}",
                           i + 1, total_batches, len(group), e)
        done_batches = i + 1
        if progress:
            progress(min(done_batches * batch, total), total)

    if failed_batches:
        preview = ", ".join(str(cid) for cid in failed_chunk_ids[:10])
        more = f" 等 {len(failed_chunk_ids)} 条" if len(failed_chunk_ids) > 10 else ""
        raise EmbeddingBatchError(
            f"向量化失败批次 {failed_batches}/{total_batches}（重试 {EMBED_MAX_ATTEMPTS} 次后仍失败），"
            f"受影响切片: {preview}{more}；最后一错: {str(last_error)[:200]}",
            failed_batches=failed_batches, failed_chunk_ids=failed_chunk_ids,
            total_batches=total_batches)


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
