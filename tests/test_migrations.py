from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import text

from muliao.db import Base, Database, alembic_config, create_engine, migrate


async def test_migrated_schema_matches_models(tmp_path):
    """改了模型却没写迁移时失败：运行 `uv run alembic revision --autogenerate` 补上。"""
    engine = create_engine(tmp_path / "m.db")
    await migrate(engine)

    def diff(conn):  # noqa: ANN001, ANN202
        ctx = MigrationContext.configure(conn, opts={"render_as_batch": True})
        return compare_metadata(ctx, Base.metadata)

    async with engine.connect() as conn:
        assert await conn.run_sync(diff) == []
    await engine.dispose()


async def test_migrate_is_idempotent_and_reaches_head(tmp_path):
    engine = create_engine(tmp_path / "m.db")
    await migrate(engine)
    await migrate(engine)

    head = ScriptDirectory.from_config(alembic_config()).get_current_head()
    async with engine.connect() as conn:
        assert (await conn.execute(text("select version_num from alembic_version"))).scalar() == head
    await engine.dispose()


async def test_foreign_keys_are_enforced_after_migration(tmp_path):
    engine = create_engine(tmp_path / "m.db")
    await migrate(engine)
    db = Database(engine)

    async with db.transaction() as s:
        assert (await s.execute(text("PRAGMA foreign_keys"))).scalar() == 1
    await engine.dispose()
