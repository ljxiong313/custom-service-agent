"""数据层完整性单测：SQLite 连接级 pragma、加列迁移、孤儿清理、级联删除。

这里覆盖的是"进程重启 / 存量旧库"才会暴露的路径：接口测试跑得通不代表删会话不会被
外键拦下，所以把 PRAGMA、ADDITIVE_COLUMNS 幂等、cleanup_orphans、delete_conversation
四件事各自钉住。
"""
from __future__ import annotations

import pytest
import sqlalchemy as sa
from sqlmodel import Session, select

from app.db import migrate
from app.db.cascade import delete_conversation
from app.db.models import (
    Chunk,
    Conversation,
    ConversationSummary,
    ExternalConversation,
    HandoffTicket,
    InboundDedupe,
    IngestJob,
    KnowledgeFile,
    Message,
    Product,
    User,
)
from app.db.session import engine, init_db
from app.services import knowledge_jobs
from app.services.rag import ingest


def _pragma(conn: sa.Connection, name: str):
    return conn.exec_driver_sql(f"PRAGMA {name}").scalar()


def _counts() -> dict[str, int]:
    with engine.connect() as conn:
        return {t: int(conn.exec_driver_sql(f'SELECT COUNT(*) FROM "{t}"').scalar() or 0)
                for t in ("message", "chunk", "conversation", "user")}


def test_sqlite_pragmas_are_applied_per_connection():
    """B1-1：WAL 让 sweeper/后台入库/请求线程能并发写，foreign_keys=ON 让孤儿行当场暴露。"""
    with engine.connect() as conn:
        assert _pragma(conn, "journal_mode") == "wal"
        assert _pragma(conn, "foreign_keys") == 1
        assert int(_pragma(conn, "busy_timeout")) >= 5000
        assert _pragma(conn, "synchronous") == 1        # NORMAL：WAL 下不掉已提交事务


def test_foreign_key_blocks_orphan_message(db: Session):
    """FK 打开后插不存在的会话必须报错——旧实现手写级联漏删就是靠这条兜住的。"""
    with pytest.raises(sa.exc.IntegrityError):
        db.add(Message(conversation_id=99999, role="user", content="孤儿"))
        db.commit()


@pytest.fixture
def legacy_table():
    """造一张"旧表"（缺待补的列），用来走 ALTER 路径而不是 create_all 路径。"""
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE IF EXISTS legacy_demo")
        conn.exec_driver_sql("CREATE TABLE legacy_demo (id INTEGER PRIMARY KEY, name TEXT)")
    yield "legacy_demo"
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE IF EXISTS legacy_demo")


def test_column_migration_is_idempotent(legacy_table, monkeypatch):
    monkeypatch.setattr(migrate, "ADDITIVE_COLUMNS",
                        [(legacy_table, "role", "VARCHAR NOT NULL DEFAULT 'user'")])
    with engine.begin() as conn:
        conn.exec_driver_sql("INSERT INTO legacy_demo (id, name) VALUES (1, 'old')")

    for _ in range(3):                      # 连跑三次 = 重启三次，结果必须一致
        migrate.apply_column_migrations(engine)
    with engine.begin() as conn:
        cols = {r[1] for r in conn.exec_driver_sql(f'PRAGMA table_info("{legacy_table}")')}
        assert "role" in cols
        assert conn.exec_driver_sql(
            "SELECT role FROM legacy_demo WHERE id=1").scalar() == "user"   # 旧行拿到默认值


def test_registered_migration_list_matches_models():
    """ADDITIVE_COLUMNS 登记的列必须真的还在模型上：补一个模型没有的列等于悄悄失效。"""
    from sqlmodel import SQLModel

    for table, column, _ddl in migrate.ADDITIVE_COLUMNS:
        t = SQLModel.metadata.tables.get(table)
        assert t is not None, f"{table} 表不存在，迁移声明需删"
        assert column in t.columns, f"{table}.{column} 已不在模型里，迁移声明需删"


def test_ensure_admin_promotes_only_when_no_admin(db: Session):
    """老库补完 role 列后全是 user，管理端会没人能用：把最早注册的账号提上来。"""
    db.add_all([User(username="first", hashed_password="x", role="user"),
                User(username="second", hashed_password="x", role="user")])
    db.commit()

    migrate._ensure_admin(engine)          # noqa: SLF001 - 就是要单独验这个兜底
    with Session(engine) as s:
        roles = {u.username: u.role for u in s.exec(select(User)).all()}
    assert roles == {"first": "admin", "second": "user"}

    with Session(engine) as s:             # 已有 admin 之后再跑不得改动任何人
        second = s.exec(select(User).where(User.username == "second")).first()
        second.role = "admin"
        s.add(second)
        s.commit()
    migrate._ensure_admin(engine)          # noqa: SLF001
    with Session(engine) as s:
        roles = {u.username: u.role for u in s.exec(select(User)).all()}
    assert roles == {"first": "admin", "second": "admin"}


def _insert_orphans() -> None:
    """模拟旧版本级联不全留下的孤儿行：FK 打开后 ORM 写不进去，只能绕过它制造脏数据。"""
    with engine.connect() as conn:
        conn.exec_driver_sql("PRAGMA foreign_keys=OFF")
        conn.exec_driver_sql("INSERT INTO message (conversation_id, role, content, created_at) "
                             "VALUES (99999, 'user', '孤儿消息', '2026-01-01 00:00:00')")
        conn.exec_driver_sql("INSERT INTO chunk (file_id, seq, text, tokens, created_at) "
                             "VALUES (88888, 1, '孤儿切片', 1, '2026-01-01 00:00:00')")
        conn.exec_driver_sql("PRAGMA foreign_keys=ON")
        conn.commit()


def test_cleanup_orphans_removes_dangling_rows(db: Session):
    user = User(username="owner", hashed_password="x", role="user")
    db.add(user)
    db.commit()
    conv = Conversation(user_id=user.id, title="留着")
    db.add(conv)
    db.commit()
    db.add(Message(conversation_id=conv.id, role="user", content="正常消息"))
    db.commit()
    _insert_orphans()

    migrate.cleanup_orphans(engine)

    with Session(engine) as s:
        assert s.exec(select(Message).where(Message.content == "正常消息")).first() is not None
        assert s.exec(select(Message).where(Message.content == "孤儿消息")).first() is None
        assert s.exec(select(Chunk).where(Chunk.file_id == 88888)).first() is None
        assert s.get(Conversation, conv.id) is not None


def test_cleanup_orphans_is_repeatable(db: Session):
    """启动自检每个进程都要跑，第二遍必须一条都不删。"""
    _insert_orphans()
    migrate.cleanup_orphans(engine)
    before = _counts()
    migrate.cleanup_orphans(engine)
    assert _counts() == before


def test_delete_conversation_cascades_every_child(db: Session):
    """删会话要一次清掉消息/摘要/工单/平台映射，残留任一行都会撞 FK 或在列表里留幽灵。"""
    user = User(username="u1", hashed_password="x", role="user")
    db.add(user)
    db.commit()
    conv = Conversation(user_id=user.id, title="待删")
    db.add(conv)
    db.commit()
    cid = conv.id
    db.add_all([
        Message(conversation_id=cid, role="user", content="m"),
        ConversationSummary(conversation_id=cid, summary="摘要", until_message_id=1),
        HandoffTicket(conversation_id=cid, user_id=user.id, reason="要人工"),
        ExternalConversation(platform="taobao", shop_id="s", buyer_id="b", conversation_id=cid),
    ])
    db.commit()
    db.add(InboundDedupe(platform="taobao", shop_id="s", buyer_id="b", external_msg_id="k1"))
    db.commit()

    delete_conversation(db, conv)

    with Session(engine) as s:
        for model in (Message, ConversationSummary, HandoffTicket, ExternalConversation):
            rows = s.exec(select(model).where(model.conversation_id == cid)).all()
            assert rows == [], f"{model.__name__} 残留 {len(rows)} 行"
        assert s.get(Conversation, cid) is None
        # 幂等键不属于某个会话：跟着删会让平台重推的历史消息被再处理一遍
        assert s.exec(select(InboundDedupe).where(
            InboundDedupe.external_msg_id == "k1")).first() is not None


def test_delete_conversation_survives_legacy_orphans(db: Session):
    """存量库里有孤儿行时（旧数据），先 init_db 自愈再删才不会被 FK 拦下。"""
    user = User(username="u2", hashed_password="x", role="user")
    db.add(user)
    db.commit()
    conv = Conversation(user_id=user.id, title="有孤儿干扰")
    db.add(conv)
    db.commit()
    _insert_orphans()

    init_db()                       # 启动自检：补列 + 清孤儿 + 回收中断任务
    with Session(engine) as s:
        delete_conversation(s, s.get(Conversation, conv.id))
        assert s.get(Conversation, conv.id) is None


def test_knowledge_file_delete_removes_chunks_products_jobs(db: Session):
    """删文件走 rag.ingest.remove_file：chunk/商品/任务行都外键指向它，留一行就删不掉。"""
    kf = KnowledgeFile(name="a.txt", path="x", file_type="text", status="ready")
    db.add(kf)
    db.commit()
    db.add_all([Chunk(file_id=kf.id, seq=1, text="切片", tokens=1),
                Product(file_id=kf.id, name="面霜", specs="30ml", price=39.0)])
    db.commit()
    job = knowledge_jobs.create_job(db, kf, knowledge_jobs.KIND_REINGEST)

    ingest.remove_file(db, kf)

    with Session(engine) as s:
        assert s.get(KnowledgeFile, kf.id) is None
        for model in (Chunk, Product, IngestJob):
            rows = s.exec(select(model).where(model.file_id == kf.id)).all()  # type: ignore[attr-defined]
            assert rows == [], f"{model.__name__} 残留 {len(rows)} 行"
        assert s.get(IngestJob, job.id) is None
