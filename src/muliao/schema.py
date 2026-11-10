"""全部领域的表：Alembic 与迁移测试从这里拿到完整的 metadata（各领域的表定义在各自的 tables 模块里）；表属于哪个
领域也由这里推导（表定义所在的包），据此查一个事务只写一个领域、给前端发「哪个领域的数据变了」。"""

from functools import cache
from typing import Literal

from pydantic import BaseModel

from muliao.agents import tables as agents  # noqa: F401
from muliao.files import tables as files  # noqa: F401
from muliao.history import tables as history  # noqa: F401
from muliao.issues import tables as issues  # noqa: F401
from muliao.notifications import tables as notifications  # noqa: F401
from muliao.platform import tables as platform  # noqa: F401
from muliao.platform.db import Base  # noqa: E402  先导入全部表定义，映射才完整
from muliao.sessions import tables as sessions  # noqa: F401
from muliao.triggers import tables as triggers  # noqa: F401
from muliao.usage import tables as usage  # noqa: F401
from muliao.workspace import tables as workspace  # noqa: F401

# 平台的表（任务队列）是每个领域的 outbox，可以和任何一个领域的表在同一个事务里写
_SHARED = "platform"


@cache
def _domains() -> dict[str, str]:
    return {m.local_table.name: m.class_.__module__.split(".")[1] for m in Base.registry.mappers}


# 有表的领域（平台的任务表除外：任务状态另有 TaskEvent）。写成字面量是为了进 OpenAPI、前端按它类型化；与表推导出的
# 领域一致由测试守住
Domain = Literal[
    "agents", "files", "history", "issues", "notifications", "sessions", "triggers", "usage", "workspace"
]


class ChangedEvent(BaseModel):
    """一个领域的数据变了（推给前端 SSE，展示它的页面从接口重新拉取）：写过它的表的事务提交后发（platform.db 的
    after_commit），平台自己改 AI 资源文件后也以 agents 发（agents.source）。不管是谁改的：页面自己、agent、触发器、
    推送通知上的操作都一样。"""

    type: Literal["changed"] = "changed"
    resource: Domain


def changes_of(tables: frozenset[str]) -> list[ChangedEvent]:
    """写了这些表，哪些领域的数据变了（按领域名）。"""
    domains = _domains()
    return [
        ChangedEvent.model_validate({"resource": d}) for d in sorted({domains[t] for t in tables} - {_SHARED})
    ]


def single_domain_writes(tables: frozenset[str]) -> None:
    """一个写事务只写一个领域的表（加上平台的任务表）：领域之间只经命令与事件通信，强一致只在一个领域内部
    （docs/architecture.md 2.2、2.3）。"""
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
