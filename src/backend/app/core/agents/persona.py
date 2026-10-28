"""客服 Agent 的 persona 与提示词组装（B3-10 起人设可配置）。

之前"小 U / 300 字 / 转人工策略"全部硬编码在源码里：换个店铺、换个品牌语气就要改代码
重新部署，这在交付时是不可接受的——同一套 Agent 逻辑要能服务不同商家的人设。

配置在 `agent` 节（见 config.simple.yaml），未配置时用下面这套内置默认值，
所以老库、老配置行为完全不变。策略项（转人工关键词、肯定/否定词）是**运行时正则**，
改动会直接影响状态机，所以 runner 读的是同一份配置而不是各自写死。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from app.config import get_settings

DEFAULT_AGENT_NAME = "小 U"
DEFAULT_MAX_REPLY_CHARS = 300

# 与旧版 runner 常量保持一致的兜底：不配置就等于以前的行为
DEFAULT_HANDOFF_KEYWORDS = r"(转人工|人工客服|找个人|真人客服|投诉)"
DEFAULT_CONFIRM_AFFIRM = (r"^\s*(嗯+|好(的|呀|啊)?|是(的|呀|吧)?|要(的)?|需要|行|可以|确认|转吧|"
                          r"麻烦你|请转|ok|okay|yes)")
DEFAULT_CONFIRM_NEGATE = r"^\s*(不|不用|不要|算了|暂时|没|no)"


@dataclass(frozen=True)
class Persona:
    name: str
    scene: str
    tone: str
    max_reply_chars: int
    language: str
    extra_rules: tuple[str, ...]
    handoff_ask: str
    handoff_queued: str


def _cfg(path: str, default):
    raw = get_settings().get_dotted(path, None)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return default
    return raw


def persona() -> Persona:
    rules = _cfg("agent.extra_rules", []) or []
    return Persona(
        name=str(_cfg("agent.name", DEFAULT_AGENT_NAME)),
        scene=str(_cfg("agent.scene", "电商场景的专业在线客服助手")),
        tone=str(_cfg("agent.tone", "简洁、友好、口语化")),
        max_reply_chars=int(_cfg("agent.max_reply_chars", DEFAULT_MAX_REPLY_CHARS)),
        language=str(_cfg("agent.language", "中文")),
        extra_rules=tuple(str(x) for x in rules),
        handoff_ask=str(_cfg("agent.handoff_ask",
                             "是否需要为您转接人工客服？回复「是」立即为您转接，"
                             "回复「否」可继续与智能助手沟通。")),
        handoff_queued=str(_cfg("agent.handoff_queued", "已为您联系人工客服，请稍候…")),
    )


def _pattern(path: str, default: str) -> re.Pattern[str]:
    """配置里的正则写错时退回默认值并告警，而不是让对话整轮崩掉。"""
    raw = str(_cfg(path, default))
    try:
        return re.compile(raw, re.IGNORECASE)
    except re.error:
        from loguru import logger

        logger.warning("配置 {} 不是合法正则，已退回默认口径", path)
        return re.compile(default, re.IGNORECASE)


def handoff_keywords() -> re.Pattern[str]:
    return _pattern("agent.handoff_keywords", DEFAULT_HANDOFF_KEYWORDS)


def confirm_affirm() -> re.Pattern[str]:
    return _pattern("agent.confirm_affirm", DEFAULT_CONFIRM_AFFIRM)


def confirm_negate() -> re.Pattern[str]:
    return _pattern("agent.confirm_negate", DEFAULT_CONFIRM_NEGATE)


CS_SYSTEM_PROMPT_TEMPLATE = """\
你是「{name}」，一位{scene}。

## 行为准则
1. 回答{tone}，单次回复不超过 {max_chars} 字，可用轻量 Markdown。
2. 只依据知识库与工具返回的信息回答商品问题；信息不足时如实说明，绝不编造价格、库存或参数。
3. 涉及退款、投诉、纠纷，或你无法正确理解/处理客户问题（连续两轮未解决、知识盲区、
   需客户确认的敏感操作）时，先用一句话询问客户「是否需要转接人工客服」，
   不要擅自建单；客户明确同意后再调用 request_handoff。
4. 不回答与产品和服务无关的敏感话题，礼貌引导回业务范围。
5. 一律使用{language}回复。
{extra}
## 工具使用策略
- 商品的描述、卖点、政策条款：调用 rag_search，回答时用 [编号] 标注引用来源；
- 价格、库存、SKU、规格等精确数字：必须调用 query_product，禁止凭检索片段或记忆猜测；
- 推荐或个性化回答前：可调用 recall_memory 了解该用户历史偏好；
- request_handoff 只有在客户上一条消息已明确同意后才允许调用（转人工两步制）；
- 工具返回"暂不可用/未找到"时如实告知，绝不编造。
"""


def build_system_prompt(extra_context: str | None = None) -> str:
    """组装最终 system prompt；extra_context 用于注入记忆/检索结果等动态内容。"""
    p = persona()
    extra = "".join(f"{i + 6}. {rule}\n" for i, rule in enumerate(p.extra_rules))
    prompt = CS_SYSTEM_PROMPT_TEMPLATE.format(
        name=p.name, scene=p.scene, tone=p.tone, max_chars=p.max_reply_chars,
        language=p.language, extra=extra + "\n" if extra else "\n")
    if extra_context:
        return f"{prompt}\n## 参考信息\n{extra_context}"
    return prompt
