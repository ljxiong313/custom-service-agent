"""启动自检里的附件可达性闸：真实外呼却没配公网地址，生产直接拒绝启动。

口径（app/startup_check.py）：
- 有任一平台 enabled 且 mock=false 且 server.public_base_url 为空 → 缺 base url 是 **fatal**
  （生产拒绝启动；开发形态只 warn，演示环境没有公网域名是预期状态）；
- 平台全是 mock 时维持原口径：生产 warn（避免把"还没接平台"的部署卡死），开发形态不报；
- 配了 base url 后这条彻底消失。

用例只改配置字典（monkeypatch.setitem 自动还原），不重启应用。
"""
from __future__ import annotations

from typing import Any

from app import startup_check
from app.config import get_settings

API = "/api/v1"


def _patch(section: str, monkeypatch, **kwargs: Any) -> None:
    merged = {**(get_settings().get(section) or {}), **kwargs}
    monkeypatch.setitem(get_settings(), section, merged)


def _platform(**over: Any) -> dict:
    """一个"真实外呼已打开"的平台配置（conftest 里默认 mock=true，这里按需翻转）。"""
    base = get_settings().get_dotted("platforms.taobao", {}) or {}
    return {**base, "enabled": True, "mock": False, "callback_verify": "sign", **over}


def _levels(monkeypatch, **server_over) -> dict[str, str]:
    _patch("server", monkeypatch, **server_over)
    return {c["key"]: c["level"] for c in startup_check.run_checks()}


def test_real_platform_without_public_base_is_fatal_in_production(monkeypatch):
    _patch("platforms", monkeypatch, taobao=_platform())
    _patch("server", monkeypatch, public_base_url="")
    levels = _levels(monkeypatch, env="production")
    assert levels["public_base_url"] == "fatal"
    keys = {f["key"] for f in startup_check.fatal_findings(startup_check.run_checks())}
    assert "public_base_url" in keys
    message = next(c["message"] for c in startup_check.run_checks() if c["key"] == "public_base_url")
    assert "点不开" in message and "taobao" in message


def test_real_platform_without_public_base_only_warns_in_dev(monkeypatch):
    _patch("platforms", monkeypatch, taobao=_platform())
    _patch("server", monkeypatch, public_base_url="")
    levels = _levels(monkeypatch, env="dev")
    assert levels["public_base_url"] == "warn"
    # 开发形态永远没有 fatal 项：演示环境不该被这些闸卡住启动
    assert startup_check.fatal_findings(startup_check.run_checks()) == []


def test_configured_public_base_clears_the_finding(monkeypatch):
    _patch("platforms", monkeypatch, taobao=_platform())
    _patch("server", monkeypatch, env="production", public_base_url="https://cs.example.com")
    assert "public_base_url" not in _levels(monkeypatch)


def test_mock_only_platforms_keep_the_old_warning_in_production(monkeypatch):
    """平台还没切真实外呼时维持 warn：不配 base url 的演示/试运行部署不能被卡死启动。"""
    _patch("server", monkeypatch, env="production", public_base_url="")
    checks = startup_check.run_checks()
    levels = {c["key"]: c["level"] for c in checks}
    assert levels["public_base_url"] == "warn"
    assert all(f["key"] != "public_base_url" for f in startup_check.fatal_findings(checks))


def test_disabled_platform_does_not_count_as_real_outbound(monkeypatch):
    _patch("platforms", monkeypatch, taobao=_platform(enabled=False))
    _patch("server", monkeypatch, env="production", public_base_url="")
    levels = _levels(monkeypatch, env="production")
    assert levels["public_base_url"] == "warn"           # 关掉的渠道不触发 fatal


def test_ready_endpoint_reports_the_missing_public_base(client, monkeypatch):
    """/ready 要把这个致命项算进"不可服务"：真实外呼发出去的是点不开的链接。"""
    _patch("platforms", monkeypatch, taobao=_platform())
    _patch("server", monkeypatch, env="production", public_base_url="")
    r = client.get("/ready")
    assert r.status_code == 503
    assert "public_base_url" in r.json()["checks"]["security"]

    _patch("server", monkeypatch, env="production", public_base_url="https://cs.example.com")
    r2 = client.get("/ready")
    assert r2.status_code == 200, r2.text
    assert "public_base_url" not in str(r2.json()["checks"].get("security", ""))


def test_alerts_carry_the_finding_as_fatal(client, staff, monkeypatch):
    _patch("platforms", monkeypatch, taobao=_platform())
    _patch("server", monkeypatch, env="production", public_base_url="")
    items = client.get(f"{API}/system/alerts", headers=staff["headers"]).json()["items"]
    hit = [i for i in items if i["key"] == "public_base_url"]
    assert len(hit) == 1 and hit[0]["level"] == "fatal"
