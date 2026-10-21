"""Skill 文件存储。

一个 skill 是一个目录，入口 `SKILL.md` 带 YAML frontmatter（name、description）。存在资源仓库里，每次修改提交一版。
owner 为 None 表示公共 skill，否则为 agent 私有。agent 视角下同名私有 skill 遮蔽公共 skill。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import frontmatter

from muliao.agents.naming import validate_slug
from muliao.agents.ownership import Scope, scope_of, visible
from muliao.agents.resources import ResourceRepo
from muliao.errors import CorruptState, InvalidInput, NotFound
from muliao.platform import text_edit

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
    def __init__(self, repo: ResourceRepo):
        self._repo = repo

    def _root(self, owner: str | None) -> Path:
        return self._repo.owner_dir(owner) / "skills"

    def _load(self, owner: str | None, skill_dir: Path) -> Skill:
        path = skill_dir / SKILL_FILE
        post = frontmatter.load(path)
        name, description = post.get("name"), post.get("description")
        # 读的是存下的文件（写入时校验过）：不合法是被绕过工具改坏了，不是调用方的输入错误
        if not isinstance(name, str) or not isinstance(description, str):
            raise CorruptState(f"{path}: frontmatter must contain string 'name' and 'description'")
        if name != skill_dir.name:
            raise CorruptState(
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

    async def write(
        self, owner: str | None, name: str, description: str, body: str, author: str
    ) -> SkillMeta:
        validate_slug("skill", name)
        description = _valid_description(description)
        skill_dir = self._root(owner) / name
        skill_dir.mkdir(parents=True, exist_ok=True)
        post = frontmatter.Post(body.strip() + "\n", name=name, description=description)
        path = skill_dir / SKILL_FILE
        await self._repo.apply(
            {path: frontmatter.dumps(post) + "\n"}, f"{scope_of(owner)} skill {name}", author
        )
        return SkillMeta(name, description, owner, path)

    async def edit(
        self,
        owner: str | None,
        name: str,
        author: str,
        *,
        old: str = "",
        new: str = "",
        description: str | None = None,
    ) -> SkillMeta:
        """改一个 skill，不整篇重写：正文的一部分、描述，或两者一起（同一次提交）。
        正文规则同记忆与指令（见 text_edit.edit）：old 恰好出现一次时替换为 new，old 为空时把 new 追加到末尾；
        old、new 都为空表示不改正文。description 为 None 表示不改描述。什么都不改是错误。名称不变。"""
        edits_body = bool(old or new)
        if description is not None:
            description = _valid_description(description)
        elif not edits_body:
            raise InvalidInput(
                "nothing to change: give old_text/new_text to edit the instructions, description, or both"
            )
        meta = self.read(owner, name).meta

        def transform(current: str | None) -> str:
            if current is None:
                raise NotFound(f"skill {name!r} was deleted")
            post = frontmatter.loads(current)
            if edits_body:
                post.content = text_edit.edit(post.content, old, new, f"skill {name}").strip() + "\n"
            if description is not None:
                post["description"] = description
            return frontmatter.dumps(post) + "\n"

        await self._repo.update(meta.path, transform, f"edit {scope_of(owner)} skill {name}", author)
        return SkillMeta(name, meta.description if description is None else description, owner, meta.path)

    async def delete(self, owner: str | None, name: str, author: str) -> None:
        skill = self.read(owner, name)
        await self._repo.apply(
            {skill.meta.path.parent: None}, f"delete {scope_of(owner)} skill {name}", author
        )


def _valid_description(description: str) -> str:
    description = description.strip()
    if not description:
        raise InvalidInput("skill description must not be empty")
    return description
