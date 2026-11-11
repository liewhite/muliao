"""AI 资源文件的写前校验（#500、#578）：写入口 AssetSource.write（界面编辑与文件工具都经它）把要写成的全文交来，这里按读树
（agents.assets）的同一套规则校验——布局与名字问 place_of，各种文件的格式问各自的格式模块（memory_file、agent_file、
skill_package、mcp_config、secrets_format、access_file）——返回实际存下的全文；不合格就拒绝并说明原因、这是谁的资源、
正确的写法在公共 skill 的哪一节。用 shell 写的不经过这里，下次读树时照样报出。

只校验格式与名字，不管谁能写（那是写沙箱的事，agents.access.write_scope）。隐藏文件（`.` 开头的路径段）不加载，不校验。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from muliao.agents import access_file, agent_file, memory_file
from muliao.agents.agent_file import AgentFile
from muliao.agents.assets import (
    PLATFORM,
    Config,
    Entry,
    Misplaced,
    Shelf,
    Snapshot,
    place_of,
)
from muliao.agents.kinds import Section, StoredKind, label, spec_of
from muliao.agents.mcp_config import parse_mcp_file
from muliao.agents.naming import validate_slug
from muliao.agents.secrets_format import parse_secret_file
from muliao.agents.skill_package import SKILL_FILE, approval_scripts, parse_frontmatter
from muliao.errors import Conflict, InvalidInput
from muliao.platform.fs import real

# 讲资源目录怎么改的公共 skill（#501）；报错里指向它的小节（kinds.Section，小节标题与它逐字一致）
SKILL = "assets-files"


class _Rejected(Exception):
    def __init__(self, reason: str, section: Section):
        self.reason, self.section = reason, section


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
    try:
        place = place_of(rest, None if platform else head)
    except Misplaced as e:
        raise _Rejected(str(e), "Layout" if e.kind is None else spec_of(e.kind).section) from e
    match place:
        case Config():
            if platform:
                _access(snapshot, text)
            else:
                assert owner is not None
                _agent(owner, text, check_agent)
            return text
        case Shelf(kind=kind) | Entry(kind=kind, inner=()) if spec_of(kind).is_directory:
            raise _Rejected(f"{'/'.join(rest)} is a directory, not a file", spec_of(kind).section)
        case Entry(kind=kind, name=name, inner=inner):
            section = spec_of(kind).section
            item = snapshot.root / head / Path(*rest[: len(rest) - len(inner)])
            _new(snapshot, kind, name, item, platform, section)
            try:
                return _content(kind, name, item, inner, text, memory_max_chars)
            except InvalidInput as e:
                raise _Rejected(str(e), section) from e


def _content(
    kind: StoredKind, name: str, item: Path, inner: tuple[str, ...], text: str, memory_max_chars: int
) -> str:
    """一项资源的文件要写成 text 时存下的全文；不合规则时 InvalidInput。"""
    match kind:
        case StoredKind.MEMORY:
            # 只为比长度：读坏了的（非 UTF-8）也要能整个覆盖修好
            old = item.read_text(encoding="utf-8", errors="replace") if item.is_file() else None
            return memory_file.stored(text, old, memory_max_chars)
        case StoredKind.SKILL:
            if inner == (SKILL_FILE,):
                approval_scripts(item, parse_frontmatter(text, name).approval)
            return text
        case StoredKind.MCP:
            parse_mcp_file(text)
            return text
        case StoredKind.SECRET:
            _secret(item, name, text)
            return text


def _new(
    snapshot: Snapshot, kind: StoredKind, name: str, path: Path, platform: bool, section: Section
) -> None:
    """新建一项资源（它的文件或目录还不在磁盘上）：名字每种资源全局唯一，别处已有的不让建（读树时两处都会被跳过）；新建
    的平台记忆要是 slug（读树还认能当文件名的：agent 删除或转移时挪来的私有记忆带着 agent 名，不经这里）。"""
    if path.exists():
        return
    try:
        if kind == StoredKind.MEMORY and platform:
            validate_slug(label(kind), name)
        snapshot.check_free(kind, name)
    except (Conflict, InvalidInput) as e:
        raise _Rejected(str(e), section) from e


def _agent(agent_id: str, text: str, check_agent: Callable[[AgentFile], None]) -> None:
    try:
        data = agent_file.parse(text)
        check_agent(data)
    except InvalidInput as e:
        raise _Rejected(str(e), "agent.toml") from e
    if data.id != agent_id:
        raise _Rejected(f"id must stay {agent_id!r} (it ties the directory to the agent)", "agent.toml")


def _access(snapshot: Snapshot, text: str) -> None:
    """按读树的同一套规则读一遍；只拦这次新出现的问题（与磁盘上现在的文件比，原有的、与这次改动无关的不挡，比如某个 agent
    的目录暂时读坏了）。现在的文件不合法时，新内容合法就放行（#550、#573：让 admin 能把它改回来，只是引用了不存在的
    东西的问题写入后照常报出）；都不合法、又没有新的不合法之处的也放行（读树照旧沿用上一份，不放宽）。"""
    agents = {a.name: a.id for a in snapshot.agents.values()}
    after = access_file.parse(text, agents, snapshot.tools)
    before = access_file.read(snapshot.access_path, agents, snapshot.tools)
    if after.readable and not before.readable:
        return
    existing = {p.reason for p in before.problems}
    new = [p.reason for p in after.problems if p.reason not in existing]
    if new:
        raise _Rejected("; ".join(new), "Roles and permissions")


def _secret(path: Path, key: str, text: str) -> None:
    """密文只能由 vault_set 加密写入；已有的 secret 可以只改 description（密文原样不动）。"""
    hint = "secret values are encrypted by the platform: store them with vault_set"
    if not path.is_file():
        raise _Rejected(f"new secrets are not written by hand; {hint}", "Secrets")
    try:
        new = parse_secret_file(key, text)
        old = parse_secret_file(key, path.read_text(encoding="utf-8"))
    except InvalidInput as e:
        raise _Rejected(f"{e}; {hint}", "Secrets") from e
    if new.ciphertext != old.ciphertext:
        raise _Rejected(
            f"only the description and allow_shell may be edited by hand, the ciphertext must stay; {hint}",
            "Secrets",
        )
