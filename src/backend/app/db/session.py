"""数据库引擎与会话管理。

默认 SQLite（data/cs_agent.db）；切 MySQL 只需改配置 database.url 并安装 pymysql。
SQLite 需要 check_same_thread=False 以配合 FastAPI 多线程请求。

建库方式由配置 database.migrate_mode 决定（见 init_db）：auto 是开发体验（create_all），
alembic 是生产口径（只认 migrations/versions 里的脚本，漏写迁移就会启动失败）。

WAL 是必需的而不是优化：默认 journal_mode=delete 下写事务会阻塞读，而本进程同时有
请求线程、lifecycle sweeper 与重入库后台任务三方写入（见 services/lifecycle.py），
不开 WAL + busy_timeout 就会随机 `database is locked`。
"""
from __future__ import annotations

from collections.abc import Generator

import sqlalchemy as sa
from loguru import logger
from sqlalchemy import event
from sqlmodel import Session, SQLModel, create_engine

from app.config import BACKEND_ROOT, get_settings

_url = get_settings().get_dotted("database.url", "sqlite:///./data/cs_agent.db")

# SQLite 相对路径统一锚定到后端根目录，避免 uvicorn 从不同 cwd 启动时库文件漂移
if _url.startswith("sqlite:///") and not _url.startswith("sqlite:///./"):
    pass  # 绝对路径，交给 SQLAlchemy
elif _url.startswith("sqlite:///"):
    rel = _url.removeprefix("sqlite:///").removeprefix("./")
    abs_path = (BACKEND_ROOT / rel).resolve()
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    _url = f"sqlite:///{abs_path.as_posix()}"

IS_SQLITE = _url.startswith("sqlite")

# SQLite 走 SingletonThreadPool（每线程一连接），不接受 pool_size/max_overflow
_engine_kwargs: dict = {"echo": False}
if IS_SQLITE:
    _engine_kwargs["connect_args"] = {"check_same_thread": False}
else:
    _engine_kwargs.update(pool_size=12, max_overflow=20)

engine = create_engine(_url, **_engine_kwargs)

if IS_SQLITE:

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):  # noqa: ANN001 - DBAPI 连接对象由驱动给出
        """每个新连接都要执行：journal_mode 持久在库文件里，busy_timeout/synchronous 是连接级。

        不开 WAL 时 sweeper、后台重入库与请求线程三方并发写会直接抛 database is locked；
        foreign_keys=ON 是 SQLite 默认关闭的，打开后孤儿行会在删除时暴露而不是静默留下。
        """
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()


def _migrate_mode() -> str:
    """读 database.migrate_mode；认 auto / alembic 两种，写错就直接拒绝启动。

    不做"未知值悄悄回落到 auto"：那等于把"漏写迁移"重新变成静默补表，
    而生产上最怕的就是库结构和代码不一致却没人知道。
    """
    mode = str(get_settings().get_dotted("database.migrate_mode", "auto")).strip().lower()
    if mode not in ("auto", "alembic"):
        raise ValueError(f"database.migrate_mode 只支持 auto / alembic，当前配置是 {mode!r}")
    return mode


def _alembic_version() -> str | None:
    """库里当前的 alembic 版本号；从没跑过迁移就没有这张表，返回 None。

    只用于日志（auto 模式下也打，方便看出"这个库其实是迁移建起来的"），
    读失败不影响启动流程，所以异常只记 warning 不外抛。
    """
    try:
        if not sa.inspect(engine).has_table("alembic_version"):
            return None
        with engine.connect() as conn:
            row = conn.exec_driver_sql("SELECT version_num FROM alembic_version").first()
        return str(row[0]) if row else None
    except sa.exc.SQLAlchemyError as exc:
        logger.warning("读取 alembic 版本失败（不影响启动）：{}", exc)
        return None


def _run_alembic_upgrade() -> None:
    """编程式执行 alembic upgrade head（不起子进程）。

    用 alembic 的 Python API 有两个好处：迁移跑在和请求线程同一个 engine/同一套 URL
    解析口径上（SQLite 相对路径锚定只在 app.db.session 做过一次），以及 Windows 下
    不必指望 .venv\\Scripts\\alembic.exe 与当前 cwd 正好合适。
    alembic.ini 里的 script_location 用 %(here)s 锚到 src/backend，所以从任何目录启动都对。

    存量库第一次切到本模式会撞 "table xxx already exists"：那说明库是 create_all 建出来的，
    结构与基线一致时用 `alembic stamp 0001` 打版本即可，不要重跑建表。
    """
    from alembic import command
    from alembic.config import Config

    before = _alembic_version()
    command.upgrade(Config(str(BACKEND_ROOT / "alembic.ini")), "head")
    logger.info(
        "数据库迁移模式=alembic：upgrade head 完成，版本 {} → {}（跳过 create_all：漏写的迁移不会被悄悄补上）",
        before or "（无版本表）",
        _alembic_version() or "（无版本表）",
    )


def init_db() -> None:
    """建表 + 轻量迁移 + 启动自检。

    两种模式（database.migrate_mode）：
    - auto（默认，开发/测试）：create_all 补新表新索引，新列走 db/migrate.py 的 ADDITIVE_COLUMNS，
      结构改动不用重建库。代价是"模型改了但忘了写迁移"会被 create_all 悄悄补上，问题留到生产。
    - alembic（生产）：只跑 `alembic upgrade head`，**跳过 create_all**——库里有什么完全由迁移脚本
      决定，漏写迁移的表不会被悄悄补上，第一次用到就报"no such table"。真正的拦截点是 CI：
      tests/test_migrations.py 断言"跑完 baseline 再 autogenerate 必须是空 diff"（等价于
      `alembic check`），补列与孤儿清理在本模式下照旧执行（都是幂等的）。

    注意 create_all 只建新表/新索引、不会 ALTER 已有列，所以新增字段仍要在 migrate.py 登记；
    进程内状态（后台任务、sweeper 游标）重启即丢，也要在这里把遗留的中间态收回。
    """
    import app.db.models  # noqa: F401  确保表定义已注册到 metadata

    mode = _migrate_mode()
    if mode == "alembic":
        _run_alembic_upgrade()
    else:
        SQLModel.metadata.create_all(engine)
        logger.info("数据库迁移模式=auto：create_all 建表（alembic 版本 {}）", _alembic_version() or "未初始化")

    from app.db.migrate import apply_column_migrations, cleanup_orphans
    from app.services.knowledge_jobs import recover_interrupted_jobs
    from app.services.memory.pipeline import recover_memory_jobs

    apply_column_migrations(engine)
    cleanup_orphans(engine)
    recover_interrupted_jobs()
    recover_memory_jobs()


def get_session() -> Generator[Session, None, None]:
    """FastAPI 依赖：每请求一个会话。"""
    with Session(engine) as session:
        yield session
