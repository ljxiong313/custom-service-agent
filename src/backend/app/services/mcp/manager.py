"""MCP 服务管理：配置 CRUD 的运行时侧——连接发现、工具桥接、确认名单。

实现选型：langchain-mcp-adapters（MultiServerMCPClient）负责 stdio/SSE/HTTP 三种
传输的会话管理，我们只做「配置 → 工具列表」的桥接与缓存，不手写协议。
- get_mcp_tools(): 每次对话请求时实时发现（工具集小、秒级，避免缓存失效问题）；
- confirm 名单：require_confirm=True 的服务器下所有工具，对话循环执行前需前端确认。

安全边界（B1-7）：stdio 传输会用 subprocess 拉起配置的 command+args，任何登录用户
能写这条配置就等于能在服务器上执行命令。两道闸：
1) 写接口要 admin（见 api/v1/mcp.py）；
2) command 必须命中 mcp.allowed_commands 白名单，登记时与连接时都校验（白名单收紧后
   旧行也连不上，不用手工清库）。默认只放行常见解释器，绝不含 shell/系统二进制。
"""
from __future__ import annotations

import json
import shlex
import time
from typing import Any

from langchain_core.tools import BaseTool
from loguru import logger
from sqlmodel import Session, col, select

from app.config import get_settings
from app.db.models import McpServer
from app.db.session import engine

# 未配置白名单时的兜底：只允许常见语言运行时，管理员想放开就在 yaml 里追加
DEFAULT_ALLOWED_COMMANDS = ("python", "python3", "py", "node", "npx", "uv", "uvx")


def allowed_commands() -> tuple[str, ...]:
    raw = get_settings().get_dotted("mcp.allowed_commands", None) or []
    names = [str(x).strip().lower() for x in raw if str(x).strip()]
    return tuple(names) if names else DEFAULT_ALLOWED_COMMANDS


def _exe_base(exe: str) -> str:
    """命令的可执行名（去引号、去首尾空白、小写、去掉 Windows 的 .exe 后缀）。"""
    base = exe.strip().strip("\"'").replace("\\", "/").rsplit("/", 1)[-1].lower()
    return base[:-4] if base.endswith(".exe") else base


def _first_token(command: str) -> str:
    """command 里填了带参数的整条命令行时，按第一个 token 判定。

    参数本来有 args 字段可填，但 "python server.py" 这种写法很常见，按整串比对会误拒；
    posix=False 是为了保住 Windows 路径里的反斜杠。
    """
    try:
        parts = shlex.split(command.strip(), posix=False)
    except ValueError:          # 引号不闭合等畸形输入：交给白名单判定拒绝
        return command.strip()
    return parts[0] if parts else ""


def validate_command(transport: str, command: str) -> None:
    """校验 stdio 命令；不合法直接抛 ValueError（接口层转 400），让误配在保存时就暴露。"""
    if transport != "stdio":
        return
    exe = _first_token(command or "")
    if not exe:
        raise ValueError("stdio 传输必须填写 command")
    if _exe_base(exe) not in allowed_commands():
        raise ValueError(
            f"command「{_exe_base(exe)}」不在白名单内：只允许 {', '.join(allowed_commands())}，"
            "需要放行其他程序请在配置 mcp.allowed_commands 中追加")


def _connection_params(row: McpServer) -> dict | None:
    if row.transport == "stdio" and row.command:
        if not _command_allowed(row.command):
            _last_error[row.id or 0] = (
                f"command「{row.command}」不在 mcp.allowed_commands 白名单内，已跳过连接")
            logger.warning("MCP 服务器被跳过: {} → {}", row.name, _last_error[row.id or 0])
            return None
        # 校验放行的可能是"python server.py"这种带参数的写法，拉起子进程时同样要拆成
        # 可执行名 + 参数，否则 subprocess 找不到名叫 "python server.py" 的程序。
        parts = [p.strip("\"'") for p in shlex.split(row.command, posix=False)]
        args = parts[1:] + (shlex.split(row.args) if row.args else [])
        return {"transport": "stdio", "command": parts[0], "args": args}
    if row.transport in ("sse", "streamable_http") and row.url:
        return {"transport": row.transport, "url": row.url}
    return None


def _command_allowed(command: str) -> bool:
    try:
        validate_command("stdio", command)
    except ValueError:
        return False
    return True

# 最近一次发现成功的工具名集合（属于 require_confirm 服务器）
_confirm_tool_names: set[str] = set()
# 服务器状态回写缓存：连接失败不阻断对话，但管理页要能看到原因
_last_error: dict[int, str] = {}
_tools_cache: tuple[float, list[BaseTool]] = (0.0, [])
CACHE_TTL = 30.0  # 工具发现缓存 30s，避免每条消息都重连所有 MCP 服务器


def enabled_servers(session: Session | None = None) -> list[McpServer]:
    stmt = select(McpServer).where(col(McpServer.enabled) == True)  # noqa: E712
    if session is not None:
        return list(session.exec(stmt).all())
    with Session(engine) as own:
        return list(own.exec(stmt).all())


async def get_mcp_tools() -> list[BaseTool]:
    """发现并返回全部启用的 MCP 工具（带缓存）。任何服务器失败只记日志不抛出。"""
    global _tools_cache, _confirm_tool_names

    now = time.monotonic()
    if now - _tools_cache[0] < CACHE_TTL:
        return _tools_cache[1]

    with Session(engine) as session:
        servers = enabled_servers(session)
        confs: dict[str, Any] = {}
        name_map: dict[str, McpServer] = {}
        for srv in servers:
            params = _connection_params(srv)
            if params:
                confs[srv.name] = params
                name_map[srv.name] = srv
        if not confs:
            _tools_cache = (now, [])
            return []

        try:
            from langchain_mcp_adapters.client import MultiServerMCPClient

            client = MultiServerMCPClient(confs)
            tools = []
            per_server: dict[str, list[BaseTool]] = {}
            for srv_name in confs:
                try:
                    srv_tools = await client.get_tools(server_name=srv_name)
                except TypeError:  # 旧版 adapters 无 server_name 参数：全量发现一次
                    srv_tools = await client.get_tools()
                per_server[srv_name] = srv_tools
                tools.extend(srv_tools)
            _confirm_tool_names = {t.name for name, ts in per_server.items()
                                   if name_map[name].require_confirm for t in ts}
            for srv in name_map.values():
                srv.status = "connected"
                srv.tools_json = json.dumps(
                    [{"name": t.name, "description": (t.description or "")[:200]}
                     for t in per_server.get(srv.name, [])],
                    ensure_ascii=False)
            _last_error.clear()
            logger.info("MCP 工具发现成功: {} servers, {} tools", len(confs), len(tools))
        except Exception as e:
            tools = []
            for srv in name_map.values():
                srv.status = "error"
            _last_error.update({s.id or 0: str(e)[:300] for s in name_map.values()})
            logger.warning("MCP 连接失败（不影响对话）: {}", e)
        session.add_all(list(name_map.values()))
        session.commit()

    _tools_cache = (now, tools)
    return tools


def is_confirm_required(tool_name: str) -> bool:
    return tool_name in _confirm_tool_names


def invalidate_cache() -> None:
    global _tools_cache
    _tools_cache = (0.0, [])


def last_error(server_id: int) -> str | None:
    return _last_error.get(server_id)
