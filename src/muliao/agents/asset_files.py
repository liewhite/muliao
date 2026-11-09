"""文件工具写资源目录里的文件之前的校验（#500）：write_file / edit_file 把写成后的全文交来，不合格式就拒绝并说明原因、
这是谁的资源、正确的写法在公共 skill 的哪一节。规则与读树（agents.assets）是同一套：读树时会被跳过、报成问题的写法，
这里在写之前就挡住。用 shell 写的不经过这里，下次读树时照样报出。

只校验格式与名字，不管谁能写（那是写沙箱的事，agents.access.write_scope）。隐藏文件（`.` 开头的路径段）不加载，不校验。
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Literal, get_args

from muliao.agents.assets import (
    ACCESS_FILE,
    AGENT_FILE,
    MCP_DIR,
    MEMORY_DIR,
    PLATFORM,
    PRIVATE_MEMORY_FILE,
    SECRETS_DIR,
    SKILLS_DIR,
    Snapshot,
    access_problems,
    parse_agent_file,
)
from muliao.agents.kinds import StoredKind, label
from muliao.agents.mcp_config import parse_mcp_file
from muliao.agents.naming import file_name, validate_slug
from muliao.agents.secrets_format import parse_secret_file, validate_key
from muliao.agents.skill_package import SKILL_FILE, parse_frontmatter
from muliao.agents.thinking import Thinking
from muliao.errors import Conflict, InvalidInput

# 讲资源目录怎么改的公共 skill（#501）；报错里指向它的小节，小节标题（`## <名>`）与 Section 逐字一致，改这里要同步改 skill
SKILL = "assets-files"
Section = Literal[
    "Layout", "Memory", "Skills", "MCP servers", "Secrets", "agent.toml", "Roles and permissions"
]
SECTIONS: tuple[Section, ...] = get_args(Section)


class _Rejected(Exception):
    def __init__(self, reason: str, section: Section):
        self.reason, self.section = reason, section


def real(path: Path) -> Path:
    return Path(os.path.realpath(path))


def in_tree(root: Path, path: Path) -> bool:
    """path 在资源目录里（按真实路径）。"""
    r, p = real(root), real(path)
    return p != r and r in p.parents


def vet(
    snapshot: Snapshot,
    path: Path,
    text: str,
    memory_max_chars: int,
    check_model: Callable[[str, Thinking], None],
) -> None:
    """path（在资源目录里）要写成 text：不合格时 InvalidInput，说明原因、是谁的、看公共 skill 哪一节。"""
    root = real(snapshot.root)
    rel = real(path).relative_to(root).parts
    if any(p.startswith(".") for p in rel):
        return
    by_name = {n: a for a, n in snapshot.names.items()}
    owner: str | None = None
    if rel[0] != PLATFORM:
        owner = by_name.get(rel[0])
    whose = (
        "the platform's" if rel[0] == PLATFORM else f"agent {rel[0]!r}'s" if owner is not None else "no one's"
    )
    try:
        _check(snapshot, rel, owner, text, memory_max_chars, check_model)
    except _Rejected as e:
        raise InvalidInput(
            f"{path} ({whose} asset files under {snapshot.root}): {e.reason}; nothing was written. The layout "
            f"and file formats are in the {SKILL} skill, section {e.section!r}."
        ) from e


def _check(
    snapshot: Snapshot,
    rel: tuple[str, ...],
    owner: str | None,
    text: str,
    memory_max_chars: int,
    check_model: Callable[[str, Thinking], None],
) -> None:
    head, rest = rel[0], rel[1:]
    if not rest:
        raise _Rejected("the root holds only platform/ and agent directories", "Layout")
    platform = head == PLATFORM
    if not platform and owner is None:
        raise _Rejected(
            f"{head}/ is not an existing agent's directory; agent directories are made by create_agent",
            "Layout",
        )
    base = snapshot.root / head
    if rest == (ACCESS_FILE,) and platform:
        _access(snapshot, text)
    elif rest == (AGENT_FILE,) and not platform:
        assert owner is not None
        _agent(owner, text, check_model)
    elif rest == (PRIVATE_MEMORY_FILE,) and not platform:
        _memory(base / PRIVATE_MEMORY_FILE, text, memory_max_chars)
    elif platform and len(rest) == 2 and rest[0] == MEMORY_DIR and rest[1].endswith(".md"):
        name = rest[1].removesuffix(".md")
        _named(lambda: file_name(label(StoredKind.MEMORY), name), "Memory")
        _new(snapshot, StoredKind.MEMORY, name, base / MEMORY_DIR / rest[1], "Memory")
        _memory(base / MEMORY_DIR / rest[1], text, memory_max_chars)
    elif len(rest) >= 3 and rest[0] == SKILLS_DIR:
        name = rest[1]
        _named(lambda: validate_slug(label(StoredKind.SKILL), name), "Skills")
        _new(snapshot, StoredKind.SKILL, name, base / SKILLS_DIR / name, "Skills")
        if rest[2:] == (SKILL_FILE,):
            try:
                parse_frontmatter(text, name)
            except InvalidInput as e:
                raise _Rejected(str(e), "Skills") from e
    elif len(rest) == 2 and rest[0] == MCP_DIR and rest[1].endswith(".json"):
        name = rest[1].removesuffix(".json")
        _named(lambda: validate_slug(label(StoredKind.MCP), name), "MCP servers")
        _new(snapshot, StoredKind.MCP, name, base / MCP_DIR / rest[1], "MCP servers")
        try:
            parse_mcp_file(text)
        except InvalidInput as e:
            raise _Rejected(str(e), "MCP servers") from e
    elif len(rest) == 2 and rest[0] == SECRETS_DIR and rest[1].endswith(".toml"):
        _secret(base / SECRETS_DIR / rest[1], rest[1].removesuffix(".toml"), text)
    else:
        raise _Rejected(f"{'/'.join(rest)} is not a place for an asset file in {head}/", "Layout")


def _named(validate: Callable[[], object], section: Section) -> None:
    try:
        validate()
    except InvalidInput as e:
        raise _Rejected(str(e), section) from e


def _new(snapshot: Snapshot, kind: StoredKind, name: str, path: Path, section: Section) -> None:
    """新建一项资源（它的文件或目录还不在磁盘上）：名字每种资源全局唯一，别处已有的不让建（读树时两处都会被跳过）。"""
    if path.exists():
        return
    try:
        snapshot.check_free(kind, name)
    except Conflict as e:
        raise _Rejected(str(e), section) from e


def _memory(path: Path, text: str, limit: int) -> None:
    """超过上限且比原来长才拒绝：已经超长的（shell 追加、上限调低）可以一段段删短，不必一次删够。"""
    size = len(text.strip())
    old = len(path.read_text(encoding="utf-8", errors="replace").strip()) if path.is_file() else 0
    if size > limit and size > old:
        raise _Rejected(f"memory would be {size} chars, over the limit of {limit}; condense it", "Memory")


def _agent(agent_id: str, text: str, check_model: Callable[[str, Thinking], None]) -> None:
    try:
        data = parse_agent_file(text)
    except InvalidInput as e:
        raise _Rejected(str(e), "agent.toml") from e
    if data.id != agent_id:
        raise _Rejected(f"id must stay {agent_id!r} (it ties the directory to the agent)", "agent.toml")
    if not data.description.strip():
        raise _Rejected("description must not be empty", "agent.toml")
    try:
        check_model(data.model, data.thinking)
    except InvalidInput as e:
        raise _Rejected(str(e), "agent.toml") from e


def _access(snapshot: Snapshot, text: str) -> None:
    """按读树的同一套规则读一遍；只拦这次新出现的问题（文件里原有的、与这次改动无关的不挡，比如某个 agent 的目录暂时
    读坏了）。"""
    existing = {p.reason for p in snapshot.problems if p.path == snapshot.access_file}
    new = [r for r in access_problems(snapshot, text) if r not in existing]
    if new:
        raise _Rejected("; ".join(new), "Roles and permissions")


def _secret(path: Path, key: str, text: str) -> None:
    """密文只能由 vault_set 加密写入；已有的 secret 可以只改 description（密文原样不动）。"""
    hint = "secret values are encrypted by the platform: store them with vault_set"
    try:
        validate_key(key)
    except InvalidInput as e:
        raise _Rejected(str(e), "Secrets") from e
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
