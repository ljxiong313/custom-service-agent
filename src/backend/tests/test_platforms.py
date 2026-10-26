"""平台适配层单测：签名、报文解析、四态出站留痕、就绪自检、入站建档与幂等。

全程不打网络：真实外呼用 monkeypatch 顶掉，配置改写直接落在 get_settings() 的单例上
（Settings 是 dict 子类，用例结束后必须原样还原，否则后面的用例会被"上一个平台改成
真实模式"污染）。
"""
from __future__ import annotations

import copy
import hashlib
import hmac
import json

import pytest
from sqlmodel import Session, select

from app.config import get_settings
from app.db.models import ExternalConversation, InboundDedupe
from app.db.session import engine
from app.services import outbox
from app.services.platforms import adapters as platform_adapters
from app.services.platforms import base, inbound
from app.services.platforms.adapters import Douyin, Jd, Pdd, Taobao


@pytest.fixture
def conf_patch():
    """临时改 platforms.<name> 配置（None 值表示删掉该项）。"""
    settings = get_settings()
    original = copy.deepcopy(dict(settings.get("platforms", {})))

    def _patch(platform: str, **kv):
        node = settings.setdefault("platforms", {}).setdefault(platform, {})
        for key, value in kv.items():
            if value is None:
                node.pop(key, None)
            else:
                node[key] = value

    yield _patch
    settings["platforms"] = original


def _msg(platform="taobao", buyer="b1", text="有货吗", msg_id=""):
    return base.InboundMessage(platform=platform, shop_id="shop1",
                               buyer_id=buyer, text=text, external_msg_id=msg_id)


# ---- 签名 ----

def test_md5_sign_top_is_order_independent_and_uppercase():
    secret = "s3cr3t"
    a = base.md5_sign_top({"b": "2", "a": "1"}, secret)
    b = base.md5_sign_top({"a": "1", "b": "2"}, secret)
    assert a == b and len(a) == 32 and a == a.upper()
    expect = hashlib.md5(f"{secret}a1b2{secret}".encode()).hexdigest().upper()
    assert a == expect                                   # 拼串口径：secret + 排序 k+v + secret
    assert base.md5_sign_top({"a": "1", "b": "2"}, "other") != a


def test_hmac_sha256_sign_matches_stdlib():
    assert base.hmac_sha256_sign("abc", "k") == hmac.new(
        b"k", b"abc", hashlib.sha256).hexdigest()


def test_taobao_verify_signature_real_mode_rejects_forgery(conf_patch):
    conf_patch("taobao", mock=False, app_secret="topsecret")
    adapter = Taobao()
    query = {"app_key": "ak", "method": "x", "sign": "DEADBEEF"}
    assert adapter.verify_signature(query, b"{}", {}) is False
    good = base.md5_sign_top({k: v for k, v in query.items() if k != "sign"}, "topsecret")
    assert adapter.verify_signature({**query, "sign": good}, b"{}", {}) is True


def test_douyin_verify_uses_header_sign(conf_patch):
    conf_patch("douyin", mock=False, app_secret="ddsecret")
    adapter = Douyin()
    body, ts = json.dumps({"data": {"uid": "u1", "content": "hi"}}).encode(), "1700000000"
    expect = base.hmac_sha256_sign(ts + body.decode(), "ddsecret")
    assert adapter.verify_signature({"timestamp": ts}, body, {"x-doudian-sign": expect}) is True
    assert adapter.verify_signature({"timestamp": ts}, body, {"x-doudian-sign": "nope"}) is False


def test_mock_mode_skips_verification():
    assert Taobao().verify_signature({"sign": "anything"}, b"{}", {}) is True


# ---- 入站报文解析 ----

def test_top_like_parse_message_is_key_flexible():
    taobao, jd, pdd = Taobao(), Jd(), Pdd()
    m = taobao.parse_message({}, json.dumps({"Buyer_Nick": "u", "Text": "问下"}).encode())
    assert m is not None and m.platform == "taobao" and m.buyer_id == "u"
    assert jd.parse_message({}, json.dumps({"buyer_pin": "p", "msg": "x"}).encode()).shop_id == ""
    m = pdd.parse_message({}, json.dumps({"uid": "9", "content": "c", "msgId": "m1"}).encode())
    assert m.external_msg_id == "m1" and m.platform == "pdd"       # 键名统一转小写后再匹配


def test_parse_message_rejects_unusable_payloads():
    for adapter in (Taobao(), Douyin()):
        assert adapter.parse_message({}, b"not json") is None
        assert adapter.parse_message({}, b"[1,2]") is None          # 顶层必须是对象
        assert adapter.parse_message({}, json.dumps({"text": "没有买家"}).encode()) is None
        assert adapter.parse_message({}, json.dumps({"buyer_id": "u"}).encode()) is None


def test_douyin_reads_nested_data_and_truncates_long_text():
    m = Douyin().parse_message({}, json.dumps(
        {"data": {"uid": "u2", "content": "长" * 5000, "shop_id": "s2", "msg_id": "M"}}).encode())
    assert m.buyer_id == "u2" and m.shop_id == "s2" and m.external_msg_id == "M"
    assert len(m.text) == 4000                                      # 防超长报文撑爆上下文


# ---- 出站四态（每条都必须在外面留痕）----

def test_mock_send_records_mocked(conf_patch):
    rec = Taobao().send_reply("shop1", "buyer1", "您好")
    assert rec["status"] == "mocked" and rec["channel"] == "taobao"
    assert rec["mock"] is True and rec["to"] == "buyer1"


def test_real_mode_without_config_skips_and_names_missing(conf_patch):
    """配置没填齐不能报成功：状态 skipped，error 文案要指到缺的键。"""
    conf_patch("taobao", mock=False, reply_method=None, app_key="", app_secret="",
               access_token="", session_key="")
    rec = Taobao().send_reply("shop1", "buyer1", "您好")
    assert rec["status"] == "skipped"
    assert "reply_method" in rec["error"] and "app_key" in rec["error"]
    assert "平台接入.md" in rec["error"]


def test_real_mode_sends_and_records_sent(conf_patch, monkeypatch):
    conf_patch("taobao", mock=False, reply_method="taobao.kf.reply", app_key="ak",
               app_secret="sk", access_token="tok", session_key=None)
    calls = []
    monkeypatch.setattr(platform_adapters, "http_post_form",
                        lambda url, form: calls.append((url, form)) or {"ok": True})
    rec = Taobao().send_reply("shop1", "buyer1", "您好")
    assert rec["status"] == "sent" and calls and calls[0][0].endswith("/taobao")
    assert rec["ok"] is True                            # 网关原始返回不能被状态覆盖掉
    _, form = calls[0]
    assert form["method"] == "taobao.kf.reply" and form["session"] == "tok"
    assert form["content"] == "您好" and form["sign"] == base.md5_sign_top(
        {k: v for k, v in form.items() if k != "sign"}, "sk")


def test_real_mode_failure_records_failed_and_raises(conf_patch, monkeypatch):
    conf_patch("taobao", mock=False, reply_method="m", app_key="ak", app_secret="sk",
               access_token="tok", session_key=None)

    def boom(url, form):
        raise RuntimeError("网关 502")

    monkeypatch.setattr(platform_adapters, "http_post_form", boom)
    with pytest.raises(RuntimeError, match="网关 502"):
        Taobao().send_reply("shop1", "buyer1", "您好")
    assert outbox.count_by_status().get("failed") == 1


def test_media_send_without_media_method_degrades_to_skipped(conf_patch):
    """没配 media_method 时不假装附件已送达，但降级文本仍是正文的一部分。"""
    conf_patch("jd", mock=False, reply_method="m", app_key="ak", app_secret="sk",
               access_token="tok", media_method=None)
    rec = Jd().send_media("shop1", "buyer1", "/api/files/a.png", name="a.png")
    assert rec["status"] == "skipped" and rec["kind"] == "media"
    assert "media_method" in rec["error"] and rec["text"].startswith("[附件]")


def test_media_send_mocked_records_media_kind():
    rec = Pdd().send_media("shop1", "buyer1", "http://cdn/a.png", name="a.png",
                           media_type="image")
    assert rec["status"] == "mocked" and rec["kind"] == "media"


# ---- 就绪自检（B2-1：静态、不回显密钥值）----

def test_readiness_reports_missing_and_risks_without_secret_values(conf_patch):
    conf_patch("taobao", mock=False, app_secret="super-secret-value")
    report = base.readiness("taobao")
    assert report["ready_in"] is True                       # enabled + 有 app_secret
    assert report["ready_out"] is False                     # 还缺 reply_method / token
    assert "reply_method" in report["missing_out"]
    assert any(r["level"] == "warn" for r in report["risks"])
    blob = json.dumps(report, ensure_ascii=False)
    assert "super-secret-value" not in blob                 # 只报"有没有配"，绝不回显值
    keys = {i["key"] for i in report["items"]}
    assert {"reply_method", "app_key", "app_secret", "media_method", "callback_verify"} <= keys


def test_readiness_flags_none_verify_as_risk(conf_patch):
    conf_patch("pdd", mock=False, callback_verify="none")
    report = base.readiness("pdd")
    assert report["callback_verify"] == "none"
    assert any("伪造买家推送" in r["message"] for r in report["risks"])


def test_readiness_mock_platform_is_ready_for_demo():
    report = base.readiness("douyin")
    assert report["mock"] is True and report["ready_out"] is True
    assert any(r["level"] == "info" for r in report["risks"])


def test_platforms_api_endpoints_shape(client, admin):
    r = client.get("/api/v1/platforms/readiness", headers=admin["headers"])
    assert r.status_code == 200
    body = r.json()
    assert {p_["platform"] for p_ in body["platforms"]} == {"taobao", "jd", "pdd", "douyin"}
    assert body["summary"]["all_mock"] is True
    assert "reply_method" in body["summary"]["missing_keys"]
    assert "super-secret" not in json.dumps(body, ensure_ascii=False)
    out = client.get("/api/v1/platforms/outbox", headers=admin["headers"]).json()
    assert "taobao" in out


# ---- 外发箱落库 ----

def test_outbox_record_roundtrip_and_recent_order():
    for i in range(3):
        outbox.record("taobao", "text", to=f"b{i}", body=f"消息{i}", payload={"n": i})
    outbox.record("jd", "text", to="b9", body="x", payload={"n": 9})
    rows = outbox.recent("taobao")
    assert [r["n"] for r in rows] == [0, 1, 2]               # 时间正序，形状沿用旧内存记录
    assert rows[-1]["channel"] == "taobao" and rows[-1]["status"] == "mocked"
    assert outbox.count_by_channel() == {"taobao": 3, "jd": 1}
    assert outbox.count_by_status() == {"mocked": 4}


def test_outbox_tolerates_broken_payload():
    rec = outbox.record("taobao", "text", payload={"a": 1})
    from app.db.models import OutboxRecord
    with Session(engine) as s:
        row = s.get(OutboxRecord, rec["id"])
        row.payload_json = "{不是 json"
        s.add(row)
        s.commit()
    broken = outbox.recent("taobao")[0]
    assert "a" not in broken and broken["status"] == "mocked"  # 坏 payload 不抛，只当空对象


# ---- 入站：影子用户、幂等、重开一轮 ----

def test_claim_message_dedupes_platform_retries():
    with Session(engine) as s:
        assert inbound.claim_message(s, _msg(msg_id="m-1")) is True
        assert inbound.claim_message(s, _msg(msg_id="m-1")) is False   # 平台超时重推
        assert inbound.claim_message(s, _msg(msg_id="m-2")) is True
        assert inbound.claim_message(s, _msg()) is True                # 无 id 不去重
    with Session(engine) as s:
        assert len(s.exec(select(InboundDedupe)).all()) == 2   # 空 id 不落键


def test_ensure_external_conversation_creates_shadow_user():
    with Session(engine) as s:
        user, conv = inbound.ensure_external_conversation(s, _msg(buyer="buyer-A"))
        assert user.username.startswith("pl_") and not user.email
        assert conv.title.startswith("[taobao]")
        assert s.exec(select(ExternalConversation).where(
            ExternalConversation.conversation_id == conv.id)).first().buyer_id == "buyer-A"


def test_closed_conversation_reopens_a_new_round():
    """上一轮被超时终结后买家再来咨询：同一影子用户下开新会话，旧映射保留。"""
    with Session(engine) as s:
        user, conv = inbound.ensure_external_conversation(s, _msg(buyer="buyer-B"))
        first_id = conv.id
        conv.status = "closed"
        s.add(conv)
        s.commit()

        user2, conv2 = inbound.ensure_external_conversation(s, _msg(buyer="buyer-B"))
        assert user2.id == user.id and conv2.id != first_id
        assert conv2.status == "active"
        rows = s.exec(select(ExternalConversation).where(
            ExternalConversation.buyer_id == "buyer-B")).all()
        assert {r.conversation_id for r in rows} == {first_id, conv2.id}


def test_cascade_delete_removes_mapping_and_next_message_reopens_round():
    """外键打开后"只删会话留下悬空映射"已不可能；删除走 cascade，映射一并清掉。"""
    from app.db.cascade import delete_conversation

    with Session(engine) as s:
        user, conv = inbound.ensure_external_conversation(s, _msg(buyer="buyer-C"))
        user_id, conv_id = user.id, conv.id
        delete_conversation(s, conv)
        assert s.exec(select(ExternalConversation).where(
            ExternalConversation.conversation_id == conv_id)).first() is None
    with Session(engine) as s:
        user2, conv2 = inbound.ensure_external_conversation(s, _msg(buyer="buyer-C"))
        assert user2.id == user_id                      # 影子用户（长期记忆归属）不跟着会话消失
        assert conv2.id is not None and conv2.status == "active"
        assert s.exec(select(ExternalConversation).where(
            ExternalConversation.conversation_id == conv2.id)).first() is not None


def test_deliver_notice_and_media_for_web_conversation_are_noops(client, admin, db):
    """网页会话没有平台映射：外发直接返回 False，但绝不能抛错打断对话落库。"""
    r = client.post("/api/v1/conversations", headers=admin["headers"], json={"title": "网页"})
    conv_id = r.json()["id"]
    assert inbound.deliver_notice(conv_id, "您好") is False
    assert inbound.deliver_media(conv_id, [{"url": "/api/files/a.png", "name": "a.png"}]) is False


def test_deliver_paths_for_platform_conversation_in_mock(client, admin):
    with Session(engine) as s:
        _, conv = inbound.ensure_external_conversation(s, _msg(buyer="buyer-D"))
        conv_id = conv.id
    assert inbound.deliver_notice(conv_id, "您好") is True
    assert inbound.deliver_media(conv_id, [
        {"url": "/api/files/a.png", "name": "a.png", "type": "image"},
        {"url": "", "name": "空的"},                      # 没有 url 的附件不计入送达数
    ]) is True
    kinds = [r["kind"] for r in outbox.recent("taobao")]
    assert "text" in kinds and "media" in kinds


def test_shadow_users_cannot_log_in(client, admin):
    """影子用户密码是随机 uuid：绝不能被当普通账号登录。"""
    with Session(engine) as s:
        user, _ = inbound.ensure_external_conversation(s, _msg(buyer="buyer-E"))
        name = user.username
    r = client.post("/api/v1/auth/login", json={"username": name, "password": "whatever1"})
    assert r.status_code == 401
    assert client.get("/api/v1/auth/me", headers=admin["headers"]).status_code == 200


def test_inbound_dedupe_rows_survive_conversation_delete(client, admin):
    """删会话不能连带删幂等键，否则平台重推的历史消息会被再处理一遍。"""
    from app.db.cascade import delete_conversation

    with Session(engine) as s:
        assert inbound.claim_message(s, _msg(buyer="buyer-F", msg_id="keep-me")) is True
        _, conv = inbound.ensure_external_conversation(s, _msg(buyer="buyer-F"))
        delete_conversation(s, conv)
    with Session(engine) as s:
        assert s.exec(select(InboundDedupe).where(
            InboundDedupe.external_msg_id == "keep-me")).first() is not None
