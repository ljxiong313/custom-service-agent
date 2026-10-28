<%doc>
模板说明（本块不会出现在生成的迁移文件里）。刻意与 alembic 默认模板有三处不同，
都是为了让迁移文件直接过仓库闸门（闸门跑 ruff check src，会扫到 migrations/versions/）：
1) import 顺序按 ruff/isort 的结果排：直入 import sqlalchemy as sa 在前、
   from alembic import op 在后；默认模板反过来，会被 I001 判为未排序。
   sqlmodel 的类型 import 由 env.py 的 _render_item 追加到 op 那行后面（空行不留垃圾）。
2) 类型注解用 PEP 604 与 collections.abc.Sequence；默认模板的 typing.Union /
   typing.Sequence 会被 UP007 / UP035 判为过时写法。
3) 生成后仍要人工读过再提交：autogenerate 只是草稿，downgrade 必须真能执行。
   根迁移（无 down_revision）时 `Revises:` 行会带一个尾随空格，被 W291 判掉，手工去掉即可。
</%doc>"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op${"\n" + imports if imports else ""}

revision: str = ${repr(up_revision)}
down_revision: str | Sequence[str] | None = ${repr(down_revision)}
branch_labels: str | Sequence[str] | None = ${repr(branch_labels)}
depends_on: str | Sequence[str] | None = ${repr(depends_on)}


def upgrade() -> None:
    """建表/加列/加索引：结构变更的唯一入口。"""
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    """upgrade 的反操作：必须真的能执行，不要写 pass 糊过去。"""
    ${downgrades if downgrades else "pass"}
