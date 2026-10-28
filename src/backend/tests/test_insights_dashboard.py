"""B3-10 运营看板单测：解决率/转人工率/时长的口径，以及权限与空窗口行为。

看板的数字是给业务负责人看的，口径错了比没数字更糟——所以这里断言的是"比率怎么算"，
不是"有没有返回字段"。
"""
from __future__ import annotations

import datetime

import pytest
from sqlmodel import Session

from app.db.models import (
    Conversation,
    ExternalConversation,
    HandoffTicket,
    LlmCall,
    Message,
    User,
    utcnow,
)
from app.db.session import engine

API = "/api/v1"


def _mk_user(db: Session, name: str, role: str = "user") -> User:
    user = User(username=name, hashed_password="x", role=role)
    db.add(user)
    db.commit()
    return user


def _mk_conv(db: Session, user: User, *, status: str = "active",
             created: datetime.datetime | None = None) -> Conversation:
    conv = Conversation(user_id=user.id, title="用例会话", status=status)
    if created is not None:
        conv.created_at = created
    db.add(conv)
    db.commit()
    return conv


def _dashboard(client, headers, days: int = 7) -> dict:
    r = client.get(f"{API}/insights/dashboard", headers=headers, params={"days": days})
    assert r.status_code == 200, r.text
    return r.json()


def test_dashboard_requires_staff(client, buyer):
    assert client.get(f"{API}/insights/dashboard", headers=buyer["headers"]).status_code == 403
    assert client.get(f"{API}/insights/dashboard").status_code == 401


def test_dashboard_empty_window_shape(client, staff):
    body = _dashboard(client, staff["headers"])
    assert body["conversations"]["conversations"] == 0
    # 零会话时比率必须是 null，而不是 0%——把"没数据"显示成"解决率 0%"会被业务方误读
    assert body["conversations"]["bot_resolved_rate"] is None
    assert body["conversations"]["handoff_rate"] is None
    assert body["session_duration"]["avg_session_seconds"] is None
    assert body["llm"]["totals"]["calls"] == 0


def test_rates_use_conversation_denominator(client, staff, db):
    user = _mk_user(db, "dash_owner")
    for _ in range(4):
        _mk_conv(db, user)
    bot_side = _mk_conv(db, user)
    ticket = HandoffTicket(conversation_id=bot_side.id, user_id=user.id,
                           reason="用例", status="open")
    db.add(ticket)
    db.commit()

    body = _dashboard(client, staff["headers"])
    conv = body["conversations"]
    assert conv["conversations"] == 5
    assert conv["conversations_with_handoff"] == 1
    assert conv["handoff_rate"] == 0.2
    assert conv["bot_resolved_rate"] == 0.8
    assert body["handoff"]["tickets"] == 1
    assert body["handoff"]["by_status"] == {"open": 1}


def test_out_of_window_conversations_are_excluded(client, staff, db):
    user = _mk_user(db, "old_owner")
    old = _mk_conv(db, user, created=utcnow() - datetime.timedelta(days=30))
    _mk_conv(db, user)                                   # 窗口内一条
    db.add(HandoffTicket(conversation_id=old.id, user_id=old.id, reason="老工单", status="open",
                         created_at=utcnow() - datetime.timedelta(days=30)))
    db.commit()

    body = _dashboard(client, staff["headers"], days=7)
    assert body["conversations"]["conversations"] == 1
    assert body["conversations"]["conversations_with_handoff"] == 0
    assert body["handoff"]["tickets"] == 0


def test_first_response_counts_only_human_replies(client, staff, db):
    """机器人自动回复不能算首响，否则首响永远"很快"。"""
    user = _mk_user(db, "fr_owner")
    conv = _mk_conv(db, user)
    created = utcnow()
    db.add(HandoffTicket(conversation_id=conv.id, user_id=user.id, reason="用例",
                         status="open", created_at=created))
    db.commit()
    db.add(Message(conversation_id=conv.id, role="assistant", content="机器人代答",
                   meta_json='{"from": "assistant"}', created_at=created + datetime.timedelta(seconds=5)))
    db.add(Message(conversation_id=conv.id, role="assistant", content="人工来了",
                   meta_json='{"from": "human_operator", "staff": "cs01"}',
                   created_at=created + datetime.timedelta(seconds=125)))
    db.commit()

    body = _dashboard(client, staff["headers"])
    assert body["handoff"]["responded"] == 1
    assert body["handoff"]["avg_first_response_seconds"] == pytest.approx(125.0, abs=1.0)


def test_handling_and_session_durations(client, staff, db):
    user = _mk_user(db, "dur_owner")
    conv = _mk_conv(db, user)
    now = utcnow()
    conv.status = "closed"
    conv.created_at = now - datetime.timedelta(minutes=10)
    conv.updated_at = now
    db.add(conv)
    created = now - datetime.timedelta(minutes=6)
    db.add(HandoffTicket(conversation_id=conv.id, user_id=user.id, reason="用例", status="closed",
                         created_at=created, closed_at=created + datetime.timedelta(seconds=90)))
    db.commit()

    body = _dashboard(client, staff["headers"])
    assert body["session_duration"]["avg_session_seconds"] == pytest.approx(600.0, abs=2.0)
    assert body["session_duration"]["median_session_seconds"] == pytest.approx(600.0, abs=2.0)
    assert body["handoff"]["avg_handling_seconds"] == pytest.approx(90.0, abs=1.0)


def test_channel_breakdown(client, staff, db):
    user = _mk_user(db, "chan_owner")
    conv = _mk_conv(db, user)
    db.add(ExternalConversation(platform="taobao", shop_id="s1", buyer_id="b1",
                                conversation_id=conv.id))
    db.commit()
    body = _dashboard(client, staff["headers"])
    assert body["conversations"]["by_channel"] == {"web": 0, "taobao": 1}


def test_llm_block_aggregates_cost_and_error_rate(client, staff):
    with Session(engine) as session:
        for status in ("ok", "ok", "error"):
            session.add(LlmCall(kind="chat", provider="dashscope", model="qwen-plus",
                                input_tokens=100, output_tokens=50, total_tokens=150,
                                cost_est=0.01, status=status))
        session.add(LlmCall(kind="embedding", provider="dashscope", model="text-embedding-v4",
                            input_tokens=400, total_tokens=400, estimated=True, status="ok"))
        session.commit()

    body = _dashboard(client, staff["headers"])
    llm = body["llm"]
    assert llm["totals"]["calls"] == 4
    assert llm["totals"]["not_ok"] == 1
    assert llm["totals"]["error_rate"] == 0.25
    assert llm["by_kind"]["chat"]["calls"] == 3
    # 没配 llm.pricing 时按 kind 的成本汇总显示 null（即便流水里有历史 cost 值也不当作当前价目）
    assert llm["by_kind"]["chat"]["cost_est"] is None
    assert llm["estimated_only_kinds"] == ["embedding"]      # 这两类只有本地估算值
    assert llm["pricing_configured"] is False


def test_cost_shows_none_when_pricing_missing(client, staff):
    """没配价时各 kind 的成本必须是 null：报表显示 0 元会被当成"模型免费"。"""
    with Session(engine) as session:
        session.add(LlmCall(kind="chat", provider="dashscope", model="qwen-plus",
                            input_tokens=10, total_tokens=10, status="ok"))   # cost_est 留空
        session.commit()
    body = _dashboard(client, staff["headers"])
    assert body["llm"]["pricing_configured"] is False
    assert body["llm"]["by_kind"]["chat"]["cost_est"] is None


def test_days_param_is_clamped(client, staff):
    for days, expected in ((0, 1), (999, 90), (7, 7)):
        assert _dashboard(client, staff["headers"], days=days)["days"] == expected
