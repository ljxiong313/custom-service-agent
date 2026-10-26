"""会话管理接口：列表 / 新建 / 重命名 / 删除，以及会话内消息历史。"""
from __future__ import annotations

import datetime

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlmodel import col, select

from app.core.security import CurrentUser, DbSession
from app.db import cascade
from app.db.models import Conversation, Message, pk

router = APIRouter(prefix="/conversations", tags=["conversation"])


class ConversationCreate(BaseModel):
    title: str = Field(default="新会话", max_length=64)


class ConversationRename(BaseModel):
    title: str = Field(min_length=1, max_length=64)


class ConversationOut(BaseModel):
    id: int
    title: str
    updated_at: datetime.datetime
    handoff_status: str = "bot"  # P3：前端据此显示"人工服务中"横幅


def _get_owned(session, conversation_id: int, user_id: int) -> Conversation:
    """校验会话归属，防止越权访问他人会话（IDOR）。"""
    conv = session.get(Conversation, conversation_id)
    if conv is None or conv.user_id != user_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "会话不存在")
    return conv


@router.get("", response_model=list[ConversationOut])
def list_conversations(user: CurrentUser, session: DbSession):
    rows = session.exec(
        select(Conversation)
        .where(Conversation.user_id == user.id)
        .order_by(col(Conversation.updated_at).desc())
    ).all()
    return rows


@router.post("", response_model=ConversationOut)
def create_conversation(body: ConversationCreate, user: CurrentUser, session: DbSession):
    conv = Conversation(user_id=pk(user), title=body.title)
    session.add(conv)
    session.commit()
    session.refresh(conv)
    return conv


@router.patch("/{conversation_id}", response_model=ConversationOut)
def rename_conversation(conversation_id: int, body: ConversationRename,
                        user: CurrentUser, session: DbSession):
    conv = _get_owned(session, conversation_id, pk(user))
    conv.title = body.title
    conv.updated_at = datetime.datetime.now(datetime.UTC)
    session.add(conv)
    session.commit()
    session.refresh(conv)
    return conv


@router.delete("/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_conversation(conversation_id: int, user: CurrentUser, session: DbSession):
    conv = _get_owned(session, conversation_id, pk(user))
    # 级联删全部附属行：外键已开 PRAGMA foreign_keys=ON，漏删任何一张子表都会让删除报错，
    # 而不是像以前那样静默留下孤儿行（见 db/cascade.py）
    cascade.delete_conversation(session, conv)


@router.get("/{conversation_id}/messages")
def list_messages(conversation_id: int, user: CurrentUser, session: DbSession):
    conv = _get_owned(session, conversation_id, pk(user))
    rows = session.exec(
        select(Message).where(Message.conversation_id == conv.id).order_by(col(Message.id))
    ).all()
    return [{"id": m.id, "role": m.role, "content": m.content,
             "meta_json": m.meta_json, "created_at": m.created_at} for m in rows]
