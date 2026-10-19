"""Skill 文件存储。

一个 skill 是一个目录，入口 `SKILL.md` 带 YAML frontmatter（name、description）。
owner 为 None 表示公共 skill，否则为 agent 私有。agent 视角下同名私有 skill 遮蔽公共 skill。
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

import frontmatter

from muliao.errors import InvalidInput, NotFound
from muliao.naming import validate_slug
from muliao.ownership import Scope, scope_of, visible

SKILL_FILE = "SKILL.md"


@dataclass(frozen=True)
class SkillMeta:
    name: str
    description: str
    owner: str | None
    path: Path

    @property
    def scope(self) -> Scope:
        return scope_of(self.owner)


@dataclass(frozen=True)
class Skill:
    meta: SkillMeta
    body: str


class SkillStore:
    def __init__(self, public_dir: Path, agents_dir: Path):
        self._public_dir = public_dir
        self._agents_dir = agents_dir

    def _root(self, owner: str | None) -> Path:
        return self._public_dir if owner is None else self._agents_dir / owner / "skills"

    def _load(self, owner: str | None, skill_dir: Path) -> Skill:
        path = skill_dir / SKILL_FILE
        post = frontmatter.load(path)
        name, description = post.get("name"), post.get("description")
        if not isinstance(name, str) or not isinstance(description, str):
            raise InvalidInput(f"{path}: frontmatter must contain string 'name' and 'description'")
        if name != skill_dir.name:
            raise InvalidInput(
                f"{path}: frontmatter name {name!r} does not match directory {skill_dir.name!r}"
            )
        return Skill(SkillMeta(name, description, owner, path), post.content)

    def list(self, owner: str | None) -> list[SkillMeta]:
        root = self._root(owner)
        if not root.exists():
            return []
        return [
            self._load(owner, d).meta
            for d in sorted(root.iterdir())
            if d.is_dir() and (d / SKILL_FILE).exists()
        ]

    def visible_to(self, agent: str) -> list[SkillMeta]:
        return visible(
            self.list(agent) + self.list(None), agent_id=agent, owner=lambda s: s.owner, name=lambda s: s.name
        )

    def read(self, owner: str | None, name: str) -> Skill:
        skill_dir = self._root(owner) / validate_slug("skill", name)
        if not (skill_dir / SKILL_FILE).exists():
            raise NotFound(f"{scope_of(owner)} skill {name!r} not found")
        return self._load(owner, skill_dir)

    def resolve(self, agent: str, name: str) -> Skill:
        """agent 视角读取（合并规则见 ownership.visible）。"""
        for meta in self.visible_to(agent):
            if meta.name == name:
                return self.read(meta.owner, name)
        raise NotFound(f"skill {name!r} not found in private or public skills")

    def write(self, owner: str | None, name: str, description: str, body: str) -> SkillMeta:
        validate_slug("skill", name)
        description = description.strip()
        if not description:
            raise InvalidInput("skill description must not be empty")
        skill_dir = self._root(owner) / name
        skill_dir.mkdir(parents=True, exist_ok=True)
        post = frontmatter.Post(body.strip() + "\n", name=name, description=description)
        path = skill_dir / SKILL_FILE
        tmp = path.with_suffix(".tmp")
        tmp.write_text(frontmatter.dumps(post) + "\n", encoding="utf-8")
        tmp.replace(path)
        return SkillMeta(name, description, owner, path)

    def delete(self, owner: str | None, name: str) -> None:
        skill = self.read(owner, name)
        shutil.rmtree(skill.meta.path.parent)
