"""Agent 单轮执行（runner.run_turn）离线单测：状态机、工具循环、断连与失败落库。

用一个假模型替掉 get_chat_model：本项目真调模型的链路由 smoke_test/eval 覆盖，
这里要钉住的是"轮次怎么推进、消息怎么落库"，不该为了一条状态分支去花 token。
"""
from __future__ import annotations

import asyncio
import json

import pytest
from langchain_core.messages import AIMessageChunk
from sqlmodel import Session, select

from app.core.agents import runner
from app.core.agents.persona import persona
from app.db.models import Conversation, HandoffTicket, Message, ToolConfirmRequest, User
from app.db.session import engine
from app.services.mcp import manager as mcp_manager
from app.services.memory import pipeline


class FakeModel:
    """bind_tools/astream 的最小可替身：每轮 astream 吐一组 chunk。

    最后一轮之外都返回 tool_calls，用来测轮次上限；rounds 用完后一直吐 rounds[-1]。
    """

    def __init__(self, rounds, *, error: Exception | None = None):
        self.rounds = rounds
        self.error = error
        self.calls = 0
        self.seen_messages: list[list] = []
        self.bound: list[str] | None = None

    def bind_tools(self, tools):
        self.bound = [t.name for t in tools]
        return self

    async def astream(self, messages, **_kw):
        self.seen_messages.append(list(messages))
        if self.error is not None and self.calls == 0:
            raise self.error
        idx = min(self.calls, len(self.rounds) - 1)
        self.calls += 1
        for chunk in self.rounds[idx]:
            yield chunk


class FakeTool:
    def __init__(self, name="order_query", reply="订单 A1 已发货"):
        self.name = name
        self.reply = reply

    def invoke(self, args):
        return self.reply


@pytest.fixture(autouse=True)
def _no_memory_pipeline(monkeypatch):
    """后台记忆任务会真去调模型，这里只关心本轮对话。"""
    monkeypatch.setattr(pipeline, "schedule_memory_jobs", lambda *a, **k: None)


@pytest.fixture
def pair(db: Session):
    user = User(username="u", hashed_password="x", role="user")
    db.add(user)
    db.commit()
    conv = Conversation(user_id=user.id, title="新会话", status="active", handoff_status="bot")
    db.add(conv)
    db.commit()
    return user, conv


def _collect(conv, user, text, *, interactive=True):
    async def _go():
        events = []
        with Session(engine) as s:
            row = s.get(Conversation, conv.id)
            ur = s.get(User, user.id)
            async for name, data in runner.run_turn(s, row, ur, text, interactive=interactive):
                events.append((name, data))
        return events

    return asyncio.run(_go())


def _messages(conv_id: int, role: str) -> list[Message]:
    with Session(engine) as s:
        return list(s.exec(select(Message).where(
            Message.conversation_id == conv_id, Message.role == role)).all())


def _install(monkeypatch, model, tools=()):
    async def build(_user, _session):
        return list(tools)
    monkeypatch.setattr(runner, "get_chat_model", lambda *a, **k: model)
    monkeypatch.setattr(runner, "build_tools_async", build)


# ---- 状态机：机器人什么时候该闭嘴 ----

def test_closed_conversation_rejects_without_writing(monkeypatch, pair):
    user, conv = pair
    with Session(engine) as s:
        row = s.get(Conversation, conv.id)
        row.status = "closed"
        s.add(row)
        s.commit()
    model = FakeModel([[AIMessageChunk(content="不该出现")]])
    _install(monkeypatch, model)

    events = _collect(conv, user, "有货吗")
    assert events[0][0] == "error" and "已结束" in events[0][1]["message"]
    assert model.calls == 0
    assert _messages(conv.id, "user") == []       # 拒收不能把消息写进已终结的会话


def test_keyword_asks_for_confirm_before_calling_model(monkeypatch, pair):
    """P8：说"转人工"只进入待确认态，绝不直接建单（误触一次就占用客服资源）。"""
    user, conv = pair
    model = FakeModel([[AIMessageChunk(content="不该调用")]])
    _install(monkeypatch, model)

    events = _collect(conv, user, "我要投诉")
    assert [e[0] for e in events] == ["meta", "handoff", "delta", "done"]
    assert events[1][1]["status"] == "awaiting_confirm"
    # 文案来自 agent 配置（persona.persona()），不再硬编码在 runner 里
    assert events[2][1]["text"] == persona().handoff_ask
    assert model.calls == 0
    with Session(engine) as s:
        assert s.get(Conversation, conv.id).handoff_status == "awaiting_confirm"
        assert s.exec(select(HandoffTicket)).first() is None


def test_affirm_creates_ticket_and_queues(monkeypatch, pair):
    user, conv = pair
    with Session(engine) as s:
        row = s.get(Conversation, conv.id)
        row.handoff_status = "awaiting_confirm"
        s.add(row)
        s.commit()
    model = FakeModel([[AIMessageChunk(content="不该调用")]])
    _install(monkeypatch, model)

    events = _collect(conv, user, "是")
    assert events[1][1]["status"] == "pending_human"
    assert events[1][1]["ticket_id"] is not None
    with Session(engine) as s:
        ticket = s.exec(select(HandoffTicket)).first()
        assert ticket and ticket.conversation_id == conv.id
        assert s.get(Conversation, conv.id).handoff_status == "pending_human"


def test_negative_reply_goes_back_to_bot(monkeypatch, pair):
    user, conv = pair
    with Session(engine) as s:
        row = s.get(Conversation, conv.id)
        row.handoff_status = "awaiting_confirm"
        s.add(row)
        s.commit()
    model = FakeModel([[AIMessageChunk(content="好的，还有别的需要帮忙吗")]])
    _install(monkeypatch, model)

    events = _collect(conv, user, "不用了")
    assert model.calls == 1
    assert events[-1][0] == "done"
    with Session(engine) as s:
        assert s.get(Conversation, conv.id).handoff_status == "bot"


@pytest.mark.parametrize("state", ["pending_human", "human"])
def test_human_handling_suspends_the_bot(monkeypatch, pair, state):
    """人工服务中机器人不抢答：只回固定提示，一个字都不让模型生成。"""
    user, conv = pair
    with Session(engine) as s:
        row = s.get(Conversation, conv.id)
        row.handoff_status = state
        s.add(row)
        s.commit()
    model = FakeModel([[AIMessageChunk(content="抢答")]])
    _install(monkeypatch, model)

    events = _collect(conv, user, "我的订单呢")
    assert model.calls == 0 and events[-1][0] == "done"
    assert "人工" in events[2][1]["text"]


# ---- 模型失败与断连：回答到底落没落库 ----

def test_model_error_yields_error_and_keeps_question(monkeypatch, pair):
    user, conv = pair
    _install(monkeypatch, FakeModel([], error=RuntimeError("503 upstream")))
    events = _collect(conv, user, "价格多少")
    assert events[-1][0] == "error" and "模型调用失败" in events[-1][1]["message"]
    assert len(_messages(conv.id, "user")) == 1       # 买家问过的话要留痕
    assert _messages(conv.id, "assistant") == []


def test_partial_answer_is_persisted_when_client_disconnects(monkeypatch, pair):
    """B1-6：浏览器中途关页面，已生成的半截回答也要落库并标记不完整。"""
    user, conv = pair
    model = FakeModel([[AIMessageChunk(content="前半段"), AIMessageChunk(content="后半段")]])
    _install(monkeypatch, model)

    async def go():
        with Session(engine) as s:
            gen = runner.run_turn(s, s.get(Conversation, conv.id), s.get(User, user.id),
                                  "讲个故事", interactive=True)
            got = []
            async for name, data in gen:
                got.append((name, data))
                if len([e for e in got if e[0] == "delta"]) == 1:
                    break
            await gen.aclose()
            return got

    asyncio.run(go())
    partial = _messages(conv.id, "assistant")
    assert len(partial) == 1
    assert partial[0].content == "前半段"
    meta = json.loads(partial[0].meta_json)
    assert meta["partial"] is True and meta["truncated_by"] == "client_disconnect"


def test_missing_model_config_is_a_readable_error(monkeypatch, pair):
    """没配 api_key 时必须给出可读提示，而不是把 SSE 流炸成 500。"""
    user, conv = pair
    from app.core.models.manager import ModelNotConfiguredError

    def boom(*a, **k):
        raise ModelNotConfiguredError("未配置模型 dashscope 的 api_key")

    monkeypatch.setattr(runner, "get_chat_model", boom)
    events = _collect(conv, user, "你好")
    assert events[-1] == ("error", {"message": "未配置模型 dashscope 的 api_key"})


# ---- 工具循环 ----

def test_tool_call_round_feeds_result_back(monkeypatch, pair):
    user, conv = pair
    call = {"name": "order_query", "args": {"order_id": "A1"}, "id": "c1", "type": "tool_call"}
    model = FakeModel([
        [AIMessageChunk(content="", tool_calls=[call])],
        [AIMessageChunk(content="您的订单已发货")],
    ])
    _install(monkeypatch, model, tools=[FakeTool()])

    events = _collect(conv, user, "A1 到哪了")
    kinds = [e[0] for e in events]
    assert kinds.count("tool") == 2 and "confirm_request" not in kinds
    end = next(d for n, d in events if n == "tool" and d["status"] == "end")
    assert end["output"] == "订单 A1 已发货"
    assert model.bound == ["order_query"]              # 工具确实绑给了模型
    # 第二轮上下文里必须带上 ToolMessage，否则模型永远看不到结果
    assert any(m.__class__.__name__ == "ToolMessage" for m in model.seen_messages[-1])
    meta = json.loads(_messages(conv.id, "assistant")[0].meta_json)
    assert meta["tools"] == [{"name": "order_query", "args": {"order_id": "A1"}}]


def test_tool_loop_is_bounded(monkeypatch, pair):
    """模型一直要调工具时最多跑 MAX_TOOL_ROUNDS 轮，不能把一次对话变成死循环。"""
    user, conv = pair
    call = {"name": "order_query", "args": {}, "id": "c", "type": "tool_call"}
    model = FakeModel([[AIMessageChunk(content="", tool_calls=[call])]])
    _install(monkeypatch, model, tools=[FakeTool()])

    events = _collect(conv, user, "查单")
    assert model.calls == runner.MAX_TOOL_ROUNDS
    assert len([e for e in events if e[0] == "tool" and e[1]["status"] == "start"]) \
        == runner.MAX_TOOL_ROUNDS


def test_unknown_tool_falls_back_to_readable_error(monkeypatch, pair):
    """工具名被模型编出来时不能抛栈：把错误当结果回给模型，让它自纠。"""
    user, conv = pair
    call = {"name": "imaginary", "args": {}, "id": "c1", "type": "tool_call"}
    model = FakeModel([
        [AIMessageChunk(content="", tool_calls=[call])],
        [AIMessageChunk(content="抱歉，我查不到这个信息")],
    ])
    _install(monkeypatch, model, tools=[FakeTool()])

    events = _collect(conv, user, "随便查")
    end = next(d for n, d in events if n == "tool" and d["status"] == "end")
    assert "未知工具" in end["output"]
    assert events[-1][0] == "done"


def test_tool_exception_is_reported_to_model(monkeypatch, pair):
    user, conv = pair

    class Broken(FakeTool):
        def invoke(self, args):
            raise RuntimeError("数据库连不上")

    call = {"name": "order_query", "args": {}, "id": "c1", "type": "tool_call"}
    model = FakeModel([
        [AIMessageChunk(content="", tool_calls=[call])],
        [AIMessageChunk(content="稍后再试")],
    ])
    _install(monkeypatch, model, tools=[Broken()])
    events = _collect(conv, user, "查单")
    end = next(d for n, d in events if n == "tool" and d["status"] == "end")
    assert "工具执行失败" in end["output"] and "数据库连不上" in end["output"]


def test_confirm_required_tool_is_cancelled_on_platform(monkeypatch, pair):
    """平台渠道没有 SSE 监听者：需确认的工具直接取消，而不是挂 180s 等超时。"""
    user, conv = pair
    call = {"name": "refund_apply", "args": {}, "id": "c1", "type": "tool_call"}
    model = FakeModel([
        [AIMessageChunk(content="", tool_calls=[call])],
        [AIMessageChunk(content="已为您取消该操作")],
    ])
    _install(monkeypatch, model, tools=[FakeTool(name="refund_apply")])
    monkeypatch.setattr(mcp_manager, "is_confirm_required", lambda name: True)

    events = _collect(conv, user, "退款", interactive=False)
    kinds = [e[0] for e in events]
    assert "confirm_request" not in kinds
    end = next(d for n, d in events if n == "tool" and d["status"] == "end")
    assert "平台渠道已取消" in end["output"]


def test_confirm_card_approved_in_band(monkeypatch, pair):
    """网页端：卡片下发后 runner 轮询库等裁决，点"允许"把行置 approved 才执行工具。

    B3-8 起注册表落库、不再有进程内 _pending/future；conftest 每用例清表，
    这里无需再手工清理。resolve 发生在生成器 yield 之后、wait_decision 首轮
    读取之前（同一线程串行推进），所以不会卡在 1 秒轮询上，也不存在死锁。
    """
    user, conv = pair
    call = {"name": "refund_apply", "args": {}, "id": "c1", "type": "tool_call"}
    model = FakeModel([
        [AIMessageChunk(content="", tool_calls=[call])],
        [AIMessageChunk(content="退款已提交")],
    ])
    _install(monkeypatch, model, tools=[FakeTool(name="refund_apply", reply="退款成功")])
    monkeypatch.setattr(mcp_manager, "is_confirm_required", lambda name: True)

    from app.services.mcp import confirm

    seen = {}

    async def go():
        with Session(engine) as s:
            gen = runner.run_turn(s, s.get(Conversation, conv.id), s.get(User, user.id),
                                  "帮我退款", interactive=True)
            events = []
            async for name, data in gen:
                events.append((name, data))
                if name == "confirm_request":
                    seen["rid"] = data["request_id"]
                    assert confirm.resolve(data["request_id"], True) is True
            return events

    events = asyncio.run(go())
    assert seen["rid"]
    end = next(d for n, d in events if n == "tool" and d["status"] == "end")
    assert end["output"] == "退款成功"
    with Session(engine) as s:                    # 裁决落库留痕：状态机终态是 approved
        row = s.exec(select(ToolConfirmRequest).where(
            ToolConfirmRequest.request_id == seen["rid"])).first()
        assert row is not None and row.status == "approved"


def test_confirm_denied_skips_tool_execution(monkeypatch, pair):
    user, conv = pair
    call = {"name": "refund_apply", "args": {}, "id": "c1", "type": "tool_call"}
    model = FakeModel([
        [AIMessageChunk(content="", tool_calls=[call])],
        [AIMessageChunk(content="好的，未做处理")],
    ])
    _install(monkeypatch, model, tools=[FakeTool(name="refund_apply", reply="不该执行")])
    monkeypatch.setattr(mcp_manager, "is_confirm_required", lambda name: True)

    async def go():
        with Session(engine) as s:
            gen = runner.run_turn(s, s.get(Conversation, conv.id), s.get(User, user.id),
                                  "退款", interactive=True)
            events = []
            async for name, data in gen:
                events.append((name, data))
                if name == "confirm_request":
                    from app.services.mcp import confirm
                    confirm.resolve(data["request_id"], False)
            return events

    events = asyncio.run(go())
    end = next(d for n, d in events if n == "tool" and d["status"] == "end")
    assert "拒绝" in end["output"]


def test_history_is_replayed_into_next_turn(monkeypatch, pair):
    """短期记忆滑窗：上一轮问答要进第二轮的模型上下文（否则多轮就是空的）。"""
    user, conv = pair
    model = FakeModel([[AIMessageChunk(content="回答一")]])
    _install(monkeypatch, model)
    _collect(conv, user, "问题一")

    model2 = FakeModel([[AIMessageChunk(content="回答二")]])
    _install(monkeypatch, model2)
    _collect(conv, user, "问题二")
    roles = [m.__class__.__name__ for m in model2.seen_messages[0]]
    assert roles[0] == "SystemMessage"
    assert roles.count("HumanMessage") == 2 and roles.count("AIMessage") == 1
    assert model2.seen_messages[0][-1].content == "问题二"
