"""P8 会话生命周期：空闲超时提示与强制终结（后台周期扫描）。

状态推进（均满足"客户消息即重置"——见 runner.run_turn 的 last_customer_msg_at 锚点）：
    active + idle_seconds 无客户消息 → 落库并出站发送"是否结束会话"提示（end_prompted_at 置位）
    提示后 close_after_seconds 仍无回复 → status=closed；未结工单强制关闭（中断转人工）
closed 会话：run_turn 入口统一拒收；平台入站另在 handle_inbound 提前丢弃。

扫描必须能扛住"同一个会话被两轮扫描/两个线程同时看到"（B1-5）：
候选集只是 SELECT 出来的快照，真正推进状态前要用带条件的 UPDATE 抢一次行
（rowcount 为 0 就说明别的轮次已经做过），否则 --reload 热重载或误开多 worker 时
会出现重复提示、重复告别、甚至给同一会话外发两遍。

时长全部来自 YAML session 节（代码规范：可变参数进配置）。
"""
from __future__ import annotations

import asyncio
import datetime
import json

import sqlalchemy as sa
from loguru import logger
from sqlalchemy import text
from sqlmodel import Session, col, select

from app.config import get_settings
from app.db.models import Conversation, HandoffTicket, Message, pk
from app.db.session import engine
from app.utils import feedback_url


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


# 抢行 UPDATE 里 :now 的绑定类型：直接取列自己的类型，保证裸 SQL 写进去的时间串
# 与 ORM 写的完全一致（text() 的参数没有类型，塞 datetime 会走 sqlite3 的默认适配器，
# Python 3.12 起已弃用，且格式和列不一致 → 下一轮比较读错）。
_DT = Conversation.__table__.columns["updated_at"].type  # type: ignore[attr-defined]


def _claim(sql: str, conv_id: int, now: datetime.datetime) -> int:
    """带条件的抢行 UPDATE：rowcount 为 0 说明别的轮次已经推进过，调用方据此不重复外发。"""
    with engine.begin() as conn:
        return conn.execute(text(sql).bindparams(
            sa.bindparam("id", value=conv_id, type_=sa.Integer),
            sa.bindparam("now", value=now, type_=_DT))).rowcount


def _as_utc(dt: datetime.datetime) -> datetime.datetime:
    """SQLite 回读的 datetime 无 tzinfo，统一按 UTC 补齐再比较。"""
    return dt if dt.tzinfo else dt.replace(tzinfo=datetime.UTC)


def sweep_once(now: datetime.datetime | None = None) -> dict:
    """执行一轮扫描（公开供冒烟脚本以回溯时间的方式驱动）。"""
    now = now or _now()
    conf = get_settings().get_dotted("session", {}) or {}
    idle = int(conf.get("idle_seconds", 300))
    close_after = int(conf.get("close_after_seconds", 60))

    prompted_ids: list[int] = []
    close_ids: list[int] = []
    with Session(engine) as session:
        convs = session.exec(select(Conversation).where(
            Conversation.status == "active",
            col(Conversation.last_customer_msg_at).is_not(None))).all()
        for conv in convs:
            if conv.last_customer_msg_at is None:
                continue          # 老数据里可能有过 is_not(None) 之前写入的行
            last = _as_utc(conv.last_customer_msg_at)
            if conv.end_prompted_at:
                if (now - _as_utc(conv.end_prompted_at)).total_seconds() >= close_after:
                    close_ids.append(pk(conv))
            elif (now - last).total_seconds() >= idle:
                prompted_ids.append(pk(conv))

    prompted = sum(1 for cid in prompted_ids if prompt_conversation(cid, close_after))
    closed = sum(1 for cid in close_ids if close_conversation(cid))
    return {"prompted": prompted, "closed": closed}


def prompt_conversation(conv_id: int, close_after: int) -> bool:
    """发空闲提示。返回 False 表示没抢到（别的轮次已发过），调用方据此不重复外发。"""
    now = _now()
    claimed = _claim("UPDATE conversation SET end_prompted_at=:now, updated_at=:now "
                     "WHERE id=:id AND status='active' AND end_prompted_at IS NULL",
                     conv_id, now)
    if not claimed:
        return False

    minutes = max(1, round(close_after / 60))
    text_ = (f"看起来您暂时没有新的问题。若 {minutes} 分钟内未回复，本次会话将自动结束；"
             "如需继续请直接发送您的问题。")
    with Session(engine) as session:
        conv = session.get(Conversation, conv_id)
        if conv:
            session.add(Message(conversation_id=pk(conv), role="assistant", content=text_,
                                meta_json=json.dumps({"from": "system_idle_prompt"},
                                                     ensure_ascii=False)))
            session.commit()
    logger.info("会话#{} 空闲超时，已发送结束提示", conv_id)
    _deliver(conv_id, text_)
    return True


def close_conversation(conv_id: int) -> bool:
    """终结会话：closed + 未结工单强制关闭（中断转人工）。"""
    now = _now()
    claimed = _claim("UPDATE conversation SET status='closed', updated_at=:now "
                     "WHERE id=:id AND status='active' AND end_prompted_at IS NOT NULL",
                     conv_id, now)
    if not claimed:
        return False

    with Session(engine) as session:
        conv = session.get(Conversation, conv_id)
        if conv:
            tickets = session.exec(select(HandoffTicket).where(
                HandoffTicket.conversation_id == conv.id,
                HandoffTicket.status.in_(("open", "handling")))).all()  # type: ignore[attr-defined]
            for t in tickets:
                t.status = "closed"
                t.closed_at = now
                session.add(t)
                logger.info("会话#{} 超时终结，工单#{} 已强制关闭", conv.id, t.id)
        text_ = "本次会话已结束，感谢您的咨询；如需继续请重新发起对话。"
        # B3-13：开启后在告别语后面接一条带签名的回访链接（免登录但只授权本会话一票）
        survey = feedback_url.survey_text(conv_id)
        if survey:
            text_ = f"{text_}\n{survey}"
        session.add(Message(conversation_id=conv_id, role="assistant", content=text_,
                            meta_json=json.dumps({"from": "system_session_closed"},
                                                 ensure_ascii=False)))
        session.commit()
    logger.info("会话#{} 超时自动终结", conv_id)
    _deliver(conv_id, text_)
    return True


def _deliver(conversation_id: int, text_: str) -> None:
    """系统提示需同步送达平台买家（网页会话无出站渠道，落库即界面可见）。"""
    try:
        from app.services.platforms.inbound import deliver_notice

        deliver_notice(conversation_id, text_)
    except Exception as e:
        logger.warning("会话#{} 超时提示平台外发失败: {}", conversation_id, e)


async def session_sweeper() -> None:
    """lifespan 后台任务：周期扫描。异常只记日志不退出（下一轮自愈）。

    sweep 内部走同步 DB + 平台外发，必须放线程池，否则一次慢外发会卡住事件循环，
    同进程里的 SSE 对话流跟着抖。

    B3-8 起带租约：每个周期先抢 session_sweeper 约（TTL = 3 个扫描周期），
    没抢到的进程直接跳过——多副本部署下只有一家真正在扫，其余实例随时可以在
    持约者挂掉后 3 个周期内接管。会话级的抢行 UPDATE 依然保留，所以即使租约抖动
    出现两个持约者，也不会重复提示或重复结单。
    """
    from app.services import lease

    interval = int(get_settings().get_dotted("session.sweep_interval_seconds", 30))
    logger.info("会话生命周期扫描任务启动（每 {}s，租约选主 holder={}）", interval, lease.HOLDER_ID)
    while True:
        try:
            if lease.acquire("session_sweeper", ttl_seconds=max(60.0, interval * 3)):
                await asyncio.to_thread(sweep_once)
        except Exception:
            logger.exception("会话超时扫描失败")
        await asyncio.sleep(interval)
