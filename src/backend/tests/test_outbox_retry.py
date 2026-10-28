"""出站失败退避重试与死信可见性：外发箱的 failed 行不再"写一次就完"。

全程不打网络：真实外呼用 monkeypatch 顶掉 adapters 模块里的 http_post_form，
时间用显式传入的 `now` 推进（retry_due / claim_for_retry / finish_retry 都接 now 参数），
所以 15s/60s/240s 三档退避在一分钟内跑完，不靠 sleep 等真时间。
"""
from __future__ import annotations

import asyncio
import copy
import datetime
from contextlib import suppress

import pytest
from sqlmodel import Session

from app.config import get_settings
from app.db.models import OutboxRecord, utcnow
from app.db.session import engine
from app.services import outbox
from app.services.platforms import adapters as platform_adapters
from app.services.platforms import outbound_retry
from app.services.platforms.adapters import Jd, Taobao

STEP = datetime.timedelta


@pytest.fixture
def conf_patch():
    """临时改 platforms.<name> 配置（None 值表示删掉该项），用例结束原样还原。"""
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


def _public_base(monkeypatch, value: str) -> None:
    """临时钉住 server.public_base_url（媒体可达性的总开关）。"""
    server = get_settings().get("server", {}) or {}
    monkeypatch.setitem(get_settings(), "server", {**server, "public_base_url": value})


REAL_TAOBAO = {"mock": False, "reply_method": "taobao.kf.reply", "app_key": "ak", "app_secret": "sk",
               "access_token": "tok", "session_key": None}
REAL_JD = {"mock": False, "reply_method": "jd.kf.reply", "app_key": "ak", "app_secret": "sk",
           "access_token": "tok", "media_method": "jd.img.send"}


def _boom(url, form):
    raise RuntimeError("网关 502")


def _fail_one_reply(conf_patch, monkeypatch) -> int:
    """真实模式外呼失败一次，返回 failed 行 id（attempts=1、next_retry_at=+15s）。"""
    conf_patch("taobao", **REAL_TAOBAO)
    monkeypatch.setattr(platform_adapters, "http_post_form", _boom)
    with pytest.raises(RuntimeError, match="网关 502"):
        Taobao().send_reply("shop1", "buyer-1", "您好，有货")
    return int(outbox.recent("taobao")[-1]["id"])


def _get(row_id: int) -> OutboxRecord:
    with Session(engine) as session:
        row = session.get(OutboxRecord, row_id)
    assert row is not None, f"外发记录#{row_id} 不在了"
    return row


def _drive_loop(monkeypatch, *, acquired: bool) -> tuple[list, list]:
    """跑一小段后台循环：返回 (acquire 调用记录, retry_once 调用记录)。"""
    from app.services import lease

    leases: list[tuple[str, float]] = []
    worked: list[int] = []

    def fake_acquire(name, ttl_seconds, holder=None):
        leases.append((name, ttl_seconds))
        return acquired

    monkeypatch.setattr(lease, "acquire", fake_acquire)
    monkeypatch.setattr(outbound_retry, "retry_once", lambda **kw: worked.append(1))

    async def drive():
        task = asyncio.create_task(outbound_retry.outbox_retry_loop())
        await asyncio.sleep(0.05)
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    asyncio.run(drive())
    return leases, worked


# ---- record() 写入时的 attempts / next_retry_at 语义 ----

def test_failed_platform_row_enters_retry_queue(conf_patch, monkeypatch):
    row_id = _fail_one_reply(conf_patch, monkeypatch)
    row = _get(row_id)
    assert (row.status, row.attempts) == ("failed", 1)
    assert row.next_retry_at is not None
    assert "网关 502" in (row.last_error or "")
    assert "shop1" in (row.payload_json or "")        # 重投要用 shop_id，列里装不下就进 payload
    now = utcnow()
    assert outbox.retry_due(now=now - STEP(seconds=5)) == []          # 还没到退避窗口
    assert [r["id"] for r in outbox.retry_due(now=now + STEP(seconds=30))] == [row_id]


def test_wecom_mocked_and_skipped_rows_never_enter_the_queue():
    """wecom 卡片没有平台适配器可重投；mocked/skipped 更不是"失败待重试"。"""
    outbox.record(outbox.WECOM_CHANNEL, "textcard", to="kefu01", status="failed", error="企微 45009")
    outbox.record("taobao", "text", to="b1", status="mocked")
    outbox.record("jd", "text", to="b1", status="skipped", error="缺 reply_method")
    assert outbox.retry_due(now=utcnow() + STEP(hours=1)) == []
    # wecom 的失败行 attempts=0：它从没进过队列，不能被算成"重试用尽"的死信
    assert outbox.count_retry_queue() == {"waiting": 0, "dead": 0, "untracked": 1}


def test_retry_deadline_ladder_and_exhaustion():
    base = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
    assert outbox.retry_deadline(0, base) is None
    assert outbox.retry_deadline(1, base) == base + STEP(seconds=15)
    assert outbox.retry_deadline(2, base) == base + STEP(seconds=60)
    assert outbox.retry_deadline(3, base) == base + STEP(seconds=240)
    assert outbox.retry_deadline(4, base) is None          # 第 4 次失败即死信，不再排
    assert outbox.MAX_ATTEMPTS == 4


def test_claim_for_retry_is_atomic(conf_patch, monkeypatch):
    row_id = _fail_one_reply(conf_patch, monkeypatch)
    due = utcnow() + STEP(seconds=60)
    assert outbox.claim_for_retry(row_id, now=due) == 2        # 认领：attempts 1 → 2
    assert outbox.claim_for_retry(row_id, now=due) is None     # 已被认领，第二家拿不到
    # 没到期的行不能被认领：退避窗口是给平台网关恢复用的
    assert outbox.claim_for_retry(row_id, now=utcnow() - STEP(hours=1)) is None


# ---- 重投执行 ----

def test_retry_success_flips_original_row_to_sent_without_new_rows(conf_patch, monkeypatch):
    row_id = _fail_one_reply(conf_patch, monkeypatch)
    rows_before = len(outbox.recent("taobao"))
    monkeypatch.setattr(platform_adapters, "http_post_form", lambda url, form: {"ok": True})

    stats = outbound_retry.retry_once(now=utcnow() + STEP(seconds=30))
    assert stats["resent"] == 1 and stats["dead"] == 0
    row = _get(row_id)
    assert (row.status, row.attempts, row.next_retry_at) == ("sent", 2, None)
    assert len(outbox.recent("taobao")) == rows_before          # 关键：重投不新增外发记录
    assert outbox.count_by_status().get("failed") is None       # 失败被自愈
    assert outbox.count_retry_queue() == {"waiting": 0, "dead": 0, "untracked": 0}


def test_retry_backoff_ladder_ends_in_dead_letter(conf_patch, monkeypatch):
    row_id = _fail_one_reply(conf_patch, monkeypatch)
    monkeypatch.setattr(platform_adapters, "http_post_form", _boom)
    base = utcnow()

    got = outbound_retry.retry_once(now=base + STEP(seconds=16))
    assert got["waiting"] == 1 and _get(row_id).attempts == 2
    scheduled = _get(row_id).next_retry_at
    assert scheduled is not None
    assert abs((scheduled - (base + STEP(seconds=76))).total_seconds()) < 5   # 第二档 60s
    got = outbound_retry.retry_once(now=base + STEP(seconds=77))
    assert got["waiting"] == 1 and _get(row_id).attempts == 3
    got = outbound_retry.retry_once(now=base + STEP(seconds=318))
    assert got == {"due": 1, "not_claimed": 0, "resent": 0, "waiting": 0, "dead": 1, "stopped": 0}

    row = _get(row_id)
    assert (row.status, row.attempts, row.next_retry_at) == ("failed", 4, None)   # 死信
    assert "网关 502" in (row.last_error or "")
    assert outbox.dead_letter_count() == 1
    # 死信不再被取出：自动重试到此为止，等人来管
    assert outbound_retry.retry_once(now=base + STEP(days=1))["due"] == 0


def test_media_row_is_resent_with_its_original_url(conf_patch, monkeypatch):
    _public_base(monkeypatch, "https://cs.example.com")
    conf_patch("jd", **REAL_JD)
    calls: list[dict] = []
    state = {"fail": True}

    def flaky(url, form):
        if state["fail"]:
            raise RuntimeError("网关超时")
        calls.append(form)
        return {"ok": True}

    monkeypatch.setattr(platform_adapters, "http_post_form", flaky)
    with pytest.raises(RuntimeError, match="网关超时"):
        Jd().send_media("shop9", "buyer-9", "https://cs.example.com/api/files/b.png",
                        name="b.png", media_type="image")
    failed = [r for r in outbox.recent("jd") if r["status"] == "failed"]
    assert len(failed) == 1 and failed[0]["url"].endswith("/b.png")

    state["fail"] = False
    stats = outbound_retry.retry_once(now=utcnow() + STEP(seconds=30))
    assert stats["resent"] == 1
    assert calls and calls[-1]["pic_url"].endswith("/b.png")     # 重投用的还是原附件地址
    assert calls[-1]["msg_type"] == "image"


# ---- 重投前提没了：停止排队并说明原因（而不是无限重试）----

def test_retry_stops_when_platform_switched_back_to_mock(conf_patch, monkeypatch):
    row_id = _fail_one_reply(conf_patch, monkeypatch)
    conf_patch("taobao", mock=True)
    stats = outbound_retry.retry_once(now=utcnow() + STEP(seconds=30))
    assert stats["stopped"] == 1 and stats["resent"] == 0
    row = _get(row_id)
    assert row.status == "failed" and row.next_retry_at is None
    assert "mock" in (row.last_error or "")
    assert outbox.dead_letter_count() == 1            # 死信可见：等人来补发


def test_retry_stops_when_credentials_gone(conf_patch, monkeypatch):
    row_id = _fail_one_reply(conf_patch, monkeypatch)
    conf_patch("taobao", app_secret="", access_token="")
    assert outbound_retry.retry_once(now=utcnow() + STEP(seconds=30))["stopped"] == 1
    assert "缺" in (_get(row_id).last_error or "")


def test_retry_stops_for_legacy_media_row_without_url(conf_patch):
    """补参数之前写的媒体行 payload 里没有 url：无法原样重投，说清楚为什么。"""
    conf_patch("pdd", mock=False, reply_method="m", app_key="ak", app_secret="sk",
               access_token="tok", media_method="pdd.media.send")
    with Session(engine) as session:
        legacy = OutboxRecord(channel="pdd", kind="media", to="b2", status="failed", attempts=1,
                              body="[附件] 说明书", error="网关 502",
                              payload_json='{"platform": "pdd", "text": "[附件] 说明书"}',
                              next_retry_at=utcnow() - STEP(seconds=1))
        session.add(legacy)
        session.commit()
        row_id = int(legacy.id)
    assert outbound_retry.retry_once(now=utcnow())["stopped"] == 1
    assert "没记录附件地址" in (_get(row_id).last_error or "")


def test_retry_stops_for_relative_media_link(conf_patch, monkeypatch):
    """没配 server.public_base_url 时重投也是白投：买家点不开，先补配置再人工补发。"""
    _public_base(monkeypatch, "")
    conf_patch("jd", **REAL_JD)
    with Session(engine) as session:
        row = OutboxRecord(channel="jd", kind="media", to="b3", status="failed", attempts=1,
                           body="[附件] 图", error="网关 502",
                           payload_json='{"text": "[附件] 图", "url": "/api/files/a.png", '
                                        '"shop_id": "s3", "media_type": "image"}',
                           next_retry_at=utcnow() - STEP(seconds=1))
        session.add(row)
        session.commit()
        row_id = int(row.id)
    view = outbox.recent("jd")[-1]
    assert "public_base_url" in outbound_retry.stop_reason(view)
    assert outbound_retry.retry_once(now=utcnow())["stopped"] == 1
    assert _get(row_id).status == "failed"


def test_stop_reason_for_unknown_channel():
    assert "适配器" in outbound_retry.stop_reason({"channel": "wecom", "kind": "text", "text": "x"})


# ---- 后台循环：租约选主（与 session_sweeper 同一口径）----

def test_retry_loop_skips_work_without_the_lease(monkeypatch):
    leases, worked = _drive_loop(monkeypatch, acquired=False)
    assert worked == []        # 没抢到约：一行都不重投
    assert leases == [("outbox_retry", 90)]


def test_retry_loop_works_for_the_lease_holder(monkeypatch):
    leases, worked = _drive_loop(monkeypatch, acquired=True)
    assert worked == [1]
    assert leases == [("outbox_retry", 90)]           # TTL 90s，持约者挂掉后一个 TTL 内可接管


def test_retry_interval_comes_from_config(monkeypatch):
    settings = get_settings()
    platforms = copy.deepcopy(dict(settings.get("platforms", {})))
    platforms["retry_interval_seconds"] = 7
    monkeypatch.setitem(settings, "platforms", platforms)
    assert outbound_retry.interval_seconds() == 7
    platforms.pop("retry_interval_seconds")
    monkeypatch.setitem(settings, "platforms", platforms)
    assert outbound_retry.interval_seconds() == outbound_retry.DEFAULT_INTERVAL_SECONDS


# ---- 死信在告警里可见 ----

def test_alerts_report_dead_letter_count(client, staff):
    with Session(engine) as session:
        # 一条还在退避、一条重试用尽（死信）、一条 wecom（从没进队列，不该算死信）
        session.add(OutboxRecord(channel="taobao", to="b1", status="failed", error="网关 502",
                                 attempts=2, created_at=utcnow(),
                                 next_retry_at=utcnow() + STEP(seconds=30)))
        session.add(OutboxRecord(channel="taobao", to="b2", status="failed", error="网关 502",
                                 attempts=outbox.MAX_ATTEMPTS, next_retry_at=None,
                                 created_at=utcnow()))
        session.add(OutboxRecord(channel="wecom", to="kefu", status="failed", error="45009",
                                 created_at=utcnow()))
        session.commit()

    body = client.get("/api/v1/system/alerts", headers=staff["headers"]).json()
    failed = [i for i in body["items"] if i["key"] == "outbox_failed"]
    assert len(failed) == 1 and failed[0]["level"] == "error"
    message = failed[0]["message"]
    assert "3 条外发失败" in message
    assert "1 条已进入死信" in message            # 不是 2 条：wecom 那行没进过重试队列
    assert "1 条仍在退避重试中" in message
    assert "platforms/outbox" in message


def test_alerts_downgrade_when_everything_is_still_retryable(client, staff):
    """都还在退避窗口里时按 warn：还有救的失败不必惊叫。"""
    with Session(engine) as session:
        session.add(OutboxRecord(channel="taobao", to="b1", status="failed", error="网关 502",
                                 attempts=1, created_at=utcnow(),
                                 next_retry_at=utcnow() + STEP(seconds=15)))
        session.commit()
    items = client.get("/api/v1/system/alerts", headers=staff["headers"]).json()["items"]
    failed = [i for i in items if i["key"] == "outbox_failed"]
    assert len(failed) == 1 and failed[0]["level"] == "warn"
    assert "0 条已进入死信" in failed[0]["message"]


def test_readiness_summary_exposes_retry_queue(client, admin):
    r = client.get("/api/v1/platforms/readiness", headers=admin["headers"])
    assert r.status_code == 200
    summary = r.json()["summary"]
    assert summary["outbox_retry"] == {"waiting": 0, "dead": 0, "untracked": 0}
    assert summary["public_base_url_configured"] is False


# ---- 补列声明：老库不重建也能拿到新列 ----

def test_retry_columns_are_declared_and_idempotent():
    from app.db.migrate import ADDITIVE_COLUMNS, apply_column_migrations

    declared = {(t, c) for t, c, _ in ADDITIVE_COLUMNS}
    assert {("outboxrecord", "attempts"), ("outboxrecord", "next_retry_at"),
            ("outboxrecord", "last_error")} <= declared
    apply_column_migrations(engine)          # 已建好的库上再跑：幂等，不抛
    apply_column_migrations(engine)
    with engine.connect() as conn:
        cols = {r[1] for r in conn.exec_driver_sql('PRAGMA table_info("outboxrecord")').fetchall()}
    assert {"attempts", "next_retry_at", "last_error"} <= cols
