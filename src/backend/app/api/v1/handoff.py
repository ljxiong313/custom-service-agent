"""转人工接口：用户侧触发、客服工作台（看板/工单列表/接单回复/结单）、mock 外发记录。

工作台权限说明：演示阶段任意登录用户可见工单列表（模拟"客服"角色）；
生产应引入 staff 角色表做 RBAC（见 docs/代码规范.md 的演进备注）。
"""
from __future__ import annotations

import datetime

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import or_
from sqlmodel import col, func, select

from app.core.security import CurrentUser, DbSession
from app.db.models import Conversation, HandoffTicket, Message, User
from app.services.wecom import client
from app.services.wecom.handoff import close_ticket, create_ticket, staff_reply

router = APIRouter(prefix="/handoff", tags=["handoff"])


class RequestBody(BaseModel):
    conversation_id: int
    reason: str = "用户请求转人工"


@router.post("/request")
def request_handoff_api(body: RequestBody, user: CurrentUser, session: DbSession):
    """买家/用户侧触发：只能对自己的会话建单。"""
    conv = session.get(Conversation, body.conversation_id)
    if conv is None or conv.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "会话不存在")
    ticket = create_ticket(session, conv, user, body.reason)
    return {"ticket_id": ticket.id, "status": conv.handoff_status}


@router.post("/tickets")
def create_ticket_by_operator(body: RequestBody, user: CurrentUser, session: DbSession):
    """运营台手动建单：客服对任意渠道会话开单（买家没点确认、但运营判断需要人工时）。

    工单归属记会话的真实主人（平台买家是影子用户），企微卡片才显示对的人名。
    """
    conv = session.get(Conversation, body.conversation_id)
    if conv is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "会话不存在")
    if conv.status == "closed":
        raise HTTPException(status.HTTP_409_CONFLICT, "会话已终结，无法建单")
    owner = session.get(User, conv.user_id) or user
    ticket = create_ticket(session, conv, owner, body.reason)
    return {"ticket_id": ticket.id, "status": conv.handoff_status}


@router.get("/stats")
def handoff_stats(user: CurrentUser, session: DbSession):
    """工作台看板：待接/处理中/今日新建 + 平均首响时长。

    首响 = 工单创建 → 该会话第一条**人工**回复（meta.from 为 human_agent 工作台回复
    或 human_operator 运营代答）。机器人的 assistant 消息不计，否则自动应答会把首响"刷平"。
    今日按 UTC 日界算（库里存的就是 UTC）。
    """
    counts = dict(session.exec(
        select(HandoffTicket.status, func.count()).group_by(col(HandoffTicket.status))
    ).all())
    day_start = datetime.datetime.now(datetime.UTC).replace(
        hour=0, minute=0, second=0, microsecond=0)
    created_today = session.exec(
        select(func.count()).select_from(HandoffTicket)
        .where(HandoffTicket.created_at >= day_start)).one()

    human_reply = or_(col(Message.meta_json).like("%human_agent%"),
                      col(Message.meta_json).like("%human_operator%"))
    rows = session.exec(
        select(HandoffTicket.created_at, func.min(Message.created_at))
        .outerjoin(Message, (col(Message.conversation_id) == col(HandoffTicket.conversation_id))
                   & human_reply & (col(Message.created_at) >= col(HandoffTicket.created_at)))
        .group_by(col(HandoffTicket.id))
    ).all()
    deltas = [(first - created).total_seconds() for created, first in rows if first]
    return {
        "open": int(counts.get("open", 0)),
        "handling": int(counts.get("handling", 0)),
        "closed": int(counts.get("closed", 0)),
        "created_today": int(created_today),
        "responded": len(deltas),
        "avg_first_response_seconds": round(sum(deltas) / len(deltas), 1) if deltas else None,
    }


@router.get("/tickets")
def list_tickets(user: CurrentUser, session: DbSession, status_filter: str | None = None):
    stmt = select(HandoffTicket).order_by(col(HandoffTicket.id).desc())
    if status_filter:
        stmt = stmt.where(HandoffTicket.status == status_filter)
    return session.exec(stmt).all()


class ReplyBody(BaseModel):
    text: str = Field(min_length=1, max_length=2000)


@router.post("/tickets/{ticket_id}/reply")
def reply(ticket_id: int, body: ReplyBody, user: CurrentUser, session: DbSession):
    ticket = session.get(HandoffTicket, ticket_id)
    if not ticket or ticket.status == "closed":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "工单不存在或已结单")
    msg = staff_reply(session, ticket, body.text, staff=user.username)
    return {"message_id": msg.id, "ticket_status": ticket.status}


@router.post("/tickets/{ticket_id}/close")
def close(ticket_id: int, user: CurrentUser, session: DbSession):
    ticket = session.get(HandoffTicket, ticket_id)
    if not ticket:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "工单不存在")
    close_ticket(session, ticket)
    return {"ok": True}


@router.get("/outbox")
def outbox(user: CurrentUser, limit: int = 50):
    """企微外发记录（演示转人工提醒送达）；B1-2 起读库，skipped/failed 也留痕。"""
    return client.outbox_records(max(1, min(limit, 200)))
