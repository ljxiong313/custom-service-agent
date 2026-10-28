"""B3-9 部署形态单测：同源托管前端、配置环境变量覆盖、镜像内脚本可导入。

这些是"只有真部署才会踩"的路径：SPA 深链接、把 404 接口掩盖成 200 的兜底路由、
容器里靠环境变量注入密钥/端口。用临时目录当 dist，不依赖前端是否已经构建。
"""
from __future__ import annotations

import pytest

from app.config import ENV_OVERRIDES, Settings, _apply_env_overrides, _coerce, _set_dotted, get_settings


@pytest.fixture
def web_dist(tmp_path):
    root = tmp_path / "web"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text("<!doctype html><title>CS Agent</title>", encoding="utf-8")
    (root / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("不该被读到", encoding="utf-8")
    return root


@pytest.fixture
def serving(client, monkeypatch, web_dist):
    monkeypatch.setitem(get_settings(), "server", {
        "host": "127.0.0.1", "port": 7861, "serve_frontend": True,
        "frontend_dist": str(web_dist)})
    return web_dist


def test_spa_disabled_returns_404(client):
    assert client.get("/").status_code == 404
    assert client.get("/chat/12").status_code == 404


def test_spa_serves_index_and_assets(client, serving):
    assert client.get("/").text.strip().startswith("<!doctype html>")
    assert client.get("/assets/app.js").text == "console.log(1)"
    # 深链接必须回 index.html，由前端路由解析；不能 404，否则刷新页面就死
    assert client.get("/chat/12").text.strip().startswith("<!doctype html>")


def test_spa_does_not_shadow_api(client, serving):
    assert client.get("/api/v1/nope").status_code == 404
    body = client.get("/api/v1/nope").json()
    assert body["detail"] == "接口不存在"          # 而不是被 index.html 掩盖成 200
    assert client.get("/health").status_code == 200
    assert client.get("/metrics?token=x").status_code in (200, 403)


def test_spa_blocks_path_escape(client, serving):
    r = client.get("/..%2Fsecret.txt")
    assert r.status_code in (200, 404)
    if r.status_code == 200:
        assert "不该被读到" not in r.text          # 命中也只能是 index.html


def test_missing_dist_is_clear_error(client, monkeypatch, tmp_path):
    monkeypatch.setitem(get_settings(), "server", {
        "host": "127.0.0.1", "port": 7861, "serve_frontend": True,
        "frontend_dist": str(tmp_path / "nowhere")})
    r = client.get("/")
    assert r.status_code == 404 and "前端未构建" in r.json()["detail"]


# ---- 配置：环境变量覆盖 ----

def test_coerce_types():
    assert _coerce("7862", "int") == 7862
    assert _coerce("true", "bool") is True
    assert _coerce("off", "bool") is False
    assert _coerce("a.com, b.com", "list") == ["a.com", "b.com"]
    assert _coerce("x", "str") == "x"


def test_set_dotted_creates_nested_dict():
    s = Settings({})
    _set_dotted(s, "server.public_base_url", "https://cs.example.com")
    assert s.get_dotted("server.public_base_url") == "https://cs.example.com"


def test_env_overrides_apply_without_file(monkeypatch):
    s = Settings({"default_model": "dashscope",
                  "multi_models": {"dashscope": {"api_key": ""}},
                  "server": {"port": 7861}})
    monkeypatch.setenv("CS_SERVER_PORT", "8080")
    monkeypatch.setenv("CS_JWT_SECRET", "from-env-secret")
    monkeypatch.setenv("CS_MODEL_API_KEY", "sk-from-env")
    monkeypatch.setenv("CS_CORS_ORIGINS", "https://a.example,https://b.example")
    monkeypatch.setenv("CS_SERVE_FRONTEND", "true")
    _apply_env_overrides(s)
    assert s.get_dotted("server.port") == 8080
    assert s.get_dotted("security.jwt_secret") == "from-env-secret"
    assert s.get_dotted("multi_models.dashscope.api_key") == "sk-from-env"
    assert s.get_dotted("server.cors_origins") == ["https://a.example", "https://b.example"]
    assert s.get_dotted("server.serve_frontend") is True


def test_env_override_follows_default_model(monkeypatch):
    s = Settings({"default_model": "openai",
                  "multi_models": {"openai": {"api_key": ""}}})
    monkeypatch.setenv("CS_MODEL_API_KEY", "sk-oai")
    _apply_env_overrides(s)
    assert s.get_dotted("multi_models.openai.api_key") == "sk-oai"


def test_bad_env_value_keeps_file_value_and_does_not_crash(monkeypatch):
    s = Settings({"server": {"port": 7861}})
    monkeypatch.setenv("CS_SERVER_PORT", "not-a-number")
    _apply_env_overrides(s)
    assert s.get_dotted("server.port") == 7861


def test_empty_env_is_ignored(monkeypatch):
    s = Settings({"security": {"jwt_secret": "keep-me"}})
    monkeypatch.setenv("CS_JWT_SECRET", "   ")
    _apply_env_overrides(s)
    assert s.get_dotted("security.jwt_secret") == "keep-me"


def test_override_table_covers_container_keys():
    for name in ("CS_SERVER_ENV", "CS_JWT_SECRET", "CS_MODEL_API_KEY", "CS_PUBLIC_BASE_URL",
                 "CS_SERVE_FRONTEND", "CS_SERVER_WORKERS"):
        assert name in ENV_OVERRIDES, name
