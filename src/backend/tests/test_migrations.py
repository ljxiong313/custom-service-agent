"""Alembic 迁移体系自检。

这些用例守的是同一件事：**迁移脚本建出来的库，必须和代码要的库一模一样**。
migrate_mode=auto（开发默认）时 create_all 会替我们兜住结构，"漏表/漏列"在开发机上根本
暴露不了；生产切成 migrate_mode=alembic 后跳过 create_all，漏写迁移的表就只能等第一次
请求踩到它才现形。所以 CI 必须先把这种不一致拦下来，从三个方向夹住 baseline：
1. baseline 在全新库上能跑通，且跑完的表集合 == SQLModel.metadata 的表集合（防漏表）；
2. 跑完 baseline 的库再 autogenerate 必须是**空 diff**（防漏列/漏索引/漏约束，价值最高）；
3. upgrade → downgrade base 能把全部表收回去（防 downgrade 写成 pass 糊过去）。

每个用例都用 tmp_path 下的独立 SQLite 文件，不碰 conftest 那个会话级测试库：
连接口径走 migrations/env.py 的 sqlalchemy.url 覆盖项（见该文件注释）。唯一例外是
"init_db 走 alembic 模式"那条，它刻意不覆盖 URL，就是为了验证迁移复用的是 app.db.session
的 engine——SQLite 相对路径锚定只在 session.py 做过一次，不能有第二套解析。
"""
import pathlib
from collections.abc import Iterator

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlmodel import Session, SQLModel, select

import app.db.models
from app.config import BACKEND_ROOT, get_settings
from app.db import session as db_session
from app.db.models import User

INI_PATH = BACKEND_ROOT / "alembic.ini"
VERSIONS_DIR = BACKEND_ROOT / "migrations" / "versions"
ALEMBIC_VERSION_TABLE = "alembic_version"


def _url(db_path: pathlib.Path) -> str:
    return f"sqlite:///{db_path.as_posix()}"


def _config(db_path: pathlib.Path) -> Config:
    """指向某个临时库的 alembic 配置（绝对路径，避免 SQLite 相对路径漂移）。"""
    cfg = Config(str(INI_PATH))
    cfg.set_main_option("sqlalchemy.url", _url(db_path))
    return cfg


def _tables(engine: sa.Engine) -> set[str]:
    """库里真实存在的表（直接读 sqlite_master，不走 SQLAlchemy 的反射缓存）。"""
    with engine.connect() as conn:
        rows = conn.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {row[0] for row in rows}


def _model_tables() -> set[str]:
    return set(SQLModel.metadata.tables)


def _revision(engine: sa.Engine) -> str | None:
    with engine.connect() as conn:
        row = conn.exec_driver_sql(f"SELECT version_num FROM {ALEMBIC_VERSION_TABLE}").first()
    return str(row[0]) if row else None


def _head() -> str | None:
    """versions/ 里最新的迁移号（不连库）。断言"库跑到了 head"而不是写死 0001，
    这样以后追加 0002 时这些用例仍然有意义。"""
    return ScriptDirectory.from_config(Config(str(INI_PATH))).get_current_head()


@pytest.fixture
def migrated_db(tmp_path: pathlib.Path) -> Iterator[tuple[sa.Engine, Config]]:
    """只由 baseline 建起来的空库：engine 与 alembic 配置指向同一个文件。"""
    db_path = tmp_path / "migrate.db"
    cfg = _config(db_path)
    command.upgrade(cfg, "head")
    engine = sa.create_engine(_url(db_path))
    yield engine, cfg
    engine.dispose()


@pytest.fixture
def tmp_engine(tmp_path: pathlib.Path) -> Iterator[sa.Engine]:
    """供 alembic 模式用例使用的空库（app.db.session.engine 会被换成它）。"""
    engine = sa.create_engine(_url(tmp_path / "session-mode.db"))
    yield engine
    engine.dispose()


def test_baseline_upgrade_creates_every_model_table(migrated_db) -> None:
    """baseline 在全新空库上跑通，一张表都不漏、也不多。"""
    engine, _cfg = migrated_db
    created = _tables(engine)
    assert created - {ALEMBIC_VERSION_TABLE} == _model_tables()
    # 反向也要干净：迁移多建了代码里没有的表，同样是"两套口径"
    assert created - _model_tables() == {ALEMBIC_VERSION_TABLE}
    assert _revision(engine) == _head()


def test_baseline_matches_create_all_structure(migrated_db, tmp_path: pathlib.Path) -> None:
    """表齐了还不够：列、索引、唯一约束、外键都要与 create_all 建出来的库一致。

    两条路径必须等价，否则"开发用 auto、生产用 alembic"就成了两套数据库结构。
    """
    engine, _cfg = migrated_db
    reference_engine = sa.create_engine(_url(tmp_path / "create_all.db"))
    try:
        SQLModel.metadata.create_all(reference_engine)
        for table in sorted(_model_tables()):
            assert _column_facts(reference_engine, table) == _column_facts(engine, table), table
            assert _index_facts(reference_engine, table) == _index_facts(engine, table), table
            assert _constraint_facts(reference_engine, table) == _constraint_facts(engine, table), table
    finally:
        reference_engine.dispose()


def _column_facts(engine: sa.Engine, table: str) -> set[tuple[str, str, bool, bool]]:
    insp = sa.inspect(engine)
    return {(c["name"], str(c["type"]), c["nullable"], bool(c["primary_key"])) for c in insp.get_columns(table)}


def _index_facts(engine: sa.Engine, table: str) -> set[tuple[str, tuple[str, ...], bool]]:
    insp = sa.inspect(engine)
    return {(ix["name"], tuple(ix["column_names"]), bool(ix["unique"])) for ix in insp.get_indexes(table)}


def _constraint_facts(engine: sa.Engine, table: str) -> set[tuple[str, str, tuple[str, ...]]]:
    """唯一约束与外键都按"类型 + 列集合"比：名字由数据库自己生成，跨库不可比。"""
    insp = sa.inspect(engine)
    facts = {("unique", "", tuple(sorted(c["column_names"]))) for c in insp.get_unique_constraints(table)}
    facts |= {
        ("foreignkey", fk["referred_table"], tuple(sorted(fk["constrained_columns"])))
        for fk in insp.get_foreign_keys(table)
    }
    return facts


def test_baseline_downgrade_to_base_leaves_no_model_tables(tmp_path: pathlib.Path) -> None:
    """downgrade base 必须能把全部表收回去（alembic 自己的版本表除外）。"""
    db_path = tmp_path / "migrate.db"
    cfg = _config(db_path)
    command.upgrade(cfg, "head")

    engine = sa.create_engine(_url(db_path))
    try:
        assert _model_tables() <= _tables(engine)
        command.downgrade(cfg, "base")
        assert _tables(engine) - {ALEMBIC_VERSION_TABLE} == set()
    finally:
        engine.dispose()


def test_autogenerate_after_baseline_has_empty_diff(migrated_db) -> None:
    """最重要的防线上：当前代码 vs 刚跑完 baseline 的库，autogenerate 必须是空 diff。

    非空就说明 baseline 漏了东西（表/列/索引/约束/类型），而 migrate_mode=alembic 的生产库
    正是由它建起来的——那时没有任何东西会替我们补上。
    """
    _engine, cfg = migrated_db
    files_before = {p.name for p in VERSIONS_DIR.glob("*.py")}
    try:
        diffs = _autogenerate_diffs(cfg)
        assert diffs == [], f"baseline 与模型不一致：{diffs}"
    finally:
        # autogenerate 走的是"比对不落盘"的分支：versions/ 不该多出草稿文件
        assert {p.name for p in VERSIONS_DIR.glob("*.py")} == files_before


def _autogenerate_diffs(cfg: Config) -> list[str]:
    """跑一次真正的 autogenerate 并取出候选操作，同时阻止 alembic 落盘空迁移。

    用 command.revision 而不是直接调 alembic 的 compare_metadata：这样比对口径
    （compare_type / compare_server_default / render_as_batch / render_item）来自
    migrations/env.py 本身，测试与日常 `alembic revision --autogenerate` 走的是同一套配置。
    """
    captured: list[str] = []

    def _capture(_ctx, _rev, directives):
        script = directives[0]
        captured.extend(str(op) for op in script.upgrade_ops.ops)
        # 清空 directives：这只是一次比对，不要往 versions/ 里写文件
        directives[:] = []

    command.revision(cfg, message="diff check", autogenerate=True,
                     rev_id="0000-diff-check", process_revision_directives=_capture)
    return captured


def test_init_db_alembic_mode_upgrades_and_skips_create_all(
    tmp_engine: sa.Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """migrate_mode=alembic：启动跑 upgrade head，并且**不碰 create_all**。

    把 create_all 换成"调用即失败"是这条用例的关键——否则漏写迁移会被 create_all 悄悄补上，
    而 alembic 模式的全部意义就是让这种漏写在启动时暴露。
    这里不覆盖 sqlalchemy.url：env.py 必须回落到 app.db.session 的 engine（被换成临时库）。
    """
    monkeypatch.setattr(db_session, "engine", tmp_engine)
    monkeypatch.setitem(get_settings()["database"], "migrate_mode", "alembic")

    def _boom(*_args, **_kwargs):
        raise AssertionError("alembic 模式下不能调用 create_all：结构只能来自迁移脚本")

    monkeypatch.setattr(SQLModel.metadata, "create_all", _boom)

    db_session.init_db()

    created = _tables(tmp_engine)
    assert created - {ALEMBIC_VERSION_TABLE} == _model_tables()
    assert _revision(tmp_engine) == _head()


def test_init_db_auto_mode_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    """migrate_mode=auto（默认）：连调两次不报错、表还在、已有数据不动。"""
    monkeypatch.setitem(get_settings()["database"], "migrate_mode", "auto")
    engine = db_session.engine

    db_session.init_db()
    tables_before = _tables(engine)
    assert _model_tables() <= tables_before

    with Session(engine) as session:
        session.add(User(username="keepme", hashed_password="x"))
        session.commit()

    db_session.init_db()

    assert _tables(engine) == tables_before
    with Session(engine) as session:
        assert len(session.exec(select(User).where(User.username == "keepme")).all()) == 1


def test_init_db_rejects_unknown_migrate_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """配错 migrate_mode 要拒绝启动，不能静默回落到 auto（那等于把生产口径改回开发口径）。"""
    monkeypatch.setitem(get_settings()["database"], "migrate_mode", "alembik")
    with pytest.raises(ValueError, match="migrate_mode"):
        db_session.init_db()


def test_metadata_tables_come_from_models_module() -> None:
    """防呆：SQLModel.metadata 与 models.py 里的 table=True 类必须是一回事。

    否则上面那条"表集合"断言会变成自我循环（拿 metadata 比 metadata）。
    """
    declared = {
        klass.__table__.name
        for klass in vars(app.db.models).values()
        if isinstance(klass, type) and hasattr(klass, "__table__")
    }
    assert declared == _model_tables()
