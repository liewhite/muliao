import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, DateTime, event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.types import TypeDecorator


def utcnow() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator[datetime]):
    """SQLite 不保存时区：入库统一转为 UTC naive，出库补回 UTC。拒绝 naive 输入，避免本地时间混入。"""

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: object) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError(f"naive datetime is not allowed: {value!r}")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect: object) -> datetime | None:
        return None if value is None else value.replace(tzinfo=UTC)


class Base(DeclarativeBase):
    pass


def create_engine(db_path: Path) -> AsyncEngine:
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")

    @event.listens_for(engine.sync_engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record):  # noqa: ANN001
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.close()

    return engine


MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def alembic_config(connection: Connection | None = None) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.attributes["connection"] = connection
    return cfg


def _upgrade(connection: Connection) -> None:
    # batch 迁移会「建新表-拷数据-删旧表」，外键开着时删旧表会触发 ON DELETE CASCADE 删掉子表数据。
    # PRAGMA foreign_keys 在事务内无效，必须在迁移事务开始前关闭，结束后校验完整性再打开。
    connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
    disabled = connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 0
    # 结束 SQLAlchemy 的自动事务（pysqlite 不会为 PRAGMA 发 BEGIN，这里只是关闭逻辑事务）
    connection.commit()
    if not disabled:
        raise RuntimeError("could not disable SQLite foreign keys before migrating")
    try:
        with connection.begin():
            command.upgrade(alembic_config(connection), "head")
            violations = connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise RuntimeError(f"migration left foreign key violations, rolled back: {violations[:10]}")
    finally:
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")
        connection.commit()


async def migrate(engine: AsyncEngine) -> None:
    """把数据库升级到最新结构。启动时调用；已是最新时什么也不做。"""
    from muliao import models  # noqa: F401  注册所有表

    async with engine.connect() as conn:
        await conn.run_sync(_upgrade)


class Database:
    """事务入口。退出时提交，异常时回滚。

    SQLite 只有一个写者：进程内用锁把事务串行化，换来无丢失更新、无 SQLITE_BUSY，
    代价是事务内禁止做慢操作（模型调用、网络 IO）——所有调用方都遵守这一点。
    """

    def __init__(self, engine: AsyncEngine):
        self.engine = engine
        self._factory = async_sessionmaker(engine, expire_on_commit=False)
        self._lock = asyncio.Lock()
        self._holder: asyncio.Task[object] | None = None

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncSession]:
        current = asyncio.current_task()
        if current is not None and current is self._holder:
            raise RuntimeError("nested Database.transaction() would deadlock; pass the open session instead")
        async with self._lock:
            self._holder = current
            try:
                async with self._factory() as s, s.begin():
                    yield s
            finally:
                self._holder = None
