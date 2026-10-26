"""BM25 关键词索引（rank_bm25 + jieba 中文分词）。

规模判断：商品知识库在 10k chunk 以内，每次检索从 DB 现取现建索引（毫秒级），
不做持久化索引服务——避免增量同步复杂度，这是本项目明确的规模假设。
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

import jieba
from sqlmodel import Session, select

from app.db.models import Chunk, KnowledgeFile, pk


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


def search(session: Session, query: str, top_k: int = 20) -> list[Bm25Hit]:
    # BM25Plus 而非 Okapi：Okapi 的 idf=log((N-n+0.5)/(n+0.5)) 在小语料下
    # 词频过半即归零/为负，商品知识库初期文档少，必须保证排序信号有效
    from rank_bm25 import BM25Plus

    rows = list(session.exec(
        select(Chunk, KnowledgeFile.name)
        .join(KnowledgeFile, Chunk.file_id == KnowledgeFile.id)  # type: ignore[arg-type]
        .where(KnowledgeFile.enabled == True)  # noqa: E712
    ).all())
    if not rows:
        return []
    corpus = [_tokenize(r[0].text) for r in rows]
    bm25 = BM25Plus(corpus)
    scores = bm25.get_scores(_tokenize(query))
    ranked = sorted(zip(scores, rows, strict=True), key=lambda x: -x[0])[:top_k]
    # 注意：rank_bm25 的 idf 在语料极小/词频过半时会为负——只排除"零命中"(0 分)，
    # 不能按 s>0 过滤，否则小库全部丢失；排序语义由 RRF 融合层负责。
    return [Bm25Hit(chunk_id=pk(c), score=float(s), file_id=c.file_id)
            for s, (c, _name) in ranked if s != 0]
