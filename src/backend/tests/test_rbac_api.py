"""B1-7 RBAC 与鉴权单测：JWT 校验、角色门禁、角色变更保护。

口径（用户确认的设计决策）：会改动全局语料、拉起本机进程或他人账号的写操作限 admin；
运营台跨用户读/代答保留给任意登录用户，所以这里也断言"普通坐席仍能干活"。
"""
from __future__ import annotations

import time

import jwt as pyjwt
import pytest

from app.config import get_settings
from app.db.models import User

SECRET = get_settings().get_dotted("security.jwt_secret")
API = "/api/v1"
MCP_SERVER = {"name": "kb-order", "transport": "stdio", "command": "python",
              "args": "scripts/demo_mcp_server.py", "enabled": True, "require_confirm": True}


# ---- JWT ----

def test_me_roundtrip(client, admin):
    r = client.get(f"{API}/auth/me", headers=admin["headers"])
    assert r.status_code == 200 and r.json()["username"] == "boss" and r.json()["role"] == "admin"


def test_missing_and_bad_token(client):
    assert client.get(f"{API}/auth/me").status_code == 401
    assert client.get(f"{API}/auth/me", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.get(f"{API}/auth/me", headers={"Authorization": "Basic abc"}).status_code == 401


def test_tampered_token_rejected(client, admin):
    tampered = admin["token"][:-4] + ("AAAA" if not admin["token"].endswith("AAAA") else "BBBB")
    assert client.get(f"{API}/auth/me", headers={"Authorization": f"Bearer {tampered}"}).status_code == 401


def test_expired_token_rejected(client, admin):
    """过期即失效：不能靠"7 天内都能用"绕过重新登录。"""
    payload = pyjwt.decode(admin["token"], SECRET, algorithms=["HS256"])
    expired = pyjwt.encode({**payload, "exp": int(time.time()) - 1},
                           SECRET, algorithm="HS256")
    assert client.get(f"{API}/auth/me", headers={"Authorization": f"Bearer {expired}"}).status_code == 401


def test_token_carries_only_subject(client, admin):
    """role 不入 token：改角色要立刻生效，否则要等 token 自然过期（默认 7 天）。"""
    assert set(pyjwt.decode(admin["token"], SECRET, algorithms=["HS256"])) == {"sub", "exp"}


def test_login_wrong_password(client, admin):
    r = client.post(f"{API}/auth/login", json={"username": "boss", "password": "wrongpass"})
    assert r.status_code == 401


# ---- 角色门禁矩阵 ----

@pytest.mark.parametrize("method,path,payload", [
    ("patch", "/knowledge/files/1", {"enabled": False}),
    ("delete", "/knowledge/files/1", None),
    ("post", "/knowledge/files/1/reingest", None),
    ("post", "/knowledge/files/1/reindex", None),
    ("post", "/knowledge/jobs/1/retry", None),
    ("post", "/mcp/servers", MCP_SERVER),
    ("patch", "/mcp/servers/1", MCP_SERVER),
    ("delete", "/mcp/servers/1", None),
    ("post", "/mcp/servers/1/test", None),
    ("get", "/auth/users", None),
    ("patch", "/auth/users/1/role", {"role": "admin"}),
])
def test_staff_is_403_on_admin_writes(client, staff, admin, method, path, payload):
    verb = getattr(client, method)
    r = verb(f"{API}{path}", headers=staff["headers"],
             **({"json": payload} if payload is not None else {}))
    assert r.status_code == 403, (path, r.status_code, r.text[:120])
    assert "管理员" in r.json()["detail"]


def test_staff_cannot_upload_knowledge(client, staff):
    r = client.post(f"{API}/knowledge/files", headers=staff["headers"],
                    files={"file": ("x.txt", b"hello", "text/plain")})
    assert r.status_code == 403


@pytest.mark.parametrize("path", [
    "/knowledge/files", "/knowledge/jobs", "/knowledge/products",
    "/mcp/servers", "/mcp/pending", "/platforms/status", "/platforms/readiness",
    "/platforms/outbox", "/system/status", "/memory/facts", "/memory/summaries",
    "/handoff/tickets", "/handoff/stats", "/handoff/outbox", "/console/conversations",
])
def test_staff_can_read_everything_ops_needs(client, staff, path):
    assert client.get(f"{API}{path}", headers=staff["headers"]).status_code == 200, path


def test_staff_still_owns_and_answers_conversations(client, staff):
    """运营台代答/建会话不受 role 限制——这是产品决策，不是漏配。"""
    conv = client.post(f"{API}/conversations", headers=staff["headers"],
                       json={"title": "坐席会话"}).json()
    assert client.patch(f"{API}/conversations/{conv['id']}", headers=staff["headers"],
                        json={"title": "改名"}).status_code == 200
    assert client.post(f"{API}/console/conversations/{conv['id']}/reply",
                       headers=staff["headers"], json={"text": "您好"}).status_code == 200
    assert client.get(f"{API}/conversations/{conv['id']}/messages",
                      headers=staff["headers"]).status_code == 200
    assert client.delete(f"{API}/conversations/{conv['id']}", headers=staff["headers"]).status_code == 204


def test_search_is_open_to_staff(client, staff):
    assert client.post(f"{API}/knowledge/search", headers=staff["headers"],
                       json={"query": "面霜", "top_k": 3}).status_code == 200


def test_anonymous_rejected_everywhere(client):
    for path in ("/knowledge/files", "/mcp/servers", "/console/conversations", "/system/status"):
        assert client.get(f"{API}{path}").status_code == 401, path


# ---- 角色变更保护 ----

def test_promote_and_demote(client, admin, db):
    target = db.get(User, int(admin["user"]["id"]))
    assert target.role == "admin"
    other = client.post(f"{API}/auth/register", json={"username": "cs02", "password": "secret123"}).json()
    r = client.patch(f"{API}/auth/users/{other['id']}/role", headers=admin["headers"],
                     json={"role": "admin"})
    assert r.status_code == 200 and r.json()["role"] == "admin"
    # 有两个 admin 之后，降级其中一个才允许
    assert client.patch(f"{API}/auth/users/{other['id']}/role", headers=admin["headers"],
                        json={"role": "user"}).json()["role"] == "user"


def test_cannot_demote_self(client, admin):
    r = client.patch(f"{API}/auth/users/{admin['user']['id']}/role", headers=admin["headers"],
                     json={"role": "user"})
    assert r.status_code == 409 and "自己" in r.json()["detail"]


def test_last_admin_cannot_be_removed(client, admin, staff):
    """只有一个 admin 时，任何降级请求都拦住——否则没人能再改角色，等于自锁。"""
    r = client.patch(f"{API}/auth/users/{staff['user']['id']}/role", headers=admin["headers"],
                     json={"role": "user"})
    assert r.status_code == 409 and "管理员" in r.json()["detail"]


def test_role_value_is_validated(client, admin, db):
    assert client.patch(f"{API}/auth/users/{admin['user']['id']}/role", headers=admin["headers"],
                        json={"role": "root"}).status_code == 422
    assert client.patch(f"{API}/auth/users/999999/role", headers=admin["headers"],
                        json={"role": "user"}).status_code == 404


def test_second_user_is_not_admin(client, admin):
    """首个账号 admin、其余 user：全新库里没人能配置 MCP 的局面不能出现，但也不能人人是 admin。"""
    body = client.post(f"{API}/auth/register",
                       json={"username": "cs04", "password": "secret123"}).json()
    assert body["role"] == "user"
    login = client.post(f"{API}/auth/login",
                        json={"username": "cs04", "password": "secret123"}).json()
    me = client.get(f"{API}/auth/me", headers={"Authorization": f"Bearer {login['access_token']}"})
    assert me.json()["role"] == "user"
