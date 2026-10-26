"""MCP 门禁单测：stdio 命令白名单 + 工具确认注册表（B1-7 / B2-3）。

不会真的拉起子进程：只验"登记时能不能写进来"和"连接前会不会被跳过"这两道闸。
"""
from __future__ import annotations

import asyncio
import time

import pytest
from sqlmodel import Session, select

from app.config import get_settings
from app.db.models import McpServer
from app.db.session import engine
from app.services.mcp import confirm, manager

API = "/api/v1"


@pytest.fixture(autouse=True)
def _clean_registry():
    """确认注册表是进程内单例：用例之间必须干净，否则 pending_count 断言互相污染。"""
    confirm._pending.clear()
    yield
    confirm._pending.clear()


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
    """runner 挂在 future 上等确认：前端点"允许"后必须立刻拿到 True 并继续调用工具。"""
    async def scenario():
        rid = confirm.create_request(1, 2, "order_query", {"order_id": "A1"})
        assert confirm.pending_count() == 1
        task = asyncio.create_task(confirm.wait_decision(rid, timeout=5))
        await asyncio.sleep(0)                      # 让 waiter 真的挂到 future 上
        assert confirm.resolve(rid, True) is True
        return await task

    assert asyncio.run(scenario()) is True
    assert confirm.pending_count() == 0


def test_confirm_deny_and_unknown_request():
    async def scenario():
        rid = confirm.create_request(1, 2, "refund_apply", {})
        task = asyncio.create_task(confirm.wait_decision(rid, timeout=5))
        await asyncio.sleep(0)
        assert confirm.resolve(rid, False) is True
        assert await task is False
        assert confirm.pending_count() == 0
        assert confirm.resolve("不存在的id", True) is False      # 已消失的卡片不能被"补确认"

    asyncio.run(scenario())


def test_prune_expires_silently_waiting_requests():
    """没人点确认时必须自动按拒绝收场，否则 future 和注册表会一直挂着。"""
    async def scenario():
        rid = confirm.create_request(7, 1, "order_query", {})
        task = asyncio.create_task(confirm.wait_decision(rid, timeout=30))
        await asyncio.sleep(0)
        assert confirm.prune(max_age=-1) == 1                  # 视为已过期
        assert await task is False                             # 超时前就被置为拒绝
        assert confirm.pending_count() == 0

    asyncio.run(scenario())


def test_pending_list_carries_conversation_and_args():
    """运营台要能替没有 SSE 的渠道点确认，所以列表必须带会话与参数。"""
    async def scenario():
        rid = confirm.create_request(42, 5, "order_query", {"order_id": "X9"})
        row = next(i for i in confirm.pending_list() if i["request_id"] == rid)
        assert row["conversation_id"] == 42 and row["tool_name"] == "order_query"
        assert row["args"] == {"order_id": "X9"} and row["created_at"] <= time.time()
        confirm.prune(max_age=-1)

    asyncio.run(scenario())


def test_pending_api_and_confirm_endpoint(client, admin, monkeypatch):
    """接口层：/mcp/pending 透出卡片，/mcp/confirm 一张卡片只能点一次，之后再点 410。"""
    loop = asyncio.new_event_loop()
    try:
        entry = confirm._Pending(request_id="abc123", conversation_id=9, user_id=1,
                                 tool_name="order_query", args={"id": 1},
                                 created_at=time.time(), future=loop.create_future())
        monkeypatch.setitem(confirm._pending, "abc123", entry)

        body = client.get(f"{API}/mcp/pending", headers=admin["headers"]).json()
        assert [i["request_id"] for i in body["items"]] == ["abc123"]
        assert body["items"][0]["tool_name"] == "order_query"

        assert client.post(f"{API}/mcp/confirm", headers=admin["headers"],
                           json={"request_id": "abc123", "approved": True}).status_code == 200
        assert entry.future.result() is True            # runner 那边等的就是这个结果
        gone = client.post(f"{API}/mcp/confirm", headers=admin["headers"],
                           json={"request_id": "abc123", "approved": False})
        assert gone.status_code == 410 and "过期" in gone.json()["detail"]
        assert client.get(f"{API}/mcp/pending", headers=admin["headers"]).json()["items"] == []
    finally:
        loop.close()


def test_system_status_counts_pending(client, admin, monkeypatch):
    entry = confirm._Pending(request_id="zzz", conversation_id=9, user_id=1,
                             tool_name="order_query", created_at=time.time())
    monkeypatch.setitem(confirm._pending, "zzz", entry)
    body = client.get(f"{API}/system/status", headers=admin["headers"]).json()
    assert body["mcp_pending_confirm"] == 1
