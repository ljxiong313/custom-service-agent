"""运营台接口（P8）：全渠道会话视图、人工代答、媒体上传。

会话语义：role=user 即「客」（平台买家/第一方客户），role=assistant 即「我」。
「我」的两种来源（Agent 自动回复 / 运营手动代答）只记在 meta.from 供运营侧区分，
买家/客户侧不可感知。

权限口径：这里的跨用户只读+代答是运营台的核心能力，不是 IDOR 疏漏，所以任意登录
用户均可用；B1-7 引入的 role 字段只收紧"会改动全局语料/本机进程/他人账号"的接口
（知识库写、MCP 增删改、用户角色、删除会话文件），不收紧这里的日常代答。
"""
from __future__ import annotations

import datetime
import json
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, UploadFile, status
from pydantic import BaseModel, Field
from sqlmodel import col, select

from app.config import BACKEND_ROOT
from app.core.security import CurrentUser, DbSession
from app.db.models import Conversation, ExternalConversation, HandoffTicket, Message, User, pk
from app.services.platforms.inbound import deliver_media, deliver_notice
from app.services.wecom.handoff import staff_reply

router = APIRouter(prefix="/console", tags=["console"])

MEDIA_DIR = BACKEND_ROOT / "data" / "uploads" / "media"
MAX_MEDIA_BYTES = 10 * 1024 * 1024


def _get_conv(session, conv_id: int) -> Conversation:
    conv = session.get(Conversation, conv_id)
    if conv is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "会话不存在")
    return conv


@router.get("/conversations")
def list_conversations(user: CurrentUser, session: DbSession, limit: int = 100):
    """全渠道买家会话列表（平台 + 网页），按最近更新排序。"""
    convs = list(session.exec(
        select(Conversation).order_by(col(Conversation.updated_at).desc()).limit(limit)
    ).all())
    exts = {e.conversation_id: e for e in session.exec(select(ExternalConversation)).all()}

    items = []
    for conv in convs:
        ext = exts.get(pk(conv))
        last = session.exec(select(Message).where(
            Message.conversation_id == pk(conv))
            .order_by(col(Message.id).desc())).first()
        owner = session.get(User, conv.user_id)
        items.append({
            "id": conv.id,
            "title": conv.title,
            "username": owner.username if owner else "",
            "channel": ext.platform if ext else "web",
            "shop_id": ext.shop_id if ext else "",
            "buyer_id": ext.buyer_id if ext else "",
            "handoff_status": conv.handoff_status,
            "status": conv.status,
            "updated_at": conv.updated_at,
            "last_message": (last.content[:40] if last else ""),
            "last_role": (last.role if last else ""),
        })
    return items


class Attachment(BaseModel):
    url: str
    name: str
    type: str = "file"      # image / file


class ReplyBody(BaseModel):
    text: str = Field(default="", max_length=4000)
    attachments: list[Attachment] = Field(default_factory=list)


@router.post("/conversations/{conv_id}/reply")
def operator_reply(conv_id: int, body: ReplyBody, user: CurrentUser, session: DbSession):
    """运营以「我」的身份手动回复：客户无感知人工/AI 差异；已建工单则走工单回路。"""
    conv = _get_conv(session, conv_id)
    if conv.status == "closed":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "该会话已结束，无法回复")
    text = body.text.strip()
    if not text and not body.attachments:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "回复内容不能为空")

    meta: dict[str, Any] = {"from": "human_operator", "staff": user.username}
    if body.attachments:
        meta["attachments"] = [a.model_dump() for a in body.attachments]
    outbound = text or "（图片/文件）"
    if body.attachments:
        outbound += "\n" + "\n".join(f"[附件] {a.name}: {a.url}" for a in body.attachments)

    # 有未结工单：复用 P3 客服回复回路（落库+进入 human+平台外发），并附加媒体
    ticket = session.exec(select(HandoffTicket).where(
        HandoffTicket.conversation_id == conv_id,
        col(HandoffTicket.status).in_(("open", "handling")))).first()
    if ticket:
        msg = staff_reply(session, ticket, outbound, staff=user.username)
        if body.attachments:
            msg.meta_json = json.dumps({**(json.loads(msg.meta_json or "{}")),
                                        "attachments": meta["attachments"]}, ensure_ascii=False)
            session.add(msg)
            session.commit()
            # B2-2：正文里的 [附件] 链接是降级文案，媒体本身另走平台媒体出站；
            # 平台没配 media_method 时这里只会留 skipped 记录，不会谎报送达。
            deliver_media(conv_id, meta["attachments"])
        return {"message_id": msg.id, "ticket_status": ticket.status, "via": "ticket"}

    msg = Message(conversation_id=conv_id, role="assistant", content=outbound,
                  meta_json=json.dumps(meta, ensure_ascii=False))
    # 运营主动介入等待确认/机器人态的会话：转人工请求视为已由人工答复
    if conv.handoff_status == "awaiting_confirm":
        conv.handoff_status = "bot"
    conv.updated_at = datetime.datetime.now(datetime.UTC)
    session.add(msg)
    session.add(conv)
    session.commit()
    session.refresh(msg)
    deliver_notice(conv_id, outbound)   # 平台会话外发；网页会话返回 False 静默
    if body.attachments:
        deliver_media(conv_id, meta["attachments"])
    return {"message_id": msg.id, "via": "direct"}


@router.get("/conversations/{conv_id}/messages")
def conversation_messages(conv_id: int, user: CurrentUser, session: DbSession):
    """跨渠道完整消息历史（运营侧视角，meta.from 标注回复来源）。"""
    _get_conv(session, conv_id)
    msgs = list(session.exec(select(Message).where(
        Message.conversation_id == conv_id).order_by(Message.id)).all())  # type: ignore[arg-type]
    return [{"id": m.id, "role": m.role, "content": m.content,
             "meta": json.loads(m.meta_json) if m.meta_json else None,
             "created_at": m.created_at} for m in msgs]


@router.post("/media")
async def upload_media(file: UploadFile, user: CurrentUser):
    """运营输入框粘贴图片/发送文件：存 data/uploads/media，经 /api/files 访问。"""
    data = await file.read()
    if not data:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "空文件")
    if len(data) > MAX_MEDIA_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "文件超过 10MB")
    ext = (file.filename or "").rsplit(".", 1)[-1].lower()[:10] if "." in (file.filename or "") else "bin"
    # 文件名白名单化：只保留字母数字与后缀，杜绝路径穿越
    fname = f"{uuid.uuid4().hex[:16]}.{ext}" if ext != "bin" else uuid.uuid4().hex
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    (MEDIA_DIR / fname).write_bytes(data)
    kind = "image" if (file.content_type or "").startswith("image/") else "file"
    return {"url": f"/api/files/{fname}", "name": file.filename or fname,
            "type": kind, "size": len(data)}
