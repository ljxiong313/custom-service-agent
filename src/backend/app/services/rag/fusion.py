"""RRF（Reciprocal Rank Fusion）融合向量与关键词两路召回。

RRF 只用名次不用分值，天然规避 cosine 相似度与 BM25 分数量纲不一致的问题；
k=60 为业界默认平滑常数。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class FusedHit:
    chunk_id: int
    score: float
    file_id: int


def rrf_fusion(vector_hits: list[tuple[int, float, int]],
               bm25_hits: list[tuple[int, float, int]],
               k: int = 60) -> list[FusedHit]:
    """hits 元素: (chunk_id, score, file_id)，各自已按分数降序。"""
    fused: dict[int, tuple[float, int]] = {}
    for ranked in (vector_hits, bm25_hits):
        for rank, (cid, _score, fid) in enumerate(ranked, start=1):
            fused[cid] = (fused.get(cid, (0.0, fid))[0] + 1.0 / (k + rank), fid)
    return [FusedHit(chunk_id=cid, score=v[0], file_id=v[1])
            for cid, v in sorted(fused.items(), key=lambda x: -x[1][0])]
