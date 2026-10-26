"""数据库引擎与会话管理。

默认 SQLite（data/cs_agent.db）；切 MySQL 只需改配置 database.url 并安装 pymysql。
SQLite 需要 check_same_thread=False 以配合 FastAPI 多线程请求。

WAL 是必需的而不是优化：默认 journal_mode=delete 下写事务会阻塞读，而本进程同时有
请求线程、lifecycle sweeper 与重入库后台任务三方写入（见 services/lifecycle.py），
不开 WAL + busy_timeout 就会随机 `database is locked`。
"""
from __future__ import annotations

from collections.abc import Generator

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


def init_db() -> None:
    """建表 + 轻量迁移 + 启动自检。

    create_all 只建新表/新索引、不会 ALTER 已有列，所以新增字段走 migrate.py 的补列声明；
    进程内状态（后台任务、sweeper 游标）重启即丢，需要在这里把遗留的中间态收回。
    """
    import app.db.models  # noqa: F401  确保表定义已注册到 metadata

    SQLModel.metadata.create_all(engine)

    from app.db.migrate import apply_column_migrations, cleanup_orphans
    from app.services.knowledge_jobs import recover_interrupted_jobs

    apply_column_migrations(engine)
    cleanup_orphans(engine)
    recover_interrupted_jobs()


def get_session() -> Generator[Session, None, None]:
    """FastAPI 依赖：每请求一个会话。"""
    with Session(engine) as session:
        yield session
