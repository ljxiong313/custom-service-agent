"""模型外呼计量与用量汇总（B3-7）。

为什么要落库而不是内存计数器：多副本部署下每个进程各数各的，重启即归零，
而"上个月哪个模型花了多少钱"恰恰是只在历史里存在的事实。写失败也绝不外抛——
计量是旁路，不能让一次记账失败把买家的对话打断。

`track()` 是给调用点用的上下文：正常退出记 ok，异常记 error，生成器被客户端断开
（SSE 断连会抛 GeneratorExit/CancelledError）记 interrupted，三种都要留下。
"""
from __future__ import annotations

import datetime
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from loguru import logger
from sqlmodel import Session, col, func, select

from app.config import get_settings
from app.db.models import LlmCall, utcnow
from app.db.session import engine


def estimate_tokens(text: str) -> int:
    """粗估 token 数（CJK 按字符、其余按 4 字符≈1 token）。

    和 memory/short.py 的滑窗口径一致：embedding/rerank 接口不回报用量，
    只有这种估算值可用，所以一律配 estimated=True 落库。
    """
    if not text:
        return 0
    cjk = sum(1 for ch in text if "一" <= ch <= "鿿")
    other = len(text) - cjk
    return cjk + max(1, other // 4)


def cost_of(provider: str, model: str, input_tokens: int, output_tokens: int) -> float | None:
    """按 llm.pricing.<provider>.<model> 估算金额；未配价返回 None（不谎报 0 元）。"""
    pricing = get_settings().get_dotted("llm.pricing", None)
    if not isinstance(pricing, dict):
        return None
    table = pricing.get(provider)
    if not isinstance(table, dict):
        return None
    rates = table.get(model) or table.get("default")
    if not isinstance(rates, dict):
        return None
    try:
        pin = float(rates.get("input_per_1k", 0) or 0)
        pout = float(rates.get("output_per_1k", 0) or 0)
    except (TypeError, ValueError):
        return None
    return round(input_tokens / 1000 * pin + output_tokens / 1000 * pout, 6)


def record(call: LlmCall) -> None:
    """写一条用量流水；任何异常只告警不上抛。"""
    try:
        with Session(engine) as session:
            session.add(call)
            session.commit()
    except Exception as e:  # pragma: no cover - 记账失败不该影响业务
        logger.warning("用量记账失败（不影响对话）: {}", e)


@contextmanager
def track(kind: str, *, provider: str = "", model: str = "", conversation_id: int | None = None,
          user_id: int | None = None, attempts: int = 1) -> Iterator[dict[str, Any]]:
    """包裹一次模型调用。

    调用点在 with 体内填 state：
      state["usage"]        = langchain 的 usage_metadata dict（有则按上报值记）
      state["inputs"]       = [文本...]（无上报值时按估算记）
      state["outputs"]      = [文本...]
      state["attempts"]     = 实际尝试次数（覆盖传参）
    """
    state: dict[str, Any] = {"usage": None, "inputs": [], "outputs": [], "attempts": attempts}
    started = time.monotonic()
    status = "ok"
    error: str | None = None
    try:
        yield state
    except GeneratorExit:
        status, error = "interrupted", "客户端断连或任务取消"
        raise
    except BaseException as e:
        status, error = "error", str(e)[:300] or type(e).__name__
        raise
    finally:
        usage = state.get("usage") or {}
        tin = int(usage.get("input_tokens") or 0)
        tout = int(usage.get("output_tokens") or 0)
        total = int(usage.get("total_tokens") or 0) or tin + tout
        estimated = not bool(usage)
        if estimated:
            tin = sum(estimate_tokens(t) for t in state.get("inputs") or [])
            tout = sum(estimate_tokens(t) for t in state.get("outputs") or [])
            total = tin + tout
        record(LlmCall(
            kind=kind, provider=provider, model=model,
            conversation_id=conversation_id, user_id=user_id,
            input_tokens=tin, output_tokens=tout, total_tokens=total,
            estimated=estimated, cost_est=cost_of(provider, model, tin, tout),
            latency_ms=int((time.monotonic() - started) * 1000),
            attempts=int(state.get("attempts") or attempts),
            status=status, error=error))


def summary(session: Session, since: datetime.datetime | None = None) -> dict:
    """按 kind/model 汇总用量、错误率与成本；since 缺省统计最近 7 天。"""
    since = since or (utcnow() - datetime.timedelta(days=7))
    rows = session.exec(
        # 十个列的 select() 没有对应的 stub 重载（SQLAlchemy 只标到 4 个），
        # 与仓库里其它 SQLModel 查询一样按行 ignore，不改变运行时行为。
        select(LlmCall.kind, LlmCall.provider, LlmCall.model,  # type: ignore[call-overload]
               func.count(), func.sum(LlmCall.input_tokens), func.sum(LlmCall.output_tokens),
               func.sum(LlmCall.total_tokens), func.sum(LlmCall.cost_est),
               func.avg(LlmCall.latency_ms),
               func.sum(col(LlmCall.status) != "ok"))
        .where(LlmCall.created_at >= since)
        .group_by(col(LlmCall.kind), col(LlmCall.provider), col(LlmCall.model))
    ).all()
    items = [{
        "kind": k, "provider": p, "model": m, "calls": int(c or 0),
        "input_tokens": int(i or 0), "output_tokens": int(o or 0), "total_tokens": int(t or 0),
        "cost_est": (None if cost is None else round(float(cost), 6)),
        "avg_latency_ms": round(float(avg_lat or 0), 1),
        "not_ok": int(bad or 0),
    } for k, p, m, c, i, o, t, cost, avg_lat, bad in rows]
    totals = {
        "calls": sum(x["calls"] for x in items),
        "input_tokens": sum(x["input_tokens"] for x in items),
        "output_tokens": sum(x["output_tokens"] for x in items),
        "total_tokens": sum(x["total_tokens"] for x in items),
        "cost_est": (None if all(x["cost_est"] is None for x in items)
                     else round(sum(x["cost_est"] or 0 for x in items), 6)),
        "not_ok": sum(x["not_ok"] for x in items),
    }
    return {"since": since, "items": items, "totals": totals,
            "pricing_configured": bool(get_settings().get_dotted("llm.pricing", None))}


def prometheus_text(session: Session, extra: dict[str, float] | None = None) -> str:
    """Prometheus 文本格式：LLM 用量来自库表，进程类指标由调用方以 extra 传入。"""
    lines: list[str] = [
        "# HELP cs_llm_calls_total 模型外呼次数（按 kind/provider/model）",
        "# TYPE cs_llm_calls_total counter",
        "# HELP cs_llm_tokens_total 模型 token 用量（direction=input|output）",
        "# TYPE cs_llm_tokens_total counter",
        "# HELP cs_llm_cost_estimate_total 按 llm.pricing 估算的成本",
        "# TYPE cs_llm_cost_estimate_total counter",
        "# HELP cs_llm_not_ok_total 非成功外呼（error/interrupted）",
        "# TYPE cs_llm_not_ok_total counter",
    ]

    def sample(metric: str, labels: dict[str, str], value: float) -> None:
        lbl = ",".join(f'{k}="{_escape(v)}"' for k, v in labels.items())
        lines.append(f"{metric}{{{lbl}}} {value}")

    s = summary(session)
    for x in s["items"]:
        labels = {"kind": x["kind"], "provider": x["provider"] or "unknown", "model": x["model"]}
        sample("cs_llm_calls_total", labels, float(x["calls"]))
        sample("cs_llm_not_ok_total", labels, float(x["not_ok"]))
        sample("cs_llm_cost_estimate_total", labels, float(x["cost_est"] or 0))
        sample("cs_llm_tokens_total", {**labels, "direction": "input"}, float(x["input_tokens"]))
        sample("cs_llm_tokens_total", {**labels, "direction": "output"}, float(x["output_tokens"]))
    for name, value in (extra or {}).items():
        lines.append(f"# TYPE cs_{name} gauge")
        lines.append(f"cs_{name} {value}")
    return "\n".join(lines) + "\n"


def _escape(v: str) -> str:
    return str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
