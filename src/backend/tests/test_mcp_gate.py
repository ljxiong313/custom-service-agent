"""MCP 门禁单测：stdio 命令白名单 + 工具确认注册表（B1-7 / B2-3）。

不会真的拉起子进程：只验"登记时能不能写进来"和"连接前会不会被跳过"这两道闸。
"""
from __future__ import annotations

import asyncio
import time

import pytest
from sqlmodel import Session, select

from app.config import get_settings
from app.db.models import Conversation, McpServer, ToolConfirmRequest, User
from app.db.session import engine
from app.services.mcp import confirm, manager

API = "/api/v1"


def _seed_target(username: str) -> tuple[int, int]:
    """造一条真实"用户+会话"，返回 (conversation_id, user_id)。

    B3-8 起确认请求落库：toolconfirmrequest 对 conversation.id / user.id 有外键，
    且 SQLite 连接开了 PRAGMA foreign_keys=ON，create_request 只能用真实主键，
    随便编的 id 会直接 IntegrityError。conftest 的 _clean_tables 每用例清库，
    所以每用例自己种一次即可，不再需要清理进程内注册表（它已不存在）。
    """
    with Session(engine) as s:
        user = User(username=username, hashed_password="x", role="user")
        s.add(user)
        s.commit()
        s.refresh(user)
        conv = Conversation(user_id=user.id, title="确认测试会话")
        s.add(conv)
        s.commit()
        s.refresh(conv)
        return conv.id, user.id


# ---- 白名单 ----

@pytest.mark.parametrize("exe,expect", [
    ("python", "python"),
    ("C:\\Windows\\System32\\cmd.exe", "cmd"),
    ("/usr/local/bin/Python3", "python3"),
    ("  node  ", "node"),
    ("./evil.sh", "evil.sh"),
])
def test_exe_base_normalizes(exe, expect):
    assert manager._exe_base(exe) == expect


def test_allowed_commands_come_from_config():
    assert manager.allowed_commands() == ("python", "python3", "py", "node")


def test_allowed_commands_falls_back_to_default(monkeypatch):
    """配置写成空列表/空串也不能等于"全部放行"，退回内置白名单。"""
    settings = get_settings()
    original = settings.get("mcp")
    settings["mcp"] = {"allowed_commands": []}
    try:
        assert manager.allowed_commands() == manager.DEFAULT_ALLOWED_COMMANDS
    finally:
        settings["mcp"] = original


def test_validate_command_blocks_shell_but_allows_runtimes():
    manager.validate_command("stdio", "python server.py")        # 允许
    manager.validate_command("sse", "")                          # 非 stdio 不校验 command
    with pytest.raises(ValueError, match="白名单"):
        manager.validate_command("stdio", "powershell -c Get-Process")
    with pytest.raises(ValueError, match="白名单"):
        manager.validate_command("stdio", "C:\\Windows\\System32\\cmd.exe /c dir")
    with pytest.raises(ValueError, match="command"):
        manager.validate_command("stdio", "   ")


def test_connection_params_skip_banned_command():
    """白名单收紧后，已登记的历史服务器必须连不上，而不是等到对话时才炸。"""
    row = McpServer(name="bad", transport="stdio", command="powershell", args="", url="",
                    enabled=True, require_confirm=True)
    with Session(engine) as s:
        s.add(row)
        s.commit()
        s.refresh(row)
        params = manager._connection_params(row)
        assert params is None
        assert "白名单" in (manager.last_error(row.id or 0) or "")

    ok = McpServer(name="good", transport="stdio", command="node",
                   args="--from pkg mcp-server", url="", enabled=True, require_confirm=True)
    with Session(engine) as s:
        s.add(ok)
        s.commit()
        s.refresh(ok)
        assert manager._connection_params(ok) == {
            "transport": "stdio", "command": "node", "args": ["--from", "pkg", "mcp-server"]}
        # command 里顺手带上参数（很常见的写法）：校验放行后执行侧也要拆对
        sloppy = McpServer(name="sloppy", transport="stdio", command="python server.py",
                           args="--flag", url="", enabled=True, require_confirm=True)
        s.add(sloppy)
        s.commit()
        s.refresh(sloppy)
        assert manager._connection_params(sloppy) == {
            "transport": "stdio", "command": "python", "args": ["server.py", "--flag"]}
        web = McpServer(name="web", transport="sse", command="", url="http://127.0.0.1:9/sse",
                        enabled=True, require_confirm=False)
        s.add(web)
        s.commit()
        s.refresh(web)
        assert manager._connection_params(web)["url"] == "http://127.0.0.1:9/sse"


def test_server_api_rejects_banned_command_and_persists_nothing(client, admin):
    r = client.post(f"{API}/mcp/servers", headers=admin["headers"],
                    json={"name": "evil", "transport": "stdio", "command": "powershell"})
    assert r.status_code == 400 and "白名单" in r.json()["detail"]
    assert "powershell" in r.json()["detail"]
    with Session(engine) as s:
        assert s.exec(select(McpServer).where(McpServer.name == "evil")).first() is None


def test_server_api_allows_listed_command_and_patch_revalidates(client, admin):
    r = client.post(f"{API}/mcp/servers", headers=admin["headers"],
                    json={"name": "ok", "transport": "stdio", "command": "python",
                          "args": "mcp_demo.py"})
    assert r.status_code == 200
    sid = r.json()["id"]

    bad = client.patch(f"{API}/mcp/servers/{sid}", headers=admin["headers"],
                       json={"name": "ok", "transport": "stdio", "command": "sh"})
    assert bad.status_code == 400
    with Session(engine) as s:
        assert s.get(McpServer, sid).command == "python"      # 校验失败不能把旧值改掉


def test_duplicate_name_conflicts(client, admin):
    body = {"name": "dup", "transport": "stdio", "command": "python"}
    assert client.post(f"{API}/mcp/servers", headers=admin["headers"], json=body).status_code == 200
    r = client.post(f"{API}/mcp/servers", headers=admin["headers"], json=body)
    assert r.status_code == 409


# ---- 工具确认注册表 ----

def test_confirm_roundtrip_resolves_waiter():
    """runner 轮询库等确认：点"允许"把行置为 approved 后，等待方必须拿到 True 并继续调用工具。

    轮询语义（POLL_SECONDS=1）：wait_decision 第一轮读到 pending 后睡 1 秒，
    期间 resolve 写库，醒来第二轮看到终态——所以这里会比旧 future 版慢约 1 秒，
    但测的仍是"等待方能拿到裁决"这件事，断言不放宽。
    """
    conv_id, user_id = _seed_target("waiter1")

    async def scenario():
        rid = confirm.create_request(conv_id, user_id, "order_query", {"order_id": "A1"})
        assert confirm.pending_count() == 1
        task = asyncio.create_task(confirm.wait_decision(rid, timeout=5))
        await asyncio.sleep(0)                      # 让 waiter 真的进入轮询（第一轮已读到 pending）
        assert confirm.resolve(rid, True) is True
        return await task

    assert asyncio.run(scenario()) is True
    assert confirm.pending_count() == 0


def test_confirm_deny_and_unknown_request():
    conv_id, user_id = _seed_target("waiter2")

    async def scenario():
        rid = confirm.create_request(conv_id, user_id, "refund_apply", {})
        task = asyncio.create_task(confirm.wait_decision(rid, timeout=5))
        await asyncio.sleep(0)
        assert confirm.resolve(rid, False) is True
        assert await task is False
        assert confirm.pending_count() == 0
        assert confirm.resolve("不存在的id", True) is False      # 已消失的卡片不能被"补确认"

    asyncio.run(scenario())


def test_prune_expires_silently_waiting_requests():
    """没人点确认时必须自动按拒绝收场：prune 把 pending 置 expired，等待方下一轮就看到终态。"""
    conv_id, user_id = _seed_target("waiter3")

    async def scenario():
        rid = confirm.create_request(conv_id, user_id, "order_query", {})
        task = asyncio.create_task(confirm.wait_decision(rid, timeout=30))
        await asyncio.sleep(0)
        assert confirm.prune(max_age=-1) == 1                  # 视为已过期
        assert await task is False                             # 超时前就被置为拒绝
        assert confirm.pending_count() == 0
        with Session(engine) as s:                             # 终态留在库里可审计，不被改写
            row = s.exec(select(ToolConfirmRequest).where(
                ToolConfirmRequest.request_id == rid)).first()
            assert row is not None and row.status == "expired"

    asyncio.run(scenario())


def test_pending_list_carries_conversation_and_args():
    """运营台要能替没有 SSE 的渠道点确认，所以列表必须带会话与参数。"""
    conv_id, user_id = _seed_target("waiter4")

    async def scenario():
        rid = confirm.create_request(conv_id, user_id, "order_query", {"order_id": "X9"})
        row = next(i for i in confirm.pending_list() if i["request_id"] == rid)
        assert row["conversation_id"] == conv_id and row["tool_name"] == "order_query"
        assert row["args"] == {"order_id": "X9"} and row["created_at"] <= time.time()
        confirm.prune(max_age=-1)

    asyncio.run(scenario())


def test_pending_api_and_confirm_endpoint(client, admin):
    """接口层：/mcp/pending 透出卡片，/mcp/confirm 一张卡片只能点一次，之后再点 410。

    旧版往进程内 _pending 塞 _Pending 假条目并断言 entry.future.result() is True；
    注册表已删，改为走真实链路（建会话 → create_request 落库），
    "future 被置 True"改测成"库行状态变 approved"——这是落库后的等价断言，
    并额外断言 decided_by 记录了操作者 id（新实现带来的审计能力）。
    """
    conv_id = client.post(f"{API}/conversations", headers=admin["headers"],
                          json={"title": "待确认"}).json()["id"]
    rid = confirm.create_request(conv_id, admin["user"]["id"], "order_query", {"id": 1})

    body = client.get(f"{API}/mcp/pending", headers=admin["headers"]).json()
    assert [i["request_id"] for i in body["items"]] == [rid]
    assert body["items"][0]["tool_name"] == "order_query"

    assert client.post(f"{API}/mcp/confirm", headers=admin["headers"],
                       json={"request_id": rid, "approved": True}).status_code == 200
    with Session(engine) as s:
        row = s.exec(select(ToolConfirmRequest).where(
            ToolConfirmRequest.request_id == rid)).first()
        assert row is not None
        assert row.status == "approved"                  # runner 等的就是这个裁决结果
        assert row.decided_by == admin["user"]["id"]     # 审计：谁批的必须留名
        assert row.decided_at is not None
    gone = client.post(f"{API}/mcp/confirm", headers=admin["headers"],
                       json={"request_id": rid, "approved": False})
    assert gone.status_code == 410 and "过期" in gone.json()["detail"]
    assert client.get(f"{API}/mcp/pending", headers=admin["headers"]).json()["items"] == []


def test_system_status_counts_pending(client, admin):
    conv_id = client.post(f"{API}/conversations", headers=admin["headers"],
                          json={"title": "待确认"}).json()["id"]
    confirm.create_request(conv_id, admin["user"]["id"], "order_query", {})
    body = client.get(f"{API}/system/status", headers=admin["headers"]).json()
    assert body["mcp_pending_confirm"] == 1
