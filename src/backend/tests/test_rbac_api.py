"""RBAC 与鉴权单测：JWT 校验、角色门禁、角色变更保护、归属过滤。

口径（B3-6 收紧后）：
- 会改动全局语料、拉起本机进程或他人账号的写操作限 admin；
- 运营台的跨用户读/代答限 staff 及以上（此前对所有登录用户开放，等于任何买家账号
  都能读到别人的会话与工具入参）；
- 普通用户（role=user）只做"自己会话"的事：列表、消息、代答、工单、确认卡片都按归属过滤。
"""
from __future__ import annotations

import time

import jwt as pyjwt
import pytest
from sqlmodel import Session, select

from app.config import get_settings
from app.db.models import ToolConfirmRequest, User
from app.db.session import engine

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
    """admin 保护链：提权不再被误拦（B3-6 修的 bug）、降级他人要留一个 admin、不能自降。

    早先的实现把保护写成 `if body.role != "admin"`，导致给普通账号提权成 staff 也被
    409 拦下——客服根本招不进来。
    """
    assert client.patch(f"{API}/auth/users/{staff['user']['id']}/role",
                        headers=admin["headers"], json={"role": "staff"}).status_code == 200
    other = client.post(f"{API}/auth/users", headers=admin["headers"],
                        json={"username": "cs05", "password": "longenough", "role": "admin"}).json()
    # 两个 admin 时，降级其中一个可以（另一个还在）
    assert client.patch(f"{API}/auth/users/{other['id']}/role", headers=admin["headers"],
                        json={"role": "user"}).status_code == 200
    # 只剩 boss 一个 admin：自己降自己先被"不能降级自己"拦住，不会出现无人可用的状态
    r = client.patch(f"{API}/auth/users/{admin['user']['id']}/role", headers=admin["headers"],
                     json={"role": "user"})
    assert r.status_code == 409 and "自己" in r.json()["detail"]


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


# ---- 归属过滤（B3-6 新增）----

def test_buyer_cannot_read_operator_surfaces(client, buyer):
    """普通用户不再是"事实客服"：运营看板、工单看板、外发箱一律 403。"""
    for path in ("/handoff/stats", "/handoff/outbox", "/platforms/outbox",
                 "/platforms/status", "/platforms/readiness"):
        r = client.get(f"{API}{path}", headers=buyer["headers"])
        assert r.status_code == 403, (path, r.status_code)


def test_buyer_only_sees_own_conversations(client, buyer, staff):
    mine = client.post(f"{API}/conversations", headers=buyer["headers"],
                       json={"title": "我的会话"}).json()
    theirs = client.post(f"{API}/conversations", headers=staff["headers"],
                         json={"title": "客服的会话"}).json()

    listed = client.get(f"{API}/console/conversations", headers=buyer["headers"]).json()
    assert {c["id"] for c in listed} == {mine["id"]}

    # 读他人会话：按"不存在"处理，不给探测他人会话是否存在的信号
    assert client.get(f"{API}/console/conversations/{theirs['id']}/messages",
                      headers=buyer["headers"]).status_code == 404
    assert client.post(f"{API}/console/conversations/{theirs['id']}/reply",
                       headers=buyer["headers"], json={"text": "偷答"}).status_code == 404
    # 自己的会话仍然可读可回
    assert client.get(f"{API}/console/conversations/{mine['id']}/messages",
                      headers=buyer["headers"]).status_code == 200


def test_buyer_pending_confirms_are_scoped(client, buyer, admin, staff):
    """确认卡片带完整工具入参，普通用户只能看到自己会话排队的那几张。

    B3-8 起注册表落库且有外键（conversation_id→conversation、user_id→user，
    SQLite foreign_keys=ON），不能再 monkeypatch 伪造条目——两张卡都用
    create_request 建真实记录：一张挂 buyer 自己的会话，一张挂客服的会话。
    """
    from app.services.mcp import confirm

    mine = client.post(f"{API}/conversations", headers=buyer["headers"],
                       json={"title": "待确认"}).json()
    theirs = client.post(f"{API}/conversations", headers=staff["headers"],
                         json={"title": "客服的待确认"}).json()
    rid_mine = confirm.create_request(mine["id"], buyer["user"]["id"],
                                      "order_query", {"order": "A1"})
    rid_theirs = confirm.create_request(theirs["id"], staff["user"]["id"],
                                        "order_query", {"order": "B2"})

    items = client.get(f"{API}/mcp/pending", headers=buyer["headers"]).json()["items"]
    assert [i["request_id"] for i in items] == [rid_mine]
    assert items[0]["args"] == {"order": "A1"}
    # staff/admin 是全量视角：两张卡都看得到
    for who in (admin, staff):
        seen = client.get(f"{API}/mcp/pending", headers=who["headers"]).json()["items"]
        assert {i["request_id"] for i in seen} == {rid_mine, rid_theirs}
    # 替别人点确认：按"已过期"处理
    assert client.post(f"{API}/mcp/confirm", headers=buyer["headers"],
                       json={"request_id": rid_theirs, "approved": True}).status_code == 410
    # 越权点击不能把别人的卡片裁决掉
    with Session(engine) as s:
        row = s.exec(select(ToolConfirmRequest).where(
            ToolConfirmRequest.request_id == rid_theirs)).first()
        assert row is not None and row.status == "pending"


def test_staff_sees_all_conversations(client, staff, buyer):
    mine = client.post(f"{API}/conversations", headers=buyer["headers"],
                       json={"title": "买家会话"}).json()
    ids = {c["id"] for c in client.get(f"{API}/console/conversations",
                                       headers=staff["headers"]).json()}
    assert mine["id"] in ids
