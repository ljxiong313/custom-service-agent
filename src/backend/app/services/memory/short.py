"""短期记忆：会话内历史消息的 token 滑窗裁剪。

策略（参考 AgentChat / 通用做法）：
- 按时间正序取历史消息，从最新往回累加 token，超过 max_tokens 即停止；
- 保证"最近的上下文"优先保留；被截断的早期内容由 P2 的中期记忆（摘要）补位。
- token 估算用 tiktoken cl100k_base 近似（通义未公开 tiktoken 编码，误差可接受）。
"""
from __future__ import annotations

from dataclasses import dataclass

import tiktoken
from sqlmodel import Session, col, select

from app.config import get_settings
from app.db.models import Message

# 通义系列 tokenizer 未开源，用 cl100k_base 做保守估算（中文略高估，安全方向偏差）
_ENC = tiktoken.get_encoding("cl100k_base")


def estimate_tokens(text: str) -> int:
    return len(_ENC.encode(text))


@dataclass
class ShortTermMemory:
    max_tokens: int

    @classmethod
    def from_settings(cls) -> ShortTermMemory:
        return cls(max_tokens=int(get_settings().get_dotted("memory.short_max_tokens", 3000)))

    def load_history(self, session: Session, conversation_id: int,
                     before_message_id: int | None = None) -> list[Message]:
        """取滑窗内的历史消息（正序）。before_message_id 用于排除本次新追加的用户消息。"""
        stmt = (select(Message)
                .where(Message.conversation_id == conversation_id,
                       col(Message.role).in_(("user", "assistant")))
                .order_by(col(Message.id)))
        if before_message_id:
            stmt = stmt.where(col(Message.id) < before_message_id)
        rows = list(session.exec(stmt).all())

        # 从最新往回累加，超预算即截断（至少保留最后 1 条）
        used = 0
        kept: list[Message] = []
        for m in reversed(rows):
            cost = estimate_tokens(m.content) + 4  # 每条消息的角色/分隔开销约 4 token
            if kept and used + cost > self.max_tokens:
                break
            used += cost
            kept.append(m)
        return list(reversed(kept))
