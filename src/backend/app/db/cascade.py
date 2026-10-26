"""跨表级联删除。

SQLite 的 ON DELETE CASCADE 要建表时声明，本项目历史表没声明、旧实现靠调用方手写删除，
结果删一个会话只清了 Message，ConversationSummary / HandoffTicket / ExternalConversation
全留下指向已删会话的行。现在连接开了 PRAGMA foreign_keys=ON，这些孤儿行会让删除直接报错，
所以"删父行"统一走这里。
"""
from __future__ import annotations

from loguru import logger
from sqlmodel import Session, select

from app.db.models import (
    Conversation,
    ConversationSummary,
    ExternalConversation,
    HandoffTicket,
    Message,
)


def delete_conversation(session: Session, conv: Conversation) -> None:
    """删除会话及其全部附属行（消息/摘要/工单/平台映射/入站幂等记录）。"""
    conv_id = conv.id or 0
    n_msg = n_sum = n_ticket = n_ext = 0
    for m in session.exec(select(Message).where(Message.conversation_id == conv_id)).all():
        session.delete(m)
        n_msg += 1
    for row in session.exec(select(ConversationSummary).where(
            ConversationSummary.conversation_id == conv_id)).all():
        session.delete(row)
        n_sum += 1
    for t in session.exec(select(HandoffTicket).where(
            HandoffTicket.conversation_id == conv_id)).all():
        session.delete(t)
        n_ticket += 1
    # 入站幂等键按 (platform, shop, buyer) 记，与内部会话 id 无关，不能跟着会话删：
    # 删了它，平台重推的历史消息会被当新消息再处理一遍。
    for e in session.exec(select(ExternalConversation).where(
            ExternalConversation.conversation_id == conv_id)).all():
        session.delete(e)
        n_ext += 1
    session.delete(conv)
    session.commit()
    logger.info("删除会话#{}：消息={} 摘要={} 工单={} 平台映射={}",
                conv_id, n_msg, n_sum, n_ticket, n_ext)
