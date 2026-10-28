"""外发箱落库（B1-2）+ 出站失败退避重试（本模块第二段）。

企微卡片与各平台 mock 回复原来只存在进程内存 list 里，重启即丢、运营排查不到历史；
现统一写入 OutboxRecord 表。两个管理接口（/handoff/outbox、/platforms/outbox）的响应
仍按 payload_json 原样还原旧结构，前端不改。

失败重试口径（只服务平台通道，企微卡片不在自动重试范围内）：
- 平台 outbound 抛异常 → status=failed，attempts=1，next_retry_at=现在+第 1 档退避；
- `retry_due()` 取到期行 → `claim_for_retry()` 抢行（原子加 attempts、清空 next_retry_at），
  重试执行见 services/platforms/outbound_retry.py；
- 重试仍失败 → 按 attempts 取下一档退避； attempts 用尽（MAX_ATTEMPTS）→ next_retry_at 留空，
  这行就进入**死信**：`dead_letter_count()` 统计它，/api/v1/system/alerts 报警给 human。
时间一律 `datetime.now(datetime.UTC)`：SQLModel 的 UTCDateTime 拒绝 naive datetime。
"""
from __future__ import annotations

import datetime
import json

import sqlalchemy as sa
from loguru import logger
from sqlalchemy import func, text
from sqlmodel import Session, col, select

from app.db.models import OutboxRecord
from app.db.session import engine

WECOM_CHANNEL = "wecom"

# 平台通道才有适配器可供重投；wecom 走 services/wecom/client.py 自己的发送逻辑，
# 失败行 attempts 恒为 0，因此既不会被重试，也不会被误算成死信。
RETRYABLE_CHANNELS = ("taobao", "jd", "pdd", "douyin")

# 第 n 次失败后等多久再试（15s → 60s → 240s）；档数用完即死信，不再无限重试打平台
BACKOFF_SECONDS: tuple[int, ...] = (15, 60, 240)
MAX_ATTEMPTS = len(BACKOFF_SECONDS) + 1      # 首次 + 3 次重试 = 最多外呼 4 次


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


# 裸 SQL 绑定的时间必须用列自己的类型，否则走 sqlite3 默认适配器，写出的时间串
# 与 ORM 写的格式不一致 → 下一轮比较读错（同 services/lease.py 的教训）。
_NEXT_RETRY = OutboxRecord.__table__.columns["next_retry_at"].type  # type: ignore[attr-defined]


def retry_deadline(attempts: int, now: datetime.datetime | None = None) -> datetime.datetime | None:
    """第 attempts 次失败之后什么时候再试；次数用尽返回 None（= 死信）。"""
    if attempts < 1:
        return None
    index = attempts - 1
    if index >= len(BACKOFF_SECONDS):
        return None
    return (now or _now()) + datetime.timedelta(seconds=BACKOFF_SECONDS[index])


def _view(row: OutboxRecord) -> dict:
    """还原调用方放进 payload 的原始报文形状，再补上落库才有的状态字段。"""
    try:
        data = json.loads(row.payload_json or "{}")
    except json.JSONDecodeError:
        logger.warning("外发记录#{} payload 非 JSON，按空对象返回", row.id)
        data = {}
    if not isinstance(data, dict):
        data = {"payload": data}
    data.update({"id": row.id, "channel": row.channel, "kind": row.kind,
                 "to": row.to, "status": row.status, "error": row.error,
                 "attempts": row.attempts, "last_error": row.last_error,
                 "next_retry_at": row.next_retry_at.isoformat() if row.next_retry_at else None,
                 "created_at": row.created_at.isoformat() if row.created_at else None})
    return data


def record(channel: str, kind: str, to: str = "", title: str = "", body: str = "",
           payload: dict | None = None, status: str = "mocked",
           error: str | None = None) -> dict:
    """记一条出站消息；落库失败只记日志（外发状态不能反过来打断业务主流程）。

    status=failed 且渠道可重试时，这里就是**首次失败**：attempts 记 1，并按第一档退避
    排入重试队列（重试执行见 platforms/outbound_retry.py）。mocked/sent/skipped 不进队列。
    """
    payload = payload if isinstance(payload, dict) else {}
    attempts = 1 if (status == "failed" and channel in RETRYABLE_CHANNELS) else 0
    row = OutboxRecord(channel=channel, kind=kind, to=to or "", title=title or "",
                       body=(body or "")[:8000],
                       payload_json=json.dumps(payload, ensure_ascii=False, default=str),
                       status=status, error=error, attempts=attempts,
                       next_retry_at=retry_deadline(attempts),
                       last_error=error if attempts else None)
    try:
        with Session(engine) as session:
            session.add(row)
            session.commit()
            session.refresh(row)
    except Exception as e:
        logger.error("外发箱落库失败 channel={} status={}: {}", channel, status, e)
        return {**payload, "id": None, "channel": channel, "kind": kind, "to": to,
                "status": status, "error": error, "attempts": attempts,
                "next_retry_at": None, "last_error": error, "created_at": None}
    return _view(row)


# ---- 重试队列 ----

def retry_due(limit: int = 20, now: datetime.datetime | None = None) -> list[dict]:
    """到期待重试的失败行（按到期时间正序）；取到不等于认领，见 claim_for_retry。"""
    now = now or _now()
    with Session(engine) as session:
        rows = session.exec(
            select(OutboxRecord)
            .where(OutboxRecord.status == "failed",
                   col(OutboxRecord.next_retry_at).is_not(None),
                   col(OutboxRecord.next_retry_at) <= now)
            .order_by(col(OutboxRecord.next_retry_at))
            .limit(limit)
        ).all()
    return [_view(r) for r in rows]


def claim_for_retry(row_id: int, now: datetime.datetime | None = None) -> int | None:
    """原子认领一行：attempts+1 并清空 next_retry_at（本轮由谁重试，谁说了算）。

    抢到返回认领后的 attempts（调用方据此算下一档退避），None 表示没抢到——
    别的副本/别的轮次已经在做，或状态在期间被改走（人工补发、行被清理）。
    租约选主已经保证同一时刻只有一个持约者在重试，这里防的是租约抖动那一瞬间的双持。
    """
    stmt = text("UPDATE outboxrecord SET attempts=attempts+1, next_retry_at=NULL "
                "WHERE id=:id AND status='failed' AND next_retry_at IS NOT NULL "
                "AND next_retry_at <= :now").bindparams(
        sa.bindparam("now", value=now or _now(), type_=_NEXT_RETRY))
    with engine.begin() as conn:
        if int(conn.execute(stmt, {"id": row_id}).rowcount or 0) != 1:
            return None
        return int(conn.execute(text("SELECT attempts FROM outboxrecord WHERE id=:id"),
                                {"id": row_id}).scalar() or 0)


def finish_retry(row_id: int, attempts: int, *, ok: bool, error: str | None = None,
                 now: datetime.datetime | None = None) -> None:
    """写回一次重试的结果。

    成功：status=sent、退避清空，**保留原始 error 文案**（"第一次为什么没发出去"是排查线索，
    attempts 说明第几次成功的）。失败：仍在退避窗口内就排下一次，次数用尽则 next_retry_at
    留空 = 死信，等人工。
    """
    with Session(engine) as session:
        row = session.get(OutboxRecord, row_id)
        if row is None:
            logger.warning("外发重试#{} 已不在库里（被清理？），跳过状态回写", row_id)
            return
        row.attempts = attempts
        if ok:
            row.status = "sent"
            row.next_retry_at = None
            row.last_error = None
        else:
            row.status = "failed"
            row.next_retry_at = retry_deadline(attempts, now)
            row.last_error = (error or "")[:2000] or row.last_error
        session.add(row)
        session.commit()


def stop_retry(row_id: int, *, reason: str) -> None:
    """重试前提没了（渠道切回 mock、凭据被收回、历史行缺参数）：停止排队并说明原因。

    这类行的最终状态仍是 failed（买家确实没收到），只是不再自动打平台，等人工处理。
    """
    with Session(engine) as session:
        row = session.get(OutboxRecord, row_id)
        if row is None:
            return
        row.status = "failed"
        row.next_retry_at = None
        row.last_error = reason[:2000]
        session.add(row)
        session.commit()


def count_retry_queue() -> dict[str, int]:
    """失败行的三种去向：还在退避 / 死信 / 从未进队列（wecom 与历史行）。"""
    with Session(engine) as session:
        waiting = int(session.exec(
            select(func.count()).select_from(OutboxRecord)
            .where(OutboxRecord.status == "failed",
                   col(OutboxRecord.next_retry_at).is_not(None))).one() or 0)
        dead = dead_letter_count(session=session)
        untracked = int(session.exec(
            select(func.count()).select_from(OutboxRecord)
            .where(OutboxRecord.status == "failed",
                   col(OutboxRecord.next_retry_at).is_(None),
                   OutboxRecord.attempts == 0)).one() or 0)
    return {"waiting": waiting, "dead": dead, "untracked": untracked}


def dead_letter_count(since: datetime.datetime | None = None,
                      session: Session | None = None) -> int:
    """死信条数：failed 且已尝试过（attempts>0）但 next_retry_at 为空。

    为什么要带 attempts>0：wecom 卡片失败行、以及补列之前的历史 failed 行从来没有进过
    重试队列（attempts=0），把它们算成"重试用尽"会让告警数字说谎。
    """
    stmt = (select(func.count()).select_from(OutboxRecord)
            .where(OutboxRecord.status == "failed",
                   col(OutboxRecord.next_retry_at).is_(None),
                   OutboxRecord.attempts > 0))
    if since is not None:
        stmt = stmt.where(OutboxRecord.created_at >= since)
    if session is not None:
        return int(session.exec(stmt).one() or 0)
    with Session(engine) as own:
        return int(own.exec(stmt).one() or 0)


def recent(channel: str, limit: int = 50) -> list[dict]:
    """按渠道取最近若干条，时间正序（与旧内存 list 的 [-50:] 顺序一致）。"""
    with Session(engine) as session:
        rows = session.exec(
            select(OutboxRecord)
            .where(OutboxRecord.channel == channel)
            .order_by(col(OutboxRecord.id).desc())
            .limit(limit)
        ).all()
    return [_view(r) for r in reversed(list(rows))]


def count_by_channel() -> dict[str, int]:
    with Session(engine) as session:
        rows = session.exec(
            select(OutboxRecord.channel, func.count())
            .group_by(col(OutboxRecord.channel))
        ).all()
    return {str(channel): int(total) for channel, total in rows}


def count_by_status() -> dict[str, int]:
    """skipped/failed 不为 0 就说明"以为发出去了、其实没发"，设置页据此提示。"""
    with Session(engine) as session:
        rows = session.exec(
            select(OutboxRecord.status, func.count())
            .group_by(col(OutboxRecord.status))
        ).all()
    return {str(status): int(total) for status, total in rows}
