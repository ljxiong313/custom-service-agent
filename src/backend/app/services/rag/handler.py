"""检索编排：向量召回 + BM25 召回 → RRF 融合 → rerank → 组装带引用的结果。

对外唯一入口 retrieve()，供 rag_search 工具与知识库"检索测试"接口共用，
保证管理页看到的召回与 Agent 实际用的一致。
"""
from __future__ import annotations

from dataclasses import dataclass

from sqlmodel import Session, col, select

from app.config import get_settings
from app.db.models import Chunk, KnowledgeFile

from . import fusion, rerank, text_index, vectorstore


@dataclass
class Retrieved:
    chunk_id: int
    file_id: int
    file_name: str
    text: str
    score: float


def retrieve(session: Session, query: str, top_k: int | None = None) -> list[Retrieved]:
    top_k = top_k or int(get_settings().get_dotted("rag.top_k", 5))
    recall = int(get_settings().get_dotted("rag.recall_k", 20))

    vec = [(h.chunk_id, h.score, h.file_id) for h in vectorstore.query(query, recall)]
    bm25 = [(h.chunk_id, h.score, h.file_id) for h in text_index.search(session, query, recall)]
    fused = fusion.rrf_fusion(vec, bm25)
    if not fused:
        return []

    candidates = fused[: max(top_k * 3, top_k)]  # 给 rerank 留候选余量
    chunk_map = {c.id: c for c in session.exec(
        select(Chunk).where(col(Chunk.id).in_([f.chunk_id for f in candidates]))
    ).all()}
    texts = [chunk_map[f.chunk_id].text for f in candidates if f.chunk_id in chunk_map]

    order = rerank.rerank(query, texts, top_k)
    if order is not None:
        picked = [candidates[i] for i, _s in order if candidates[i].chunk_id in chunk_map]
    else:
        picked = [f for f in candidates if f.chunk_id in chunk_map][:top_k]

    names = {f.id: f.name for f in session.exec(
        select(KnowledgeFile).where(col(KnowledgeFile.id).in_({c.file_id for c in picked}))
    ).all()}
    return [Retrieved(chunk_id=c.chunk_id, file_id=c.file_id,
                      file_name=names.get(c.file_id, "?"), text=chunk_map[c.chunk_id].text,
                      score=c.score)
            for c in picked]
