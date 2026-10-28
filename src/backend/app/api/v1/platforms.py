"""平台渠道接口。

两类挂载（见 main.py）：
    /api/platform/{name}/callback      —— 免 JWT（平台服务器无法携带我们的 token），
                                          验签 → 解析 → 后台自动回复 → 返回平台要求的 ack
    /api/v1/platforms/*                —— JWT 保护的管理/演示接口（状态总览、外发箱、就绪自检）
"""
from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request, status
from sqlmodel import func, select

from app.core.security import DbSession, RequireStaff
from app.db.models import ExternalConversation
from app.services import outbox
from app.services.platforms import adapters as _adapters  # noqa: F401  触发注册
from app.services.platforms.base import (
    ADAPTERS,
    PLATFORM_ORDER,
    conf,
    get_adapter,
    is_mock,
    outbox_records,
    public_base_url,
    readiness,
)
from app.services.platforms.inbound import handle_inbound

# ---- 回调（免 JWT，挂在 /api 下） ----

callback_router = APIRouter(prefix="/platform", tags=["platform-callback"])


@callback_router.post("/{platform}/callback")
async def platform_callback(platform: str, request: Request, background: BackgroundTasks):
    adapter = get_adapter(platform)
    if adapter is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"未接入的平台: {platform}")
    if not bool(conf(platform).get("enabled", False)):
        # enabled 是"这个渠道是否接入"的总闸：关掉后连验签都不做，避免误认为已停用
        raise HTTPException(status.HTTP_409_CONFLICT,
                            f"平台 {platform} 未启用（platforms.{platform}.enabled=false）")

    query = dict(request.query_params)
    body = await request.body()
    headers = {k.lower(): v for k, v in request.headers.items()}
    if not adapter.verify_signature(query, body, headers):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "回调验签失败")

    msg = adapter.parse_message(query, body)
    if msg is not None:
        # 快速 ack + 后台生成回复：平台推送超时重推时，同一 msg_id 由
        # inbound.claim_message 按 (platform, shop, buyer, msg_id) 幂等丢弃
        background.add_task(handle_inbound, msg)
    ack = conf(platform).get("ack", {"code": 0, "msg": "success"})
    if isinstance(ack, str):
        return ack
    return ack


# ---- 管理/演示（JWT，挂在 /api/v1 下） ----

admin_router = APIRouter(prefix="/platforms", tags=["platform"])


@admin_router.get("/status")
def platforms_status(user: RequireStaff, session: DbSession):
    """各平台接入配置概览（只回显布尔与网关地址，绝不回显密钥）。"""
    counts = dict(session.exec(
        select(ExternalConversation.platform, func.count())
        .group_by(ExternalConversation.platform)
    ).all())
    out = []
    for name in PLATFORM_ORDER:
        c = conf(name)
        state = readiness(name)
        out.append({"platform": name, "enabled": bool(c.get("enabled", False)),
                    "mock": is_mock(name), "has_credentials": bool(c.get("app_key")),
                    "gateway": c.get("gateway", ""), "conversations": counts.get(name, 0),
                    "ready_in": state["ready_in"], "ready_out": state["ready_out"]})
    return out


@admin_router.get("/readiness")
def platforms_readiness(user: RequireStaff):
    """出站/入站就绪度静态自检：缺哪个配置项、影响哪条链路（不联网、不回显密钥值）。"""
    items = [readiness(name) for name in PLATFORM_ORDER]
    blocked = sorted({k for i in items for k in i["missing_out"]})
    # ★ 待核对口径：与"配没配"无关——值填了也可能是平台改版后的旧口径，必须人工过一遍文档
    review_keys = sorted({k for i in items for k in i["needs_review_keys"]})
    return {
        "platforms": items,
        "summary": {
            "total": len(items),
            "ready_in": sum(1 for i in items if i["ready_in"]),
            "ready_out": sum(1 for i in items if i["ready_out"]),
            "ready_media": sum(1 for i in items if i["ready_media"]),
            "all_mock": all(i["mock"] for i in items),
            "risks": sum(len([r for r in i["risks"] if r["level"] != "info"]) for i in items),
            # 接真实平台时要补的配置项全集：全部补齐才可能 ready_out
            "missing_keys": blocked,
            "needs_review_keys": review_keys,
            "public_base_url_configured": bool(public_base_url()),
            "outbox_counts": outbox.count_by_channel(),
            # 死信：退避重试用尽仍失败的外发，买家侧等于没回复，只能人工补发
            "outbox_retry": outbox.count_retry_queue(),
            "hint": ("mock=true 时链路可演示但不送达买家；"
                     "填齐 missing_keys 并把 mock 改为 false 才会真实外呼（见 docs/平台接入.md 第 7 节）"
                     if blocked else "各平台出站参数已齐，可将 mock 置为 false 真实外呼"),
        },
    }


@admin_router.get("/outbox")
def platforms_outbox(user: RequireStaff, limit: int = 50):
    """外发箱：mock 送达回执与 skipped/failed 留痕（B1-2 起落库，重启不丢）。"""
    per_page = max(1, min(limit, 200))
    return {name: outbox_records(name, per_page) for name in ADAPTERS}
