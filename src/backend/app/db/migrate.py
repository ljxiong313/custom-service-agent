"""轻量结构补列与启动期数据修复。

历史背景：早期项目没有 Alembic，SQLModel 的 create_all 会自动建**新表**，但不会给旧表
ALTER **新列**，加字段靠删 data/cs_agent.db 重建（只适合空库）。
新增列只需在 ADDITIVE_COLUMNS 登记一行，本模块按 PRAGMA table_info 幂等补齐，
本地已有 3100 行商品/多轮会话的库不用重建，生产也是同一套语句。

Alembic 已作为生产迁移体系落地（见 src/backend/alembic.ini 与 migrations/、
以及 database.migrate_mode 的 auto/alembic 两种模式）：改类型、改约束、删列这类
create_all 与本模块都覆盖不了的变更，现在写成迁移脚本，不再需要人工重建库。
补列声明可以继续用（幂等，且 alembic 模式下也会照常执行），但生产口径以迁移为准。
"""
from __future__ import annotations

import sqlalchemy as sa
from loguru import logger

# (表, 列, 列定义) —— 只允许带默认值的可空/有默认列，ALTER TABLE ADD COLUMN 才安全
ADDITIVE_COLUMNS: list[tuple[str, str, str]] = [
    ("user", "role", "VARCHAR NOT NULL DEFAULT 'user'"),
    # 出站失败退避重试：attempts 用 0 兜底（历史行=从未重试），时间/错误文本可空。
    # next_retry_at 的类型要和 SQLModel 的 UTCDateTime 落到 SQLite 的 DATETIME 一致，
    # 否则补列出来的库与新库建出来的列类型不同（SQLite 不强制，但排查时会误导）。
    ("outboxrecord", "attempts", "INTEGER NOT NULL DEFAULT 0"),
    ("outboxrecord", "next_retry_at", "DATETIME"),
    ("outboxrecord", "last_error", "VARCHAR"),
]

# (子表.外键列, 父表.主键列) —— PRAGMA foreign_keys=ON 之后，历史孤儿行会让删除直接报错，
# 所以启动时先把它们清掉，保证"删会话"这类操作在任何存量库上都能跑通。
ORPHAN_CHECKS: list[tuple[str, str, str, str]] = [
    ("message", "conversation_id", "conversation", "id"),
    ("conversation", "user_id", "user", "id"),
    ("chunk", "file_id", "knowledgefile", "id"),
    ("product", "file_id", "knowledgefile", "id"),
    ("conversationsummary", "conversation_id", "conversation", "id"),
    ("handoffticket", "conversation_id", "conversation", "id"),
    ("externalconversation", "conversation_id", "conversation", "id"),
    ("ingestjob", "file_id", "knowledgefile", "id"),
    ("toolconfirmrequest", "conversation_id", "conversation", "id"),
    ("toolconfirmrequest", "user_id", "user", "id"),
    ("memoryjob", "conversation_id", "conversation", "id"),
    ("sessionrating", "conversation_id", "conversation", "id"),
]


def _table_columns(conn: sa.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.exec_driver_sql(f'PRAGMA table_info("{table}")').fetchall()}


def apply_column_migrations(engine: sa.Engine) -> None:
    """给已存在的表补齐登记过的列（幂等）。"""
    if engine.dialect.name != "sqlite":
        logger.info("非 SQLite 库（{}）请改用正式迁移工具，本模块只服务 SQLite", engine.dialect.name)
        return
    with engine.begin() as conn:
        existing_tables = {r[0] for r in conn.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        for table, column, ddl in ADDITIVE_COLUMNS:
            if table not in existing_tables:
                continue          # 新库由 create_all 直接带列建好
            if column in _table_columns(conn, table):
                continue
            conn.exec_driver_sql(f'ALTER TABLE "{table}" ADD COLUMN "{column}" {ddl}')
            logger.info("结构迁移: {}.{} 已补列 ({})", table, column, ddl)

    _ensure_admin(engine)


def _ensure_admin(engine: sa.Engine) -> None:
    """role 列刚补上时全员是 user，管理端点会没人能用——把最早注册的用户设为 admin。

    只在库里一个 admin 都没有时执行，不会被脚本或界面后续的角色调整覆盖。
    """
    with engine.begin() as conn:
        has_admin = conn.exec_driver_sql(
            "SELECT 1 FROM user WHERE role='admin' LIMIT 1").scalar()
        if has_admin:
            return
        first = conn.exec_driver_sql("SELECT id, username FROM user ORDER BY id LIMIT 1").first()
        if not first:
            return
        conn.exec_driver_sql("UPDATE user SET role='admin' WHERE id=?", (first[0],))
        logger.info("角色初始化: 无 admin，已将最早用户 {}#{} 置为 admin", first[1], first[0])


def cleanup_orphans(engine: sa.Engine) -> None:
    """删除指向已不存在的父行的子记录（历史版本手动级联不全留下的孤儿）。"""
    total = 0
    with engine.begin() as conn:
        tables = {r[0] for r in conn.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        for child, col, parent, pcol in ORPHAN_CHECKS:
            if child not in tables or parent not in tables:
                continue
            sql = (f'DELETE FROM "{child}" WHERE "{col}" NOT NULL '
                   f'AND "{col}" NOT IN (SELECT "{pcol}" FROM "{parent}")')
            n = conn.exec_driver_sql(sql).rowcount or 0
            if n:
                logger.warning("清理孤儿行: {}.{} 指向缺失的 {} → 删除 {} 行", child, col, parent, n)
                total += n
    if total:
        logger.warning("启动自检共清理 {} 条孤儿行（多为旧版删除会话未级联所致）", total)
