"""迁移脚本共用的操作（只在 alembic 迁移里调用，依赖 alembic 的 op 上下文）。"""

from collections.abc import Iterator
from contextlib import contextmanager

from alembic import op


@contextmanager
def keep_sequence(table: str) -> Iterator[None]:
    """包住会重建表的 batch 操作：重建会把 sqlite_sequence 重置为现存的最大 id，删掉的最大编号会被复用。

    记下重建前的值，重建后恢复（取两者的较大值），AUTOINCREMENT 的编号永不复用的保证因此不被迁移打破。
    表还没有分配过编号（sqlite_sequence 里没有这一行）时什么都不用做；重建后表为空（新表没有序号行）时补一行。
    """
    bind = op.get_bind()
    seq = bind.exec_driver_sql("SELECT seq FROM sqlite_sequence WHERE name = ?", (table,)).scalar()
    yield
    if seq is None:
        return
    updated = bind.exec_driver_sql(
        "UPDATE sqlite_sequence SET seq = MAX(seq, ?) WHERE name = ?", (int(seq), table)
    ).rowcount
    if updated == 0:
        # 重建后表是空的：新表还没有序号行，补上
        bind.exec_driver_sql("INSERT INTO sqlite_sequence (name, seq) VALUES (?, ?)", (table, int(seq)))
