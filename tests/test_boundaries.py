"""模块依赖规则（docs/architecture.md 2.3），对源码的 import 做静态检查：

- 领域包之间只能 import 对方的 api 模块（命令、查询、数据结构）与 events 模块（它发布的事件与接收的命令），
  不能碰对方的表与内部模块；
- platform 不 import 任何领域；
- 领域不 import HTTP 适配层（api 包）与组合根（container）；
- HTTP 适配层经领域的 api（与 events）调用领域，另外可以 import 领域的 tables，把领域交给它的行序列化成响应。
"""

import ast
from pathlib import Path

SRC = Path(__file__).parent.parent / "src" / "muliao"
DOMAINS = {
    "agents",
    "agent_runtime",
    "files",
    "jobs",
    "notifications",
    "reflection",
    "sessions",
    "triggers",
    "usage",
    "vault",
}
# 一个领域对外公开的模块
PUBLIC = {"api", "events"}
# HTTP 适配层另外可以 import 的：序列化领域交给它的行
HTTP_VISIBLE = PUBLIC | {"tables"}


def _imports(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text())
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            if node.module == "muliao":
                found += [(node.lineno, f"muliao.{alias.name}") for alias in node.names]
            else:
                found += [(node.lineno, f"{node.module}.{alias.name}") for alias in node.names]
        elif isinstance(node, ast.Import):
            found += [(node.lineno, alias.name) for alias in node.names]
    return [(line, name) for line, name in found if name.startswith("muliao.")]


def _violations() -> list[str]:
    problems = []
    for path in SRC.rglob("*.py"):
        parts = path.relative_to(SRC).parts
        owner = parts[0] if len(parts) > 1 else None
        for line, name in _imports(path):
            target = name.split(".")
            where = f"{path.relative_to(SRC)}:{line} imports {name}"
            if owner == "platform" and target[1] in DOMAINS | {"api", "container"}:
                problems.append(f"{where} (platform must not depend on a domain)")
            if owner in DOMAINS:
                if target[1] in ("api", "container"):
                    problems.append(
                        f"{where} (a domain must not depend on the HTTP layer or the composition root)"
                    )
                elif (
                    target[1] in DOMAINS
                    and target[1] != owner
                    and (len(target) < 3 or target[2] not in PUBLIC)
                ):
                    problems.append(f"{where} (only {target[1]}.api and {target[1]}.events are public)")
            if owner == "api" and target[1] in DOMAINS and (len(target) < 3 or target[2] not in HTTP_VISIBLE):
                problems.append(f"{where} (the HTTP layer uses {target[1]}.api, .events and .tables only)")
    return sorted(problems)


def test_domains_only_use_each_others_api():
    assert _violations() == []


async def test_a_transaction_writing_two_domains_tables_fails(container, make_agent):
    import pytest
    from sqlalchemy import update

    from muliao.notifications.tables import Notification
    from muliao.vault.tables import Secret

    await make_agent("alpha")
    with pytest.raises(RuntimeError, match="several domains"):
        async with container.db.transaction() as s:
            s.add(Notification(title="t", body="b", session_id=None))
            await s.execute(update(Secret).values(description="x"))


async def test_a_domain_and_the_task_table_may_be_written_together(container):
    from muliao.notifications.tables import Notification

    async with container.db.transaction() as s:
        await container.notifications.enqueue_in(s, None, Notification(title="t", body="b", session_id=None))


# 领域公开接口里在调用方事务里执行的函数（参数里有调用方的事务）：只能是只读的查询（docs/architecture.md 2.2）。
# 写入一律是被调领域自己开事务的命令，调用方不替它管事务。读的事实只会单向变化或不变时，调用方可以据它在同一事务里
# 写自己的数据；否则（SessionActivity 的两个查询）只能先查后写。新增一个这样的函数要先确认它只读，再登记到这里。
READ_ONLY_IN_CALLER_TRANSACTION = {
    "agents": {"active_agent_in", "agent_in"},
    "files": {"load_metas"},
    "sessions": {
        "session_in",
        "check_receivable_in",
        "hold_registered_in",
        "SessionActivity.has_pending_work_in",
        "SessionActivity.blocks_agent_deletion_in",
    },
}


def _takes_a_transaction(fn: object) -> bool:
    """参数里有调用方的事务：名为 s，或注解是 AsyncSession（模块用 from __future__ import annotations 时注解是字符串）。"""
    import inspect

    for name, param in inspect.signature(fn).parameters.items():
        if name == "self":
            continue
        annotation = param.annotation
        text = annotation if isinstance(annotation, str) else getattr(annotation, "__name__", "")
        if name == "s" or "AsyncSession" in str(text):
            return True
    return False


def test_public_functions_in_the_callers_transaction_are_reviewed_read_only_queries():
    import importlib
    import inspect

    found: dict[str, set[str]] = {}
    for domain in sorted(DOMAINS):
        if not (SRC / domain / "api.py").exists():
            continue
        module = importlib.import_module(f"muliao.{domain}.api")
        for name in module.__all__:
            obj = getattr(module, name)
            if inspect.isfunction(obj) and _takes_a_transaction(obj):
                found.setdefault(domain, set()).add(name)
            elif inspect.isclass(obj):
                for method_name, member in inspect.getmembers(obj):
                    # classmethod 经类访问时是绑定方法：取出底层函数再看
                    method = getattr(member, "__func__", member)
                    if (
                        not method_name.startswith("_")
                        and inspect.isfunction(method)
                        and _takes_a_transaction(method)
                    ):
                        found.setdefault(domain, set()).add(f"{name}.{method_name}")
    assert found == READ_ONLY_IN_CALLER_TRANSACTION


# 组合根注入给删除 agent 的阻止查询（不经 api 模块，同样在调用方的事务里执行）：也只能是审查过的只读查询
READ_ONLY_DELETION_BLOCKERS = {
    "SessionActivity.blocks_agent_deletion_in",
    "ReflectionScheduler.blocks_agent_deletion_in",
}


async def test_injected_agent_deletion_blockers_are_reviewed_read_only_queries(container):
    blockers = container.agent_lifecycle._blockers  # noqa: SLF001
    assert {b.__qualname__ for b in blockers.values()} == READ_ONLY_DELETION_BLOCKERS
