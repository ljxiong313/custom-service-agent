"""MCP 服务器配置管理接口 + 工具确认回传。"""
from __future__ import annotations

import json

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlmodel import select

from app.core.security import CurrentUser, DbSession, RequireAdmin
from app.db.models import McpServer
from app.services.mcp import confirm, manager

router = APIRouter(prefix="/mcp", tags=["mcp"])


class ServerBody(BaseModel):
    name: str = Field(min_length=1, max_length=32)
    transport: str = "stdio"        # stdio / sse / streamable_http
    command: str = ""
    args: str = ""
    url: str = ""
    enabled: bool = True
    require_confirm: bool = True


def _validate_command(body: ServerBody) -> None:
    """白名单校验失败要变成 400 用户可读文案，而不是 500。"""
    try:
        manager.validate_command(body.transport, body.command)
    except ValueError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from e


def _out(row: McpServer) -> dict:
    err = manager.last_error(row.id or 0)
    return {**row.model_dump(),
            "tools": json.loads(row.tools_json or "[]"),
            "last_error": err}


@router.get("/servers")
def list_servers(user: CurrentUser, session: DbSession):
    rows = session.exec(select(McpServer).order_by(McpServer.id)).all()  # type: ignore[arg-type]
    return [_out(r) for r in rows]


@router.post("/servers", response_model=McpServer)
def create_server(body: ServerBody, user: RequireAdmin, session: DbSession):
    """登记 MCP 服务器（仅 admin）。

    stdio 传输会由本机 subprocess 拉起 command+args，等于"登录用户能在服务器执行命令"，
    所以这里既要 admin 角色，也要过 mcp.allowed_commands 白名单（见 manager.validate_command）。
    """
    _validate_command(body)
    if session.exec(select(McpServer).where(McpServer.name == body.name)).first():
        raise HTTPException(status.HTTP_409_CONFLICT, "同名服务器已存在")
    row = McpServer(**body.model_dump())
    session.add(row)
    session.commit()
    session.refresh(row)
    manager.invalidate_cache()
    return row


@router.patch("/servers/{server_id}", response_model=McpServer)
def update_server(server_id: int, body: ServerBody, user: RequireAdmin, session: DbSession):
    row = session.get(McpServer, server_id)
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "服务器不存在")
    _validate_command(body)
    for k, v in body.model_dump().items():
        setattr(row, k, v)
    session.add(row)
    session.commit()
    session.refresh(row)
    manager.invalidate_cache()
    return row


@router.delete("/servers/{server_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_server(server_id: int, user: RequireAdmin, session: DbSession):
    row = session.get(McpServer, server_id)
    if row:
        session.delete(row)
        session.commit()
    manager.invalidate_cache()


@router.post("/servers/{server_id}/test")
async def test_server(server_id: int, user: RequireAdmin, session: DbSession):
    """连接测试：立即发现一次工具（绕过缓存），返回清单或错误。"""
    row = session.get(McpServer, server_id)
    if not row:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "服务器不存在")
    manager.invalidate_cache()
    tools = await manager.get_mcp_tools()
    session.refresh(row)
    return {"status": row.status, "tool_count": len(tools),
            "tools": json.loads(row.tools_json or "[]"),
            "error": manager.last_error(server_id)}


@router.get("/pending")
def list_pending_confirms(user: CurrentUser):
    """等待确认的 MCP 工具调用（运营台轮询）。

    有确认卡片的会话不只来自网页 SSE：任何渠道都可能排队，所以列表与 /confirm
    分开提供，网页端与运营台都能替会话点确认。
    """
    confirm.prune()
    return {"items": confirm.pending_list()}


class ConfirmBody(BaseModel):
    request_id: str
    approved: bool


@router.post("/confirm")
def resolve_confirm(body: ConfirmBody, user: CurrentUser):
    ok = confirm.resolve(body.request_id, body.approved)
    if not ok:
        raise HTTPException(status.HTTP_410_GONE, "确认请求已过期或不存在")
    return {"ok": True}
