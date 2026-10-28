"""后台任务的租约选主（B3-8）：不引入 ZooKeeper/Redis，用同一张业务库表做协调。

语义只有一个：**同一时刻至多一个持约者**。抢约是一条带条件的 UPDATE
（`holder=我 OR 约已过期`）或首次 INSERT，两者都由数据库保证原子性；
持约进程被 kill 后不再续约， TTL 一到就有别的进程接管，代价是切换要等一个 TTL。

时间一律用**无时区的 UTC 墙钟**写入与比较：SQLite 的 DATETIME 存储格式不带时区，
用 tz-aware 时间做字符串比较会走 sqlite3 的默认适配器，格式和列不一致 → 下一轮读错
（同 services/lifecycle.py 的 _DT 绑定教训）。
"""
from __future__ import annotations

import datetime
import os
import socket
import uuid

from loguru import logger
from sqlalchemy import BindParameter, text
from sqlalchemy import bindparam as sa_bindparam
from sqlalchemy.exc import IntegrityError
from sqlalchemy.types import TypeEngine
from sqlmodel import Session, select

from app.db.models import WorkerLease
from app.db.session import engine

# 进程标识：主机名 + pid + 随机尾巴（同机多进程重启后 pid 复用也不会误认领旧租约）
HOLDER_ID = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"

def _utc_now() -> datetime.datetime:
    """带 tzinfo 的 UTC：SQLModel 的 UTCDateTime 列拒绝 naive datetime，
    裸 SQL 绑定也要走列自己的类型（_EXPIRES/_UPDATED），否则时间串格式与 ORM 不一致。"""
    return datetime.datetime.now(datetime.UTC)


def _bp(name: str, value: datetime.datetime, type_: TypeEngine) -> BindParameter:
    """bindparam 薄封装：裸 SQL 的时间参数必须按列自己的类型绑定，
    否则走 sqlite3 的默认适配器（Python 3.12 起已弃用），写出的时间串与 ORM 不一致。"""
    return sa_bindparam(name, value=value, type_=type_)


_EXPIRES = WorkerLease.__table__.columns["lease_expires_at"].type  # type: ignore[attr-defined]
_UPDATED = WorkerLease.__table__.columns["updated_at"].type  # type: ignore[attr-defined]


def acquire(name: str, ttl_seconds: float, holder: str = HOLDER_ID) -> bool:
    """抢到（或本来就归我）返回 True。"""
    now = _utc_now()
    expires = now + datetime.timedelta(seconds=ttl_seconds)
    stmt = text("UPDATE workerlease SET holder=:h, lease_expires_at=:e, epoch=epoch+1, updated_at=:n "
                "WHERE name=:n2 AND (holder=:h OR lease_expires_at IS NULL OR lease_expires_at < :n)"
                ).bindparams(_bp("e", expires, _EXPIRES), _bp("n", now, _UPDATED))
    with engine.begin() as conn:
        res = conn.execute(stmt, {"h": holder, "n2": name})
        if int(res.rowcount or 0) == 1:
            return True
        if conn.execute(text("SELECT 1 FROM workerlease WHERE name=:n"), {"n": name}).first() is None:
            try:
                conn.execute(
                    text("INSERT INTO workerlease (name, holder, epoch, lease_expires_at, updated_at) "
                         "VALUES (:nme, :h, 1, :e, :now)").bindparams(
                        _bp("e", expires, _EXPIRES), _bp("now", now, _UPDATED)),
                    {"nme": name, "h": holder})
                return True
            except IntegrityError:      # 别的进程抢先建了行：本轮放弃，下轮再抢
                logger.debug("租约 {} 首次 INSERT 撞上并发，本轮不持有", name)
                return False
    return False


def release(name: str, holder: str = HOLDER_ID) -> None:
    """主动让约（关停时用）：只清自己的，别把别人的接管踢掉。"""
    with engine.begin() as conn:
        conn.execute(text("UPDATE workerlease SET holder='', lease_expires_at=NULL "
                          "WHERE name=:n AND holder=:h"), {"n": name, "h": holder})


def current_holder(name: str) -> dict:
    with Session(engine) as session:
        row = session.get(WorkerLease, name)
        if row is None:
            return {"name": name, "holder": "", "epoch": 0,
                    "expires_at": None, "mine": False}
        expires = row.lease_expires_at
        if expires is not None and expires.tzinfo is None:
            expires = expires.replace(tzinfo=datetime.UTC)
        live = bool(expires and _utc_now() < expires)
        return {"name": name, "holder": row.holder, "epoch": row.epoch,
                "expires_at": expires, "mine": row.holder == HOLDER_ID and live}


def all_leases() -> list[dict]:
    with Session(engine) as session:
        return [current_holder(r.name) for r in session.exec(select(WorkerLease)).all()]
