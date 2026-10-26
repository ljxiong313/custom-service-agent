"""中期记忆：会话滚动摘要。

定位：短期滑窗（short.py）会截断较早的历史，中期摘要补位——
把"窗口外"的消息压缩成一段摘要注入 system prompt，保证长对话不失焦。
实现：ConversationSummary 记录摘要与游标（until_message_id），
对话结束后的后台管道（pipeline.py）增量更新，不阻塞请求。
"""
from __future__ import annotations

import datetime

from loguru import logger
from sqlmodel import Session, col, select

from app.db.models import ConversationSummary, Message, pk


def load_summary(session: Session, conversation_id: int) -> str | None:
    row = session.exec(select(ConversationSummary).where(
        ConversationSummary.conversation_id == conversation_id)).first()
    return row.summary if row else None


def ensure_summary(session: Session, conversation_id: int) -> None:
    """若存在"滑窗外未摘要"的消息则增量更新摘要。需要可用的对话模型。"""
    row = session.exec(select(ConversationSummary).where(
        ConversationSummary.conversation_id == conversation_id)).first()
    cursor = row.until_message_id if row else 0

    # 滑窗保留最近 ~3000 token（约 15 条短消息），更早的进入摘要
    recent_ids = [pk(m) for m in session.exec(
        select(Message).where(Message.conversation_id == conversation_id)
        .order_by(col(Message.id).desc()).limit(15)
    ).all()]
    stmt = (select(Message)
            .where(Message.conversation_id == conversation_id,
                   col(Message.id) > cursor,
                   ~col(Message.id).in_(recent_ids))
            .order_by(col(Message.id)))
    pending = list(session.exec(stmt).all())
    if len(pending) < 6:  # 太碎不值得调模型，攒够再摘要
        return

    from app.core.models.manager import get_chat_model

    transcript = "\n".join(f"{'用户' if m.role == 'user' else '客服'}: {m.content[:200]}"
                           for m in pending)
    old = f"已有摘要：{row.summary}\n\n" if row else ""
    prompt = (f"{old}请把新的对话记录合并进摘要，输出 120 字以内的中文要点"
              f"（保留：用户诉求、涉及商品与结论、未决事项）。只输出摘要正文。\n\n{transcript}")
    try:
        resp = get_chat_model().invoke(prompt)
        text = resp.text if hasattr(resp, "text") else str(resp.content)
    except Exception as e:
        logger.warning("中期摘要生成失败（跳过本轮）: {}", e)
        return

    if row is None:
        row = ConversationSummary(conversation_id=conversation_id, summary=text.strip())
    else:
        row.summary = text.strip()
    row.until_message_id = max(pk(m) for m in pending)
    row.updated_at = datetime.datetime.now(datetime.UTC)
    session.add(row)
    session.commit()
