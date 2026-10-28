"""密码哈希与 JWT 签发/校验。

约定：前端登录后持有 Bearer token（localStorage），所有 /api/v1 受保护接口经
get_current_user 依赖注入解析当前用户。沿用 AgentChat 的 header 方案（去掉 cookie 分支）。
"""
from __future__ import annotations

import datetime
from typing import Annotated

import bcrypt
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlmodel import Session

from app.config import get_settings
from app.db.models import Conversation, User
from app.db.session import get_session

_bearer = HTTPBearer(auto_error=False)


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except ValueError:
        return False


def create_access_token(user_id: int) -> str:
    s = get_settings()
    expire = datetime.datetime.now(datetime.UTC) + datetime.timedelta(
        minutes=int(s.get_dotted("security.jwt_expire_minutes", 10080)))
    payload = {"sub": str(user_id), "exp": expire}
    return jwt.encode(payload, s.get_dotted("security.jwt_secret"),
                      algorithm=s.get_dotted("security.jwt_algorithm", "HS256"))


def get_current_user(
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    session: Annotated[Session, Depends(get_session)],
) -> User:
    if creds is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "未登录或缺少 Authorization 头")
    s = get_settings()
    try:
        payload = jwt.decode(creds.credentials, s.get_dotted("security.jwt_secret"),
                             algorithms=[s.get_dotted("security.jwt_algorithm", "HS256")])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "登录已过期，请重新登录") from None
    except jwt.PyJWTError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "无效凭据") from None
    user = session.get(User, int(payload["sub"]))
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "用户不存在")
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]
DbSession = Annotated[Session, Depends(get_session)]


def require_admin(user: Annotated[User, Depends(get_current_user)]) -> User:
    """管理端点门禁（B1-7）。

    role 一律现查库、不写进 JWT：改角色要立刻生效，否则要等 token 自然过期（默认 7 天）。
    这里挡住的三类操作都有机器级后果：MCP 配置能以 stdio 拉起本机命令、
    知识库写操作会改召回语料、角色变更本身。
    """
    if user.role != "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "该操作需要管理员权限")
    return user


RequireAdmin = Annotated[User, Depends(require_admin)]


def is_staff(user: User) -> bool:
    """客服及以上（staff/admin）：可以代答他人会话、点确认卡片、处理工单。"""
    return user.role in ("admin", "staff")


def require_staff(user: Annotated[User, Depends(get_current_user)]) -> User:
    if not is_staff(user):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "该操作需要客服权限")
    return user


RequireStaff = Annotated[User, Depends(require_staff)]


def can_access_conversation(user: User, conv: Conversation) -> bool:
    """会话归属判定：客服及以上全量可见，普通用户只能看自己的会话。

    B3-6 之前运营台的跨用户只读+代答对**所有登录用户**开放（演示形态），这意味着
    任何买家账号都能读到别人的聊天记录和工具入参。收紧成角色 + 归属双条件后，
    客服团队协同的口径保持不变，普通账号退化为"只看自己"。
    """
    return is_staff(user) or conv.user_id == user.id
