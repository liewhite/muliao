"""Skill：AI 资源的一种，一个目录（`skills/<名>/`，在 owner 的资源目录下，agents.assets）：SKILL.md（YAML frontmatter：
description，可选 name 须与目录同名）加任意附件。agent 直接用文件工具读、改、执行里面的文件；这里是平台自己的读写：
skill 索引、界面的编辑页（只改 SKILL.md）、标准 .skill 文件的导入导出与删除。
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import frontmatter

from muliao.agents import assets, skill_package
from muliao.agents.access import changeable, usable, writable
from muliao.agents.kinds import StoredKind, label
from muliao.agents.naming import validate_slug
from muliao.agents.skill_package import SKILL_FILE, SkillFile, Skipped, parse_frontmatter
from muliao.agents.source import AssetSource
from muliao.platform.fs import write_atomic

log = logging.getLogger(__name__)

_KIND = StoredKind.SKILL


@dataclass(frozen=True)
class SkillMeta:
    name: str
    description: str
    owner: str | None
    # SKILL.md 的位置
    path: Path


@dataclass(frozen=True)
class Skill:
    meta: SkillMeta
    body: str


@dataclass(frozen=True)
class SkillImport:
    meta: SkillMeta
    # 包里没有导入的文件与原因（见 skill_package）
    skipped: list[Skipped]


def _meta(a: assets.Asset) -> SkillMeta:
    return SkillMeta(a.name, a.description, a.owner, a.path / SKILL_FILE)


def _read_files(directory: Path) -> dict[str, SkillFile]:
    """skill 目录里的全部 UTF-8 文本文件（导出用；二进制文件不进 .skill 包）。"""
    files: dict[str, SkillFile] = {}
    for current, dirs, names in os.walk(directory):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for name in sorted(names):
            path = Path(current) / name
            if name.startswith(".") or path.is_symlink() or not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            files[path.relative_to(directory).as_posix()] = SkillFile(text, os.access(path, os.X_OK))
    return files


def _replace_dir(directory: Path, files: Mapping[str, SkillFile]) -> None:
    """把 skill 目录整体换成 files：先在同级临时目录写好再换上。两次 rename 之间进程死掉时目录会不在、只剩隐藏的
    `.<名>.old`（读树不读隐藏目录）：罕见，由人把它改回原名，不自动恢复。"""
    parent = directory.parent
    parent.mkdir(parents=True, exist_ok=True)
    tmp = parent / f".{directory.name}.new"
    assets.remove(tmp)
    for rel, f in files.items():
        write_atomic(tmp / rel, f.content, executable=f.executable)
    old = parent / f".{directory.name}.old"
    assets.remove(old)
    if directory.exists():
        directory.rename(old)
    tmp.rename(directory)
    assets.remove(old)


class SkillStore:
    def __init__(self, source: AssetSource):
        self._source = source

    async def index(self, agent: str) -> list[SkillMeta]:
        """agent 能用的 skill（自己的与分发给它的，agents.access），按名称：它的 skill 索引。"""
        return [_meta(a) for a in usable(await self._source.snapshot(), _KIND, agent)]

    async def read(self, name: str) -> Skill:
        asset = (await self._source.snapshot()).asset(_KIND, name)
        text = await asyncio.to_thread((asset.path / SKILL_FILE).read_text, encoding="utf-8")
        return Skill(_meta(asset), frontmatter.loads(text).content)

    async def write(
        self, name: str, description: str, body: str, *, actor: str, owner: str | None
    ) -> SkillMeta:
        """新建或整体改写 SKILL.md（附件不变；frontmatter 里别的键保留）。已有的要 actor 能改（Grants.can 的 write），owner 不变；
        新建的归 owner。"""
        validate_slug(label(_KIND), name)
        description = skill_package.valid_description(description)
        async with self._source.writing() as snapshot:
            existing = writable(snapshot, _KIND, name, actor)
            directory = existing.path if existing is not None else snapshot.path_of(_KIND, name, owner)
            md = directory / SKILL_FILE

            def put() -> None:
                post = (
                    frontmatter.loads(md.read_text(encoding="utf-8"))
                    if md.is_file()
                    else frontmatter.Post("")
                )
                post.content = body.strip() + "\n"
                post["name"], post["description"] = name, description
                write_atomic(md, frontmatter.dumps(post) + "\n")

            await asyncio.to_thread(put)
        return _meta((await self._source.snapshot()).asset(_KIND, name))

    async def files(self, name: str) -> dict[str, SkillFile]:
        """SKILL.md 与全部文本附件（二进制文件不在内）。"""
        asset = (await self._source.snapshot()).asset(_KIND, name)
        return await asyncio.to_thread(_read_files, asset.path)

    async def export(self, name: str) -> bytes:
        """打成标准 .skill 文件（skill_package）：SKILL.md 与文本附件。"""
        return skill_package.pack(name, await self.files(name))

    async def import_package(
        self, data: bytes, *, actor: str, owner: str | None, replace: bool
    ) -> SkillImport:
        """导入标准 .skill 文件，新建的归 owner。SKILL.md 原样写入（标准里的 license、metadata 等其他 frontmatter 键保留）。
        同名 skill 已存在时：replace 为假报冲突；为真则整个目录换掉（owner 不变，actor 要能改它）。"""
        package = skill_package.unpack(data)
        name = package.name
        parse_frontmatter(package.files[SKILL_FILE].content, name)
        async with self._source.writing() as snapshot:
            existing = snapshot.get(_KIND, name)
            if existing is None or not replace:
                snapshot.check_free(_KIND, name)
            writable(snapshot, _KIND, name, actor)
            directory = existing.path if existing is not None else snapshot.path_of(_KIND, name, owner)
            await asyncio.to_thread(_replace_dir, directory, package.files)
        meta = _meta((await self._source.snapshot()).asset(_KIND, name))
        return SkillImport(meta, package.skipped)

    async def delete(self, name: str, actor: str) -> None:
        async with self._source.writing() as snapshot:
            asset = changeable(snapshot, _KIND, name, actor)
            await asyncio.to_thread(assets.remove, asset.path)
