"""B3-7 可观测性与计量单测：限流、探针、指标端点、用量/告警接口、备用模型链。

限流中间件在 conftest 的测试配置里是关的（否则同一账号连打几十个写请求会误伤），
这里按需重新打开；打开方式一律用 monkeypatch 改配置字典，不另起配置文件。
"""
from __future__ import annotations

import datetime

from sqlmodel import Session

from app.config import get_settings
from app.db.models import LlmCall, OutboxRecord, utcnow
from app.services import usage
from app.utils import rate_limit

API = "/api/v1"


def _enable_rl(monkeypatch, per_minute: int = 2, burst: int = 2) -> None:
    monkeypatch.setitem(get_settings(), "security", {
        **get_settings().get("security", {}),
        "rate_limit": {"enabled": True,
                       "default": {"per_minute": per_minute, "burst": burst},
                       "rules": []}})
    rate_limit.limiter = rate_limit.Limiter()      # 清掉别的用例留下的桶


# ---- 令牌桶 ----

def test_bucket_allows_burst_then_blocks_and_refills():
    lim = rate_limit.Limiter()
    now = 1000.0
    assert all(lim.allow("s", "k", per_minute=60, burst=3, now=now)[0] for _ in range(3))
    ok, retry = lim.allow("s", "k", per_minute=60, burst=3, now=now)
    assert ok is False and retry > 0
    # 20 秒后按 1 token/s 回填，足够再放行一次
    assert lim.allow("s", "k", per_minute=60, burst=3, now=now + 20)[0] is True


def test_bucket_zero_per_minute_means_unlimited():
    lim = rate_limit.Limiter()
    assert all(lim.allow("s", "k", per_minute=0)[0] for _ in range(200))


def test_prune_drops_idle_buckets():
    lim = rate_limit.Limiter()
    lim.allow("s", "k", per_minute=60, now=0.0)
    assert lim.size() == 1
    assert lim.prune(max_age_seconds=600, now=1000.0) == 1
    assert lim.size() == 0


def test_login_rate_limited_returns_429(client, monkeypatch):
    _enable_rl(monkeypatch, per_minute=2, burst=2)
    body = {"username": "boss", "password": "secret123"}
    codes = [client.post(f"{API}/auth/login", json=body).status_code for _ in range(4)]
    assert codes[:2] == [401, 401]          # 没注册过的账号：前两次是 401 而不是被限流
    assert 429 in codes
    limited = client.post(f"{API}/auth/login", json=body)
    assert limited.status_code == 429 and "Retry-After" in limited.headers


def test_get_requests_are_never_limited(client, monkeypatch, admin):
    _enable_rl(monkeypatch, per_minute=1, burst=1)
    for _ in range(5):
        assert client.get(f"{API}/auth/me", headers=admin["headers"]).status_code == 200


# ---- 探针与指标 ----

def test_health_and_ready(client):
    h = client.get("/health").json()
    assert h["status"] == "ok" and h["uptime_seconds"] >= 0
    r = client.get("/ready")
    assert r.status_code == 200, r.json()
    assert r.json()["checks"]["database"] == "ok"


def test_ready_is_degraded_not_failed_without_model(client, monkeypatch):
    """没配模型只降级不判死：知识库/转人工/工单这些不依赖模型的功能仍然可用。"""
    monkeypatch.setitem(get_settings(), "multi_models", {"dashscope": {"api_key": ""}})
    r = client.get("/ready")
    assert r.status_code == 200 and r.json()["ready"] is True
    assert r.json()["checks"]["model"].startswith("degraded")


def test_ready_fails_on_fatal_security_config(client, monkeypatch):
    monkeypatch.setitem(get_settings(), "server", {**get_settings().get("server", {}),
                                                   "env": "production"})
    monkeypatch.setitem(get_settings(), "security", {**get_settings().get("security", {}),
                                                     "jwt_secret": "dev-secret-change-me"})
    r = client.get("/ready")
    assert r.status_code == 503 and "jwt_secret" in r.json()["checks"]["security"]


def _record_chat_calls(n: int = 2) -> None:
    from app.db.session import engine

    with Session(engine) as session:
        for i in range(n):
            session.add(LlmCall(kind="chat", provider="dashscope", model="qwen-plus",
                                input_tokens=100, output_tokens=20, total_tokens=120,
                                estimated=False, cost_est=0.01,
                                status="ok" if i == 0 else "error"))
        session.commit()


def test_metrics_exposes_llm_and_http_series(client, monkeypatch):
    monkeypatch.setitem(get_settings(), "security", {**get_settings().get("security", {}),
                                                     "metrics_token": ""})
    _record_chat_calls()
    client.get("/health")
    r = client.get("/metrics")
    assert r.status_code == 200
    body = r.text
    assert 'cs_llm_calls_total{kind="chat",provider="dashscope",model="qwen-plus"} 2.0' in body
    token_line = ('cs_llm_tokens_total{kind="chat",provider="dashscope",model="qwen-plus",'
                  'direction="input"} 200.0')
    assert token_line in body
    assert "cs_mcp_pending_confirmations" in body
    assert 'cs_http_requests_total{method="GET",path="/health",class="2xx"}' in body


def test_metrics_requires_token_when_configured(client, monkeypatch):
    monkeypatch.setitem(get_settings(), "security", {**get_settings().get("security", {}),
                                                     "metrics_token": "s3cret-metrics"})
    assert client.get("/metrics").status_code == 403
    assert client.get("/metrics?token=wrong").status_code == 403
    assert client.get("/metrics?token=s3cret-metrics").status_code == 200


def test_metrics_closed_in_production_without_token(client, monkeypatch):
    monkeypatch.setitem(get_settings(), "server", {**get_settings().get("server", {}),
                                                   "env": "production"})
    monkeypatch.setitem(get_settings(), "security", {**get_settings().get("security", {}),
                                                     "metrics_token": ""})
    assert client.get("/metrics").status_code == 403


# ---- 用量与告警接口 ----

def test_usage_endpoint_requires_staff(client, buyer, staff):
    _record_chat_calls()
    assert client.get(f"{API}/system/usage", headers=buyer["headers"]).status_code == 403
    body = client.get(f"{API}/system/usage", headers=staff["headers"]).json()
    assert body["totals"]["calls"] == 2
    assert body["totals"]["total_tokens"] == 240
    assert body["totals"]["not_ok"] == 1
    assert body["pricing_configured"] is False


def test_alerts_surface_failed_outbox_and_security_findings(client, staff, monkeypatch):
    """告警清单要能同时反映"外发没送达"和"生产形态缺配置"。

    这里不 monkeypatch jwt_secret：换密钥会让已签发的测试 token 直接失效（401），
    fatal 级安全项在 test_security_hardening.py 里已单独覆盖。
    """
    from app.db.session import engine

    with Session(engine) as session:
        session.add(OutboxRecord(channel="taobao", to="b1", body="hi", status="failed",
                                 error="网关 502", created_at=utcnow()))
        session.commit()
    monkeypatch.setitem(get_settings(), "server", {**get_settings().get("server", {}),
                                                   "env": "production"})
    resp = client.get(f"{API}/system/alerts", headers=staff["headers"])
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    keys = {i["key"] for i in items}
    assert "outbox_failed" in keys
    assert "public_base_url" in keys
    assert any(i["level"] in ("fatal", "warn") for i in items)


def test_alerts_not_ok_count_excludes_last_24h_window(client, staff):
    """窗口外的历史失败不该进告警：只报最近 24h。"""
    from app.db.session import engine

    with Session(engine) as session:
        session.add(LlmCall(kind="chat", provider="dashscope", model="qwen-plus",
                            status="error", created_at=utcnow() - datetime.timedelta(days=3)))
        session.commit()
    resp = client.get(f"{API}/system/alerts", headers=staff["headers"])
    print("STATUS", resp.status_code, resp.text[:600])
    items = resp.json()["items"]
    assert "llm_error_rate" not in {i["key"] for i in items}


# ---- 备用模型链 ----

def test_chat_chain_order_and_dedupe(monkeypatch):
    from app.core.models.manager import chat_chain

    monkeypatch.setitem(get_settings(), "default_model", "primary")
    monkeypatch.setitem(get_settings(), "llm", {**get_settings().get("llm", {}),
                                                "fallback": ["backup", "primary", ""]})
    assert chat_chain() == ["primary", "backup"]


def test_iter_chat_models_skips_unconfigured(monkeypatch):
    from app.core.models.manager import iter_chat_models

    monkeypatch.setitem(get_settings(), "default_model", "primary")
    monkeypatch.setitem(get_settings(), "llm", {**get_settings().get("llm", {}), "fallback": ["backup"]})
    monkeypatch.setitem(get_settings(), "multi_models", {
        "primary": {"api_key": ""},                       # 没 key → 跳过
        "backup": {"api_key": "k", "base_url": "http://127.0.0.1:9/v1", "chat_model": "m"}})
    names = [n for n, _ in iter_chat_models()]
    assert names == ["backup"]


def test_summary_groups_and_totals(client, db):
    _record_chat_calls()
    s = usage.summary(db, since=utcnow() - datetime.timedelta(hours=1))
    assert len(s["items"]) == 1
    item = s["items"][0]
    assert (item["kind"], item["calls"], item["not_ok"]) == ("chat", 2, 1)
    assert item["input_tokens"] == 200 and item["cost_est"] == 0.02


def test_alert_and_usage_agree_on_failure_count(client, staff):
    """同一个窗口里，/system/alerts 的失败数与 /system/usage 的 not_ok 必须一致。

    这条是因为两边曾经各自算：报表显示"4 次调用 1 次失败"、告警却写"4 次全失败"，
    运营看到互相矛盾的数字就再也不会相信任何一个。
    """
    from app.db.session import engine

    with Session(engine) as session:
        for _ in range(3):
            session.add(LlmCall(kind="chat", provider="dashscope", model="qwen-plus",
                                input_tokens=10, total_tokens=10, status="ok"))
        session.add(LlmCall(kind="chat", provider="dashscope", model="qwen-plus",
                            input_tokens=10, total_tokens=10, status="error"))
        session.commit()

    usage_totals = client.get(f"{API}/system/usage", headers=staff["headers"]).json()["totals"]
    alerts = client.get(f"{API}/system/alerts", headers=staff["headers"]).json()["items"]
    rate = next(a for a in alerts if a["key"] == "llm_error_rate")
    assert usage_totals["calls"] == 4 and usage_totals["not_ok"] == 1
    expected = f"最近 24h 模型外呼 {usage_totals['calls']} 次，其中 {usage_totals['not_ok']} 次失败或被中断"
    assert rate["message"] == expected
