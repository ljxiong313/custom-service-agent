"""Agent 单轮执行核心（P7 起由 SSE 端点与平台 webhook 共用）。

run_turn 是纯异步事件生成器，产出 (event, data) 元组，事件与前端 SSE 协议一一对应：
    meta / tool / delta / citation / handoff / confirm_request / done / error
消息持久化、转人工状态机、引用落库、记忆管道全部在此完成——调用方只负责"怎么投递"：
chat.py 转成 SSE 字符串；平台入站聚合 delta 后经平台 API 回复买家。

interactive=False（平台渠道）：MCP 需确认工具直接按拒绝处理（无人可点确认卡片），
其余行为与网页端一致。
"""
from __future__ import annotations

import asyncio
import datetime
import json
import re
from collections.abc import AsyncIterator

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from loguru import logger
from sqlmodel import Session

from app.core.agents.persona import build_system_prompt
from app.core.agents.tools import build_tools_async
from app.core.agents.tools.handoff_tools import current_handoff_ctx
from app.core.agents.tools.rag_tools import citation_collector
from app.core.models.manager import ModelNotConfiguredError, get_chat_model
from app.db.models import Conversation, Message, User, pk
from app.db.session import engine
from app.services.memory.short import ShortTermMemory

MAX_TOOL_ROUNDS = 5

# P3 显式关键词触发（优先于 LLM 判定）；P8 起不再直接建单，而是进入确认询问态
HANDOFF_KEYWORDS = re.compile(r"(转人工|人工客服|找个人|真人客服|投诉)")
# P8 确认式转人工：awaiting_confirm 态下买家的肯定/否定回复判定（行首匹配即可）
CONFIRM_AFFIRM = re.compile(
    r"^\s*(嗯+|好(的|呀|啊)?|是(的|呀|吧)?|要(的)?|需要|行|可以|确认|转吧|麻烦你|请转|ok|okay|yes)",
    re.IGNORECASE)
CONFIRM_NEGATE = re.compile(r"^\s*(不|不用|不要|算了|暂时|没|no)", re.IGNORECASE)

HANDOFF_ASK = "是否需要为您转接人工客服？回复「是」立即为您转接，回复「否」可继续与智能助手沟通。"
HANDOFF_QUEUED = "已为您联系人工客服，请稍候…"


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def _build_meta(cites: list[dict], tool_trace: list[dict],
                partial_reason: str | None = None) -> dict:
    meta: dict = {}
    if cites:
        meta["citations"] = [{"index": i + 1, **c} for i, c in enumerate(cites)]
    if tool_trace:
        meta["tools"] = tool_trace
    if partial_reason:
        # 断连截断标记：会话页据此提示"回答不完整"，记忆管道也不会把半截回答当最终答复
        meta["partial"] = True
        meta["truncated_by"] = partial_reason
    return meta


def _persist_assistant_message(conv_id: int, content: str, meta: dict, user_text: str) -> int:
    """落库 assistant 消息 + 刷新会话活跃时间（首轮顺带起标题）。

    自己开 Session：SSE 断连后 FastAPI 会关掉依赖注入的请求会话，此时落库仍要写得进去，
    否则已生成的回答随连接一起消失，会话里查不到任何痕迹。
    """
    with Session(engine) as session:
        msg = Message(conversation_id=conv_id, role="assistant", content=content,
                      meta_json=json.dumps(meta, ensure_ascii=False) if meta else None)
        session.add(msg)
        conv_row = session.get(Conversation, conv_id)
        if conv_row:
            conv_row.updated_at = _now()
            if conv_row.title == "新会话":
                conv_row.title = user_text[:20]
            session.add(conv_row)
        session.commit()
        session.refresh(msg)
        return pk(msg)


async def run_turn(session: Session, conv: Conversation, user: User,
                   text: str, *, interactive: bool = True) -> AsyncIterator[tuple[str, dict]]:
    """执行一轮对话：入库用户消息 → 状态机判定 → 模型+工具循环 → 落库回答。"""
    # P8 会话终结后拒收（运营台/入站渠道统一在此兜底）
    if conv.status == "closed":
        yield "error", {"message": "该会话已结束，无法继续发送消息"}
        return

    # 会话/用户主键在本轮里到处要用（外键、记忆、工单、确认表），一次性收敛成 int
    conv_id, uid = pk(conv), pk(user)

    user_msg = Message(conversation_id=conv_id, role="user", content=text)
    session.add(user_msg)
    session.commit()
    session.refresh(user_msg)
    user_msg_id = pk(user_msg)
    yield "meta", {"user_message_id": user_msg_id, "conversation_id": conv_id}

    # P8 空闲计时锚点：任何客户入站消息都重置超时与"是否结束"提示
    conv.last_customer_msg_at = _now()
    conv.end_prompted_at = None
    session.add(conv)
    session.commit()

    # ---- P8 确认式转人工：awaiting_confirm 态下按买家回复裁决 ----
    if conv.handoff_status == "awaiting_confirm":
        if CONFIRM_AFFIRM.match(text):
            from app.services.wecom.handoff import create_ticket

            tip, status, ticket_id = HANDOFF_QUEUED, "pending_human", None
            try:
                ticket = create_ticket(session, conv, user, "客户确认转人工")
                ticket_id = ticket.id
            except Exception:
                logger.exception("确认转人工建单失败")
                conv.handoff_status = "bot"
                tip, status = "转接人工客服失败，请稍后重试或直接联系店铺客服。", "bot"
                session.add(conv)
                session.commit()
            note = Message(conversation_id=conv_id, role="assistant", content=tip,
                           meta_json=json.dumps({"from": "system_handoff"}, ensure_ascii=False))
            session.add(note)
            conv.updated_at = _now()
            session.add(conv)
            session.commit()
            session.refresh(note)
            yield "handoff", {"status": status, "ticket_id": ticket_id}
            yield "delta", {"text": tip}
            yield "done", {"assistant_message_id": note.id}
            return
        # 否定或答非所问：撤销待确认态，回 bot 走正常应答（模型可承接"不用了"寒暄）
        conv.handoff_status = "bot"
        session.add(conv)
        session.commit()

    # ---- P3 关键词显式触发（先于一切 LLM 判定）→ P8 进入确认询问态，不直接建单 ----
    if conv.handoff_status == "bot" and HANDOFF_KEYWORDS.search(text):
        conv.handoff_status = "awaiting_confirm"
        note = Message(conversation_id=conv_id, role="assistant", content=HANDOFF_ASK,
                       meta_json=json.dumps({"from": "system_handoff_ask"}, ensure_ascii=False))
        session.add(note)
        conv.updated_at = _now()
        session.add(conv)
        session.commit()
        session.refresh(note)
        yield "handoff", {"status": "awaiting_confirm", "ticket_id": None}
        yield "delta", {"text": HANDOFF_ASK}
        yield "done", {"assistant_message_id": note.id}
        return

    # ---- P3 状态机：人工服务中机器人挂起 ----
    if conv.handoff_status in ("pending_human", "human"):
        tip = (HANDOFF_QUEUED if conv.handoff_status == "pending_human"
               else "当前由人工客服为您服务，您的消息已同步给客服。")
        note = Message(conversation_id=conv_id, role="assistant", content=tip,
                       meta_json=json.dumps({"from": "system_handoff"}, ensure_ascii=False))
        session.add(note)
        conv.updated_at = _now()
        session.add(conv)
        session.commit()
        session.refresh(note)
        yield "handoff", {"status": conv.handoff_status, "ticket_id": None}
        yield "delta", {"text": tip}
        yield "done", {"assistant_message_id": note.id}
        return

    try:
        model = get_chat_model()
    except ModelNotConfiguredError as e:
        yield "error", {"message": str(e)}
        return

    # ---- 上下文组装：system + 中期摘要 + 短期滑窗 + 本轮消息 ----
    stm = ShortTermMemory.from_settings()
    history = stm.load_history(session, conv_id, before_message_id=user_msg_id)

    extra = []
    try:  # P2 中期记忆：滑窗外的会话摘要
        from app.services.memory.mid import load_summary

        s = load_summary(session, conv_id)
        if s:
            extra.append(f"本会话较早内容的摘要：{s}")
    except Exception:
        pass

    lc_messages: list = [SystemMessage(content=build_system_prompt(
        "\n\n".join(extra) if extra else None))]
    for m in history:
        lc_messages.append(HumanMessage(content=m.content) if m.role == "user"
                           else AIMessage(content=m.content))
    lc_messages.append(HumanMessage(content=text))

    citations_token = citation_collector.set([])
    handoff_token = current_handoff_ctx.set((conv_id, uid))
    tool_trace: list[dict] = []
    answer_parts: list[str] = []
    assistant_persisted = False
    interrupt_reason: str | None = None
    try:
        tools = await build_tools_async(user, session)
        bound = model.bind_tools(tools) if tools else model
        tools_by_name = {t.name: t for t in tools}
        messages = list(lc_messages)

        for _round in range(MAX_TOOL_ROUNDS):
            acc = None
            async for chunk in bound.astream(messages):
                acc = chunk if acc is None else acc + chunk
                # 工具调用参数流式拼半截时 text 增量仍可能是思考文本，照常下发
                if isinstance(chunk.content, str) and chunk.content:
                    answer_parts.append(chunk.content)
                    yield "delta", {"text": chunk.content}
            if acc is None:
                break
            messages.append(acc)
            calls = getattr(acc, "tool_calls", None) or []
            if not calls:
                break  # 最终回答已流式输出
            for call in calls:
                yield "tool", {"name": call["name"], "args": call["args"], "status": "start"}
                # ---- P4: MCP 需确认工具 → 网页端挂起等确认卡片；平台端直接拒绝 ----
                from app.services.mcp.manager import is_confirm_required

                if is_confirm_required(call["name"]) and interactive:
                    from app.services.mcp.confirm import create_request, wait_decision

                    rid = create_request(conv_id, uid, call["name"], call["args"])
                    yield "confirm_request", {"request_id": rid, "name": call["name"],
                                              "args": call["args"]}
                    approved = await wait_decision(rid)
                    out = (await _run_tool(tools_by_name, call) if approved
                           else "用户拒绝了该操作。")
                elif is_confirm_required(call["name"]):
                    out = "该操作需要用户在网页端确认，平台渠道已取消。"
                else:
                    out = await _run_tool(tools_by_name, call)
                yield "tool", {"name": call["name"], "args": call["args"],
                               "status": "end", "output": str(out)[:400]}
                tool_trace.append({"name": call["name"], "args": call["args"]})
                # P3: request_handoff 成功 → 状态机切换并通知调用方
                if call["name"] == "request_handoff":
                    try:
                        from app.services.wecom.handoff import apply_handoff_from_tool

                        info = apply_handoff_from_tool(conv_id, out)
                        if info:
                            yield "handoff", info
                    except Exception:
                        logger.exception("handoff 状态更新失败")
                messages.append(ToolMessage(tool_call_id=call["id"], content=str(out)))

        full = "".join(answer_parts)
        cites = citation_collector.get() or []
        meta = _build_meta(cites, tool_trace)
        if cites:
            yield "citation", {"items": meta["citations"]}

        assistant_msg_id = _persist_assistant_message(conv_id, full, meta, text)
        assistant_persisted = True
        yield "done", {"assistant_message_id": assistant_msg_id}

        # P2 后台管道：中期摘要 + 长期画像抽取（失败不影响本轮对话）
        try:
            from app.services.memory.pipeline import schedule_memory_jobs

            schedule_memory_jobs(conv_id, uid)
        except Exception as e:
            logger.debug("记忆管道未启动: {}", e)
    except asyncio.CancelledError:
        # 任务被取消（客户端断开 / 反代超时 / 关停）：留下痕迹后必须把取消信号继续往外传
        interrupt_reason = "client_disconnect"
        raise
    except GeneratorExit:
        # SSE 消费方提前 aclose() 生成器：Starlette 在浏览器关页面/刷新时走这条路径
        interrupt_reason = "client_disconnect"
        raise
    except Exception as e:
        logger.exception("对话轮执行失败")
        yield "error", {"message": f"模型调用失败：{e}"}
    finally:
        # 半截回答也要落库，否则会话里查不到任何回复记录；citations 必须在 reset 前取
        if not assistant_persisted and interrupt_reason and answer_parts:
            try:
                _persist_assistant_message(
                    conv_id, "".join(answer_parts),
                    _build_meta(citation_collector.get() or [], tool_trace, interrupt_reason),
                    text)
            except Exception as e:
                # 收尾写失败只记日志：这里抛出会把一次正常断连变成 500
                logger.warning("中断回答落库失败（conv#{}）: {}", conv_id, e)
        citation_collector.reset(citations_token)
        current_handoff_ctx.reset(handoff_token)


async def _run_tool(tools_by_name: dict, call: dict) -> str:
    """执行工具：同步工具进线程池避免阻塞事件循环；未知工具名返回错误供模型自纠。"""
    tool_obj = tools_by_name.get(call["name"])
    if tool_obj is None:
        return f"错误：未知工具 {call['name']}"
    try:
        result = await asyncio.to_thread(tool_obj.invoke, call["args"])
        return str(result)
    except Exception as e:
        logger.warning("工具 {} 执行失败: {}", call["name"], e)
        return f"工具执行失败：{e}"
