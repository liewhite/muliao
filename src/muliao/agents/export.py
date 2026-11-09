"""一次性迁移（#498）：把 #498 之前存在数据库里的 AI 资源导出成资源目录（agents.assets）。

启动时资源根目录不存在才做（export_if_missing）：读 agents、items、item_files、roles、agent_rules，先写进同级的临时目录，
写完再改名成根目录（中途失败不留半棵树，下次启动重来）。旧表不删、不改（回滚到旧版本照常能用）。

- agent：`<名>/agent.toml`（id、description、model、thinking、group），私有记忆写成 `<名>/memory.md`；
- 归平台的记忆、skill、MCP、secret 写进 `platform/`，归 agent 的写进它的目录；secret 的密文原样搬过去（Fernet token 是
  ASCII）；
- 角色与各 agent 的 roles / allow / deny 写进 `platform/access.toml`（内置角色只写可编辑的部分）；
- skill 与记忆正文里写死的旧 skill 目录路径（`~/.muliao/data/skills/<名>` 等写法）改成新位置；
- 最后删掉旧的 skill 只读副本目录（data/skills）。
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import stat
from collections.abc import Callable
from pathlib import Path

import tomlkit
from sqlalchemy import select

from muliao.agents import access_file, assets
from muliao.agents.agent_rules import AgentRules
from muliao.agents.assets import (
    AGENT_FILE,
    MCP_DIR,
    MEMORY_DIR,
    PLATFORM,
    PRIVATE_MEMORY_FILE,
    SECRETS_DIR,
    SKILLS_DIR,
)
from muliao.agents.mcp_config import McpConfig, mcp_file
from muliao.agents.secrets_format import SecretFile, secret_file
from muliao.agents.skill_package import SKILL_FILE
from muliao.agents.tables import Agent, AgentRulesRow, Item, ItemFile, RoleRow
from muliao.platform.db import Database
from muliao.platform.fs import write_atomic

log = logging.getLogger(__name__)

_SKILL_NAME = r"([a-z0-9][a-z0-9_-]{0,63})"


async def export_if_missing(db: Database, root: Path, legacy_skills_dir: Path) -> bool:
    """资源根目录不存在时从数据库导出；导出了返回 True。"""
    if await asyncio.to_thread(root.exists):
        return False
    async with db.transaction() as s:
        agents = list(await s.scalars(select(Agent).order_by(Agent.name)))
        items = list(await s.scalars(select(Item).order_by(Item.kind, Item.name)))
        files = list(await s.scalars(select(ItemFile).order_by(ItemFile.item_id, ItemFile.path)))
        roles = list(await s.scalars(select(RoleRow).order_by(RoleRow.name)))
        rules = {r.agent_id: r for r in await s.scalars(select(AgentRulesRow))}
        snapshot = _Rows(agents, items, files, roles, rules)
    await asyncio.to_thread(_export, snapshot, root, legacy_skills_dir)
    log.info(
        "AI assets exported from the database to files",
        extra={"root": str(root), "agents": len(agents), "items": len(items), "roles": len(roles)},
    )
    return True


class _Rows:
    def __init__(
        self,
        agents: list[Agent],
        items: list[Item],
        files: list[ItemFile],
        roles: list[RoleRow],
        rules: dict[str, AgentRulesRow],
    ):
        self.agents = agents
        self.items = items
        self.files = files
        self.roles = roles
        self.rules = rules


def _export(rows: _Rows, root: Path, legacy_skills_dir: Path) -> None:
    tmp = root.parent / f".{root.name}.export"
    assets.remove(tmp)
    _write(rows, tmp, root, legacy_skills_dir)
    tmp.rename(root)
    _remove_legacy_dir(legacy_skills_dir)


def _write(rows: _Rows, tmp: Path, root: Path, legacy_skills_dir: Path) -> None:
    names = {a.id: a.name for a in rows.agents}

    def owner_dir(base: Path, owner: str | None) -> Path:
        return base / (PLATFORM if owner is None else names[owner])

    (tmp / PLATFORM).mkdir(parents=True)
    for a in rows.agents:
        doc = tomlkit.document()
        doc["id"] = a.id
        doc["description"] = a.description
        doc["model"] = a.model
        doc["thinking"] = a.thinking
        if a.group is not None:
            doc["group"] = a.group
        write_atomic(tmp / a.name / AGENT_FILE, tomlkit.dumps(doc))

    # 旧路径 → 新路径（导出后的根目录下）
    skill_dirs = {
        i.name: owner_dir(root, i.owner) / SKILLS_DIR / i.name for i in rows.items if i.kind == "skill"
    }
    rewrite = _path_rewriter(legacy_skills_dir, skill_dirs)
    by_item: dict[int, list[ItemFile]] = {}
    for f in rows.files:
        by_item.setdefault(f.item_id, []).append(f)

    for i in rows.items:
        base = owner_dir(tmp, i.owner)
        match i.kind:
            case "memory":
                path = (
                    base / PRIVATE_MEMORY_FILE if i.owner is not None else base / MEMORY_DIR / f"{i.name}.md"
                )
                if i.owner is not None and names[i.owner] != i.name:
                    # 私有记忆名就是 agent 名；名字不同的（旧版允许）挪到平台，内容不丢
                    path = tmp / PLATFORM / MEMORY_DIR / f"{i.name}.md"
                    log.warning(
                        "memory not named after its agent moved to the platform", extra={"memory": i.name}
                    )
                text = rewrite(i.content)
                write_atomic(path, text + "\n" if text else "")
            case "skill":
                directory = base / SKILLS_DIR / i.name
                write_atomic(directory / SKILL_FILE, rewrite(i.content))
                for f in by_item.get(i.id, []):
                    write_atomic(directory / f.path, rewrite(f.content), executable=f.executable)
            case "mcp":
                config = McpConfig.model_validate_json(i.content)
                write_atomic(base / MCP_DIR / f"{i.name}.json", mcp_file(config, i.enabled))
            case "secret":
                assert i.secret is not None, "a secret item always has its ciphertext"
                write_atomic(
                    base / SECRETS_DIR / f"{i.name}.toml",
                    secret_file(SecretFile(i.description, i.secret, allow_shell=True)),
                )
            case other:
                raise RuntimeError(f"unknown item kind {other!r} ({i.name})")

    def change(doc: tomlkit.TOMLDocument) -> None:
        for r in rows.roles:
            access_file.set_role(doc, r.name, r.description, list(r.allow), list(r.deny))
        for a in rows.agents:
            row = rows.rules.get(a.id)
            if row is not None:
                access_file.set_agent_rules(doc, a.name, AgentRules.of(row.roles, row.allow, row.deny))

    access_file.edit(tmp / PLATFORM / assets.ACCESS_FILE, change)


def _path_rewriter(legacy: Path, skill_dirs: dict[str, Path]) -> Callable[[str], str]:
    """正文里旧 skill 目录的写法（绝对路径、`~/…`、`$HOME/…`）后面跟着 skill 名的，换成同一种写法的新位置。"""
    legacy = legacy.expanduser()
    home = Path.home()
    forms: list[tuple[str, Callable[[Path], str]]] = [(str(legacy), str)]
    if legacy.is_relative_to(home):
        rel = legacy.relative_to(home)
        forms += [
            (f"~/{rel}", lambda p: f"~/{p.relative_to(home)}" if p.is_relative_to(home) else str(p)),
            (f"$HOME/{rel}", lambda p: f"$HOME/{p.relative_to(home)}" if p.is_relative_to(home) else str(p)),
        ]
    patterns = [(re.compile(re.escape(prefix) + "/" + _SKILL_NAME), render) for prefix, render in forms]

    def rewrite(text: str) -> str:
        for pattern, render in patterns:

            def sub(m: re.Match[str], render: Callable[[Path], str] = render) -> str:
                target = skill_dirs.get(m.group(1))
                return m.group(0) if target is None else render(target)

            text = pattern.sub(sub, text)
        return text

    return rewrite


def _remove_legacy_dir(path: Path) -> None:
    """旧的只读副本（目录 0555、文件 0444）：先放开权限再删。"""
    if not path.exists():
        return
    for d, _, _ in os.walk(path):
        os.chmod(d, stat.S_IRWXU)
    shutil.rmtree(path)
