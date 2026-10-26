"""转人工领域服务：工单创建、状态机流转、客服回复回写。

状态机：Conversation.handoff_status
    bot --(触发: 关键词/request_handoff工具/手动)--> pending_human（机器人挂起，推送企微卡片）
    pending_human --(客服接单/回复)--> human
    human --(结单)--> bot
"""
from __future__ import annotations

import datetime
import json

from loguru import logger
from sqlmodel import Session, col, select

from app.db.models import Conversation, HandoffTicket, Message, User, pk
from app.services.wecom import client


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def _conversation_digest(session: Session, conv_id: int, limit: int = 6) -> str:
    msgs = list(session.exec(
        select(Message).where(Message.conversation_id == conv_id)
        .order_by(col(Message.id).desc()).limit(limit)
    ).all())
    return "\n".join(f"{'用户' if m.role == 'user' else '客服'}: {m.content[:80]}" for m in reversed(msgs))


def create_ticket(session: Session, conv: Conversation, user: User, reason: str) -> HandoffTicket:
    """建工单 + 切状态 + 推企微卡片。幂等：已有未结单则复用（不重复推送）。"""
    exist = session.exec(select(HandoffTicket).where(
        HandoffTicket.conversation_id == conv.id,
        col(HandoffTicket.status).in_(("open", "handling")))).first()
    if exist:
        return exist

    digest = _conversation_digest(session, pk(conv))
    ticket = HandoffTicket(conversation_id=pk(conv), user_id=pk(user),
                           reason=reason, summary=digest)
    session.add(ticket)
    conv.handoff_status = "pending_human"
    conv.updated_at = _now()
    session.add(conv)
    session.commit()
    session.refresh(ticket)

    try:
        client.send_textcard(
            touser="", title=f"转人工工单 #{ticket.id}",
            description=f"用户: {user.username}\n原因: {reason}\n\n最近对话:\n{digest[:400]}")
    except Exception as e:
        # 推送失败不回滚工单：状态保持 pending_human，工作台仍可人工处理
        logger.error("企微卡片推送失败（工单已建）: {}", e)
    return ticket


def staff_reply(session: Session, ticket: HandoffTicket, text: str, staff: str) -> Message:
    """客服回复 → 写入用户会话（role=assistant，meta 标记人工来源）并进入 human 态。"""
    if ticket.status == "open":
        ticket.status = "handling"
        ticket.staff = staff
    conv = session.get(Conversation, ticket.conversation_id)
    assert conv is not None, f"工单#{ticket.id} 指向的会话不存在（删会话会级联删工单）"
    conv.handoff_status = "human"
    msg = Message(conversation_id=ticket.conversation_id, role="assistant", content=text,
                  meta_json=json.dumps({"from": "human_agent", "staff": staff}, ensure_ascii=False))
    session.add(msg)
    session.add(conv)
    session.add(ticket)
    session.commit()
    session.refresh(msg)
    logger.info("工单#{} 客服({})回复已回写会话#{}", ticket.id, staff, conv.id)

    # P7：会话若绑定外部平台渠道，客服回复同步送达买家（网页会话自动跳过）
    try:
        from app.services.platforms.inbound import push_staff_reply

        push_staff_reply(ticket.conversation_id, text)
    except Exception as e:
        logger.error("平台渠道客服回复外发异常: {}", e)
    return msg


def close_ticket(session: Session, ticket: HandoffTicket) -> None:
    ticket.status = "closed"
    ticket.closed_at = _now()
    conv = session.get(Conversation, ticket.conversation_id)
    if conv:
        conv.handoff_status = "bot"
        session.add(conv)
    session.add(ticket)
    session.commit()


def apply_handoff_from_tool(conv_id: int, tool_output: str) -> dict | None:
    """解析 request_handoff 工具输出（JSON），完成会话状态切换。供 chat 层发 SSE 事件。"""
    try:
        data = json.loads(tool_output)
        if not data.get("ok"):
            return None
    except (json.JSONDecodeError, TypeError):
        return None
    with Session(_engine()) as session:
        conv = session.get(Conversation, conv_id)
        if conv:
            conv.handoff_status = "pending_human"
            session.add(conv)
            session.commit()
    return {"status": "pending_human", "ticket_id": data.get("ticket_id")}


def _engine():
    from app.db.session import engine

    return engine
