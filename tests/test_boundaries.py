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
# 经 api 的依赖方向（docs/architecture.md 2.3）：领域只能 import 更早一层的领域的 api，同层与反向都不行，所以依赖无环。
# 事件与定向命令不受方向限制（events 模块只放消息的数据形状）。新增领域要在这里定它在哪一层
LAYERS: list[set[str]] = [
    {"files", "agents", "usage", "speech", "web"},
    {"agent_runtime"},
    {"sessions"},
    {"workspace", "notifications", "triggers", "issues", "history"},
]
DOMAINS = set().union(*LAYERS)
RANK = {d: rank for rank, layer in enumerate(LAYERS) for d in layer}
# 一个领域对外公开的模块
PUBLIC = {"api", "events"}
# HTTP 适配层另外可以 import 的：序列化领域交给它的行
HTTP_VISIBLE = PUBLIC | {"tables"}
# src/muliao 下不是领域的目录
NOT_DOMAINS = {"api", "platform", "migrations", "webui"}


def _module_of(path: Path, root: Path) -> list[str]:
    """path 这个模块所在的包（相对 import 从这里算起）：muliao/agents/x.py 与 muliao/agents/__init__.py 都在 muliao.agents。"""
    return ["muliao", *path.relative_to(root).parts[:-1]]


def _imports(path: Path, root: Path = SRC) -> list[tuple[int, str]]:
    """这个文件 import 的 muliao 模块（含相对 import，换算成绝对名）。`from x import y` 记成 x.y：y 可能是子模块。"""
    tree = ast.parse(path.read_text())
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level:
                package = _module_of(path, root)
                if node.level > len(package):
                    raise AssertionError(
                        f"{path}:{node.lineno} relative import goes above the muliao package"
                    )
                base = package[: len(package) - node.level + 1]
                module = ".".join(base + ([node.module] if node.module else []))
            else:
                module = node.module or ""
            found += [(node.lineno, f"{module}.{alias.name}") for alias in node.names]
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
                elif target[1] in DOMAINS and target[1] != owner:
                    if len(target) < 3 or target[2] not in PUBLIC:
                        problems.append(f"{where} (only {target[1]}.api and {target[1]}.events are public)")
                    elif target[2] == "api" and RANK[target[1]] >= RANK[owner]:
                        problems.append(
                            f"{where} (dependencies through api go one way: {owner} is in layer {RANK[owner]}, "
                            f"{target[1]} in layer {RANK[target[1]]})"
                        )
            if owner == "api" and target[1] in DOMAINS and (len(target) < 3 or target[2] not in HTTP_VISIBLE):
                problems.append(f"{where} (the HTTP layer uses {target[1]}.api, .events and .tables only)")
    return sorted(problems)


def test_every_domain_has_a_public_api_module():
    """边界检查只看得到 api 模块：缺了它，领域的公开接口（如在调用方事务里写入的函数）就不受下面的检查。目录有没有
    __init__.py 都算（没有它的命名空间包照样能 import）。"""
    packages = {p.name for p in SRC.iterdir() if p.is_dir() and p.name != "__pycache__"} - NOT_DOMAINS
    assert packages == DOMAINS
    assert sorted(d for d in DOMAINS if not (SRC / d / "api.py").exists()) == []


def test_domains_only_use_each_others_api():
    assert _violations() == []


# pydantic-ai 的运行与挂起类型只在 agent_runtime 里用（docs/architecture.md 2.1）：别的领域经它的 Finished / Paused、
# `{调用 id: 结果}` 与 defer_call。消息类型（pydantic_ai.messages）是历史的存储形态，不在此列
RUN_TYPES = {
    "AgentRun",
    "AgentRunResult",
    "ApprovalRequired",
    "CallDeferred",
    "DeferredToolRequests",
    "DeferredToolResults",
}


# agents 的子域各自独占的表（docs/architecture.md 2.1）：表类只能被它的子域的模块 import，别的子域经 `*_in` 接口
AGENTS_TABLES: dict[str, set[str]] = {
    # 目录
    "Agent": {"service", "active"},
    # 分发
    "Role": {"roles", "distribution"},
    "AgentRules": {"agent_rules"},
    # 条目（access 的能改检查只读交给它的行，不查表）
    "Item": {"items", "skills", "mcp", "secrets", "ownership", "access"},
    "ItemFile": {"skills", "ownership"},
    # 记忆
    "Memory": {"memory"},
    # 版本
    "Revision": {"revisions"},
}


def test_agents_subdomains_only_touch_their_own_tables():
    from muliao.agents import tables

    declared = {
        n for n, v in vars(tables).items() if isinstance(v, type) and getattr(v, "__tablename__", None)
    }
    assert declared == set(AGENTS_TABLES), "every agents table belongs to exactly one subdomain"
    problems = []
    for path in sorted((SRC / "agents").glob("*.py")):
        for line, name in _imports(path):
            if name.startswith("muliao.agents.tables."):
                table = name.rsplit(".", 1)[1]
                if path.stem not in AGENTS_TABLES[table]:
                    problems.append(f"agents/{path.name}:{line} imports {table}")
    assert not problems, "\n".join(problems)


# sessions 的子域各自独占的表（docs/architecture.md 2.1）：表类只在它的模块里出现在查询与构造中（`select(X)`、`X.列`、
# `X(...)`）；别的模块经它们的 `*_in` 接口。领域内传递行对象、按 id 取会话（`s.get(Session, id)`）不算
SESSIONS_TABLES: dict[str, set[str]] = {
    # 会话：按属性查会话、写入新会话只在目录
    "Session": {"directory"},
    # 运行：历史与信箱
    "Message": {"history"},
    "Compaction": {"history"},
    "Mail": {"mailbox"},
    # 外部工作
    "SessionHold": {"holds"},
    # 委派
    "Delegation": {"delegations"},
}


def _table_uses(tree: ast.AST, names: set[str]) -> list[tuple[int, str]]:
    """查询与构造里用到的表类：`X.属性`、`X(...)`，以及作为 select 参数的 `X`。"""
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in names:
            found.append((node.lineno, node.value.id))
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in names:
                found.append((node.lineno, node.func.id))
            elif isinstance(node.func, ast.Name) and node.func.id == "select":
                found += [(node.lineno, a.id) for a in node.args if isinstance(a, ast.Name) and a.id in names]
    return found


def test_sessions_subdomains_only_query_their_own_tables():
    from muliao.sessions import tables

    declared = {
        n for n, v in vars(tables).items() if isinstance(v, type) and getattr(v, "__tablename__", None)
    }
    assert declared == set(SESSIONS_TABLES), "every sessions table belongs to exactly one subdomain"
    problems = []
    for path in sorted((SRC / "sessions").glob("*.py")):
        if path.stem == "tables":
            continue
        for line, name in _table_uses(ast.parse(path.read_text()), declared):
            if path.stem not in SESSIONS_TABLES[name]:
                problems.append(f"sessions/{path.name}:{line} queries {name}")
    assert not problems, "\n".join(problems)


# 模块化（不设子域）的领域里由一个模块独占的表（docs/architecture.md 第 14、21 节）：别的模块经它的函数
MODULE_TABLES: dict[str, dict[str, str]] = {
    "issues": {"IssueDependency": "graph"},
    "notifications": {"Notification": "inbox", "PushSubscription": "push"},
}


def test_tables_owned_by_one_module_are_only_queried_there():
    problems = []
    for domain, owners in MODULE_TABLES.items():
        for path in sorted((SRC / domain).glob("*.py")):
            if path.stem == "tables":
                continue
            for line, name in _table_uses(ast.parse(path.read_text()), set(owners)):
                if path.stem != owners[name]:
                    problems.append(f"{domain}/{path.name}:{line} queries {name}")
    assert not problems, "\n".join(problems)


def test_sessions_read_tasks_through_the_queue():
    """会话领域经 TaskQueue 的只读查询（TaskView）看运行，不查任务表、不按 TaskStatus 分支；Task 只出现在任务处理器的
    签名里（队列交给处理器的就是它）。"""
    problems = []
    for path in sorted((SRC / "sessions").glob("*.py")):
        for line, name in _imports(path):
            if name == "muliao.platform.tables.TaskStatus" or (
                name == "muliao.platform.tables.Task" and path.stem not in {"consumer", "compaction"}
            ):
                problems.append(f"sessions/{path.name}:{line} imports {name}")
        problems += [
            f"sessions/{path.name}:{line} queries Task"
            for line, _ in _table_uses(ast.parse(path.read_text()), {"Task"})
        ]
    assert not problems, "\n".join(problems)


def test_only_agent_runtime_touches_the_model_librarys_run_types():
    problems = []
    for path in SRC.rglob("*.py"):
        if path.relative_to(SRC).parts[0] == "agent_runtime":
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "pydantic_ai":
                problems += [
                    f"{path.relative_to(SRC)}:{node.lineno} imports {a.name}"
                    for a in node.names
                    if a.name in RUN_TYPES
                ]
    assert problems == []


def test_relative_imports_are_resolved_to_the_module_they_name(tmp_path):
    import pytest

    root = tmp_path / "muliao"
    module = root / "workspace" / "x.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "from . import api\nfrom .runner import y\nfrom ..sessions.mailbox import z\nimport os\n"
    )
    assert _imports(module, root) == [
        (1, "muliao.workspace.api"),
        (2, "muliao.workspace.runner.y"),
        (3, "muliao.sessions.mailbox.z"),
    ]
    module.write_text("from .. import x\n")
    assert _imports(module, root) == [(1, "muliao.x")]
    module.write_text("from ... import x\n")
    with pytest.raises(AssertionError, match="above the muliao package"):
        _imports(module, root)


async def test_a_transaction_writing_two_domains_tables_fails(container, make_agent):
    import pytest
    from sqlalchemy import update

    from muliao.agents.tables import Item
    from muliao.notifications.tables import Notification

    await make_agent("alpha")
    with pytest.raises(RuntimeError, match="several domains"):
        async with container.db.transaction() as s:
            s.add(Notification(title="t", body="b", session_id=None))
            await s.execute(update(Item).values(description="x"))


async def test_a_domain_and_the_task_table_may_be_written_together(container):
    from muliao.notifications.tables import Notification

    async with container.db.transaction() as s:
        await container.notifications._enqueue_in(  # noqa: SLF001
            s, None, Notification(title="t", body="b", session_id=None)
        )


# 领域公开接口里在调用方事务里执行的函数（参数里有调用方的事务）：只能是只读的查询（docs/architecture.md 2.2）。
# 写入一律是被调领域自己开事务的命令，调用方不替它管事务。读的事实只会单向变化或不变时，调用方可以据它在同一事务里
# 写自己的数据（runnable_agent_in 是双向变化、只用来拒绝的例外，理由见 2.2）；否则（SessionActivity 的两个查询）只能
# 先查后写。新增一个这样的函数要先确认它只读，再登记到这里。
READ_ONLY_IN_CALLER_TRANSACTION = {
    "agents": {"active_agent_in", "agent_in", "agent_names_in", "runnable_agent_in"},
    "files": {"load_metas"},
    "sessions": {
        "session_in",
        "receive_command_in",
        "keyed_sessions_among",
        "hold_registered_in",
        "SessionActivity.work_state_in",
        "SessionActivity.keyed_work_states_in",
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
}


async def test_injected_agent_deletion_blockers_are_reviewed_read_only_queries(container):
    blockers = container.agent_lifecycle._blockers  # noqa: SLF001
    assert {b.__qualname__ for b in blockers.values()} == READ_ONLY_DELETION_BLOCKERS


def test_the_http_layer_sees_only_domain_facades():
    """HTTP 层只经 Container 调用领域（docs/architecture.md 2.3）：它的字段类型都由领域的 api 模块导出，或是逐个
    审过的平台类型；领域内部的对象（消费者、压缩、信箱……）、平台的数据库与队列不在其中。import 检查拦不住
    经 Container 取到的对象，由这里拦。"""
    import dataclasses
    import importlib
    import types
    import typing

    from muliao.container import Container
    from muliao.platform.bus import EventFeed
    from muliao.platform.tasks import TaskAdmin

    # 平台类型：展示事件流（只订阅）与「任务」页的窄口
    reviewed = (EventFeed, TaskAdmin)

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
            if t is type(None) or any(t is r for r in reviewed):
                continue
            if not any(t is e for e in exported):
                problems.append(f"Container.{field.name}: {t.__module__}.{t.__qualname__}")
    assert problems == []


async def test_the_container_holds_exactly_the_declared_types_at_run_time(container):
    """声明类型挡不住运行时（项目不跑类型检查）：窄接口要是把完整对象传进来（如把 TaskQueue 当 TaskAdmin），HTTP 层
    照样能调 enqueue。所以每个字段的值必须恰好是声明的类型，不能是它的子类或别的对象。"""
    import dataclasses
    import types
    import typing

    from muliao.container import Container

    c = container.container
    hints = typing.get_type_hints(Container)
    problems = []
    for field in dataclasses.fields(Container):
        if field.name == "running":
            continue
        hint = hints[field.name]
        allowed = typing.get_args(hint) if isinstance(hint, types.UnionType) else (hint,)
        value = getattr(c, field.name)
        if not any(type(value) is t for t in allowed):
            problems.append(f"Container.{field.name}: {type(value).__qualname__} is not {hint}")
    assert problems == []


async def test_the_task_kinds_in_the_http_contract_are_exactly_the_registered_ones(container):
    """前端按种类判断任务事件（web/src/events.ts 的 TaskKind 取自 OpenAPI）：接口里的种类必须恰好是 worker 登记了
    处理器的那些，后端新增或改名时这里先失败，再重新生成前端类型。"""
    import typing

    from muliao.api.tasks import TaskKind

    assert set(typing.get_args(TaskKind)) == container.queue.kinds


def test_tools_refuse_with_domain_errors_not_model_retry():
    """工具的拒绝一律抛 DomainError，由 agent_runtime.tool_errors 统一交还模型：在那里预算用完时改成 ToolFailed，
    不让整轮运行失败。工具自己抛 ModelRetry 会绕过这层，连续 3 次就把整轮打成失败（#145）。所以除了这层和把 MCP
    server 的 ModelRetry 换成 DomainError 的 mcp 模块，源码里不得出现 ModelRetry（裸名、属性、导入都算）。"""
    allowed = {SRC / "agent_runtime" / "tool_errors.py", SRC / "agent_runtime" / "mcp.py"}
    offenders = []
    for path in SRC.rglob("*.py"):
        if path in allowed or "webui" in path.parts:
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                (isinstance(node, ast.Name) and node.id == "ModelRetry")
                or (isinstance(node, ast.Attribute) and node.attr == "ModelRetry")
                or (isinstance(node, ast.alias) and node.name == "ModelRetry")
            ):
                offenders.append(f"{path.relative_to(SRC)}:{getattr(node, 'lineno', '?')}")
    assert offenders == []
