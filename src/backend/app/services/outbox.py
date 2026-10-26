"""外发箱落库（B1-2）。

企微卡片与各平台 mock 回复原来只存在进程内存 list 里，重启即丢、运营排查不到历史；
现统一写入 OutboxRecord 表。两个管理接口（/handoff/outbox、/platforms/outbox）的响应
仍按 payload_json 原样还原旧结构，前端不改。
"""
from __future__ import annotations

import json

from loguru import logger
from sqlalchemy import func
from sqlmodel import Session, col, select

from app.db.models import OutboxRecord
from app.db.session import engine

WECOM_CHANNEL = "wecom"


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
                 "created_at": row.created_at.isoformat() if row.created_at else None})
    return data


def record(channel: str, kind: str, to: str = "", title: str = "", body: str = "",
           payload: dict | None = None, status: str = "mocked",
           error: str | None = None) -> dict:
    """记一条出站消息；落库失败只记日志（外发状态不能反过来打断业务主流程）。"""
    payload = payload if isinstance(payload, dict) else {}
    row = OutboxRecord(channel=channel, kind=kind, to=to or "", title=title or "",
                       body=(body or "")[:8000],
                       payload_json=json.dumps(payload, ensure_ascii=False, default=str),
                       status=status, error=error)
    try:
        with Session(engine) as session:
            session.add(row)
            session.commit()
            session.refresh(row)
    except Exception as e:
        logger.error("外发箱落库失败 channel={} status={}: {}", channel, status, e)
        return {**payload, "id": None, "channel": channel, "kind": kind, "to": to,
                "status": status, "error": error, "created_at": None}
    return _view(row)


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
