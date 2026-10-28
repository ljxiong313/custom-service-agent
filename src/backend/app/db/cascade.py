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
    MemoryJob,
    Message,
    SessionRating,
    ToolConfirmRequest,
)


def delete_conversation(session: Session, conv: Conversation) -> None:
    """删除会话及其全部附属行（消息/摘要/工单/平台映射/确认请求/记忆任务）。"""
    conv_id = conv.id or 0
    n_msg = n_sum = n_ticket = n_ext = n_confirm = n_memory = n_rating = 0
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
    # B3-8 新加的两张会话子表：漏一张，删会话就会因 foreign_keys=ON 直接报错
    for c in session.exec(select(ToolConfirmRequest).where(
            ToolConfirmRequest.conversation_id == conv_id)).all():
        session.delete(c)
        n_confirm += 1
    for j in session.exec(select(MemoryJob).where(
            MemoryJob.conversation_id == conv_id)).all():
        session.delete(j)
        n_memory += 1
    for r in session.exec(select(SessionRating).where(
            SessionRating.conversation_id == conv_id)).all():
        session.delete(r)
        n_rating += 1
    # 必须先 flush 再删父行：Message/HandoffTicket 这些有 relationship，SQLAlchemy 能自己
    # 排出删除顺序；B3 新加的确认请求/记忆任务/评分表没有 relationship，父行会被提前删除，
    # 在 foreign_keys=ON 下直接抛 FOREIGN KEY constraint failed。
    session.flush()
    session.delete(conv)
    session.commit()
    logger.info("删除会话#{}：消息={} 摘要={} 工单={} 平台映射={} 确认请求={} 记忆任务={} 评分={}",
                conv_id, n_msg, n_sum, n_ticket, n_ext, n_confirm, n_memory, n_rating)
