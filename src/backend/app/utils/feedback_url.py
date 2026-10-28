"""会话满意度回访链接的签名（B3-13）。

买家不在我们站内登录，回访页面必须免 JWT，所以链接本身就是一次性凭证：
HMAC(SHA256, 密钥, "conv|过期时间戳")，密钥取 `security.feedback_secret`，
留空则回落 `security.jwt_secret`（与媒体链接同一套思路，但**不同密钥更好**——
媒体密钥泄漏不该顺手把回访入口也开放）。

有效期默认 7 天（`session.rate_survey_days`）：会话终结后买家可能几小时后才回来看。
"""
from __future__ import annotations

import hashlib
import hmac
import time

from app.config import get_settings


def _secret() -> str:
    s = get_settings()
    return str(s.get_dotted("security.feedback_secret") or s.get_dotted("security.jwt_secret") or "")


def _ttl_seconds() -> int:
    days = int(get_settings().get_dotted("session.rate_survey_days", 7) or 7)
    return max(1, days) * 86400


def _sign(conv_id: int, exp: int) -> str:
    msg = f"{conv_id}|{exp}".encode()
    return hmac.new(_secret().encode("utf-8"), msg, hashlib.sha256).hexdigest()[:32]


def sign_link(conversation_id: int, ttl: int | None = None) -> str:
    """相对路径的回访链接（站内预览用；发给买家要用 survey_url）。"""
    exp = int(time.time()) + (ttl if ttl is not None else _ttl_seconds())
    return f"/api/feedback?conv={conversation_id}&exp={exp}&sig={_sign(conversation_id, exp)}"


def verify_link(conversation_id: int, exp: str | None, sig: str | None) -> bool:
    """校验会话归属签名与有效期；密钥未配置、过期、篡改都拒绝。"""
    if not _secret():
        return False
    try:
        exp_int = int(str(exp))
    except (TypeError, ValueError):
        return False
    if exp_int < int(time.time()):
        return False
    if not sig:
        return False
    return hmac.compare_digest(sig, _sign(int(conversation_id), exp_int))


def survey_url(conversation_id: int) -> str:
    """发给买家的绝对地址（没配 server.public_base_url 时退回相对路径）。

    平台客户端里的相对链接点不开，所以真实外发必须有公网地址——这条与附件链接同口径，
    启动自检与 readiness 都会点名。
    """
    link = sign_link(conversation_id)
    base = str(get_settings().get_dotted("server.public_base_url", "") or "").rstrip("/")
    return f"{base}{link}" if base and not link.startswith("http") else link


def survey_text(conversation_id: int) -> str:
    """结单时附在告别语后面的回访话术（未开启开关时返回空串）。"""
    s = get_settings()
    if not bool(s.get_dotted("session.rate_survey_enabled", False)):
        return ""
    template = str(s.get_dotted(
        "session.rate_survey_text",
        "本次服务还满意吗？点此评价（{url}），1-5 分，10 秒钟就好。"))
    return template.replace("{url}", survey_url(conversation_id))


