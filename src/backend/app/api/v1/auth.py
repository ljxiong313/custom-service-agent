"""认证与账号接口：注册 / 登录 / 当前用户 / 用户角色管理（admin）。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlmodel import col, select

from app.config import get_settings
from app.core.security import (
    CurrentUser,
    DbSession,
    RequireAdmin,
    create_access_token,
    hash_password,
    verify_password,
)
from app.db.models import User, pk

router = APIRouter(prefix="/auth", tags=["auth"])


class AuthBody(BaseModel):
    username: str = Field(min_length=2, max_length=32)
    password: str = Field(min_length=6, max_length=64)
    email: str | None = None


class UserOut(BaseModel):
    id: int
    username: str
    email: str | None
    role: str = "user"


class RoleBody(BaseModel):
    role: str = Field(pattern="^(user|staff|admin)$")


def _registration_open(session) -> bool:
    """注册开关（B3-6）：生产形态默认关闭自助注册，空库除外。

    演示时"注册任意测试账号"很顺，但公网实例开着自助注册 = 任何人拿到一个能读会话、
    点确认卡片的账号。生产要建号请由 admin 在设置页开，或显式
    `security.allow_registration: true`。空库例外是为了首次部署能自举出第一个 admin。
    """
    s = get_settings()
    raw = s.get_dotted("security.allow_registration", None)
    if raw is not None:
        return bool(raw)
    if not s.is_production():
        return True
    return session.exec(select(User)).first() is None


def _user_out(u: User) -> UserOut:
    return UserOut(id=u.id, username=u.username, email=u.email, role=u.role)  # type: ignore[arg-type]


@router.post("/register", response_model=UserOut)
def register(body: AuthBody, session: DbSession):
    if not _registration_open(session):
        raise HTTPException(status.HTTP_403_FORBIDDEN,
                            "当前实例已关闭自助注册，请联系管理员在设置页开通账号")
    exist = session.exec(select(User).where(User.username == body.username)).first()
    if exist:
        raise HTTPException(status.HTTP_409_CONFLICT, "用户名已存在")
    # 库里第一个账号直接给 admin：否则全新库装好后没人能配置 MCP/知识库，只能手改数据库
    first_user = session.exec(select(User)).first() is None
    user = User(username=body.username, email=body.email,
                hashed_password=hash_password(body.password),
                role="admin" if first_user else "user")
    session.add(user)
    session.commit()
    session.refresh(user)
    return _user_out(user)


@router.post("/login")
def login(body: AuthBody, session: DbSession):
    user = session.exec(select(User).where(User.username == body.username)).first()
    if not user or not verify_password(body.password, user.hashed_password):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "用户名或密码错误")
    return {"access_token": create_access_token(pk(user)), "token_type": "bearer",
            "user": _user_out(user)}


@router.get("/me", response_model=UserOut)
def me(user: CurrentUser):
    return _user_out(user)


@router.get("/users", response_model=list[UserOut])
def list_users(user: RequireAdmin, session: DbSession):
    return [_user_out(u) for u in session.exec(select(User).order_by(col(User.id))).all()]


class CreateBody(BaseModel):
    username: str = Field(min_length=2, max_length=32)
    password: str = Field(min_length=8, max_length=64)
    role: str = Field(default="staff", pattern="^(user|staff|admin)$")
    email: str | None = None


@router.post("/users", response_model=UserOut)
def create_user(body: CreateBody, actor: RequireAdmin, session: DbSession):
    """管理员建号（配合生产关闭自助注册）：默认给 staff，客服不用抢注账号。

    这里刻意不做"密码复杂度"花活，只把最低长度从注册的 6 位提到 8 位：
    管理侧账号能进运营台、点确认卡片，密码门槛要对得起这个权限。
    """
    if session.exec(select(User).where(User.username == body.username)).first():
        raise HTTPException(status.HTTP_409_CONFLICT, "用户名已存在")
    user = User(username=body.username, email=body.email, role=body.role,
                hashed_password=hash_password(body.password))
    session.add(user)
    session.commit()
    session.refresh(user)
    return _user_out(user)


@router.patch("/users/{user_id}/role", response_model=UserOut)
def set_role(user_id: int, body: RoleBody, actor: RequireAdmin, session: DbSession):
    target = session.get(User, user_id)
    if not target:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "用户不存在")
    if target.id == actor.id and body.role != "admin":
        raise HTTPException(status.HTTP_409_CONFLICT, "不能降级自己，否则没人能再改角色")
    # 「至少保留一个管理员」只在**把唯一的 admin 降级**时才成立；
    # 早先的实现写成 `if body.role != "admin"`，结果给普通账号提权成 staff 也会被拦，
    # 客服根本招不进来。
    if target.role == "admin" and body.role != "admin":
        admins = session.exec(select(User).where(User.role == "admin")).all()  # noqa: E712
        if len(admins) <= 1:
            raise HTTPException(status.HTTP_409_CONFLICT, "必须至少保留一个管理员")
    target.role = body.role
    session.add(target)
    session.commit()
    session.refresh(target)
    return _user_out(target)
