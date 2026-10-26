"""企微回调单测：验签口径 + 客服回复回写。

钉住的是 B3-2 上静态检查抓出来的两处真实缺陷（都在生产路径上，不跑一次就会 500）：
- `starlette.requests.Request` 没有 `.text()`，回调 POST 必须先 `body()` 再解码；
- GET 验证的签名对象是 **base64 密文字符串本身**，早期实现先 b64decode 再签，
  既不符合企微口径，也会在 `sorted()` 里拿 str 和 bytes 比较直接 TypeError。
"""
from __future__ import annotations

import copy
import hashlib

import pytest
from fastapi import HTTPException
from sqlmodel import Session, select

from app.config import get_settings
from app.db.models import Conversation, HandoffTicket, Message, User, pk
from app.db.session import engine
from app.services.wecom.crypto import WecomCrypto

API = "/api/wecom"
TOKEN = "test-callback-token"
AES_KEY = "K" * 43          # 企微 EncodingAESKey 固定 43 字符，补 '=' 后正好 32 字节
CORP_ID = "ww_test_corp"


@pytest.fixture
def wecom_conf():
    """临时填上回调三要素（conftest 里留空，_crypto() 会直接 400）。用完原样还原。"""
    settings = get_settings()
    original = copy.deepcopy(dict(settings.get("wecom", {})))
    node = settings.setdefault("wecom", {})
    node.update({"callback_token": TOKEN, "encoding_aes_key": AES_KEY, "corp_id": CORP_ID})
    yield
    settings["wecom"] = original


def _sign(encrypt: str, timestamp: str, nonce: str) -> str:
    return hashlib.sha1("".join(sorted([TOKEN, timestamp, nonce, encrypt])).encode()).hexdigest()


def _staff_xml(ticket_id: int, text: str, staff: str = "kefu01") -> tuple[str, str, str, str]:
    crypto = WecomCrypto(TOKEN, AES_KEY, CORP_ID)
    plain = (f"<xml><MsgType>text</MsgType><FromUserName>{staff}</FromUserName>"
             f"<Content>工单#{ticket_id} {text}</Content></xml>")
    encrypt = crypto.encrypt(plain)
    body = f"<xml><Encrypt>{encrypt}</Encrypt></xml>"
    ts, nonce = "1700000000", "n0nce"
    return body, _sign(encrypt, ts, nonce), ts, nonce


def _post(client, body: str, signature: str, ts: str, nonce: str):
    return client.post(f"{API}/callback", content=body.encode("utf-8"),
                       params={"msg_signature": signature, "timestamp": ts, "nonce": nonce},
                       headers={"Content-Type": "application/xml"})


# ---- 验签口径 ----

def test_verify_url_signs_the_base64_echostr():
    crypto = WecomCrypto(TOKEN, AES_KEY, CORP_ID)
    echostr = crypto.encrypt("plain-echo")
    ts, nonce = "1700000000", "n0nce"
    assert crypto.verify_url(_sign(echostr, ts, nonce), ts, nonce, echostr) == "plain-echo"
    with pytest.raises(HTTPException) as exc:
        crypto.verify_url("deadbeef", ts, nonce, echostr)
    assert exc.value.status_code == 400


def test_verify_url_rejects_other_corp_id():
    """密文里的 receive_id 必须等于 corp_id，否则是别的企业的回调串门。"""
    other = WecomCrypto(TOKEN, AES_KEY, "ww_other_corp")
    echostr = other.encrypt("hi")
    ts, nonce = "1700000000", "n0nce"
    with pytest.raises(HTTPException) as exc:
        WecomCrypto(TOKEN, AES_KEY, CORP_ID).verify_url(_sign(echostr, ts, nonce), ts, nonce, echostr)
    assert exc.value.status_code == 400


def test_get_callback_returns_decrypted_echo(client, wecom_conf):
    crypto = WecomCrypto(TOKEN, AES_KEY, CORP_ID)
    echostr = crypto.encrypt("echo-ok")
    ts, nonce = "1700000000", "n0nce"
    r = client.get(f"{API}/callback", params={"msg_signature": _sign(echostr, ts, nonce),
                                              "timestamp": ts, "nonce": nonce, "echostr": echostr})
    assert r.status_code == 200 and r.json() == "echo-ok"


def test_callback_without_config_is_400(client):
    """配置缺失要报人话，不能让 WecomCrypto 在 b64decode 处抛裸异常。"""
    r = client.get(f"{API}/callback", params={"msg_signature": "x", "timestamp": "1",
                                              "nonce": "1", "echostr": "1"})
    assert r.status_code == 400 and "企微回调未配置" in r.json()["detail"]


# ---- 客服回复回写 ----

def _ticket(session: Session) -> HandoffTicket:
    user = User(username="buyer1", hashed_password="x")
    session.add(user)
    session.commit()
    session.refresh(user)
    conv = Conversation(user_id=pk(user), handoff_status="pending_human")
    session.add(conv)
    session.commit()
    session.refresh(conv)
    ticket = HandoffTicket(conversation_id=pk(conv), user_id=pk(user), reason="用户要求人工")
    session.add(ticket)
    session.commit()
    session.refresh(ticket)
    return ticket


def test_post_callback_writes_staff_reply_into_conversation(client, wecom_conf, db):
    ticket = _ticket(db)
    conv_id, ticket_id = ticket.conversation_id, pk(ticket)
    body, sign, ts, nonce = _staff_xml(ticket_id, "已为您加急退款")

    r = _post(client, body, sign, ts, nonce)
    assert r.status_code == 200 and r.json() == "success"

    with Session(engine) as s:      # 端点自己提交，另开会话读，避开 db fixture 的缓存
        msg = s.exec(select(Message).where(Message.conversation_id == conv_id,
                                          Message.role == "assistant")).one()
        assert msg.content == "已为您加急退款" and '"human_agent"' in (msg.meta_json or "")
        t = s.get(HandoffTicket, ticket_id)
        assert (t.status, t.staff) == ("handling", "kefu01")
        assert s.get(Conversation, conv_id).handoff_status == "human"


def test_post_callback_ignores_free_text(client, wecom_conf, db):
    """格式不含工单号的消息只记日志：不能凭空往用户会话里插一条客服发言。"""
    ticket = _ticket(db)
    conv_id = ticket.conversation_id
    crypto = WecomCrypto(TOKEN, AES_KEY, CORP_ID)
    encrypt = crypto.encrypt("<xml><MsgType>text</MsgType><FromUserName>kefu01</FromUserName>"
                             "<Content>在忙，稍等</Content></xml>")
    ts, nonce = "1700000000", "n0nce"
    r = _post(client, f"<xml><Encrypt>{encrypt}</Encrypt></xml>", _sign(encrypt, ts, nonce), ts, nonce)
    assert r.status_code == 200 and r.json() == "success"

    with Session(engine) as s:
        assert s.exec(select(Message).where(Message.conversation_id == conv_id)).all() == []


def test_post_callback_rejects_tampered_signature(client, wecom_conf, db):
    ticket = _ticket(db)
    body, _, ts, nonce = _staff_xml(pk(ticket), "已退款")
    r = _post(client, body, "0" * 40, ts, nonce)
    assert r.status_code == 400

    with Session(engine) as s:
        assert s.get(HandoffTicket, pk(ticket)).status == "open"
