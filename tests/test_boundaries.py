"""模块依赖规则（docs/domain-design.md 第 10 节），对源码的 import 做静态检查：

- 领域包之间只能 import 对方的 api 模块（命令、查询、数据结构）与 events 模块（它发布的事件与接收的命令），
  不能碰对方的表与内部模块；
- platform 不 import 任何领域；
- 领域不 import HTTP 适配层（api 包）与组合根（container）。
"""

import ast
from pathlib import Path

SRC = Path(__file__).parent.parent / "src" / "muliao"
DOMAINS = {
    "agents",
    "agent_runtime",
    "delegations",
    "files",
    "jobs",
    "notifications",
    "reflection",
    "sessions",
    "triggers",
    "vault",
}
# 一个领域对外公开的模块
PUBLIC = {"api", "events"}


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
                    problems.append(f"{where} (a domain must not depend on the HTTP layer or the composition root)")
                elif target[1] in DOMAINS and target[1] != owner and (len(target) < 3 or target[2] not in PUBLIC):
                    problems.append(f"{where} (only {target[1]}.api and {target[1]}.events are public)")
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
        await container.notifier.enqueue_in(s, None, Notification(title="t", body="b", session_id=None))
