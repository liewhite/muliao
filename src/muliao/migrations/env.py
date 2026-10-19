"""Alembic 环境。运行时由 muliao.db.migrate 传入已关闭外键的连接；开发时由仓库根目录 alembic.ini 驱动。"""

from alembic import context
from sqlalchemy import create_engine

from muliao import models  # noqa: F401  注册所有表
from muliao.db import Base

config = context.config


def _run(connection) -> None:  # noqa: ANN001
    # SQLite 不支持大多数 ALTER，batch 模式以「建新表-拷数据-换名」实现
    context.configure(connection=connection, target_metadata=Base.metadata, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()


connection = config.attributes.get("connection")
if connection is not None:
    _run(connection)
else:
    with create_engine(config.get_main_option("sqlalchemy.url")).connect() as conn:
        _run(conn)
