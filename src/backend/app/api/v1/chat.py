"""流式对话接口（SSE 薄壳）。

P7 起执行逻辑整体迁至 core/agents/runner.run_turn（与平台 webhook 共用同一轮次实现），
本文件只负责 HTTP 语义：归属校验、事件→SSE 字符串编码。

事件协议（与前端 apis/chat.ts 严格对应）：
    meta / tool / delta / citation / handoff / confirm_request / done / error
"""
from __future__ import annotations

import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.core.agents.runner import run_turn
from app.core.security import CurrentUser, DbSession
from app.db.models import Conversation

router = APIRouter(prefix="/chat", tags=["chat"])

SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


class ChatRequest(BaseModel):
    conversation_id: int
    message: str = Field(min_length=1, max_length=8000)


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.post("/completions")
def chat_completions(body: ChatRequest, user: CurrentUser, session: DbSession):
    conv = session.get(Conversation, body.conversation_id)
    if conv is None or conv.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "会话不存在")

    async def stream() -> AsyncIterator[str]:
        # 轮次内的写库由 runner 自管会话完成：请求级会话随断连关闭也不会丢已生成的回答
        async for event, data in run_turn(session, conv, user, body.message):
            yield _sse(event, data)

    return StreamingResponse(stream(), media_type="text/event-stream", headers=SSE_HEADERS)
