"""工具执行确认注册表：SSE 流内挂起等待确认卡片的结果。

流程：对话循环发现需确认工具 → create_request 得 request_id →
下发 confirm_request 事件 → await future（超时视为拒绝）→
前端 POST /mcp/confirm 携带 request_id + approved → resolve。

为什么还要带元数据（B2-3）：确认卡片原来只顺着当次 SSE 事件下发，
平台渠道（企微/电商）没有 SSE 监听者，卡片根本没地方点，只能超时拒绝。
现在注册表记录 conversation_id/tool/args，运营台轮询 GET /mcp/pending 就能替
任何渠道的会话做确认。

单进程内存态是本项目的既定约束（见 README「部署形态」）：多 worker 下
SSE 与 /mcp/confirm 可能落到不同进程，future 找不到必然超时。
"""
from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field

TTL_SECONDS = 180.0  # 超过这个时间没人应答就当拒绝并清掉，避免 _pending 无界增长


@dataclass
class _Pending:
    request_id: str
    conversation_id: int
    user_id: int
    tool_name: str
    args: dict = field(default_factory=dict)
    created_at: float = field(default_factory=time.time)
    future: asyncio.Future | None = None


_pending: dict[str, _Pending] = {}


def create_request(conversation_id: int, user_id: int, tool_name: str,
                   args: dict | None = None) -> str:
    loop = asyncio.get_running_loop()
    rid = uuid.uuid4().hex[:12]
    _pending[rid] = _Pending(request_id=rid, conversation_id=conversation_id,
                             user_id=user_id, tool_name=tool_name, args=args or {},
                             future=loop.create_future())
    return rid


def pending_list() -> list[dict]:
    """当前等待确认的卡片（运营台轮询用）。顺带回收过期条目。

    future 已 done = 已经有人点过了（等待方还没轮到 pop 的这几毫秒），列表里就不该再出现，
    否则运营台会看到一张点了只报"已过期"的卡片。
    """
    prune()
    return [{"request_id": p.request_id, "conversation_id": p.conversation_id,
             "tool_name": p.tool_name, "args": p.args,
             "created_at": p.created_at}
            for p in _pending.values()
            if p.future is None or not p.future.done()]


def prune(max_age: float = TTL_SECONDS) -> int:
    """清掉超期无人应答的请求：SSE 侧本来就会超时拒绝，注册表不能一直留着。"""
    now = time.time()
    expired = [rid for rid, p in _pending.items() if now - p.created_at > max_age]
    for rid in expired:
        p = _pending.pop(rid, None)
        if p and p.future and not p.future.done():
            p.future.set_result(False)   # 显式置 False：等待方按"拒绝"处理，不必再等超时
    return len(expired)


async def wait_decision(request_id: str, timeout: float = TTL_SECONDS) -> bool:
    entry = _pending.get(request_id)
    if entry is None or entry.future is None:
        return False
    try:
        return await asyncio.wait_for(entry.future, timeout)
    except (TimeoutError, asyncio.CancelledError):
        return False
    finally:
        _pending.pop(request_id, None)


def resolve(request_id: str, approved: bool) -> bool:
    entry = _pending.get(request_id)
    if entry is None or entry.future is None or entry.future.done():
        return False
    # 不在这里 pop：runner 是"下发卡片事件 → 才去 await"，买家点的快的话 resolve 先发生，
    # 提前摘掉等待方就找不到 future（会被当成拒绝）。交给 wait_decision 的 finally 收尾，
    # 等待方中途死掉时由 prune()（pending_list/pending_count 都会调）按 TTL 回收。
    entry.future.set_result(approved)
    return True


def pending_count() -> int:
    return len(_pending)
