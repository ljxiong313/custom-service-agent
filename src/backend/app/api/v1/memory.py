"""记忆管理接口：长期画像事实的浏览/删除，中期会话摘要的浏览。

删除事实同时清理 Chroma 向量（long.remove_fact），保证库内与向量索引一致。
摘要为只读展示（重新生成由对话后的后台管道负责，不在本页做编辑）。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from sqlmodel import col, select

from app.core.security import CurrentUser, DbSession
from app.db.models import Conversation, ConversationSummary, MemoryFact
from app.services.memory import long as long_mem

router = APIRouter(prefix="/memory", tags=["memory"])


@router.get("/facts")
def list_facts(user: CurrentUser, session: DbSession):
    rows = session.exec(
        select(MemoryFact).where(MemoryFact.user_id == user.id)
        .order_by(MemoryFact.id.desc())  # type: ignore[union-attr]
    ).all()
    return [r.model_dump() for r in rows]


@router.delete("/facts/{fact_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_fact(fact_id: int, user: CurrentUser, session: DbSession):
    row = session.get(MemoryFact, fact_id)
    if row is None or row.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "记忆不存在")
    long_mem.remove_fact(session, row)


@router.get("/summaries")
def list_summaries(user: CurrentUser, session: DbSession):
    """当前用户所有会话的中期摘要（演示记忆如何跨轮次压缩上下文）。"""
    rows = session.exec(
        select(ConversationSummary)
        .join(Conversation,
              col(ConversationSummary.conversation_id) == col(Conversation.id))
        .where(Conversation.user_id == user.id)
        .order_by(col(ConversationSummary.updated_at).desc())
    ).all()
    return [{"conversation_id": s.conversation_id, "summary": s.summary,
             "until_message_id": s.until_message_id, "updated_at": s.updated_at}
            for s in rows]
