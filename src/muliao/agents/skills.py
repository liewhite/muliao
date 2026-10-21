"""Skill 文件存储。

一个 skill 是一个目录，入口 `SKILL.md` 带 YAML frontmatter（name、description）。存在资源仓库里，每次修改提交一版。
owner 为 None 表示公共 skill，否则为 agent 私有。agent 视角下同名私有 skill 遮蔽公共 skill。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import partial
from pathlib import Path

import frontmatter
import yaml

from muliao.agents.active import OwnerCheck
from muliao.agents.naming import validate_slug
from muliao.agents.ownership import Scope, scope_of, visible
from muliao.agents.resources import CorruptResource, ResourceRepo
from muliao.errors import CorruptState, InvalidInput, NotFound
from muliao.platform import text_edit

log = logging.getLogger(__name__)

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
    def __init__(self, repo: ResourceRepo, check_owner: OwnerCheck):
        self._repo = repo
        # 写私有 skill 之前在仓库的锁里检查 owner 还可用（agents.active）
        self._check_owner = check_owner

    def _root(self, owner: str | None) -> Path:
        return self._repo.owner_dir(owner) / "skills"

    def _load(self, owner: str | None, skill_dir: Path) -> Skill:
        path = skill_dir / SKILL_FILE
        try:
            post = frontmatter.load(path)
        except (yaml.YAMLError, ValueError) as e:
            raise CorruptState(f"{path}: cannot parse: {e}") from e
        name, description = post.get("name"), post.get("description")
        # 读的是存下的文件（写入时校验过）：不合法是被绕过工具改坏了，不是调用方的输入错误
        if not isinstance(name, str) or not isinstance(description, str):
            raise CorruptState(f"{path}: frontmatter must contain string 'name' and 'description'")
        if name != skill_dir.name:
            raise CorruptState(
                f"{path}: frontmatter name {name!r} does not match directory {skill_dir.name!r}"
            )
        return Skill(SkillMeta(name, description, owner, path), post.content)

    def _scan(self, owner: str | None) -> tuple[list[SkillMeta], list[CorruptResource]]:
        """owner 名下的 skill：读得出来的，和读不出来的（CorruptResource，每次读到都记 error）。"""
        root = self._root(owner)
        if not root.exists():
            return [], []
        skills: list[SkillMeta] = []
        corrupt: list[CorruptResource] = []
        for d in sorted(root.iterdir()):
            if not (d.is_dir() and (d / SKILL_FILE).exists()):
                continue
            try:
                skills.append(self._load(owner, d).meta)
            except CorruptState as e:
                log.error("corrupt skill", extra={"owner": owner, "skill": d.name, "error": str(e)})
                corrupt.append(CorruptResource("skill", owner, d.name, d / SKILL_FILE, str(e)))
        return skills, corrupt

    def list(self, owner: str | None) -> list[SkillMeta]:
        """owner 名下读得出来的 skill；坏的见 corrupt_in。"""
        return self._scan(owner)[0]

    def corrupt_in(self, owner: str | None) -> list[CorruptResource]:
        """owner 名下读不出来的 skill（管理界面列出，供删除或重写）。"""
        return self._scan(owner)[1]

    def index(self, agent: str) -> tuple[list[SkillMeta], list[CorruptResource]]:
        """agent 的 skill 索引：看得到的 skill（合并规则见 ownership.shadowing），以及它私有的与公共的里读不出来的
        （索引里逐项说明，不让整次运行失败）。坏的不参与同名遮蔽——读不出来就不知道它是什么——所以坏掉的私有 skill
        的同名公共 skill 照常可见。每个目录只扫一次。"""
        private, private_corrupt = self._scan(agent)
        public, public_corrupt = self._scan(None)
        skills = visible(private + public, agent_id=agent, owner=lambda s: s.owner, name=lambda s: s.name)
        return skills, private_corrupt + public_corrupt

    def visible_to(self, agent: str) -> list[SkillMeta]:
        return self.index(agent)[0]

    def _existing(self, owner: str | None, name: str) -> Path:
        skill_dir = self._root(owner) / validate_slug("skill", name)
        if not (skill_dir / SKILL_FILE).exists():
            raise NotFound(f"{scope_of(owner)} skill {name!r} not found")
        return skill_dir

    def read(self, owner: str | None, name: str) -> Skill:
        return self._load(owner, self._existing(owner, name))

    def resolve(self, agent: str, name: str) -> Skill:
        """agent 视角读取（合并规则见 ownership.shadowing）。"""
        skills, corrupt_skills = self.index(agent)
        for meta in skills:
            if meta.name == name:
                return self.read(meta.owner, name)
        for corrupt in corrupt_skills:
            if corrupt.name == name:
                # 交还模型（不让整次运行失败）：它可以照说明重写或删掉
                raise NotFound(corrupt.describe())
        raise NotFound(f"skill {name!r} not found in private or public skills")

    async def write(
        self, owner: str | None, name: str, description: str, body: str, author: str
    ) -> SkillMeta:
        validate_slug("skill", name)
        description = _valid_description(description)
        post = frontmatter.Post(body.strip() + "\n", name=name, description=description)
        path = self._root(owner) / name / SKILL_FILE
        await self._repo.apply(
            {path: frontmatter.dumps(post) + "\n"},
            f"{scope_of(owner)} skill {name}",
            author,
            precondition=partial(self._check_owner, owner),
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
        try:
            meta = self.read(owner, name).meta
        except CorruptState as e:
            # 交还调用方（模型照 skill 索引里的说明来修它时，不让整次运行失败）：坏的只能整篇重写或删除
            raise InvalidInput(f"{e}; it cannot be edited in part, rewrite it as a whole or delete it") from e

        def transform(current: str | None) -> str:
            if current is None:
                raise NotFound(f"skill {name!r} was deleted")
            post = frontmatter.loads(current)
            if edits_body:
                post.content = text_edit.edit(post.content, old, new, f"skill {name}").strip() + "\n"
            if description is not None:
                post["description"] = description
            return frontmatter.dumps(post) + "\n"

        await self._repo.update(
            meta.path,
            transform,
            f"edit {scope_of(owner)} skill {name}",
            author,
            precondition=partial(self._check_owner, owner),
        )
        return SkillMeta(name, meta.description if description is None else description, owner, meta.path)

    async def delete(self, owner: str | None, name: str, author: str) -> None:
        """只看 skill 在不在，不解析内容：改坏的 skill 也能删掉。"""
        skill_dir = self._existing(owner, name)
        await self._repo.apply(
            {skill_dir: None},
            f"delete {scope_of(owner)} skill {name}",
            author,
            precondition=partial(self._check_owner, owner),
        )


def _valid_description(description: str) -> str:
    description = description.strip()
    if not description:
        raise InvalidInput("skill description must not be empty")
    return description
