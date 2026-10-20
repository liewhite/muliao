"""Alembic 环境。运行时由 muliao.platform.db.migrate 传入已关闭外键的连接；开发时由仓库根目录 alembic.ini 驱动。"""

from alembic import context
from sqlalchemy import create_engine

from muliao import models  # noqa: F401  注册所有表
from muliao.platform.db import Base, StrEnumType, UTCDateTime

config = context.config


def _render_item(type_: str, obj: object, autogen_context: object) -> str | bool:
    """迁移里只写库里的实际类型，不依赖应用里的类型包装：枚举列是字符串，时间列是 DateTime。"""
    if type_ == "type" and isinstance(obj, StrEnumType):
        return f"sa.String(length={obj.impl.length})"
    if type_ == "type" and isinstance(obj, UTCDateTime):
        return "sa.DateTime()"
    return False


def _run(connection) -> None:  # noqa: ANN001
    # SQLite 不支持大多数 ALTER，batch 模式以「建新表-拷数据-换名」实现
    context.configure(
        connection=connection, target_metadata=Base.metadata, render_as_batch=True, render_item=_render_item
    )
    with context.begin_transaction():
        context.run_migrations()


connection = config.attributes.get("connection")
if connection is not None:
    _run(connection)
else:
    with create_engine(config.get_main_option("sqlalchemy.url")).connect() as conn:
        _run(conn)
