"""转人工工具（副作用型）：request_handoff。

chat 层通过 contextvar 注入当前会话/用户身份，工具本身不接受"会话 id"参数——
防止模型被诱导为他人会话建工单（工具规划文档中的安全约定）。
"""
from __future__ import annotations

import json
from contextvars import ContextVar

from langchain_core.tools import tool

# chat.py 每个请求 set：(conversation_id, user_id)
current_handoff_ctx: ContextVar[tuple[int, int] | None] = ContextVar("handoff_ctx", default=None)


@tool
def request_handoff(reason: str) -> str:
    """把当前会话转接给人工客服。

    用法（P8 两步制）：只有当你已询问客户「是否需要转人工」且客户在上一条消息
    中明确同意时才允许调用——客户只是表达了不满或问题复杂时，请先询问而非直接转接。
    reason 用一句话概括转接原因与用户核心诉求。
    调用后机器人自动挂起该会话，请勿再尝试回答业务问题。
    """
    ctx = current_handoff_ctx.get()
    if ctx is None:
        return json.dumps({"ok": False, "error": "上下文缺失"}, ensure_ascii=False)
    conv_id, user_id = ctx
    from app.db.models import Conversation, User
    from app.db.session import Session, engine
    from app.services.wecom.handoff import create_ticket

    try:
        with Session(engine) as session:
            conv = session.get(Conversation, conv_id)
            user = session.get(User, user_id)
            if not conv or not user:
                return json.dumps({"ok": False, "error": "会话或用户不存在"}, ensure_ascii=False)
            ticket = create_ticket(session, conv, user, reason=f"[Agent判定] {reason}")
        return json.dumps({"ok": True, "ticket_id": ticket.id,
                           "message": "已转人工，请告知用户等待客服接入"}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False)
