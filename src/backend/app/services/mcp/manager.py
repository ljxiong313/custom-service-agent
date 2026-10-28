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

import ipaddress
import json
import shlex
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from langchain_core.tools import BaseTool
from loguru import logger
from sqlmodel import Session, col, select

from app.config import get_settings
from app.db.models import McpServer
from app.db.session import engine

# 未配置白名单时的兜底：只允许常见语言运行时，管理员想放开就在 yaml 里追加
DEFAULT_ALLOWED_COMMANDS = ("python", "python3", "py", "node", "npx", "uv", "uvx")

# 解释器的"直接执行代码"开关：命中就等于把 command 白名单形同虚设
# （`python -c "import os;os.system(...)"` 的 command 仍然是 python）。
INLINE_CODE_FLAGS: dict[str, tuple[str, ...]] = {
    "python": ("-c", "-m"),
    "python3": ("-c", "-m"),
    "py": ("-c", "-m"),
    "node": ("-e", "--eval", "-p", "--print", "-M", "--import", "--require"),
}
# 这些命令每次运行都会拉包执行，等价于"任意代码"，生产默认拒绝（开发期可放行演示）
PACKAGE_RUNNERS = ("npx", "uvx", "uv")


def allowed_commands() -> tuple[str, ...]:
    raw = get_settings().get_dotted("mcp.allowed_commands", None) or []
    names = [str(x).strip().lower() for x in raw if str(x).strip()]
    return tuple(names) if names else DEFAULT_ALLOWED_COMMANDS


def _package_runners_allowed() -> bool:
    """npx/uvx/uv 每次运行都拉包执行，等价于任意代码。

    未显式配置时：开发形态放行（演示要在本机起 FastMCP 服务器），
    生产形态拒绝——想放行必须写 `mcp.allow_package_runners: true`，逼这个决定留痕。
    """
    s = get_settings()
    raw = s.get_dotted("mcp.allow_package_runners", None)
    if raw is not None:
        return bool(raw)
    return not s.is_production()


def allowed_roots() -> list[Path]:
    """stdio 脚本允许存放的目录；配置里的相对路径一律按后端根目录解析（不是进程 cwd）。"""
    from app.config import BACKEND_ROOT

    raw = get_settings().get_dotted("mcp.allowed_script_roots", None) or []
    roots = []
    for r in raw:
        if not str(r).strip():
            continue
        p = Path(str(r))
        roots.append((p if p.is_absolute() else BACKEND_ROOT / p).resolve())
    return roots or [BACKEND_ROOT.resolve()]


def _exe_base(exe: str) -> str:
    """命令的可执行名（去引号、去首尾空白、小写、去掉 Windows 的 .exe 后缀）。"""
    base = exe.strip().strip("\"'").replace("\\", "/").rsplit("/", 1)[-1].lower()
    return base[:-4] if base.endswith(".exe") else base


def validate_url(transport: str, url: str) -> None:
    """非 stdio 传输的端点校验：只允许 http/https，生产默认拒绝内网/链路本地地址。

    配 MCP 的虽然是 admin，但 sse/http 传输会让**服务器**去发起请求，
    不校验就是内网扫描/云元数据抓取（SSRF）的入口。
    """
    if transport not in ("sse", "streamable_http"):
        return
    parsed = urlparse(url or "")
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError(f"MCP 端点 {transport} 必须是 http/https URL，当前值：{url!r}")
    s = get_settings()
    if s.is_production() and not bool(s.get_dotted("mcp.allow_private_urls", False)):
        host = parsed.hostname
        try:
            ip = ipaddress.ip_address(host)
            private = (ip.is_private or ip.is_loopback
                       or ip.is_link_local or ip.is_reserved)
        except ValueError:
            private = (host == "localhost" or host.endswith(".localhost")
                       or host.startswith(("127.", "10.", "192.168.", "172.16.", "169.254."))
                       or host.endswith(".internal"))
        if private:
            raise ValueError(
                f"生产形态默认拒绝内网/链路本地 MCP 端点 {url}（SSRF 面）："
                "确实需要请设置 mcp.allow_private_urls: true")


def _split_commandline(command: str, args: str) -> list[str]:
    """把 command（可能自带参数）+ args 拼成最终 argv。"""
    parts = [p.strip("\"'") for p in shlex.split(command.strip(), posix=False)] if command.strip() else []
    if args.strip():
        parts += [p.strip("\"'") for p in shlex.split(args, posix=False)]
    return parts


def _script_in_allowed_roots(candidate: str) -> bool:
    from app.config import BACKEND_ROOT

    p = Path(candidate)
    if not p.is_absolute():
        p = BACKEND_ROOT / p
    try:
        resolved = p.resolve()
    except OSError:
        return False
    return any(resolved == root or root in resolved.parents for root in allowed_roots())


def validate_command(transport: str, command: str, args: str = "") -> None:
    """校验 stdio 拉起命令；不合法直接抛 ValueError（接口层转 400）。

    B3-6 之前只比对命令名，`python -c "..."` 一样放行，等于把"命令名白名单"当成
    唯一防线却给了它挡不住的活儿。现在四层：
    1) 可执行名在白名单内；
    2) 生产形态默认拒绝 npx/uvx/uv（每次运行拉包执行 = 任意代码）；
    3) 拒绝解释器的内联代码开关（-c / -m / -e / --eval …）；
    4) 脚本参数必须落在 mcp.allowed_script_roots（默认后端根目录）内，防止
       `python ../../etc/evil.py` 这类越界读取后执行。
    """
    if transport != "stdio":
        return
    argv = _split_commandline(command, args)
    if not argv:
        raise ValueError("stdio 传输必须填写 command")
    exe = _exe_base(argv[0])
    if exe not in allowed_commands():
        raise ValueError(
            f"command「{exe}」不在白名单内：只允许 {', '.join(allowed_commands())}，"
            "需要放行其他程序请在配置 mcp.allowed_commands 中追加")
    if exe in PACKAGE_RUNNERS and not _package_runners_allowed():
        raise ValueError(
            f"生产形态默认拒绝 {exe}（每次运行都会拉包执行任意代码）："
            "确实需要请显式设置 mcp.allow_package_runners: true")
    inline = INLINE_CODE_FLAGS.get(exe, ())
    for a in argv[1:]:
        if a in inline or (a.startswith("-") and a.rstrip("0123456789") in inline):
            raise ValueError(
                f"参数 {a} 会让 {exe} 直接执行内联代码，已被拒绝：请把服务器写成脚本文件，"
                "并把脚本路径放在 mcp.allowed_script_roots 内")
    for a in argv[1:]:
        if a.startswith("-"):
            continue
        if Path(a).suffix.lower() in (".py", ".js", ".mjs", ".cjs") and not _script_in_allowed_roots(a):
            raise ValueError(
                f"脚本路径 {a} 不在允许的目录内（当前 mcp.allowed_script_roots="
                f"{', '.join(str(r) for r in allowed_roots())}）")


def _connection_params(row: McpServer) -> dict | None:
    if row.transport == "stdio" and row.command:
        if not _command_allowed(row.command, row.args):
            _last_error[row.id or 0] = (
                f"command「{row.command} {row.args}」未通过 mcp 安全校验（白名单/内联代码/脚本目录），已跳过连接")
            logger.warning("MCP 服务器被跳过: {} → {}", row.name, _last_error[row.id or 0])
            return None
        # 放行的可能是"python server.py"这种带参数的写法，拉起子进程时同样要拆成
        # 可执行名 + 参数，否则 subprocess 找不到名叫 "python server.py" 的程序。
        parts = _split_commandline(row.command, row.args)
        return {"transport": "stdio", "command": parts[0], "args": parts[1:]}
    if row.transport in ("sse", "streamable_http") and row.url:
        return {"transport": row.transport, "url": row.url}
    return None


def _command_allowed(command: str, args: str = "") -> bool:
    try:
        validate_command("stdio", command, args)
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
