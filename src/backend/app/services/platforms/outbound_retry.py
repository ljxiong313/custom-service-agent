"""出站失败的退避重试：把外发箱里 status=failed 的行按 15s/60s/240s 重投，次数用尽成死信。

为什么需要：平台网关 5xx、超时是常态，而 base.guarded_send 对失败的处理是"落一条 failed +
抛给调用方"，调用方（deliver_notice / deliver_media / handle_inbound）都不会重投——买家那条
回复就此永久丢失，外发箱只留下一行没人再看的 failed。这里补上重投与"重投也没用"的可见性。

口径：
- 只重投平台通道（outbox.RETRYABLE_CHANNELS）的 text/media 行；重投参数（shop_id/buyer_id/
  url/media_type）由 base.record_out 写进 payload，缺参数的历史行直接停止重试并说明原因；
- 每次重投前复查前提：渠道切回 mock、凭据被收回、附件链接仍是相对路径——这些情况下重投
  只会写出 mocked/skipped 行，买家照样收不到，所以判定为"停止自动重试"（status 保持 failed）；
- 重投走 adapter.call_reply/call_media，**不新增外发记录**，结果写回原行：否则一次故障期间
  的重试会把外发箱刷出几十条同源记录，死信数与告警都会被自己造出来的行污染；
- 退避与次数上限在 outbox.py（BACKOFF_SECONDS/MAX_ATTEMPTS），这里只负责"怎么重投"。

与 session_sweeper 同一套部署口径：后台循环用 workerlease 选主，多副本只有一家真正在重投；
行级抢占（outbox.claim_for_retry）兜住租约抖动那一瞬间可能出现的两个持约者。
"""
from __future__ import annotations

import asyncio
import datetime

from loguru import logger

from app.config import get_settings
from app.services import outbox

# adapters 的导入是**功能性**的：它把四平台适配器注册进 ADAPTERS，get_adapter() 才有货。
# 后台重试任务完全可能跑在 API 层之前（单测/脚本直接驱动时也是），不能指望别人帮我们导入。
from . import adapters  # noqa: F401
from .base import (
    conf,
    get_adapter,
    is_mock,
    missing_out_config,
    unreachable_attachment,
)

# 扫描周期：比最短退避（15s）不小太多即可，再密也只是空转查库
DEFAULT_INTERVAL_SECONDS = 15
LEASE_TTL_SECONDS = 90


def _now() -> datetime.datetime:
    # 带 tzinfo 的 UTC：SQLModel 的 UTCDateTime 列拒绝 naive datetime（同 lease.py）
    return datetime.datetime.now(datetime.UTC)


def interval_seconds() -> int:
    """重投扫描周期（platforms.retry_interval_seconds，缺省 15s）。"""
    return int(get_settings().get_dotted("platforms.retry_interval_seconds",
                                         DEFAULT_INTERVAL_SECONDS))


def retry_text_of(row: dict) -> str:
    """原正文：payload 里的 text 优先，退化到 body 列（历史行 payload 形状不完全一致）。"""
    return str(row.get("text") or "")


def stop_reason(row: dict) -> str:
    """重投前提没了就返回人话说明（空串表示可以重投）。

    返回非空即"不要再自动打平台了"：这类行需要人改配置或手工补发，自动重试再多轮也只是噪音。
    """
    channel = str(row.get("channel") or "")
    if get_adapter(channel) is None:
        return f"没有 {channel} 渠道的适配器，不支持自动重试，请人工处理"
    kind = str(row.get("kind") or "text")
    if is_mock(channel):
        return ("平台已切回 mock=true：重投只会写演示记录，买家依然收不到；"
                "改回 mock=false 后请人工补发本条")
    method_key = "media_method" if kind == "media" else "reply_method"
    missing = missing_out_config(channel, conf(channel), method_key=method_key)
    if missing:
        return f"出站参数已不齐（缺 {missing}），重投只会变成 skipped，请先补配置再人工补发"
    if not retry_text_of(row):
        return "外发记录里没留正文，无法原样重投，请人工补发"
    if kind == "media":
        url = str(row.get("url") or "")
        if not url:
            return "媒体行的 payload 没记录附件地址（旧版本写入），无法自动重投，请人工补发"
        if unreachable_attachment(channel, url):
            return ("附件链接仍是相对路径（server.public_base_url 没配），买家点不开；"
                    "配好公网地址后请人工补发")
    return ""


def _call(row: dict) -> dict:
    """按渠道与 kind 重投一次真实外呼（不落新行）。"""
    adapter = get_adapter(str(row["channel"]))
    assert adapter is not None      # stop_reason 已经把过一道，这里只是给 mypy/防御
    shop_id = str(row.get("shop_id") or "")
    buyer_id = str(row.get("buyer_id") or row.get("to") or "")
    if str(row.get("kind") or "text") == "media":
        return adapter.call_media(shop_id, buyer_id, str(row.get("url") or ""),
                                  media_type=str(row.get("media_type") or "file"))
    return adapter.call_reply(shop_id, buyer_id, retry_text_of(row))


def retry_once(limit: int = 20, now: datetime.datetime | None = None) -> dict:
    """跑一轮重投，返回统计（公开供单测与冒烟脚本直接驱动）。"""
    now = now or _now()
    stats = {"due": 0, "not_claimed": 0, "resent": 0, "waiting": 0, "dead": 0, "stopped": 0}
    for row in outbox.retry_due(limit=limit, now=now):
        stats["due"] += 1
        row_id = int(row["id"])
        attempts = outbox.claim_for_retry(row_id, now=now)
        if attempts is None:
            stats["not_claimed"] += 1        # 别的副本/别的轮次已经认领
            continue
        reason = stop_reason(row)
        if reason:
            outbox.stop_retry(row_id, reason=reason)
            stats["stopped"] += 1
            logger.warning("外发#{}（{} {}）停止自动重试：{}", row_id, row.get("channel"),
                           row.get("kind"), reason)
            continue
        try:
            _call(row)
        except Exception as e:
            outbox.finish_retry(row_id, attempts, ok=False, error=str(e), now=now)
            if outbox.retry_deadline(attempts) is None:
                stats["dead"] += 1
                logger.error("外发#{}（{}）第 {} 次仍失败，进入死信等人工：{}",
                             row_id, row.get("channel"), attempts, e)
            else:
                stats["waiting"] += 1
                logger.warning("外发#{}（{}）第 {} 次失败，已排下一次退避：{}",
                               row_id, row.get("channel"), attempts, e)
            continue
        outbox.finish_retry(row_id, attempts, ok=True)
        stats["resent"] += 1
        logger.info("外发#{}（{}）第 {} 次重投成功（原始失败：{}）",
                    row_id, row.get("channel"), attempts, str(row.get("error") or "")[:120])
    return stats


async def outbox_retry_loop() -> None:
    """lifespan 后台任务：周期重投到期失败行。异常只记日志不退出（下一轮自愈）。

    重投内部是同步 DB + 平台 HTTP 外呼，必须放线程池：一次 15s 的网关超时会把同进程的
    事件循环整段卡住，SSE 对话流跟着抖（同 session_sweeper 的理由）。

    租约选主：每个周期先抢 outbox_retry 约（TTL 90s），没抢到的进程直接跳过——多副本部署
    下只有一家真正在重投，其余实例随时可以在持约者挂掉后一个 TTL 内接管。行级的
    claim_for_retry 依然保留，所以即使租约抖动出现两个持约者，同一条外发也不会被投两次。
    """
    from app.services import lease

    interval = interval_seconds()
    logger.info("外发重试任务启动（每 {}s，租约选主 holder={}）", interval, lease.HOLDER_ID)
    while True:
        try:
            if lease.acquire("outbox_retry", ttl_seconds=LEASE_TTL_SECONDS):
                await asyncio.to_thread(retry_once)
        except Exception:
            logger.exception("外发重试任务失败")
        await asyncio.sleep(interval)
