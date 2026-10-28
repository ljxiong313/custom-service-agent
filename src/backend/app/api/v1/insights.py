"""运营看板（B3-10）：机器人解决率、转人工率、响应与处理时长、模型成本。

为什么要单独一个接口而不是把数字塞进 /system/status：status 回答"服务现在还健康吗"，
看板回答"这套 Agent 这个星期替客服干掉了多少活、花了多少钱"。两者受众不同
（前者是运维，后者是业务负责人），混在一起会让每次运营改口径都碰到部署代码。

口径都写在这里，别在页面上重算一遍：

- **机器人独立解决率** = 窗口内没有任何转人工工单的会话 / 窗口内创建的会话。
  注意它不等于"客户满意率"：没人转人工也可能是买家早就走了。所以同时给
  平均轮次与关闭率，三个数一起看才有意义。
- **转人工率** = 至少有一张工单的会话 / 总会话。
- **首响时长** = 工单创建 → 该会话第一条**人工**回复（meta.from 是 human_agent
  或 human_operator）。机器人自动回复不计，否则自动应答会把首响刷成"很快"。
- **处理时长** = 工单 open → closed。
- **会话时长** = 会话 created_at → closed_at（未关闭的用 updated_at 近似，并单独标出来）。
"""
from __future__ import annotations

import datetime

from fastapi import APIRouter
from sqlalchemy import func, or_
from sqlmodel import Session, col, select

from app.core.security import DbSession, RequireStaff
from app.db.models import (
    Conversation,
    ExternalConversation,
    HandoffTicket,
    Message,
    SessionRating,
    pk,
    utcnow,
)

router = APIRouter(prefix="/insights", tags=["insights"])

_HUMAN_REPLY = or_(col(Message.meta_json).like("%human_agent%"),
                   col(Message.meta_json).like("%human_operator%"))


def _window(days: int) -> datetime.datetime:
    return utcnow() - datetime.timedelta(days=max(1, min(days, 90)))


def _counts(session: Session, since: datetime.datetime) -> dict:
    conv_total = int(session.exec(select(func.count()).select_from(Conversation)
                                  .where(col(Conversation.created_at) >= since)).one())
    closed = int(session.exec(select(func.count()).select_from(Conversation)
                              .where(col(Conversation.created_at) >= since,
                                     col(Conversation.status) == "closed")).one())
    conv_with_ticket = int(session.exec(
        select(func.count(col(HandoffTicket.conversation_id).distinct()))
        .where(col(HandoffTicket.created_at) >= since)).one())
    messages = int(session.exec(select(func.count()).select_from(Message)
                                .where(col(Message.created_at) >= since)).one())
    channels = dict(session.exec(
        select(ExternalConversation.platform, func.count())
        .where(col(ExternalConversation.created_at) >= since)
        .group_by(col(ExternalConversation.platform))).all())
    return {
        "conversations": conv_total,
        "closed": closed,
        "active": conv_total - closed,
        "close_rate": round(closed / conv_total, 4) if conv_total else None,
        "messages": messages,
        "avg_messages_per_conversation": round(messages / conv_total, 2) if conv_total else None,
        "conversations_with_handoff": conv_with_ticket,
        "handoff_rate": round(conv_with_ticket / conv_total, 4) if conv_total else None,
        "bot_resolved_rate": round((conv_total - conv_with_ticket) / conv_total, 4) if conv_total else None,
        "by_channel": {"web": max(0, conv_total - sum(int(v or 0) for v in channels.values())),
                       **{str(k): int(v or 0) for k, v in channels.items()}},
    }


def _ticket_metrics(session: Session, since: datetime.datetime) -> dict:
    rows = session.exec(select(HandoffTicket)).all()
    in_window = [t for t in rows if _aware(t.created_at) >= since]
    counts: dict[str, int] = {}
    for t in in_window:
        counts[t.status] = counts.get(t.status, 0) + 1

    first_response: list[float] = []
    handled: list[float] = []
    ids = [pk(t) for t in in_window]
    if ids:
        pairs = session.exec(
            select(HandoffTicket.created_at, func.min(Message.created_at))
            .join(Message, (col(Message.conversation_id) == col(HandoffTicket.conversation_id))
                  & _HUMAN_REPLY & (col(Message.created_at) >= col(HandoffTicket.created_at)))
            .where(col(HandoffTicket.id).in_(ids))
            .group_by(col(HandoffTicket.id))).all()
        first_response = [(_aware(first) - _aware(created)).total_seconds()
                          for created, first in pairs if first]
    handled = [(_aware(t.closed_at) - _aware(t.created_at)).total_seconds()
               for t in in_window if t.closed_at]
    return {
        "tickets": len(in_window),
        "by_status": counts,
        "responded": len(first_response),
        "avg_first_response_seconds": (round(sum(first_response) / len(first_response), 1)
                                       if first_response else None),
        "closed": len(handled),
        "avg_handling_seconds": (round(sum(handled) / len(handled), 1) if handled else None),
    }


def _session_duration(session: Session, since: datetime.datetime) -> dict:
    """已关闭会话用 created→updated 近似（closed 时 sweeper 会刷 updated_at）。"""
    rows = session.exec(select(Conversation.created_at, Conversation.updated_at)
                        .where(col(Conversation.created_at) >= since,
                               col(Conversation.status) == "closed")).all()
    spans = [(_aware(end) - _aware(start)).total_seconds() for start, end in rows]
    return {"closed_with_duration": len(spans),
            "avg_session_seconds": round(sum(spans) / len(spans), 1) if spans else None,
            "median_session_seconds": _median(spans)}


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return round(ordered[mid], 1)
    return round((ordered[mid - 1] + ordered[mid]) / 2, 1)


def _aware(value: datetime.datetime) -> datetime.datetime:
    return value if value.tzinfo else value.replace(tzinfo=datetime.UTC)


def _llm_metrics(session: Session, since: datetime.datetime) -> dict:
    from app.services import usage

    summary = usage.summary(session, since=since)
    totals = dict(summary["totals"])
    totals["error_rate"] = (round(totals["not_ok"] / totals["calls"], 4)
                            if totals.get("calls") else None)
    acc: dict[str, dict[str, float]] = {}
    for item in summary["items"]:
        bucket = acc.setdefault(item["kind"], {"calls": 0, "total_tokens": 0,
                                               "cost_est": 0, "not_ok": 0, "has_cost": 0})
        bucket["calls"] += item["calls"]
        bucket["total_tokens"] += item["total_tokens"]
        bucket["not_ok"] += item["not_ok"]
        if item["cost_est"] is not None:
            bucket["cost_est"] += item["cost_est"]
            bucket["has_cost"] = 1
    priced = summary["pricing_configured"]
    by_kind = {kind: {"calls": int(b["calls"]), "total_tokens": int(b["total_tokens"]),
                      "not_ok": int(b["not_ok"]),
                      "cost_est": (round(b["cost_est"], 6) if (priced and b["has_cost"]) else None)}
               for kind, b in acc.items()}
    return {"totals": totals, "by_kind": by_kind,
            "pricing_configured": summary["pricing_configured"],
            "estimated_only_kinds": sorted({i["kind"] for i in summary["items"]
                                           if i["kind"] in ("embedding", "rerank")})}


def _satisfaction(session: Session, since: datetime.datetime) -> dict:
    """满意度：窗口内已关闭会话里有多少参评、均分与分布。

    分母用"窗口内创建的已关闭会话"而不是全部会话：还没结束的会话没有评价链接，
    把它们算进分母会让参评率虚低。均分保留一位小数，分布给 1-5 各自计数，
    因为"均分 4.2"既可能是普遍不错也可能是两极分化。
    """
    closed = int(session.exec(select(func.count()).select_from(Conversation)
                              .where(col(Conversation.created_at) >= since,
                                     col(Conversation.status) == "closed")).one())
    rows = session.exec(select(SessionRating.rating)
                        .join(Conversation,
                              col(SessionRating.conversation_id) == col(Conversation.id))
                        .where(col(Conversation.created_at) >= since)).all()
    scores = [int(r) for r in rows]
    return {
        "rated": len(scores),
        "closed_conversations": closed,
        "response_rate": round(len(scores) / closed, 4) if closed else None,
        "avg_rating": round(sum(scores) / len(scores), 1) if scores else None,
        "distribution": {str(i): sum(1 for x in scores if x == i) for i in range(1, 6)},
    }


@router.get("/dashboard")
def dashboard(user: RequireStaff, session: DbSession, days: int = 7):
    since = _window(days)
    return {
        "since": since,
        "days": max(1, min(days, 90)),
        "conversations": _counts(session, since),
        "handoff": _ticket_metrics(session, since),
        "session_duration": _session_duration(session, since),
        "satisfaction": _satisfaction(session, since),
        # 外发送达/失败在 GET /api/v1/system/status 里已经按状态分好，看板不重复统计
        "llm": _llm_metrics(session, since),
    }
