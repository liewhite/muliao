"""AI 资源文件的写前校验（#500、#578）：写入口 AssetSource.write（界面编辑与文件工具都经它）把要写成的全文交来，这里按读树
（agents.assets）的同一套规则校验——布局与名字问 place_of，各种资源文件的格式按它的 KindSpec.vet，agent.toml 问
agent_file，access.toml 问 access_file.vet——返回实际存下的全文；不合格就拒绝并说明原因、这是谁的资源、正确的写法在公共
skill 的哪一节。用 shell 写的不经过这里，下次读树时照样报出。

只校验格式与名字，不管谁能写（那是写沙箱的事，agents.access.write_scope）。隐藏文件（`.` 开头的路径段）不加载，不校验。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from muliao.agents import access_file, agent_file
from muliao.agents.agent_file import AgentFile
from muliao.agents.assets import (
    PLATFORM,
    Config,
    Entry,
    Misplaced,
    PrivateMemory,
    Shelf,
    Snapshot,
    place_of,
)
from muliao.agents.formats import Draft
from muliao.agents.kinds import Section, StoredKind, spec_of
from muliao.errors import Conflict, InvalidInput
from muliao.platform.fs import real

# 讲资源目录怎么改的公共 skill（#501）；报错里指向它的小节（kinds.Section，小节标题与它逐字一致）
SKILL = "assets-files"


class _Rejected(Exception):
    def __init__(self, reason: str, section: Section):
        self.reason = reason
        self.section: Section = section


class AssetRejected(InvalidInput):
    """写入口拒绝的一份资源文件：消息只是原因（界面上给用户看）；for_agent 是交给 agent 的完整说明（文件工具用）：是谁的
    资源文件、什么都没写、看公共 skill 哪一节。"""

    def __init__(self, reason: str, section: Section, path: Path, whose: str, root: Path):
        super().__init__(reason)
        self.reason, self.section, self.path, self.whose, self.root = reason, section, path, whose, root

    def for_agent(self) -> str:
        return (
            f"{self.path} ({self.whose} asset files under {self.root}): {self.reason}; nothing was written. The "
            f"layout and file formats are in the {SKILL} skill, section {self.section!r}."
        )


def in_tree(root: Path, path: Path) -> bool:
    """path 在资源目录里（按真实路径）。"""
    r, p = real(root), real(path)
    return p != r and r in p.parents


def vet(
    snapshot: Snapshot,
    path: Path,
    text: str,
    memory_max_chars: int,
    check_agent: Callable[[AgentFile], None],
) -> str:
    """path（在资源目录里）要写成 text：返回实际要存下的全文（记忆规整过空白，别的原样）；不合格时 AssetRejected（原因、是谁的、
    看公共 skill 哪一节）。同步 IO（读现有的文件比对）。"""
    root = real(snapshot.root)
    rel = real(path).relative_to(root).parts
    if any(p.startswith(".") for p in rel):
        return text
    by_name = {n: a for a, n in snapshot.names.items()}
    owner: str | None = None
    if rel[0] != PLATFORM:
        owner = by_name.get(rel[0])
    whose = (
        "the platform's" if rel[0] == PLATFORM else f"agent {rel[0]!r}'s" if owner is not None else "no one's"
    )
    try:
        return _check(snapshot, rel, owner, text, memory_max_chars, check_agent)
    except _Rejected as e:
        raise AssetRejected(e.reason, e.section, path, whose, snapshot.root) from e


def _check(
    snapshot: Snapshot,
    rel: tuple[str, ...],
    owner: str | None,
    text: str,
    memory_max_chars: int,
    check_agent: Callable[[AgentFile], None],
) -> str:
    head, rest = rel[0], rel[1:]
    if not rest:
        raise _Rejected("the root holds only platform/ and agent directories", "Layout")
    platform = head == PLATFORM
    if not platform and owner is None:
        raise _Rejected(
            f"{head}/ is not an existing agent's directory; agent directories are made by create_agent",
            "Layout",
        )
    if owner in snapshot.deleting:
        raise _Rejected(
            f"agent {head!r} is being deleted; nothing can be written into its directory", "Layout"
        )
    try:
        place = place_of(rest, not platform)
    except Misplaced as e:
        raise _Rejected(str(e), "Layout" if e.kind is None else spec_of(e.kind).section) from e

    def draft(kind: StoredKind, name: str, inner: tuple[str, ...], named: bool) -> str:
        """按这种资源登记的校验（KindSpec.vet）；这一项的文件或目录是 rel 去掉 inner。按名字认的（named）新建时名字要没人用。"""
        item = snapshot.root / head / Path(*rest[: len(rest) - len(inner)])
        if named and not item.exists():
            _new(snapshot, kind, name)
        try:
            return spec_of(kind).vet(Draft(item, name, inner, text, memory_max_chars))
        except InvalidInput as e:
            raise _Rejected(str(e), spec_of(kind).section) from e

    match place:
        case Config():
            if platform:
                _access(snapshot, text)
            else:
                assert owner is not None
                _agent(owner, text, check_agent)
            return text
        # 放资源的子目录本身总是目录（放文件的 mcp/、secrets/ 也是）；skill 这类目录资源的条目本身也是目录
        case Shelf(kind=kind):
            raise _Rejected(f"{'/'.join(rest)} is a directory, not a file", spec_of(kind).section)
        case Entry(kind=kind, inner=()) if spec_of(kind).is_directory:
            raise _Rejected(f"{'/'.join(rest)} is a directory, not a file", spec_of(kind).section)
        case PrivateMemory():
            assert owner is not None
            return draft(StoredKind.MEMORY, snapshot.names[owner], (), named=False)
        case Entry(kind=kind, name=name, inner=inner):
            return draft(kind, name, inner, named=True)


def _new(snapshot: Snapshot, kind: StoredKind, name: str) -> None:
    """新建一项资源（它的文件或目录还不在磁盘上）：名字每种资源全局唯一，别处已有的不让建（读树时两处都会被跳过）。"""
    try:
        snapshot.check_free(kind, name)
    except Conflict as e:
        raise _Rejected(str(e), spec_of(kind).section) from e


def _agent(agent_id: str, text: str, check_agent: Callable[[AgentFile], None]) -> None:
    try:
        data = agent_file.parse(text)
        check_agent(data)
    except InvalidInput as e:
        raise _Rejected(str(e), "agent.toml") from e
    if data.id != agent_id:
        raise _Rejected(f"id must stay {agent_id!r} (it ties the directory to the agent)", "agent.toml")


def _access(snapshot: Snapshot, text: str) -> None:
    """与平台自己改它同一个校验（access_file.vet）。"""
    path = snapshot.access_path
    try:
        before = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else None
        access_file.vet(before, text, snapshot.tools, snapshot.addressable())
    except InvalidInput as e:
        raise _Rejected(str(e), "Roles and permissions") from e
