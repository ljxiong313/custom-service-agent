"""BM25 关键词索引（rank_bm25 + jieba 中文分词）+ 版本标记驱动的缓存。

历史假设是"商品知识库 10k chunk 以内，每次检索现取现建（毫秒级）"。这个假设在生产里
不成立——实测（scripts/bench_rag.py，合成商品语料约 700 字/切片，取中位数）：1k 切片
现取现建 1.7 秒，成本几乎全在 jieba 分词上，并随切片数线性上升（10k/50k 的实测值由脚本
打印）。现在的做法是：

- **进程内缓存**：同一语料版本只建一次；
- **磁盘缓存**（rag.bm25_persist，默认开）：进程重启 / 多进程也不重复付费，命中即免去
  分词与建索引（1k 切片：现取现建 1708ms → 磁盘命中 20ms → 进程内命中 2ms）；
- **失效判定靠语料版本标记**（bm25_store.corpus_fingerprint）：每次检索先用一条聚合 SQL
  算出"当前参与检索的切片集合"指纹（1k 规模 0.6ms），与缓存里的标记不一致就当场全量重建。

所以缓存永远只影响延迟，不影响结果：任何时刻的检索结果都与"关掉缓存全量重建"逐条相同
（tests/test_rag_bm25_index.py 直接拿两份结果对比）。

开关：
- rag.bm25_persist: false → 回到今天的老行为（每次检索现取现建，不读写缓存）；
- rag.bm25_index_path: "" → 默认 data/bm25/knowledge.pkl.gz。

规模上限的诚实说明：即便缓存全部命中，rank_bm25 的 get_scores 仍是"对每个查询词扫一遍
全库词频表"，O(切片数)——1k 实测 0.7ms，线性上涨（bench 的"纯打分"列就是这一项）。缓存
解决的是"建库太贵"，解决不了"打分线性"。语料再往上走（或要求多路并发毫秒级）就该换专用
检索引擎（Meilisearch / ES / tantivy）。
"""
from __future__ import annotations

import contextlib
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import jieba
from loguru import logger
from sqlmodel import Session, col, select

from app.db.models import Chunk, KnowledgeFile, pk

from . import bm25_store
from .bm25_store import CorpusState


@dataclass
class Bm25Hit:
    chunk_id: int
    score: float
    file_id: int


def _tokenize(text: str) -> list[str]:
    return [t for t in jieba.lcut_for_search(text.lower()) if t.strip()]


@lru_cache(maxsize=1)
def _jieba_dict_ready() -> bool:
    jieba.initialize()
    return True


_cached: CorpusState | None = None


def reset_cache() -> None:
    """换库/换配置（测试用临时 SQLite、脚本改 rag.*）后必须调用，否则会沿用上一份索引。

    与 vectorstore.reset_collection_cache() 同一口径：进程内单例缓存必须有显式重置入口。
    """
    global _cached
    with bm25_store.lock():
        _cached = None


def invalidate() -> None:
    """语料写路径（入库 / 清理 / 删文件）之后调用，立刻丢弃进程内索引。

    版本标记本来就能发现变化，这里只是省掉一次"拿旧索引再比对"的开销；顺带把"绕过
    入库路径直接 UPDATE 了 Chunk.text"这种本模块不支持的写法也拉回重建轨道。
    """
    reset_cache()


def _rows_for_index(session: Session) -> list[Chunk]:
    """当前参与检索的切片，按 id 升序。

    order_by 是刻意的：缓存里的"位置→切片"顺序必须与当场重建的顺序一致，否则同分打平的
    名次会在"命中缓存"和"全量重建"两种情况下漂移，破坏"结果与全量重建一致"这条红线。
    """
    return list(session.exec(
        select(Chunk)
        .join(KnowledgeFile, Chunk.file_id == KnowledgeFile.id)  # type: ignore[arg-type]
        .where(KnowledgeFile.enabled == True)  # noqa: E712
        .order_by(col(Chunk.id))
    ).all())


def _build(session: Session, fingerprint: str) -> CorpusState | None:
    """全量重建（正确性基准）：读 Chunk → 分词 → BM25Plus。"""
    # BM25Plus 而非 Okapi：Okapi 的 idf=log((N-n+0.5)/(n+0.5)) 在小语料下
    # 词频过半即归零/为负，商品知识库初期文档少，必须保证排序信号有效
    from rank_bm25 import BM25Plus

    rows = _rows_for_index(session)
    if not rows:
        return None
    _jieba_dict_ready()
    corpus = [_tokenize(c.text) for c in rows]
    index = BM25Plus(corpus)
    return CorpusState(fingerprint=fingerprint, index=index,
                       chunk_ids=[pk(c) for c in rows], file_ids=[c.file_id for c in rows])


def _get_state(session: Session, fingerprint: str, stats: dict[str, Any]) -> CorpusState | None:
    global _cached
    if not bm25_store.persist_enabled():
        # 开关关掉：行为退回"每次检索现取现建"，连进程内缓存也不留，避免"关了还命中"的歧义
        with bm25_store.lock():
            _cached = None
        stats["source"] = "rebuild"
        stats["rebuilt"] = True
        return _build(session, fingerprint)

    with bm25_store.lock():                   # 进程内只让一个线程付重建成本（其余等同一份结果）
        if _cached is not None and _cached.fingerprint == fingerprint:
            stats["source"] = "memory"
            return _cached

        path = bm25_store.index_path()
        state = bm25_store.load(path, fingerprint)
        if state is not None:
            stats["source"] = "disk"
            with contextlib.suppress(OSError):         # 观测数据，取不到就算了，不能挡住检索
                stats["index_bytes"] = path.stat().st_size
            _cached = state
            return state

        state = _build(session, fingerprint)
        if state is None:
            stats["source"] = "empty"
            _cached = None
            return None
        size = bm25_store.save(path, state)
        stats.update(source="rebuild", rebuilt=True, index_bytes=size)
        logger.info("BM25 索引重建: {} 切片 → 缓存 {} 字节（{}）",
                    len(state.chunk_ids), size, path)
        _cached = state
        return state


def search(session: Session, query: str, top_k: int = 20,
           stats: dict[str, Any] | None = None) -> list[Bm25Hit]:
    """BM25 召回。

    stats：可选的旁路观测字典（source=memory/disk/rebuild/empty、rebuilt、index_bytes），
    给基准脚本和单测用；调用方不需要传，检索路径本身的行为与签名兼容性不变。
    """
    observed: dict[str, Any] = stats if stats is not None else {}
    tokens = _tokenize(query)
    if not tokens:
        observed["source"] = "empty-query"
        return []

    fingerprint = bm25_store.corpus_fingerprint(session)
    state = _get_state(session, fingerprint, observed)
    if state is None:
        return []

    scores = state.index.get_scores(tokens)
    ranked = sorted(zip(scores, state.chunk_ids, state.file_ids, strict=True), key=lambda x: -x[0])
    # 注意：rank_bm25 的 idf 在语料极小/词频过半时会为负——只排除"零命中"(0 分)，
    # 不能按 s>0 过滤，否则小库全部丢失；排序语义由 RRF 融合层负责。
    return [Bm25Hit(chunk_id=int(cid), score=float(s), file_id=int(fid))
            for s, cid, fid in ranked[:top_k] if s != 0]
