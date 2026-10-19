import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import DateTime, event
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


async def init_schema(engine: AsyncEngine) -> None:
    from muliao import models  # noqa: F401  注册所有表

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


_in_transaction: ContextVar[bool] = ContextVar("in_transaction", default=False)


class Database:
    """事务入口。退出时提交，异常时回滚。

    SQLite 只有一个写者：进程内用锁把事务串行化，换来无丢失更新、无 SQLITE_BUSY，
    代价是事务内禁止做慢操作（模型调用、网络 IO）——所有调用方都遵守这一点。
    """

    def __init__(self, engine: AsyncEngine):
        self.engine = engine
        self._factory = async_sessionmaker(engine, expire_on_commit=False)
        self._lock = asyncio.Lock()

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncSession]:
        if _in_transaction.get():
            raise RuntimeError("nested Database.transaction() would deadlock; pass the open session instead")
        token = _in_transaction.set(True)
        try:
            async with self._lock, self._factory() as s, s.begin():
                yield s
        finally:
            _in_transaction.reset(token)
