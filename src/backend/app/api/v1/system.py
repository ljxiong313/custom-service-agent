"""系统状态接口：设置页展示运行能力总览（模型 key、企微 mock、队列与外发留痕）。

只返回布尔与计数，绝不回显任何密钥内容。
"""
from __future__ import annotations

from fastapi import APIRouter

from app.core.security import CurrentUser, DbSession

router = APIRouter(prefix="/system", tags=["system"])


@router.get("/status")
def system_status(user: CurrentUser, session: DbSession):
    from app.core.models.manager import is_configured
    from app.services import knowledge_jobs, outbox
    from app.services.mcp.confirm import pending_count as mcp_pending_count
    from app.services.wecom import client

    jobs = knowledge_jobs.status_counts(session)
    sends = outbox.count_by_status()
    return {
        "model_ready": is_configured(),
        "wecom_mock": client.is_mock(),
        # 队列积压/中断：重启打断的重建任务会停在 interrupted，页面上要能看见
        "ingest_jobs": {"queued_running": jobs.get("queued", 0) + jobs.get("running", 0),
                        "done": jobs.get("done", 0), "failed": jobs.get("failed", 0),
                        "interrupted": jobs.get("interrupted", 0)},
        # 外发没送达的两种形态：mock（演示预期）与 skipped/failed（配置没齐或真失败）
        "outbox": {"total": sum(sends.values()),
                   "mocked": sends.get("mocked", 0), "sent": sends.get("sent", 0),
                   "skipped": sends.get("skipped", 0), "failed": sends.get("failed", 0),
                   "by_channel": outbox.count_by_channel()},
        "mcp_pending_confirm": mcp_pending_count(),
    }
