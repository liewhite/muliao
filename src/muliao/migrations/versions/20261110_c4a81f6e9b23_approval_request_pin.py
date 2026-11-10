"""approval_requests.pin（#570）：工具请求批准时定下的被批准对象的指纹（skill_run 是 skill 目录的内容指纹），批准后再
执行时与当时的比，不同就不执行（批准后、执行前脚本被改，执行的就不是用户看过的那一份）。可空：已有的行（#570 之前
停下来等批准的）为空，批准后工具当作对不上、让模型重新请求。只加列，不动已有数据。

降级：删列。

Revision ID: c4a81f6e9b23
Revises: 5d1c8e2a7f46
Create Date: 2026-11-10 21:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c4a81f6e9b23"
down_revision: str | None = "5d1c8e2a7f46"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("approval_requests") as batch:
        batch.add_column(sa.Column("pin", sa.String(length=100), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("approval_requests") as batch:
        batch.drop_column("pin")
