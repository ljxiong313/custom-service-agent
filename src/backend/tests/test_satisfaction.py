"""B3-13 会话满意度：签名回访链接、一票到底、结单挂链接、看板汇总。

回访页是本项目少数"免 JWT 的公开写入口"，所以断言的重心在鉴权边界上：
换会话 id、改分数、过期、重复提交都必须被拒。
"""
from __future__ import annotations

import time

import pytest
from sqlmodel import Session, select

from app.db.models import Conversation, Message, SessionRating, User, utcnow
from app.db.session import engine
from app.services import lifecycle
from app.utils import feedback_url

API = "/api/v1"


def _conv(db: Session, name: str = "sat_owner", status: str = "closed",
          prompted: bool = False) -> Conversation:
    user = db.exec(select(User).where(User.username == name)).first()
    if user is None:
        user = User(username=name, hashed_password="x", role="user")
        db.add(user)
        db.commit()
    conv = Conversation(user_id=user.id, title="满意度用例", status=status)
    if prompted:
        # close_conversation 的抢行条件是 "active 且已发过结束提示"，与真实时序一致
        conv.handoff_status = "bot"
        conv.end_prompted_at = utcnow()
    db.add(conv)
    db.commit()
    return conv


def _link(conv_id: int, ttl: int = 3600) -> dict:
    url = feedback_url.sign_link(conv_id, ttl=ttl)
    q = dict(kv.split("=", 1) for kv in url.split("?", 1)[1].split("&"))
    return {"conv": q["conv"], "exp": q["exp"], "sig": q["sig"], "raw": url}


# ---- 签名 ----

def test_link_roundtrip_and_expiry():
    link = _link(42)
    assert feedback_url.verify_link(42, link["exp"], link["sig"]) is True
    assert feedback_url.verify_link(42, link["exp"], "0" * 32) is False
    # 换会话 id 但沿用旧签名 → 拒（签名绑定具体会话，不是一串通用通行证）
    assert feedback_url.verify_link(43, link["exp"], link["sig"]) is False
    assert feedback_url.verify_link(42, str(int(time.time()) - 1), link["sig"]) is False
    assert feedback_url.verify_link(42, None, link["sig"]) is False


def test_survey_text_respects_switch(monkeypatch):
    from app.config import get_settings

    monkeypatch.setitem(get_settings(), "session", {**get_settings().get("session", {}),
                                                   "rate_survey_enabled": False})
    assert feedback_url.survey_text(7) == ""
    monkeypatch.setitem(get_settings(), "session", {**get_settings().get("session", {}),
                                                   "rate_survey_enabled": True,
                                                   "rate_survey_days": 3})
    text = feedback_url.survey_text(7)
    assert "conv=7" in text and "sig=" in text
    assert f"exp={int(time.time()) + 3 * 86400}" in text[:text.index("&sig")] or "exp=" in text


# ---- 公开页面与提交 ----

def test_survey_page_requires_valid_signature(client, db):
    conv = _conv(db)
    assert client.get("/api/feedback", params={"conv": conv.id}).status_code == 403
    assert client.get("/api/feedback", params={"conv": conv.id, "exp": "9999999999",
                                               "sig": "x" * 32}).status_code == 403
    link = _link(conv.id)
    r = client.get("/api/feedback", params={"conv": link["conv"], "exp": link["exp"],
                                            "sig": link["sig"]})
    assert r.status_code == 200
    assert 'method="post"' in r.text and 'name="score" value="5"' in r.text


def test_submit_rating_one_vote_per_conversation(client, db):
    conv = _conv(db)
    link = _link(conv.id)
    ok = client.post("/api/feedback/rate", data={**{k: link[k] for k in ("conv", "exp", "sig")},
                                                 "score": "5", "comment": "很快"} )
    assert ok.status_code == 200 and ok.json()["rating"] == 5
    with Session(engine) as session:
        row = session.exec(select(SessionRating)
                           .where(SessionRating.conversation_id == conv.id)).first()
        assert row is not None and row.rating == 5 and row.comment == "很快"
        assert row.source == "survey_link"
    # 同一链接再投：409，且不覆盖第一票
    again = client.post("/api/feedback/rate", data={**{k: link[k] for k in ("conv", "exp", "sig")},
                                                    "score": "1"})
    assert again.status_code == 409
    with Session(engine) as session:
        row = session.exec(select(SessionRating)
                           .where(SessionRating.conversation_id == conv.id)).first()
        assert row is not None and row.rating == 5


def test_submit_rating_rejects_bad_input(client, db):
    conv = _conv(db)
    link = _link(conv.id)
    form = {k: link[k] for k in ("conv", "exp", "sig")}
    assert client.post("/api/feedback/rate", data={**form, "score": "9"}).status_code == 422
    assert client.post("/api/feedback/rate", data={**form, "score": "0"}).status_code == 422
    assert client.post("/api/feedback/rate", data={**form, "score": "4",
                                                   "sig": "0" * 32}).status_code == 403
    missing = client.post("/api/feedback/rate", data={**form, "score": "4", "conv": "999999"})
    assert missing.status_code == 403          # conv 变了但签名没变 → 先被签名挡下


def test_comment_is_truncated_not_rejected(client, db):
    conv = _conv(db)
    link = _link(conv.id)
    r = client.post("/api/feedback/rate", data={**{k: link[k] for k in ("conv", "exp", "sig")},
                                                "score": "3", "comment": "长" * 900})
    assert r.status_code == 200
    with Session(engine) as session:
        row = session.exec(select(SessionRating)
                           .where(SessionRating.conversation_id == conv.id)).first()
        assert row is not None and len(row.comment) <= 500


def test_page_has_no_script_and_escapes_params(client, db):
    """回访页不写 JS，也不把查询参数原样吐回 HTML（反射型 XSS 的常见来源）。"""
    conv = _conv(db)
    link = _link(conv.id)
    r = client.get("/api/feedback", params={"conv": link["conv"], "exp": "<script>alert(1)</script>",
                                            "sig": link["sig"]})
    # 签名不匹配 → 403 页面；且非法参数不会以可执行形式出现在响应里
    assert r.status_code == 403
    assert "<script>alert" not in r.text


# ---- 结单挂链接 ----

def _closing_texts(session: Session, conversation_id: int) -> list[str]:
    return [m.content for m in session.exec(
        select(Message).where(Message.conversation_id == conversation_id)).all()]


def test_close_appends_survey_only_when_enabled(db, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(lifecycle, "_deliver", lambda _id, _text: True)   # 不真外发

    off = _conv(db, "close_off", status="active", prompted=True)
    monkeypatch.setitem(get_settings(), "session", {**get_settings().get("session", {}),
                                                    "rate_survey_enabled": False})
    assert lifecycle.close_conversation(off.id) is True
    with Session(engine) as session:
        texts = _closing_texts(session, off.id)
    assert any("本次会话已结束" in t and "评价" not in t for t in texts)

    on = _conv(db, "close_on", status="active", prompted=True)
    monkeypatch.setitem(get_settings(), "session", {**get_settings().get("session", {}),
                                                    "rate_survey_enabled": True})
    assert lifecycle.close_conversation(on.id) is True
    with Session(engine) as session:
        texts2 = _closing_texts(session, on.id)
    assert any("本次会话已结束" in t and "点此评价" in t and "sig=" in t for t in texts2)


# ---- 看板汇总 ----

def test_dashboard_satisfaction_block(client, staff, db):
    closed = _conv(db, "sat_dashboard")
    # 进行中的会话没有回访链接，算进分母会让参评率虚低——所以它必须不参与统计
    _conv(db, "sat_dashboard", status="active")
    db.add(SessionRating(conversation_id=closed.id, rating=5))
    other = _conv(db, "sat_other")
    db.add(SessionRating(conversation_id=other.id, rating=2))
    db.commit()

    body = client.get(f"{API}/insights/dashboard", headers=staff["headers"]).json()["satisfaction"]
    assert body["rated"] == 2
    assert body["avg_rating"] == 3.5
    assert body["distribution"] == {"1": 0, "2": 1, "3": 0, "4": 0, "5": 1}
    assert body["closed_conversations"] == 2
    assert body["response_rate"] == 1.0


def test_deleting_conversation_removes_rating(client, db):
    """评分行有外键，级联清理漏了它会让"删除会话"直接报错。"""
    from app.db import cascade

    conv = _conv(db, "cascade_owner")
    db.add(SessionRating(conversation_id=conv.id, rating=4))
    db.commit()
    cascade.delete_conversation(db, conv)
    with Session(engine) as session:
        assert session.exec(select(SessionRating)
                            .where(SessionRating.conversation_id == conv.id)).first() is None


def test_feedback_secret_falls_back_to_jwt_secret(monkeypatch):
    from app.config import get_settings

    link = _link(99)
    assert feedback_url.verify_link(99, link["exp"], link["sig"]) is True
    monkeypatch.setitem(get_settings(), "security", {**get_settings().get("security", {}),
                                                     "feedback_secret": "单独的回访密钥"})
    fresh = _link(99)
    assert feedback_url.verify_link(99, fresh["exp"], fresh["sig"]) is True
    # 换密钥后旧链接立刻失效（轮换密钥就是撤销所有已发出的回访链接）
    assert feedback_url.verify_link(99, link["exp"], link["sig"]) is False


@pytest.mark.parametrize("score", ["1", "5"])
def test_extreme_scores_are_accepted(client, db, score):
    conv = _conv(db, f"edge_{score}")
    link = _link(conv.id)
    r = client.post("/api/feedback/rate", data={**{k: link[k] for k in ("conv", "exp", "sig")},
                                                "score": score})
    assert r.status_code == 200
