import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

import anyio
from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, DateTime, Engine, String, event
from sqlalchemy import create_engine as sa_create_engine
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.pool import NullPool
from sqlalchemy.types import TypeDecorator


def utcnow() -> datetime:
    return datetime.now(UTC)


class StrEnumType[E: StrEnum](TypeDecorator[E]):
    """以字符串存储 StrEnum，读出时还原为枚举。库里出现未知取值即报错，不让裸字符串混入业务逻辑。"""

    impl = String(16)
    cache_ok = True

    def __init__(self, enum_cls: type[E]):
        super().__init__()
        self._enum = enum_cls

    def process_bind_param(self, value: E | None, dialect: object) -> str | None:
        if value is None:
            return None
        return self._enum(value).value

    def process_result_value(self, value: str | None, dialect: object) -> E | None:
        return None if value is None else self._enum(value)


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


MIGRATIONS_DIR = Path(__file__).parent.parent / "migrations"


def alembic_config(connection: Connection | None = None) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.attributes["connection"] = connection
    return cfg


def _migration_engine(db_path: Path) -> Engine:
    """迁移专用的同步引擎。

    pysqlite 默认只在 DML 前隐式 BEGIN，DDL 会逐条自动提交，导致迁移失败时只回滚一半。这里把驱动设为
    autocommit（isolation_level=None），由 begin 事件显式发 BEGIN，DDL 才真正处于事务中。

    batch 迁移会「建新表-拷数据-删旧表」，外键开着时删旧表会触发 ON DELETE CASCADE 删掉子表数据。
    PRAGMA foreign_keys 在事务内无效，所以在建立连接时（任何事务之前）关闭。
    """
    engine = sa_create_engine(
        f"sqlite:///{db_path}", connect_args={"isolation_level": None}, poolclass=NullPool
    )

    @event.listens_for(engine, "connect")
    def _foreign_keys_off(dbapi_conn, _record):  # noqa: ANN001
        dbapi_conn.execute("PRAGMA foreign_keys=OFF")
        if dbapi_conn.execute("PRAGMA foreign_keys").fetchone()[0] != 0:
            raise RuntimeError("could not disable SQLite foreign keys before migrating")

    @event.listens_for(engine, "begin")
    def _explicit_begin(conn: Connection) -> None:
        conn.exec_driver_sql("BEGIN IMMEDIATE")

    return engine


def migrate(db_path: Path, target: str = "head") -> None:
    """把数据库升级到最新结构，整体在一个事务里：任何失败都完整回滚。启动时调用；已是最新时什么也不做。

    同步执行（启动阶段由调用方放进线程）。迁移连接用完即关闭，不进入应用的连接池，应用连接的外键始终开启。
    """
    engine = _migration_engine(db_path)
    try:
        with engine.begin() as connection:
            command.upgrade(alembic_config(connection), target)
            violations = connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise RuntimeError(
                    f"migration would leave foreign key violations, rolled back: {violations[:10]}"
                )
    finally:
        engine.dispose()


class Database:
    """事务入口。退出时提交，异常时回滚。

    SQLite 只有一个写者：进程内用锁把事务串行化，换来无丢失更新、无 SQLITE_BUSY，
    代价是事务内禁止做慢操作（模型调用、网络 IO）——所有调用方都遵守这一点。

    取消不能打断进行中的语句：打断会让 SQLAlchemy 作废连接，后台的优雅关闭可能永远等不到结果，事件循环关闭时卡住。
    - anyio 的取消：每个事务整体屏蔽（transaction），取消在事务结束后生效。交换：关停与超时要等进行中的事务做完，
      数据库卡住时它们也跟着等。
    - 原生取消（task.cancel()，屏蔽挡不住）：只能在 exclusive 里发出——那时没有任何事务在进行，被取消的任务只会在
      等锁处（安全）收到取消。
    """

    def __init__(self, engine: AsyncEngine):
        self.engine = engine
        self._factory = async_sessionmaker(engine, expire_on_commit=False)
        self._lock = asyncio.Lock()
        self._holder: asyncio.Task[object] | None = None

    @asynccontextmanager
    async def exclusive(self) -> AsyncIterator[None]:
        """没有任何事务在进行的一段时间（持有事务锁）。原生取消只在这里面发出（sessions.stopping）。"""
        current = asyncio.current_task()
        if current is not None and current is self._holder:
            raise RuntimeError("Database.exclusive() inside a transaction would deadlock")
        with anyio.CancelScope(shield=True):
            async with self._lock:
                # 在这里面开事务会自锁：transaction 按持有者报错
                self._holder = current
                try:
                    yield
                finally:
                    self._holder = None

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncSession]:
        current = asyncio.current_task()
        if current is not None and current is self._holder:
            raise RuntimeError("nested Database.transaction() would deadlock; pass the open session instead")
        # 事务很短（契约：事务内不做慢操作），整体屏蔽 anyio 的取消，取消在事务结束后生效。否则电平触发的取消会
        # 打断进行中的语句：SQLAlchemy 作废连接并在后台优雅关闭它，关闭又被再次取消而强制停掉 aiosqlite 的线程，
        # 留下一个永远等不到结果的关闭任务，事件循环关闭时卡住（实测：测试与关停时挂起）
        # 等锁可以被取消（不在任何语句中）；拿到锁之后整个事务屏蔽 anyio 的取消
        async with self._lock:
            with anyio.CancelScope(shield=True):
                self._holder = current
                try:
                    async with self._factory() as s, s.begin():
                        yield s
                finally:
                    self._holder = None
