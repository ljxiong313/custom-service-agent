"""工具执行确认：落库的状态机（B3-8 起不再是进程内 Future 表）。

流程：对话循环发现需确认工具 → create_request 得 request_id →
下发 confirm_request 事件 → 轮询库等待裁决（超时视为拒绝）→
运营台/网页 POST /mcp/confirm 携带 request_id + approved → 原子置位。

为什么必须落库：
- 旧实现把 asyncio.Future 存在模块级 dict 里，"点确认"的那个 HTTP 请求必须正好落到
  持有 future 的进程才有效，多副本下大概率超时拒绝；重启更会把在等的对话直接挂死。
- 落库后等待方按轮询读状态，任何进程都能裁决，待办本身也变成了可审计的持久记录
  （谁批的、什么时候批的、当时工具入参是什么）。

轮询间隔取 1 秒：确认卡片是人在点，1 秒延迟人眼无感，而每 0.1 秒打一次库在
SQLite 单写者模型上是白受的写放大。
"""
from __future__ import annotations

import asyncio
import datetime
import json
import time
import uuid
from dataclasses import dataclass, field

from loguru import logger
from sqlalchemy import BindParameter, func
from sqlalchemy import bindparam as sa_bindparam
from sqlalchemy import text as sql_text
from sqlalchemy.types import TypeEngine
from sqlmodel import Session, col, select

from app.db.models import ToolConfirmRequest
from app.db.session import engine

TTL_SECONDS = 180.0   # 超过这个时间没人应答就当拒绝并置 expired
POLL_SECONDS = 1.0
TABLE = "toolconfirmrequest"


@dataclass
class Pending:
    """给接口层用的视图对象（保持与旧 _Pending 相同的字段名，调用方不必改写法）。"""

    request_id: str
    conversation_id: int
    user_id: int
    tool_name: str
    args: dict = field(default_factory=dict)
    created_at: float = 0.0
    status: str = "pending"


def _utc_now() -> datetime.datetime:
    """一律带 tzinfo 的 UTC：SQLModel 0.0.48 的 UTCDateTime 列拒绝 naive datetime 绑定，
    裸 SQL 也必须走同一个类型（见下面 _CREATED_AT/_DECIDED_AT 的 bindparams），否则
    写进去的时间串格式与 ORM 不一致，下一轮按字符串比较就会读错。"""
    return datetime.datetime.now(datetime.UTC)


def _bp(name: str, value: datetime.datetime, type_: TypeEngine) -> BindParameter:
    """bindparam 薄封装：裸 SQL 的时间参数必须按列自己的类型绑定，
    否则走 sqlite3 的默认适配器（Python 3.12 起已弃用），写出的时间串与 ORM 不一致。"""
    return sa_bindparam(name, value=value, type_=type_)


# 裸 SQL 里绑时间必须带上列自己的类型（同 services/lifecycle.py 的 _DT 教训）
_CREATED_AT = ToolConfirmRequest.__table__.columns["created_at"].type  # type: ignore[attr-defined]
_DECIDED_AT = ToolConfirmRequest.__table__.columns["decided_at"].type  # type: ignore[attr-defined]


def create_request(conversation_id: int, user_id: int, tool_name: str,
                   args: dict | None = None) -> str:
    rid = uuid.uuid4().hex[:12]
    with Session(engine) as session:
        session.add(ToolConfirmRequest(
            request_id=rid, conversation_id=conversation_id, user_id=user_id,
            tool_name=tool_name, args_json=json.dumps(args or {}, ensure_ascii=False, default=str),
            status="pending"))
        session.commit()
    return rid


def _to_pending(row: ToolConfirmRequest) -> Pending:
    try:
        args = json.loads(row.args_json or "{}")
    except json.JSONDecodeError:
        args = {"_raw": row.args_json}
    created = row.created_at
    if created is not None and created.tzinfo is None:
        created = created.replace(tzinfo=datetime.UTC)     # 兼容历史库里落成的 naive 值
    return Pending(request_id=row.request_id, conversation_id=row.conversation_id,
                   user_id=row.user_id, tool_name=row.tool_name, args=args,
                   created_at=created.timestamp() if created else 0.0, status=row.status)


def prune(max_age: float = TTL_SECONDS) -> int:
    """把超期无人应答的请求置 expired（等待方本来就会按拒绝处理，状态要留下可查）。"""
    cutoff = _utc_now() - datetime.timedelta(seconds=max_age)
    with engine.begin() as conn:
        res = conn.execute(
            sql_text(f"UPDATE {TABLE} SET status='expired' "
                     "WHERE status='pending' AND created_at < :c").bindparams(
                _bp("c", cutoff, _CREATED_AT)))
    return int(res.rowcount or 0)


def pending_list() -> list[dict]:
    """当前等待确认的卡片（运营台轮询用）。顺带回收过期条目。"""
    prune()
    cutoff = _utc_now() - datetime.timedelta(seconds=TTL_SECONDS)
    with Session(engine) as session:
        rows = session.exec(
            select(ToolConfirmRequest)
            .where(col(ToolConfirmRequest.status) == "pending",
                   col(ToolConfirmRequest.created_at) >= cutoff)
            .order_by(col(ToolConfirmRequest.id))).all()
        return [{"request_id": r.request_id, "conversation_id": r.conversation_id,
                 "tool_name": r.tool_name, "args": _to_pending(r).args,
                 "created_at": _to_pending(r).created_at} for r in rows]


def pending_count() -> int:
    cutoff = _utc_now() - datetime.timedelta(seconds=TTL_SECONDS)
    with Session(engine) as session:
        return int(session.exec(
            select(func.count()).select_from(ToolConfirmRequest)
            .where(col(ToolConfirmRequest.status) == "pending",
                   col(ToolConfirmRequest.created_at) >= cutoff)).one())


def get(request_id: str) -> Pending | None:
    """按 id 取确认条目（接口层据此判定会话归属）。已过期/不存在返回 None。"""
    prune()
    with Session(engine) as session:
        row = session.exec(select(ToolConfirmRequest)
                           .where(ToolConfirmRequest.request_id == request_id)).first()
        if row is None or row.status != "pending":
            return None
        return _to_pending(row)


def resolve(request_id: str, approved: bool, decided_by: int | None = None) -> bool:
    """原子裁决：只有把 pending 抢下来才算成功，重复点击/已超时的返回 False。"""
    with engine.begin() as conn:
        res = conn.execute(
            sql_text(f"UPDATE {TABLE} SET status=:s, decided_at=:d, decided_by=:w "
                     "WHERE request_id=:rid AND status='pending'").bindparams(
                _bp("d", _utc_now(), _DECIDED_AT)),
            {"s": "approved" if approved else "rejected", "w": decided_by, "rid": request_id})
    return int(res.rowcount or 0) == 1


async def wait_decision(request_id: str, timeout: float = TTL_SECONDS) -> bool:
    """轮询等待裁决。超时或记录消失都按拒绝。"""
    deadline = time.monotonic() + timeout
    while True:
        with Session(engine) as session:
            row = session.exec(select(ToolConfirmRequest)
                               .where(ToolConfirmRequest.request_id == request_id)).first()
            if row is None:
                logger.warning("确认请求 {} 在库里不存在，按拒绝处理", request_id)
                return False
            if row.status != "pending":
                # approved/rejected/expired 都是终态：裁决就是这一次等待的答案，直接返回。
                # 不再"消费后改写状态"——那会让审计记录变成没人看得懂的中转态。
                return row.status == "approved"
            if time.monotonic() >= deadline:
                with engine.begin() as conn:
                    conn.execute(sql_text(f"UPDATE {TABLE} SET status='expired' "
                                          "WHERE request_id=:rid AND status='pending'"),
                                 {"rid": request_id})
                return False
            await asyncio.sleep(POLL_SECONDS)
