"""issue 的依赖图（issue_dependencies，有向无环）：上游与下游、还在等的上游、整体替换上游时的校验（存在、不重复、
不成环），以及一批新 issue 之间按 key 的引用。读写依赖表只在这里。"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.errors import InvalidInput
from muliao.issues.rules import finished
from muliao.issues.tables import Issue, IssueDependency, IssueStatus


@dataclass(frozen=True)
class Dependency:
    """依赖关系里的另一个 issue（上游或下游）与它现在的状态。"""

    id: int
    status: IssueStatus


@dataclass(frozen=True)
class Links:
    """issue 在依赖图里的位置：它依赖的上游、依赖它的下游（都按 id 排序）。"""

    depends_on: list[Dependency]
    blocks: list[Dependency]


def waiting_on(depends_on: Iterable[Dependency]) -> tuple[int, ...]:
    """还没完成的上游：它们都完成（rules.FINISHED）之前，这个 issue 还轮不到。"""
    return tuple(d.id for d in depends_on if not finished(d.status))


def ids(found: Sequence[Dependency]) -> list[int]:
    return [d.id for d in found]


def cycle_through(
    issue_id: int, depends_on: Iterable[int], edges: Iterable[tuple[int, int]]
) -> list[int] | None:
    """让 issue_id 依赖 depends_on 会不会成环：会则返回环上的路径（issue_id 依赖 ... 依赖 issue_id），否则 None。

    edges 是其余的依赖边（issue, 它依赖的上游），不含 issue_id 自己现有的上游（它们正被整体替换）。
    """
    upstream: dict[int, list[int]] = {}
    for down, up in edges:
        if down != issue_id:
            upstream.setdefault(down, []).append(up)
    seen: set[int] = set()
    stack = [(d, [issue_id, d]) for d in depends_on]
    while stack:
        node, path = stack.pop()
        if node == issue_id:
            return path
        if node in seen:
            continue
        seen.add(node)
        stack.extend((u, [*path, u]) for u in upstream.get(node, ()))
    return None


def batch_label(n: int, key: str | None) -> str:
    """一批里第 n 项在错误信息里的名字。"""
    return f"issues[{n}]" + ("" if key is None else f" (key {key!r})")


def check_batch(items: Sequence[tuple[str | None, Sequence[int | str]]]) -> None:
    """一批新 issue（每项是 (key, depends_on)）之间的 key 引用（不查库）：key 唯一、引用的 key 都在本批、key 之间
    不成环。本批互相引用应用 key（整数按已有编号处理），环在这里按 key 报路径，而不是报会被回滚的编号；其余（编号
    存在、不重复、涉及已有编号的环）由 set_depends_on_in 在事务里查。"""
    index: dict[str, int] = {}
    for n, (key, _) in enumerate(items):
        if key is None:
            continue
        if not key.strip():
            raise InvalidInput(f"{batch_label(n, key)}: key must not be empty")
        if key in index:
            raise InvalidInput(f"key {key!r} is used by issues[{index[key]}] and issues[{n}]")
        index[key] = n
    edges: list[tuple[int, int]] = []
    for n, (key, depends_on) in enumerate(items):
        for d in depends_on:
            if isinstance(d, str):
                if d not in index:
                    raise InvalidInput(
                        f"{batch_label(n, key)}: depends_on key {d!r} is not a key of this batch"
                    )
                edges.append((n, index[d]))
    for n in range(len(items)):
        cycle = cycle_through(n, [up for down, up in edges if down == n], edges)
        if cycle is not None:
            path = " -> ".join(repr(items[i][0]) for i in cycle)
            raise InvalidInput(f"dependencies must not form a cycle: {path} (each depends on the next)")


async def links_in(s: AsyncSession, issue_ids: list[int]) -> dict[int, Links]:
    """一批 issue 的上游与下游（带对方的当前状态）：两次查询，与 issue 数无关。"""
    links = {i: Links(depends_on=[], blocks=[]) for i in issue_ids}
    upstream = (
        select(IssueDependency.issue_id, Issue.id, Issue.status)
        .join(Issue, Issue.id == IssueDependency.depends_on_id)
        .where(IssueDependency.issue_id.in_(issue_ids))
        .order_by(Issue.id)
    )
    for down, up, status in (await s.execute(upstream)).all():
        links[down].depends_on.append(Dependency(id=up, status=status))
    downstream = (
        select(IssueDependency.depends_on_id, Issue.id, Issue.status)
        .join(Issue, Issue.id == IssueDependency.issue_id)
        .where(IssueDependency.depends_on_id.in_(issue_ids))
        .order_by(Issue.id)
    )
    for up, down, status in (await s.execute(downstream)).all():
        links[up].blocks.append(Dependency(id=down, status=status))
    return links


async def downstream_in(s: AsyncSession, issue_id: int) -> list[int]:
    """依赖这个 issue 的下游（按 id 排序）。"""
    return ids((await links_in(s, [issue_id]))[issue_id].blocks)


async def set_depends_on_in(
    s: AsyncSession, issue: Issue, depends_on: Sequence[int]
) -> tuple[list[int], list[int]] | None:
    """把 issue 的上游整体换成 depends_on：上游必须存在、不能重复、不能成环。返回（原来的, 现在的），没变为 None。"""
    wanted = sorted(set(depends_on))
    if len(wanted) != len(depends_on):
        raise InvalidInput(f"depends_on lists an issue more than once: {list(depends_on)}")
    existing = set(await s.scalars(select(Issue.id).where(Issue.id.in_(wanted))))
    missing = [f"#{up}" for up in wanted if up not in existing]
    if missing:
        raise InvalidInput(f"issue #{issue.id} cannot depend on {', '.join(missing)}: not found")
    edges = [
        (down, up)
        for down, up in await s.execute(select(IssueDependency.issue_id, IssueDependency.depends_on_id))
    ]
    current = sorted(up for down, up in edges if down == issue.id)
    if current == wanted:
        return None
    cycle = cycle_through(issue.id, wanted, edges)
    if cycle is not None:
        path = " -> ".join(f"#{i}" for i in cycle)
        raise InvalidInput(f"dependencies must not form a cycle: {path} (each depends on the next)")
    await s.execute(delete(IssueDependency).where(IssueDependency.issue_id == issue.id))
    s.add_all(IssueDependency(issue_id=issue.id, depends_on_id=up) for up in wanted)
    await s.flush()
    return current, wanted
