"""企微回调端点：URL 验证（GET）与客服消息接收（POST）。

约定：客服在企微中回复「工单#<id> 回复内容」格式的文本消息 → 解密后解析工单号
→ staff_reply 回写到用户会话。非文本消息/格式不符返回 success 并记日志。
配置：wecom.callback_token / encoding_aes_key / corp_id；mock 模式下端点仍可用
（仅验签依赖真实 key）。
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET

from fastapi import APIRouter, HTTPException, Query, Request, status
from loguru import logger
from sqlmodel import Session

from app.config import get_settings
from app.db.models import HandoffTicket
from app.db.session import engine
from app.services.wecom.crypto import WecomCrypto
from app.services.wecom.handoff import staff_reply

router = APIRouter(prefix="/wecom", tags=["wecom"])

_TICKET_PATTERN = re.compile(r"工单\s*#?\s*(\d+)\s*(.+)", re.S)


def _crypto() -> WecomCrypto:
    conf = get_settings().get_dotted("wecom", {}) or {}
    missing = [k for k in ("callback_token", "encoding_aes_key", "corp_id") if not conf.get(k)]
    if missing:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            f"企微回调未配置: 缺少 {missing}（config.local.yaml wecom.*）")
    return WecomCrypto(conf["callback_token"], conf["encoding_aes_key"], conf["corp_id"])


@router.get("/callback")
def verify(msg_signature: str = Query(...), timestamp: str = Query(...),
           nonce: str = Query(...), echostr: str = Query(...)):
    return _crypto().verify_url(msg_signature, timestamp, nonce, echostr)


@router.post("/callback")
async def receive(request: Request, msg_signature: str = Query(...),
                  timestamp: str = Query(...), nonce: str = Query(...)):
    body = (await request.body()).decode("utf-8")
    encrypt = ET.fromstring(body).findtext("Encrypt") or ""
    plain = _crypto().verify_body(msg_signature, timestamp, nonce, encrypt)
    xml = ET.fromstring(plain)
    msg_type = xml.findtext("MsgType")
    userid = xml.findtext("FromUserName") or "unknown"

    if msg_type == "text":
        _handle_staff_text(userid, xml.findtext("Content") or "")
    else:
        logger.info("企微回调收到非文本消息({})，忽略", msg_type)
    return "success"  # 企微要求 5 秒内返回 success


def _handle_staff_text(staff: str, content: str) -> None:
    m = _TICKET_PATTERN.search(content.strip())
    if not m:
        logger.info("企微消息未匹配工单格式，忽略: {}", content[:60])
        return
    ticket_id, text = int(m.group(1)), m.group(2).strip()
    with Session(engine) as session:
        ticket = session.get(HandoffTicket, ticket_id)
        if not ticket or ticket.status == "closed":
            logger.warning("企微回复指向无效工单 #{}", ticket_id)
            return
        staff_reply(session, ticket, text, staff=staff)
