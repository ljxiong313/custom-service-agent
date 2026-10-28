"""模型外呼计量（B3-7）单测：估算口径、单价换算、track 落库分支与汇总/Prometheus 输出。

全程不打网络也不真调模型：track 要钉住的是"这次外呼以什么方式退出 → 库里那一行长什么样"，
所以用假异常和假 usage_metadata 覆盖 ok / error / 模型上报值三条分支。
"""
from __future__ import annotations

import pytest
from sqlmodel import Session, col, select

from app.config import get_settings
from app.db.models import LlmCall
from app.services import usage


def _latest(session: Session) -> LlmCall:
    row = session.exec(select(LlmCall).order_by(col(LlmCall.id).desc())).first()
    assert row is not None, "track 没有写出用量流水"
    return row


def _pricing(monkeypatch, table: dict) -> None:
    """只往 llm 段里补 pricing，其余键（超时/重试）原样保留，别把 conftest 的配置整段换掉。"""
    s = get_settings()
    monkeypatch.setitem(s, "llm", {**(s.get("llm") or {}), "pricing": table})


# ---- token 粗估：embedding/rerank 只有这一种口径 ----

def test_estimate_tokens_cjk_per_char_ascii_per_four():
    assert usage.estimate_tokens("") == 0
    assert usage.estimate_tokens("a") == 1                 # 再短也至少算 1，不能估成 0
    assert usage.estimate_tokens("abcdefgh") == 2          # 英文约 4 字符 ≈ 1 token
    # 中文按字符计，非中文余量再兜 1 个 token（max(1, n // 4)），所以这里只断区间不钉死数字
    assert 2 <= usage.estimate_tokens("你好") <= 3
    assert 4 <= usage.estimate_tokens("你好世界") <= 5
    assert usage.estimate_tokens("你好世界") > usage.estimate_tokens("你好")

    mixed = "请帮我看下这个订单 A12345678 什么时候发货"
    cjk = sum(1 for ch in mixed if "一" <= ch <= "鿿")
    other = len(mixed) - cjk
    # 混合文本只断区间（中文字符逐字 + 其余按 4 字符），不钉死具体数字
    assert cjk + other // 4 <= usage.estimate_tokens(mixed) <= cjk + other // 4 + 1
    assert usage.estimate_tokens(mixed + "，尽快") > usage.estimate_tokens(mixed)


# ---- 单价：没配价就不谎报 0 元 ----

def test_cost_of_is_none_when_pricing_not_configured():
    assert get_settings().get_dotted("llm.pricing", None) is None   # conftest 未配价
    assert usage.cost_of("dashscope", "test-chat", 1000, 1000) is None


def test_cost_of_uses_per_1k_rates(monkeypatch):
    _pricing(monkeypatch, {"dashscope": {
        "test-chat": {"input_per_1k": 0.5, "output_per_1k": 2.0}}})
    assert usage.cost_of("dashscope", "test-chat", 1000, 500) == pytest.approx(1.5)
    assert usage.cost_of("dashscope", "test-chat", 0, 0) == 0
    # provider 或 model 不在价目表里时仍是 None（而不是 0 元）
    assert usage.cost_of("openai", "test-chat", 1000, 0) is None
    assert usage.cost_of("dashscope", "test-embedding", 1000, 0) is None


# ---- track：退出方式决定 status ----

def test_track_writes_ok_row_with_estimated_tokens(db: Session):
    with usage.track("chat", provider="dashscope", model="test-chat",
                     conversation_id=7, user_id=9, attempts=2) as st:
        st["inputs"] = ["这单什么时候发货"]
        st["outputs"] = ["预计明天发货"]

    row = _latest(db)
    assert (row.kind, row.provider, row.model) == ("chat", "dashscope", "test-chat")
    assert row.status == "ok" and row.error is None
    assert row.estimated is True                      # 无上报值 → 本地估算
    assert row.input_tokens == usage.estimate_tokens("这单什么时候发货")
    assert row.output_tokens == usage.estimate_tokens("预计明天发货")
    assert row.total_tokens == row.input_tokens + row.output_tokens
    assert (row.conversation_id, row.user_id) == (7, 9)
    assert row.attempts == 2 and row.cost_est is None


def test_track_writes_error_row_and_reraises(db: Session):
    with (pytest.raises(RuntimeError, match="503"),
          usage.track("embedding", provider="dashscope", model="test-embedding") as st):
        st["inputs"] = ["要向量化的文本"]
        raise RuntimeError("上游 503 服务不可用")

    row = _latest(db)
    assert row.kind == "embedding" and row.status == "error"
    assert row.error and "503" in row.error
    assert row.estimated is True and row.input_tokens > 0


def test_track_prefers_reported_usage_metadata(db: Session):
    """模型报了量就走上报值，estimated=False——读的人必须能分辨这两类。"""
    with usage.track("chat", provider="dashscope", model="test-chat") as st:
        st["inputs"] = ["估算值用不上"]
        st["usage"] = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}

    row = _latest(db)
    assert row.estimated is False
    assert (row.input_tokens, row.output_tokens, row.total_tokens) == (10, 5, 15)


# ---- 汇总与 Prometheus 文本 ----

def test_summary_groups_by_kind_provider_model(db: Session):
    db.add(LlmCall(kind="chat", provider="dashscope", model="test-chat",
                   input_tokens=10, output_tokens=20, total_tokens=30, status="ok"))
    db.add(LlmCall(kind="chat", provider="dashscope", model="test-chat",
                   input_tokens=5, output_tokens=5, total_tokens=10,
                   status="error", error="超时"))
    db.add(LlmCall(kind="embedding", provider="dashscope", model="test-embedding",
                   input_tokens=7, total_tokens=7, cost_est=0.123, status="ok"))
    db.commit()

    s = usage.summary(db)
    by_key = {(x["kind"], x["model"]): x for x in s["items"]}
    chat = by_key[("chat", "test-chat")]
    assert chat["calls"] == 2 and chat["not_ok"] == 1
    assert (chat["input_tokens"], chat["output_tokens"], chat["total_tokens"]) == (15, 25, 40)
    emb = by_key[("embedding", "test-embedding")]
    assert emb["calls"] == 1 and emb["not_ok"] == 0 and emb["total_tokens"] == 7
    assert emb["cost_est"] == pytest.approx(0.123)
    assert s["totals"]["calls"] == 3 and s["totals"]["total_tokens"] == 47
    assert s["totals"]["not_ok"] == 1
    assert s["pricing_configured"] is False


def test_prometheus_text_emits_llm_samples_and_extra_gauges(db: Session):
    with usage.track("chat", provider="dashscope", model="test-chat") as st:
        st["inputs"] = ["问题"]
        st["outputs"] = ["回答"]
    with (pytest.raises(ValueError, match="失败"),
          usage.track("chat", provider="dashscope", model="test-chat")):
        raise ValueError("这一轮失败了")

    text = usage.prometheus_text(db, extra={"http_in_flight": 2})
    labels = 'kind="chat",provider="dashscope",model="test-chat"'
    # 样本行形如：指标{标签} 值
    assert 'cs_llm_calls_total{kind="chat"' in text
    assert f"cs_llm_calls_total{{{labels}}} 2.0" in text     # 两条流水
    assert f"cs_llm_not_ok_total{{{labels}}} 1.0" in text    # 其中一条失败
    tokens_prefix = 'cs_llm_tokens_total{' + labels + ',direction="input"} '
    line = next(x for x in text.splitlines() if x.startswith(tokens_prefix))
    assert line == tokens_prefix + f"{float(usage.estimate_tokens('问题'))}"
    assert "# TYPE cs_http_in_flight gauge" in text
    assert "cs_http_in_flight 2" in text
    assert text.endswith("\n")
