"""B3-6 安全加固单测：签名媒体链接、配置环境变量注入、生产形态硬闸、MCP 参数校验。

口径都写在 app/startup_check.py、app/utils/media_url.py、services/mcp/manager.py 的注释里，
这里只断言"闸真的会落下来"。

conftest 的临时配置本身是**开发形态 + 强 jwt_secret**，所以生产形态的用例一律用
monkeypatch.setitem 临时改配置字典（用例结束自动还原），不另起配置文件。
"""
from __future__ import annotations

import time

import pytest

from app import startup_check
from app.config import Settings, _interpolate, get_settings
from app.services.mcp import manager
from app.utils import media_url

API = "/api/v1"


def _patch(section: str, monkeypatch, **kwargs) -> None:
    merged = {**(get_settings().get(section) or {}), **kwargs}
    monkeypatch.setitem(get_settings(), section, merged)


# ---- 配置：环境变量注入 ----

def test_env_interpolation_whole_value_and_default(monkeypatch):
    monkeypatch.setenv("CS_T_K", "sk-real")
    assert _interpolate({"api_key": "${CS_T_K}"})["api_key"] == "sk-real"
    assert _interpolate({"a": "${CS_MISSING:-fallback}"})["a"] == "fallback"
    assert _interpolate({"a": "${CS_MISSING}"})["a"] == ""
    assert _interpolate({"nested": [{"k": "${CS_T_K}"}]})["nested"][0]["k"] == "sk-real"


def test_env_interpolation_inside_long_string(monkeypatch):
    monkeypatch.setenv("CS_HOST", "cs.example.com")
    assert _interpolate({"url": "https://${CS_HOST}/api"})["url"] == "https://cs.example.com/api"


def test_settings_is_production_helper():
    assert Settings({"server": {"env": "production"}}).is_production() is True
    assert Settings({"server": {"env": "dev"}}).is_production() is False
    assert Settings().is_production() is False


# ---- 媒体签名链接 ----

def test_sign_and_verify_roundtrip():
    signed = media_url.sign_url("/api/files/abc123.png")
    assert signed.startswith("/api/files/abc123.png?exp=")
    fname = media_url.fname_of(signed)
    exp = signed.split("exp=")[1].split("&")[0]
    sig = signed.split("sig=")[1]
    assert media_url.verify(fname, exp, sig)


def test_verify_rejects_tampered_expired_and_traversal():
    signed = media_url.sign_url("/api/files/abc123.png")
    fname, rest = signed[len(media_url.MEDIA_URL_PREFIX):].split("?")
    params = dict(kv.split("=") for kv in rest.split("&"))
    assert media_url.verify(fname, params["exp"], "0" * 32) is False
    assert media_url.verify(fname, str(int(time.time()) - 1), params["sig"]) is False
    assert media_url.verify(fname, None, None) is False
    # 路径穿越与非法文件名一律拒绝（签名看起来"对得上"也进不到比对）
    for bad in ("../cs_agent.db", "..%2Fcs_agent.db", "a/b.png", ".hidden"):
        assert media_url.verify(bad, str(int(time.time()) + 999), "x" * 32) is False


def test_bare_url_strips_signature_for_storage():
    assert media_url.bare_url(media_url.sign_url("/api/files/abc123.png")) == "/api/files/abc123.png"


def test_absolute_url_uses_public_base(monkeypatch):
    _patch("server", monkeypatch, public_base_url="https://cs.example.com")
    got = media_url.absolute_url("/api/files/abc123.png")
    assert got.startswith("https://cs.example.com/api/files/abc123.png?exp=")


def test_media_endpoint_requires_signature(client):
    """无签名/错签名都读不到文件（旧实现是裸静态挂载，拿到 URL 就能读）。"""
    from app.config import BACKEND_ROOT

    d = BACKEND_ROOT / "data" / "uploads" / "media"
    d.mkdir(parents=True, exist_ok=True)
    probe = d / "rbac_probe.png"
    probe.write_bytes(b"\x89PNG\r\n")
    try:
        assert client.get("/api/files/rbac_probe.png").status_code == 403
        assert client.get("/api/files/rbac_probe.png?exp=999999999&sig=deadbeef").status_code == 403
        r = client.get(media_url.sign_url("/api/files/rbac_probe.png"))
        assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    finally:
        probe.unlink(missing_ok=True)


def test_media_rejects_script_extensions(client, admin):
    r = client.post(f"{API}/console/media", headers=admin["headers"],
                    files={"file": ("x.svg", b"<svg onload=alert(1)>", "image/svg+xml")})
    assert r.status_code == 400
    ok = client.post(f"{API}/console/media", headers=admin["headers"],
                     files={"file": ("y.png", b"\x89PNG\r", "image/png")})
    assert ok.status_code == 200 and "sig=" in ok.json()["url"]


# ---- MCP：命令参数与端点校验 ----

def test_validate_command_blocks_inline_code():
    manager.validate_command("stdio", "python", "scripts/demo_mcp_server.py")   # 允许
    for exe, arg in (("python", "-c import os"), ("python3", "-m http.server"),
                     ("node", "-e process.exit()"), ("node", "--eval x")):
        with pytest.raises(ValueError):
            manager.validate_command("stdio", exe, arg)


def test_validate_command_blocks_outside_script_root(monkeypatch):
    _patch("mcp", monkeypatch, allowed_script_roots=["./scripts"])
    with pytest.raises(ValueError):
        manager.validate_command("stdio", "python", "../elsewhere/evil.py")
    manager.validate_command("stdio", "python", "scripts/demo_mcp_server.py")


def test_package_runners_rejected_in_production(monkeypatch):
    _patch("server", monkeypatch, env="production")
    _patch("mcp", monkeypatch, allowed_commands=["python", "npx"])
    with pytest.raises(ValueError):
        manager.validate_command("stdio", "npx", "@acme/mcp-server")
    _patch("mcp", monkeypatch, allow_package_runners=True)
    manager.validate_command("stdio", "npx", "@acme/mcp-server")


def test_validate_url_blocks_private_in_production(monkeypatch):
    _patch("server", monkeypatch, env="production")
    manager.validate_url("stdio", "")                             # stdio 不校验 url
    manager.validate_url("sse", "https://mcp.example.com/sse")
    with pytest.raises(ValueError):
        manager.validate_url("sse", "http://169.254.169.254/latest/meta-data")
    with pytest.raises(ValueError):
        manager.validate_url("streamable_http", "ftp://x/y")


# ---- 启动自检与生产硬闸 ----

def test_startup_check_fatal_on_weak_secret_and_no_verify(monkeypatch):
    _patch("server", monkeypatch, env="production")
    _patch("security", monkeypatch, jwt_secret="dev-secret-change-me")
    levels = {c["key"]: c["level"] for c in startup_check.run_checks()}
    assert levels["jwt_secret"] == "fatal"


def test_startup_check_ok_with_strong_secret(monkeypatch):
    _patch("server", monkeypatch, env="production")
    fatal = startup_check.fatal_findings(startup_check.run_checks())
    assert not any(f["key"] == "jwt_secret" for f in fatal)   # conftest 用的是 48 位测试密钥


def test_startup_check_flags_callback_verify_none(monkeypatch):
    _patch("server", monkeypatch, env="production")
    _patch("platforms", monkeypatch, taobao={
        **get_settings().get_dotted("platforms.taobao", {}), "mock": False, "callback_verify": "none"})
    levels = {c["key"]: c["level"] for c in startup_check.run_checks()}
    assert levels["platform.taobao.callback_verify"] == "fatal"


def test_none_verify_rejects_callback_in_production(client, monkeypatch):
    """免 JWT 的回调 + 不验签 = 任何人可伪造买家消息；生产形态下直接判签失败。"""
    from app.services.platforms import get_adapter

    _patch("server", monkeypatch, env="production")
    _patch("platforms", monkeypatch, taobao={
        **get_settings().get_dotted("platforms.taobao", {}), "mock": False, "callback_verify": "none"})
    assert get_adapter("taobao").verify_signature({"sign": "x"}, b"{}", {}) is False


def test_production_rejects_self_registration_but_admin_can_create(client, admin, monkeypatch):
    _patch("server", monkeypatch, env="production")
    r = client.post(f"{API}/auth/register", json={"username": "intruder", "password": "secret123"})
    assert r.status_code == 403
    ok = client.post(f"{API}/auth/users", headers=admin["headers"],
                     json={"username": "cs09", "password": "longenough", "role": "staff"})
    assert ok.status_code == 200 and ok.json()["role"] == "staff"
