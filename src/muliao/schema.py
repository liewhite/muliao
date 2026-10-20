"""全部领域的表：Alembic 与迁移测试从这里拿到完整的 metadata（各领域的表定义在各自的 tables 模块里）；表属于哪个
领域也由这里推导（表定义所在的包）。"""

from muliao.agents import tables as agents  # noqa: F401
from muliao.delegations import tables as delegations  # noqa: F401
from muliao.files import tables as files  # noqa: F401
from muliao.jobs import tables as jobs  # noqa: F401
from muliao.notifications import tables as notifications  # noqa: F401
from muliao.platform import tables as platform  # noqa: F401
from muliao.platform.db import Base  # noqa: E402  先导入全部表定义，映射才完整
from muliao.reflection import tables as reflection  # noqa: F401
from muliao.sessions import tables as sessions  # noqa: F401
from muliao.triggers import tables as triggers  # noqa: F401
from muliao.vault import tables as vault  # noqa: F401

# 平台的表（任务队列）是每个领域的 outbox，可以和任何一个领域的表在同一个事务里写
_SHARED = "platform"


def _domains() -> dict[str, str]:
    return {m.local_table.name: m.class_.__module__.split(".")[1] for m in Base.registry.mappers}


def single_domain_writes(tables: frozenset[str]) -> None:
    """一个写事务只写一个领域的表（加上平台的任务表）：领域之间只经命令与事件通信，强一致只在一个领域内部
    （docs/domain-design.md 原则 1、4，第 10 节的开发期断言）。"""
    domains = _domains()
    unknown = sorted(tables - domains.keys())
    if unknown:
        raise RuntimeError(f"transaction wrote tables no domain owns: {unknown}")
    touched = {domains[t] for t in tables} - {_SHARED}
    if len(touched) > 1:
        raise RuntimeError(
            f"a transaction wrote tables of several domains {sorted(touched)}: {sorted(tables)}; "
            "domains talk through commands and events, each in its own transaction"
        )
