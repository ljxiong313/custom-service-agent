"""系统状态接口：设置页展示运行能力总览（模型 key、企微 mock、队列与外发留痕）、
用量汇总（/usage）与告警清单（/alerts）。

只返回布尔与计数，绝不回显任何密钥内容；/usage 与 /alerts 是运营视角，限 staff+。
"""
from __future__ import annotations

import time

from fastapi import APIRouter

from app.core.security import CurrentUser, DbSession, RequireStaff

router = APIRouter(prefix="/system", tags=["system"])


@router.get("/status")
def system_status(user: CurrentUser, session: DbSession):
    from app import startup_check
    from app.core.models import manager
    from app.core.models.manager import is_configured
    from app.services import knowledge_jobs, lease, outbox
    from app.services.mcp.confirm import pending_count as mcp_pending_count
    from app.services.memory import pipeline
    from app.services.wecom import client

    jobs = knowledge_jobs.status_counts(session)
    memory_jobs = pipeline.status_counts(session)
    sends = outbox.count_by_status()
    return {
        "model_ready": is_configured(),
        # 四类能力分开报：套餐类 key 常只放行对话模型，向量化不可用会让检索静默降级 BM25
        "capabilities": {k: manager.capability_ready(k) for k in
                         ("chat", "embedding", "vision", "rerank")},
        "wecom_mock": client.is_mock(),
        # 队列积压/中断：重启打断的重建任务会停在 interrupted，页面上要能看见
        "ingest_jobs": {"queued_running": jobs.get("queued", 0) + jobs.get("running", 0),
                        "done": jobs.get("done", 0), "failed": jobs.get("failed", 0),
                        "interrupted": jobs.get("interrupted", 0)},
        # 记忆管道同样有队列（B3-8 落库后重启不丢，但要看得见积压）
        "memory_jobs": {"queued": memory_jobs.get("queued", 0),
                        "running": memory_jobs.get("running", 0),
                        "done": memory_jobs.get("done", 0),
                        "failed": memory_jobs.get("failed", 0)},
        # 后台任务租约：多副本部署时确认只有一家在扫描/补跑
        "leases": lease.all_leases(),
        # 外发没送达的两种形态：mock（演示预期）与 skipped/failed（配置没齐或真失败）
        "outbox": {"total": sum(sends.values()),
                   "mocked": sends.get("mocked", 0), "sent": sends.get("sent", 0),
                   "skipped": sends.get("skipped", 0), "failed": sends.get("failed", 0),
                   "by_channel": outbox.count_by_channel()},
        "mcp_pending_confirm": mcp_pending_count(),
        "security_findings": [f for f in startup_check.run_checks() if f["level"] != "ok"],
    }


@router.get("/usage")
def system_usage(user: RequireStaff, session: DbSession, days: int = 7):
    """模型用量与成本汇总（设置页看板用）。

    口径：estimated=False 的行是模型上报的 token，True 的是本地估算（embedding/rerank
    接口不回报用量）。价格没配 llm.pricing 时 cost_est 为 None，页面必须显示"未配价"
    而不是 0 元——把未知当零是最容易被戳穿的报表错误。
    """
    import datetime

    from app.db.models import utcnow
    from app.services import usage

    since = utcnow() - datetime.timedelta(days=max(1, min(days, 90)))
    return usage.summary(session, since=since)


@router.get("/alerts")
def system_alerts(user: RequireStaff, session: DbSession):
    """当前需要人看一眼的事项：安全配置、模型错误率、外发失败、任务中断、卡住的确认卡片。

    没有告警面的系统等于把"静默失败"留给客户先发现——回调验签被关掉、外发一直 skipped、
    模型在报错降级，这些在日志里都能查到，但没人会去翻日志。
    """
    import datetime

    from sqlalchemy import func as sa_func
    from sqlmodel import col, select

    from app import startup_check
    from app.db.models import IngestJob, LlmCall, OutboxRecord, utcnow
    from app.services import outbox
    from app.services.mcp import confirm

    items: list[dict[str, str]] = []
    for f in startup_check.run_checks():
        if f["level"] != "ok":
            items.append({"key": f["key"], "level": "fatal" if f["level"] == "fatal" else "warn",
                          "message": f["message"]})

    day_ago = utcnow() - datetime.timedelta(hours=24)
    counts = dict(session.exec(
        select(LlmCall.status, sa_func.count()).where(LlmCall.created_at >= day_ago)
        .group_by(col(LlmCall.status))).all())
    bad = int(counts.get("error", 0)) + int(counts.get("interrupted", 0))
    total = sum(int(v or 0) for v in counts.values())
    if total and bad:
        items.append({"key": "llm_error_rate",
                      "level": "error" if bad / max(total, 1) > 0.1 else "warn",
                      "message": f"最近 24h 模型外呼 {total} 次，其中 {bad} 次失败或被中断"})

    sends = dict(session.exec(
        select(OutboxRecord.status, sa_func.count()).where(OutboxRecord.created_at >= day_ago)
        .group_by(col(OutboxRecord.status))).all())
    if int(sends.get("failed", 0)):
        # 失败不再是一条死记录：outbound_retry 按 15s/60s/240s 退避重投，
        # 重投次数用尽（next_retry_at 为空且已试过）的行才是"只能人工补发"的死信。
        dead = outbox.dead_letter_count(since=day_ago, session=session)
        waiting = int(session.exec(
            select(sa_func.count()).select_from(OutboxRecord)
            .where(OutboxRecord.status == "failed",
                   col(OutboxRecord.next_retry_at).is_not(None),
                   OutboxRecord.created_at >= day_ago)).one() or 0)
        message = (f"最近 24h 有 {sends['failed']} 条外发失败（买家没收到回复），"
                   f"其中 {dead} 条已进入死信（退避重试用尽，需人工补发）")
        if waiting:
            message += f"、{waiting} 条仍在退避重试中"
        items.append({"key": "outbox_failed",
                      # 还有救的失败别按 error 惊叫：只有死信（或从没进队列的）才抬到 error
                      "level": "error" if dead or not waiting else "warn",
                      "message": message + "，见 GET /api/v1/platforms/outbox"})
    if int(sends.get("skipped", 0)):
        items.append({"key": "outbox_skipped", "level": "warn",
                      "message": f"最近 24h 有 {sends['skipped']} 条外发被跳过"
                                 "（平台出站参数没配齐，见 /platforms/readiness）"})

    stuck = session.exec(select(sa_func.count()).select_from(IngestJob).where(
        col(IngestJob.status).in_(("interrupted", "queued", "running")))).one()
    if int(stuck or 0):
        items.append({"key": "ingest_jobs", "level": "warn",
                      "message": f"{stuck} 个知识库任务处于排队/运行/中断态，请核对是否卡住"})

    aged = [p for p in confirm.pending_list()
            if time.time() - p["created_at"] > 60]
    if aged:
        items.append({"key": "mcp_confirm_aged", "level": "warn",
                      "message": f"{len(aged)} 张 MCP 确认卡片等待超过 60s，"
                                 "超过 180s 会被自动按拒绝处理"})

    return {"generated_at": utcnow(), "count": len(items), "items": items}
