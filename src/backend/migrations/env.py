"""Alembic 运行环境：迁移用的连接、以及 autogenerate 的结构比对口径都从这里出。

为什么不在这里读 alembic.ini 的 sqlalchemy.url：
本项目 database.url 的唯一入口是 app.config（CS_CONFIG 指向的文件 > config.simple.yaml），
而且 SQLite 的相对路径还要锚定到后端根目录——否则 uvicorn 换个 cwd 就指着另一个库文件。
这两件事 app/db/session.py 已经做过一次，所以默认**复用它的 engine**，不写第二套 URL 解析。
唯一例外是 alembic 侧显式给了 sqlalchemy.url（编程式 `config.set_main_option("sqlalchemy.url", ...)`，
测试用临时库就走这条），这时另建 engine；该路径请传绝对路径，相对路径漂移本来就是要避免的东西。

target_metadata 用 SQLModel.metadata：表和 app/** 用的是同一个对象，
所以 create_all 建出来的结构与迁移建出来的结构不存在两套口径。
"""
import sys
from pathlib import Path
from typing import Any

# env.py 可能被 alembic 以任意 cwd 加载，先把后端根放进 sys.path（alembic.ini 的
# prepend_sys_path 也做了同一件事，这里兜底，保证单独 copy 这个文件也能跑）。
_BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(_BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(_BACKEND_ROOT))

from alembic import context  # noqa: E402
from sqlalchemy import Engine, create_engine, make_url  # noqa: E402
from sqlmodel import SQLModel  # noqa: E402

import app.db.models  # noqa: E402, F401  导入即把全部表注册进 SQLModel.metadata

config = context.config

# 迁移的目标结构：与运行时 create_all 完全同一个 MetaData
target_metadata = SQLModel.metadata


def _override_url() -> str | None:
    """alembic 侧显式指定的库：编程式 `set_main_option("sqlalchemy.url", ...)`（测试走这条）。

    本版本 alembic 的 CLI 没有覆盖 ini 选项的参数，命令行想换库就改 CS_CONFIG 指向的配置文件。
    """
    return config.get_main_option("sqlalchemy.url")


def _app_engine() -> Engine:
    """按 app.config 建好的 engine。延迟导入：本文件常在 app.db.session 已加载后才执行。"""
    from app.db.session import engine

    return engine


def _bind() -> Engine:
    url = _override_url()
    return create_engine(url) if url else _app_engine()


def _compare_opts(dialect_name: str) -> dict[str, Any]:
    """结构比对口径（autogenerate 用它判断"有什么变了"）。

    compare_type：默认 False 会导致"改了列类型"静默不生成迁移，与引入 alembic 的初衷相反。
    compare_server_default：SQLModel 的 Field(default=...) 走的是 Python 侧默认值、不落
        DDL DEFAULT，所以这条不会误报；开了之后万一有人显式写 server_default 也不会漏。
    render_as_batch：SQLite 只支持 ADD COLUMN/DROP COLUMN，改类型/加约束必须走
        "建新表-拷数据-改名-重建索引" 的 batch 模式，否则生成的 ALTER 直接报错；
        MySQL 原生支持这些 ALTER，batch 会原样下压，所以两边都安全。
    render_item：见 _render_item，SQLModel 的类型必须走它，否则生成的迁移跑不起来。
    """
    return {
        "compare_type": True,
        "compare_server_default": True,
        "render_as_batch": dialect_name == "sqlite",
        "render_item": _render_item,
        "include_schemas": False,
    }


def _render_item(kind: str, obj: Any, autogen_context: Any) -> str | bool:
    """把 SQLModel 自定义列类型渲染成"短名 + 自动补 import"。

    alembic 对不是 sqlalchemy.* 的类型只输出全路径 repr（sqlmodel.sql.sqltypes.AutoString()）
    而**不会写对应 import**，生成的迁移一执行就是 NameError。这里返回短名写法并把
    `from sqlmodel.sql import sqltypes` 登记进 autogen_context.imports，
    于是每次 autogenerate 出来的文件都自带这条 import（顺序也正好符合 ruff/isort）。

    返回 False 才是"这个对象我不处理，走 alembic 默认渲染"——注意契约不是 None：
    返回 None 会被当成"渲染结果就是 None"，整行 sa.Column(...) 直接消失（静默生成空表）。
    sa.* 的类型与 column/table/index 等其它 kind 都走默认分支。
    """
    if kind != "type":
        return False
    module = type(obj).__module__
    if not module.startswith("sqlmodel"):
        return False
    text = repr(obj)
    if not text.startswith(f"{module}."):
        # 有些类型自定义了 __repr__（sqlmodel 的 UTCDateTime 只回 "UTCDateTime()"），补上模块前缀
        text = f"{module}.{text}"
    autogen_context.imports.add("from sqlmodel.sql import sqltypes")
    return f"sqltypes.{text.removeprefix(f'{module}.')}"


def run_migrations_offline() -> None:
    """离线模式（alembic upgrade head --sql）：只按方言把 DDL 打到标准输出，不连库。

    生产 DBA 要预审/手工执行时走这条；literal_binds 让脚本里不出现占位符，可直接复制粘贴。
    """
    url = _override_url() or _app_engine().url.render_as_string(hide_password=False)
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        # get_dialect() 只加载方言类，不会 import DBAPI，所以 --sql 模式不需要装 pymysql
        **_compare_opts(make_url(url).get_dialect().name),
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """在线模式：连到库上真正执行（生产启动时的 database.migrate_mode=alembic 走的就是这里）。"""
    connectable = _bind()
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            transaction_per_migration=True,
            **_compare_opts(connection.dialect.name),
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
