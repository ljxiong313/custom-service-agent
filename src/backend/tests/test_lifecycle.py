"""P8 会话生命周期扫描单测：抢占式推进、重复扫描幂等、超时强制结单。

时间用 sweep_once(now=...) 注入，配置里的 idle/close 在 conftest 里被拉到 10 万秒，
所以 lifespan 启动的后台扫描绝不会和这些用例抢状态。
"""
from __future__ import annotations

import datetime

import pytest
from sqlmodel import Session, select

from app.db.models import Conversation, HandoffTicket, Message, User
from app.db.session import engine
from app.services import lifecycle, outbox
from app.services.platforms import adapters as _adapters  # noqa: F401  触发适配器注册


def _utc(dt: datetime.datetime) -> datetime.datetime:
    return dt.replace(tzinfo=datetime.UTC)


@pytest.fixture
def make_conv(db: Session):
    def _make(user_id: int, *, minutes_since_customer: int = 0, prompted_minutes_ago: int | None = None,
              status: str = "active") -> Conversation:
        now = datetime.datetime.now(datetime.UTC)
        conv = Conversation(user_id=user_id, title="会话", status=status,
                            last_customer_msg_at=_utc(now - datetime.timedelta(minutes=minutes_since_customer))
                            if minutes_since_customer is not None else None,
                            end_prompted_at=_utc(now - datetime.timedelta(minutes=prompted_minutes_ago))
                            if prompted_minutes_ago is not None else None)
        db.add(conv)
        db.commit()
        db.refresh(conv)
        return conv
    return _make


@pytest.fixture
def buyer(db: Session) -> int:
    u = User(username="buyer1", hashed_password="x", email=None, role="user")
    db.add(u)
    db.commit()
    return u.id or 0


def _system_messages(conv_id: int) -> list[str]:
    with Session(engine) as s:
        return [m.meta_json for m in s.exec(
            select(Message).where(Message.conversation_id == conv_id)).all()
            if m.meta_json and "system_" in m.meta_json]


def test_idle_conversation_gets_prompt_once(make_conv, buyer):
    conv = make_conv(buyer, minutes_since_customer=9999)
    with Session(engine) as s:
        s.add(Message(conversation_id=conv.id, role="user", content="有货吗"))
        s.commit()

    assert lifecycle.sweep_once()["prompted"] == 1
    with Session(engine) as s:
        fresh = s.get(Conversation, conv.id)
        assert fresh.status == "active" and fresh.end_prompted_at is not None
    assert _system_messages(conv.id) == ['{"from": "system_idle_prompt"}']

    # 第二轮扫描不能再提示一次：买家会收到两遍"是否结束会话"
    assert lifecycle.sweep_once()["prompted"] == 0
    assert len(_system_messages(conv.id)) == 1


def test_customer_reply_resets_the_clock(make_conv, buyer):
    """锚点是 last_customer_msg_at：客户一说话，idle 计时重新开始（旧实现用 updated_at 会误结束）。"""
    now = datetime.datetime.now(datetime.UTC)
    conv = make_conv(buyer, minutes_since_customer=0)
    with Session(engine) as s:
        conv2 = s.get(Conversation, conv.id)
        conv2.last_customer_msg_at = _utc(now)      # 刚刚有客户消息
        s.add(conv2)
        s.commit()
    assert lifecycle.sweep_once(now=now + datetime.timedelta(minutes=1)) == {"prompted": 0, "closed": 0}


def test_prompted_conversation_closes_after_grace(make_conv, buyer):
    conv = make_conv(buyer, minutes_since_customer=9999, prompted_minutes_ago=9999)
    assert lifecycle.sweep_once()["closed"] == 1
    with Session(engine) as s:
        assert s.get(Conversation, conv.id).status == "closed"
    assert _system_messages(conv.id) == ['{"from": "system_session_closed"}']
    # 已 closed 的会话不再进入候选集，重复扫描也不会再落一条告别
    assert lifecycle.sweep_once()["closed"] == 0
    assert len(_system_messages(conv.id)) == 1


def test_close_forces_open_tickets_closed(make_conv, buyer, db: Session):
    conv = make_conv(buyer, minutes_since_customer=9999, prompted_minutes_ago=9999)
    t = HandoffTicket(conversation_id=conv.id, user_id=buyer, reason="要人工",
                      status="open", customer_name="张三")
    db.add(t)
    db.commit()
    lifecycle.sweep_once()
    with Session(engine) as s:
        row = s.get(HandoffTicket, t.id)
        assert row.status == "closed" and row.closed_at is not None


def test_claim_loses_race_to_another_sweep(make_conv, buyer):
    """同一会话被两轮扫描/两个线程看到时，只有一个能推进状态（B1-5 的抢占）。"""
    conv = make_conv(buyer, minutes_since_customer=9999)
    cid = conv.id or 0
    assert lifecycle.prompt_conversation(cid, 60) is True
    assert lifecycle.prompt_conversation(cid, 60) is False       # end_prompted_at 已置位，抢不到
    assert len(_system_messages(cid)) == 1
    assert lifecycle.close_conversation(cid) is True             # 提示过了，允许终结
    assert lifecycle.close_conversation(cid) is False            # 已 closed：告别语不会发第二遍
    assert len(_system_messages(cid)) == 2


def test_close_requires_a_prior_prompt(make_conv, buyer):
    """没发过提示就终结会把正在聊的买家直接踢掉，SQL 条件里就把它挡住。"""
    conv = make_conv(buyer, minutes_since_customer=9999, prompted_minutes_ago=None)
    assert lifecycle.close_conversation(conv.id or 0) is False


def test_null_anchor_conversations_are_ignored(make_conv, buyer):
    """last_customer_msg_at 为空的历史会话（老数据）不参与超时，避免凭空终结。"""
    with Session(engine) as s:
        conv = Conversation(user_id=buyer, title="旧", status="active",
                            last_customer_msg_at=None)
        s.add(conv)
        s.commit()
        conv_id = conv.id
    assert lifecycle.sweep_once() == {"prompted": 0, "closed": 0}
    with Session(engine) as s:
        assert s.get(Conversation, conv_id).status == "active"


def test_prompt_and_close_are_delivered_to_platform(make_conv, buyer):
    """平台会话的超时提示/告别必须真的走外发箱，否则买家侧只看到永久已读。"""
    from app.services.platforms.base import InboundMessage
    from app.services.platforms.inbound import ensure_external_conversation

    with Session(engine) as s:
        user, conv = ensure_external_conversation(
            s, InboundMessage(platform="taobao", shop_id="s1", buyer_id="b1", text="hi"))
        conv.last_customer_msg_at = _utc(
            datetime.datetime.now(datetime.UTC) - datetime.timedelta(minutes=9999))
        s.add(conv)
        s.commit()
    lifecycle.sweep_once()
    statuses = [r["status"] for r in outbox.recent("taobao")]
    assert statuses and all(s_ == "mocked" for s_ in statuses)
    assert all(r["kind"] == "text" for r in outbox.recent("taobao"))
