"""平台入站处理：买家消息 → 影子用户/会话映射 → Agent 自动回复 → 平台发送 API。

复用 runner.run_turn(interactive=False)——与网页端同一轮次实现：
短期滑窗、中期摘要、RAG/商品/记忆工具、转人工关键词与状态机全部生效。
人工服务中（pending_human/human）由 runner 的挂起分支统一处理（机器人不抢答）。
两道入站保护：按平台消息 id 幂等去重（claim_message，防超时重推重复应答），
已超时终结的会话收到新消息时重新开一轮（ensure_external_conversation，防买家被永久静音）。
出站三态（deliver_notice / deliver_media）都落 OutboxRecord：mocked 演示、sent 真实送达、
skipped/failed 说明"为什么买家没收到"，任何情况都不阻断会话落库。
"""
from __future__ import annotations

import hashlib
import uuid

from loguru import logger
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, col, select

from app.core.agents.runner import run_turn
from app.db.models import Conversation, ExternalConversation, InboundDedupe, User, pk
from app.db.session import engine
from app.utils import media_url

from .base import InboundMessage, get_adapter


def ensure_external_conversation(session: Session, msg: InboundMessage) -> tuple[User, Conversation]:
    """按 (platform, shop_id, buyer_id) 找/建影子 User 与 Conversation。

    影子用户不可登录（随机密码、email 为空），仅承载消息与长期记忆的归属。
    上一轮会话被 P8 空闲超时终结（status=closed）后买家再来咨询，或者映射指向的会话
    已被运营删除（悬空映射），都在同一影子用户名下开一条新会话并把映射指过去：
    买家侧是同一条连续对话，内部则是新一轮（旧会话历史保留，运营台两条都看得见）。
    """
    digest = hashlib.md5(f"{msg.platform}:{msg.shop_id}:{msg.buyer_id}".encode()).hexdigest()
    username = f"pl_{digest[:20]}"
    user = session.exec(select(User).where(User.username == username)).first()
    if user is None:
        user = User(username=username, email=None,
                    hashed_password=uuid.uuid4().hex)  # 随机即禁用
        session.add(user)
        session.commit()
        session.refresh(user)

    row = session.exec(
        select(ExternalConversation)
        .where(
            ExternalConversation.platform == msg.platform,
            ExternalConversation.shop_id == msg.shop_id,
            ExternalConversation.buyer_id == msg.buyer_id)
        .order_by(col(ExternalConversation.id).desc())
    ).first()
    old = session.get(Conversation, row.conversation_id) if row else None
    if old is not None and old.status != "closed":
        return user, old

    conv = Conversation(user_id=pk(user),
                        title=old.title if old else f"[{msg.platform}] {msg.buyer_id}"[:64])
    session.add(conv)
    session.commit()
    session.refresh(conv)
    # 每轮一条映射记录（不覆盖旧的）：外发按 conversation_id 反查平台，
    # 覆盖会让已终结那轮在运营台丢掉渠道标识。
    session.add(ExternalConversation(platform=msg.platform, shop_id=msg.shop_id,
                                     buyer_id=msg.buyer_id, conversation_id=pk(conv)))
    session.commit()
    logger.info("平台 {} shop={} buyer={} → 会话#{}（{}）",
                msg.platform, msg.shop_id or "-", msg.buyer_id, conv.id,
                "上一轮 #" + str(old.id) + " 已终结，重新开一轮" if old else "建档")
    return user, conv


def claim_message(session: Session, msg: InboundMessage) -> bool:
    """占用本条平台消息 id；已被占用（平台超时重推）返回 False，交由上层按幂等丢弃。

    平台不给 msg_id 时无法去重，按新消息处理（宁可重复应答，也不能漏答）。
    先占位再处理：若在生成回复中途崩溃，重推也不会再答一次——重复应答对买家的
    伤害大于偶尔漏答，且平台自身会重试。
    """
    if not msg.external_msg_id:
        return True
    try:
        session.add(InboundDedupe(platform=msg.platform, shop_id=msg.shop_id,
                                  buyer_id=msg.buyer_id, external_msg_id=msg.external_msg_id))
        session.commit()
        return True
    except IntegrityError:
        session.rollback()
        return False


async def handle_inbound(msg: InboundMessage) -> dict:
    """处理一条平台买家消息；返回执行摘要（供日志/冒烟断言）。"""
    adapter = get_adapter(msg.platform)
    if adapter is None:
        return {"ok": False, "reason": "unknown-platform"}

    with Session(engine) as session:
        if not claim_message(session, msg):
            logger.info("平台 {} 重复推送 msg_id={}，按幂等丢弃", msg.platform, msg.external_msg_id)
            return {"ok": True, "reason": "duplicate"}
        user, conv = ensure_external_conversation(session, msg)
        deltas: list[str] = []
        error = ""
        async for event, data in run_turn(session, conv, user, msg.text, interactive=False):
            if event == "delta":
                deltas.append(data["text"])
            elif event == "error":
                error = data["message"]
        reply = "".join(deltas).strip()
        conv_id, handoff = conv.id, conv.handoff_status

    if error:
        logger.error("平台 {} 消息处理失败（conv#{}）: {}", msg.platform, conv_id, error)
        return {"ok": False, "conversation_id": conv_id, "error": error}

    if not reply:
        return {"ok": True, "conversation_id": conv_id, "handoff_status": handoff,
                "sent": False}

    try:
        result = adapter.send_reply(msg.shop_id, msg.buyer_id, reply)
    except Exception as e:
        logger.error("平台 {} 回复发送失败: {}", msg.platform, e)
        return {"ok": False, "conversation_id": conv_id, "error": str(e)}
    status = str(result.get("status", "mocked"))
    return {"ok": True, "conversation_id": conv_id, "handoff_status": handoff,
            "sent": status in ("mocked", "sent"), "outbox_status": status,
            "reply_len": len(reply), "platform_result": result}


def push_staff_reply(conversation_id: int, text: str) -> bool:
    """人工客服在工作台的回复 → 同步发送到买家所在平台渠道（P7 出站）。"""
    return deliver_notice(conversation_id, text)


def _channel(conversation_id: int) -> tuple[str, str, str] | None:
    """会话 → (platform, shop_id, buyer_id)；网页会话或未注册平台返回 None。"""
    with Session(engine) as session:
        row = session.exec(
            select(ExternalConversation).where(
                ExternalConversation.conversation_id == conversation_id)
        ).first()
        if not row or get_adapter(row.platform) is None:
            return None
        return row.platform, row.shop_id, row.buyer_id


def deliver_notice(conversation_id: int, text: str) -> bool:
    """向会话对应的外部买家发送一条文本（P8 超时提示等系统消息复用此出站口）。

    网页会话无 ExternalConversation 映射，返回 False 且不报错（落库即界面可见）；
    配置没填齐时适配器写 skipped 记录，这里同样返回 False——买家确实没收到。
    """
    channel = _channel(conversation_id)
    if channel is None:
        return False
    platform, shop_id, buyer_id = channel
    try:
        result = get_adapter(platform).send_reply(shop_id, buyer_id, text)  # type: ignore[union-attr]
    except Exception as e:
        logger.error("客服回复外发平台 {} 失败: {}", platform, e)
        return False
    if str(result.get("status")) not in ("mocked", "sent"):
        return False
    logger.info("客服回复已送达平台 {} 买家 {}", platform, buyer_id)
    return True


def deliver_media(conversation_id: int, attachments: list[dict]) -> bool:
    """媒体出站统一入口（B2-2）：运营台人工代答/买家附件的平台侧发送走这里。

    attachments 与 console.ReplyBody.Attachment 同构：{"url","name","type"}。
    platforms.<name>.media_method 未配置时不假装送达——外发箱留 skipped 记录，
    [附件] name: url 的降级文本仍由调用方的正文走 deliver_notice，买家至少看得到链接。
    """
    channel = _channel(conversation_id)
    if channel is None:
        return False
    platform, shop_id, buyer_id = channel
    adapter = get_adapter(platform)
    total = delivered = 0
    for item in attachments or []:
        # 库里存的是裸路径 /api/files/<文件名>；买家在平台客户端里点不开相对路径，
        # 也读不到需要鉴权的文件——出站一律换成"绝对地址 + 签名"的临时链接。
        url = media_url.absolute_url(str(item.get("url") or ""))
        if not url:
            continue
        total += 1
        try:
            result = adapter.send_media(shop_id, buyer_id, url,  # type: ignore[union-attr]
                                        name=str(item.get("name") or ""),
                                        media_type=str(item.get("type") or "file"))
        except Exception as e:
            logger.error("会话#{} 平台 {} 媒体外发失败: {}", conversation_id, platform, e)
            continue
        if str(result.get("status")) in ("mocked", "sent"):
            delivered += 1
    return total > 0 and delivered == total
