"""0002: 新增 sessionrating（会话满意度评分）

B3-13 加了结单后的签名回访评分，表是新增的、全是可空/有默认值的列，
所以老库直接 upgrade 就能上，不需要重建。
生产切了 `database.migrate_mode=alembic` 的实例，这条必须先于代码上线跑；
`auto` 模式的开发库由 create_all 建表，两边结构一致（test_migrations 的
"空 diff" 断言会在不一致时失败）。

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-27 13:28:40

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlmodel.sql import sqltypes

revision: str = '0002'
down_revision: str = '0001'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('sessionrating',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('conversation_id', sa.Integer(), nullable=False),
    sa.Column('rating', sa.Integer(), nullable=False),
    sa.Column('comment', sqltypes.AutoString(), nullable=False),
    sa.Column('source', sqltypes.AutoString(), nullable=False),
    sa.Column('created_at', sqltypes.UTCDateTime(), nullable=False),
    sa.ForeignKeyConstraint(['conversation_id'], ['conversation.id'], ),
    sa.PrimaryKeyConstraint('id')
    )
    # conversation_id 唯一 = 一会话一票：重复提交走应用层 409，而不是覆盖第一票
    with op.batch_alter_table('sessionrating', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_sessionrating_conversation_id'),
                              ['conversation_id'], unique=True)
        batch_op.create_index(batch_op.f('ix_sessionrating_created_at'), ['created_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_sessionrating_source'), ['source'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('sessionrating', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_sessionrating_source'))
        batch_op.drop_index(batch_op.f('ix_sessionrating_created_at'))
        batch_op.drop_index(batch_op.f('ix_sessionrating_conversation_id'))
    op.drop_table('sessionrating')
