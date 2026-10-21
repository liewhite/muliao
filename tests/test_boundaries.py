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
    "issues",
    "jobs",
    "notifications",
    "reflection",
    "sessions",
    "triggers",
    "speech",
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


def test_every_domain_has_a_public_api_module():
    """边界检查只看得到 api 模块：缺了它，领域的公开接口（如在调用方事务里写入的函数）就不受下面的检查。"""
    packages = {p.name for p in SRC.iterdir() if (p / "__init__.py").exists()} - {
        "api",
        "platform",
        "migrations",
        "webui",
    }
    assert packages == DOMAINS
    assert sorted(d for d in DOMAINS if not (SRC / d / "api.py").exists()) == []


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
        await container.notifications._enqueue_in(  # noqa: SLF001
            s, None, Notification(title="t", body="b", session_id=None)
        )


# 领域公开接口里在调用方事务里执行的函数（参数里有调用方的事务）：只能是只读的查询（docs/architecture.md 2.2）。
# 写入一律是被调领域自己开事务的命令，调用方不替它管事务。读的事实只会单向变化或不变时，调用方可以据它在同一事务里
# 写自己的数据；否则（SessionActivity 的两个查询）只能先查后写。新增一个这样的函数要先确认它只读，再登记到这里。
READ_ONLY_IN_CALLER_TRANSACTION = {
    "agents": {"active_agent_in", "agent_in", "agent_names_in"},
    "files": {"load_metas"},
    "reflection": {"ReflectionScheduler.blocks_agent_deletion_in"},
    "sessions": {
        "session_in",
        "receive_command_in",
        "keyed_sessions_among",
        "hold_registered_in",
        "SessionActivity.work_state_in",
        "SessionActivity.keyed_work_states_in",
        "SessionActivity.blocks_agent_deletion_in",
    },
    # 触发器领域内部（各触发方式）用的查询；TriggerService 由 api 导出给 HTTP 层
    "triggers": {"TriggerService.get_in", "TriggerService.busy_in"},
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


def test_the_http_layer_sees_only_domain_facades():
    """HTTP 层只经 Container 调用领域（docs/architecture.md 2.3）：它的字段类型都由领域的 api 模块导出，或是平台的
    类型与配置；领域内部的对象（消费者、压缩、信箱……）不在其中。import 检查拦不住经 Container 取到的对象，由这里拦。"""
    import dataclasses
    import importlib
    import types
    import typing

    from muliao.config import Settings
    from muliao.container import Container

    exported = [
        getattr(module, name)
        for module in (importlib.import_module(f"muliao.{d}.api") for d in sorted(DOMAINS))
        for name in module.__all__
    ]
    hints = typing.get_type_hints(Container)
    problems = []
    for field in dataclasses.fields(Container):
        if field.name == "running":
            continue
        hint = hints[field.name]
        members = typing.get_args(hint) if isinstance(hint, types.UnionType) else (hint,)
        for t in members:
            if t is type(None) or t is Settings or t.__module__.startswith("muliao.platform."):
                continue
            if not any(t is e for e in exported):
                problems.append(f"Container.{field.name}: {t.__module__}.{t.__qualname__}")
    assert problems == []
