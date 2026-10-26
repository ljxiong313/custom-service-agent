"""文本分块：按 token 数滑窗切分（chunk_size / overlap 来自配置 rag.*）。

中文场景不做句法切分——商品文档多为短行结构，按 token 滑窗已足够，
且 overlap 保证跨块语义连续；行优先聚合避免把一行商品拆到两块里。
"""
from __future__ import annotations

from app.config import get_settings
from app.services.memory.short import estimate_tokens


def chunk_text(text: str) -> list[str]:
    size = int(get_settings().get_dotted("rag.chunk_size", 500))
    overlap = int(get_settings().get_dotted("rag.chunk_overlap", 100))

    # 1) 按行聚合成"段落单元"（单行超长时先硬切）
    units: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        while estimate_tokens(line) > size:
            units.append(line[:size * 2])  # 粗按字符切，token 数近似 2 字符/token
            line = line[size * 2:]
        units.append(line)

    # 2) 滑窗聚合：累加到 chunk_size 即成块，块间保留 overlap token
    chunks: list[str] = []
    cur: list[str] = []
    cur_tokens = 0
    for u in units:
        t = estimate_tokens(u)
        if cur and cur_tokens + t > size:
            chunks.append("\n".join(cur))
            # 回退 overlap：从尾部丢弃至超出预算
            keep: list[str] = []
            kept = 0
            for piece in reversed(cur):
                pt = estimate_tokens(piece)
                if kept + pt > overlap:
                    break
                keep.insert(0, piece)
                kept += pt
            cur, cur_tokens = keep, kept
        cur.append(u)
        cur_tokens += t
    if cur:
        chunks.append("\n".join(cur))
    return [c for c in chunks if c.strip()]
