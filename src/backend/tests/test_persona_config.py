"""B3-10 Agent 人设可配置：改 YAML 就能换名字/语气/长度/转人工口径，不必改代码。

这些断言同时钉住"不配置时行为与旧版完全一致"——否则升级配置就成了行为变更，
而 README/评测用例都是按默认人设写的。
"""
from __future__ import annotations

from app.config import get_settings
from app.core.agents import runner
from app.core.agents.persona import (
    DEFAULT_AGENT_NAME,
    build_system_prompt,
    confirm_affirm,
    confirm_negate,
    handoff_keywords,
    persona,
)


def _agent(monkeypatch, **kwargs):
    monkeypatch.setitem(get_settings(), "agent", kwargs)


# ---- 默认值 ----

def test_default_persona_matches_legacy_prompt():
    p = persona()
    assert p.name == DEFAULT_AGENT_NAME
    assert p.max_reply_chars == 300
    prompt = build_system_prompt()
    assert f"你是「{DEFAULT_AGENT_NAME}」" in prompt
    assert "单次回复不超过 300 字" in prompt
    assert "request_handoff 只有在客户上一条消息已明确同意后才允许调用" in prompt
    assert "## 参考信息" not in prompt


def test_extra_context_is_appended_as_reference_block():
    prompt = build_system_prompt("用户预算 100 元以内")
    assert "## 参考信息\n用户预算 100 元以内" in prompt
    assert prompt.index("## 参考信息") > prompt.index("## 工具使用策略")


# ---- 配置生效 ----

def test_persona_fields_come_from_config(monkeypatch):
    _agent(monkeypatch, name="小星", scene="母婴用品店铺客服", tone="亲切、耐心",
           max_reply_chars=120, language="简体中文",
           handoff_ask="需要真人客服吗？回复「是」马上为您转接。",
           handoff_queued="已通知人工客服，请稍等。",
           extra_rules=["禁止承诺到货时间", "涉及金额一律复述两遍"])
    p = persona()
    assert (p.name, p.scene, p.tone, p.max_reply_chars) == ("小星", "母婴用品店铺客服", "亲切、耐心", 120)
    prompt = build_system_prompt()
    assert "你是「小星」，一位母婴用品店铺客服。" in prompt
    assert "单次回复不超过 120 字" in prompt
    assert "回答亲切、耐心" in prompt
    # 附加规则接在内置 5 条之后连续编号，模型对有序列表更敏感
    assert "6. 禁止承诺到货时间" in prompt and "7. 涉及金额一律复述两遍" in prompt
    assert p.handoff_ask.startswith("需要真人客服吗")
    assert p.handoff_queued == "已通知人工客服，请稍等。"


def test_handoff_strategy_patterns_come_from_config(monkeypatch):
    _agent(monkeypatch, handoff_keywords=r"(找客服|human)",
           confirm_affirm=r"^\s*(yep|确认)", confirm_negate=r"^\s*(nah|不用)")
    assert handoff_keywords().search("我要找客服")
    assert not handoff_keywords().search("投诉")        # 旧口径不再触发，说明真的换成配置了
    assert confirm_affirm().match("yep")
    assert confirm_negate().match("nah")


def test_broken_regex_falls_back_instead_of_crashing(monkeypatch):
    _agent(monkeypatch, handoff_keywords=r"(未闭合")
    p = handoff_keywords()                        # 不抛异常
    assert p.search("转人工")                     # 退回内置口径


# ---- runner 集成：状态机用的是配置口径 ----

def test_runner_uses_configured_keyword_and_ask(client, db, monkeypatch):
    """配置改口径后，触发词与询问文案都跟着变——这条是"配置真的接进了状态机"的证据。"""
    import asyncio

    from app.db.models import Conversation, User

    _agent(monkeypatch, handoff_keywords=r"(找真人)", handoff_ask="要为您转真人吗？")
    user = User(username="persona_owner", hashed_password="x", role="user")
    db.add(user)
    db.commit()
    conv = Conversation(user_id=user.id, title="人设用例")
    db.add(conv)
    db.commit()

    async def collect(text: str):
        return [e async for e in runner.run_turn(db, conv, user, text)]

    events = asyncio.run(collect("我要找真人"))
    assert ("delta", {"text": "要为您转真人吗？"}) in events
    assert ("handoff", {"status": "awaiting_confirm", "ticket_id": None}) in events
    db.refresh(conv)
    assert conv.handoff_status == "awaiting_confirm"

    # 旧关键词在改口径后不再触发状态机：买家说"转人工"不会再进确认询问态
    conv.handoff_status = "bot"
    db.add(conv)
    db.commit()
    events2 = asyncio.run(collect("转人工"))
    assert not any(e[0] == "handoff" and e[1].get("status") == "awaiting_confirm" for e in events2)
    # 没配模型 key 时这一轮走到模型链，给出可读错误而不是静默空回答
    assert events2[-1][0] in ("error", "done")
