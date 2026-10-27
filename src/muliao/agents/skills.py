"""Skill：条目（items，kind=skill）。SKILL.md（YAML frontmatter：name、description + 正文）存在 items.content，附件
（脚本、配置、参考文档）存在 item_files，每次修改记一版（revisions，文档是全部文件的 JSON）。

磁盘上的 skill 目录（`<skills_dir>/<name>/`）是数据库生成的只读副本：每次写入后重新生成这个 skill 的目录，启动时全量
重新生成（多余的目录删掉）。agent 读 skill 里的脚本、执行它们都在这个目录里；改附件要经 write_skill / edit_skill 的
attachments（从工作目录里的一个目录收进来），直接改副本会被拒绝（只读）且下次生成时被覆盖。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import stat
import tempfile
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import frontmatter
import yaml
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.agents import skill_package
from muliao.agents.access import grants_in
from muliao.agents.items import changeable_in, creatable_in, existing_in, item_in, usable_in, writable_in
from muliao.agents.kinds import Kind, StoredKind, Subject, label
from muliao.agents.naming import validate_slug
from muliao.agents.revisions import record_in, revision_doc_in
from muliao.agents.skill_package import SKILL_FILE, SkillFile, Skipped
from muliao.agents.tables import Item, ItemFile
from muliao.errors import InvalidInput
from muliao.platform import text_edit
from muliao.platform.db import Database

log = logging.getLogger(__name__)

# 生成目录里的权限：只读（脚本保留可执行位）
_FILE_MODE, _EXEC_MODE, _DIR_MODE = 0o444, 0o555, 0o555
# 生成中的临时目录与被替换下来的旧目录的前缀（隐藏，生成完即删；启动时清理残留）
_TMP_PREFIX = "."


@dataclass(frozen=True)
class SkillMeta:
    name: str
    description: str
    owner: str | None
    # 生成目录里的 SKILL.md
    path: Path


@dataclass(frozen=True)
class Skill:
    meta: SkillMeta
    body: str
    # 读的 agent 是否用得到它（自己的或分发给它的，agents.access）；不是也照常读到（软隔离）
    usable: bool = True


@dataclass(frozen=True)
class SkillImport:
    meta: SkillMeta
    # 包里没有导入的文件与原因（见 skill_package）
    skipped: list[Skipped]


def parse_frontmatter(text: str, name: str) -> tuple[str, frontmatter.Post]:
    """SKILL.md 的 frontmatter 规则：name、description 都是字符串，name 与 skill 名一致，description 非空。返回描述与解析结果；
    不合规则时 InvalidInput（写入的 SKILL.md 来自调用方：导入的包、恢复的旧版）。"""
    try:
        post = frontmatter.loads(text)
    except (yaml.YAMLError, ValueError) as e:
        raise InvalidInput(f"{name}/{SKILL_FILE}: cannot parse frontmatter: {e}") from e
    fm_name, description = post.get("name"), post.get("description")
    if not isinstance(fm_name, str) or not isinstance(description, str):
        raise InvalidInput(f"{name}/{SKILL_FILE}: frontmatter must contain string 'name' and 'description'")
    if fm_name != name:
        raise InvalidInput(f"{name}/{SKILL_FILE}: frontmatter name {fm_name!r} does not match {name!r}")
    return _valid_description(description), post


def _skill_md(name: str, description: str, body: str) -> str:
    return frontmatter.dumps(frontmatter.Post(body.strip() + "\n", name=name, description=description)) + "\n"


def document(skill_md: str, attachments: Mapping[str, SkillFile]) -> str:
    """一个 skill 在版本表里的文档：全部文件（SKILL.md 与附件）的 JSON。"""
    files = {SKILL_FILE: SkillFile(skill_md), **attachments}
    return json.dumps(
        {rel: {"content": f.content, "executable": f.executable} for rel, f in sorted(files.items())},
        ensure_ascii=False,
        indent=1,
    )


def parse_document(doc: str) -> dict[str, SkillFile]:
    return {rel: SkillFile(f["content"], f["executable"]) for rel, f in json.loads(doc).items()}


def attachments_from(directory: Path) -> tuple[dict[str, SkillFile], list[Skipped]]:
    """一个目录里能作为附件收进 skill 的文件（同导入的规则：skill_package.accept），以及跳过的与原因。目录里的 SKILL.md
    不算附件（SKILL.md 由 content 给出）。不跟随符号链接；隐藏目录与依赖目录整个跳过（只记一条，不逐个列出里面的文件，
    给错目录时结果也不会有成千上万行）。读文件是同步 IO，调用方放到线程里。"""
    if not directory.is_dir():
        raise InvalidInput(f"attachments directory {directory} does not exist or is not a directory")
    files: dict[str, SkillFile] = {}
    skipped: list[Skipped] = []
    for current, dirs, names in os.walk(directory):
        base = Path(current)
        for d in sorted(dirs):
            if d.startswith(".") or d in skill_package.NOT_ATTACHMENT_DIRS:
                skipped.append(
                    Skipped((base / d).relative_to(directory).as_posix() + "/", "directory not kept")
                )
        dirs[:] = sorted(
            d for d in dirs if not d.startswith(".") and d not in skill_package.NOT_ATTACHMENT_DIRS
        )
        for name in sorted(names):
            path = base / name
            rel = path.relative_to(directory).as_posix()
            if path.is_symlink():
                skipped.append(Skipped(rel, "symbolic link"))
                continue
            if not path.is_file() or rel == SKILL_FILE:
                continue
            accepted = skill_package.accept(rel, path.stat().st_size, path.read_bytes, skipped)
            if accepted is not None:
                files[rel] = SkillFile(accepted.content, os.access(path, os.X_OK))
    return files, skipped


class SkillStore:
    def __init__(self, db: Database, skills_dir: Path):
        self._db = db
        self.skills_dir = skills_dir
        # 生成目录的写入串行：每次都按数据库里最新的状态生成，并发的写入最终收敛到最后一次
        self._materialize_lock = asyncio.Lock()

    def dir(self, name: str) -> Path:
        return self.skills_dir / validate_slug(label(StoredKind.SKILL), name)

    def _meta(self, row: Item) -> SkillMeta:
        return SkillMeta(row.name, row.description, row.owner, self.dir(row.name) / SKILL_FILE)

    async def index(self, agent: str) -> list[SkillMeta]:
        """agent 能用的 skill（自己的与分发给它的，agents.access），按名称：它的 skill 索引。"""
        async with self._db.transaction() as s:
            return [self._meta(r) for r in await usable_in(s, StoredKind.SKILL, agent)]

    async def read(self, name: str) -> Skill:
        async with self._db.transaction() as s:
            row = await existing_in(s, StoredKind.SKILL, name)
            return Skill(self._meta(row), frontmatter.loads(row.content).content)

    async def resolve(self, agent: str, name: str) -> Skill:
        """agent 读取：任何存在的 skill 都读得到（软隔离），usable 标明它是否在这个 agent 能用的范围里。"""
        async with self._db.transaction() as s:
            row = await existing_in(s, StoredKind.SKILL, name)
            usable = (await grants_in(s, agent)).uses(Kind.SKILL, name, row.owner)
            return Skill(self._meta(row), frontmatter.loads(row.content).content, usable)

    async def files(self, name: str) -> dict[str, SkillFile]:
        """SKILL.md 与全部附件。"""
        async with self._db.transaction() as s:
            row = await existing_in(s, StoredKind.SKILL, name)
            return {SKILL_FILE: SkillFile(row.content), **await _attachments_in(s, row)}

    async def write(
        self,
        name: str,
        description: str,
        body: str,
        *,
        actor: str,
        owner: str | None,
        attachments: Mapping[str, SkillFile] | None = None,
    ) -> SkillMeta:
        """新建或整体改写 SKILL.md；attachments 为 None 时附件不变，否则整体换成它。actor（agent 的 id 或 USER）是改的人，
        已有的要它能改（规则 2）；新建的归 owner，已有的 owner 不变。"""
        validate_slug(label(StoredKind.SKILL), name)
        description = _valid_description(description)
        return await self._put(name, _skill_md(name, description, body), attachments, actor, owner, "write")

    async def edit(
        self,
        name: str,
        actor: str,
        *,
        old: str = "",
        new: str = "",
        description: str | None = None,
        attachments: Mapping[str, SkillFile] | None = None,
    ) -> SkillMeta:
        """改一个 skill，不整篇重写：正文的一部分、描述、附件（整体换成 attachments），可以同时改（同一版）。
        正文规则同记忆（见 text_edit.edit）：old 恰好出现一次时替换为 new，old 为空时把 new 追加到末尾；
        old、new 都为空表示不改正文。description / attachments 为 None 表示不改。什么都不改是错误。名称不变。"""
        edits_body = bool(old or new)
        if description is not None:
            description = _valid_description(description)
        elif not edits_body and attachments is None:
            raise InvalidInput(
                "nothing to change: give old_text/new_text to edit the instructions, description, "
                "attachments_dir, or several of them"
            )
        # 读取、修改、写入与记版本在同一个写事务里（同记忆）：并发的编辑依次基于最新正文应用，互不覆盖
        async with self._db.transaction() as s:
            row = await changeable_in(s, StoredKind.SKILL, name, actor)
            post = frontmatter.loads(row.content)
            if edits_body:
                post.content = text_edit.edit(post.content, old, new, f"skill {name}").strip() + "\n"
            if description is not None:
                post["description"] = description
            meta = await self._store_in(
                s, row, name, frontmatter.dumps(post) + "\n", attachments, actor, row.owner, "edit"
            )
        await self.materialize(name)
        return meta

    async def _put(
        self,
        name: str,
        skill_md: str,
        attachments: Mapping[str, SkillFile] | None,
        actor: str,
        owner: str | None,
        verb: str,
        *,
        replace: bool = True,
    ) -> SkillMeta:
        """新建或整篇写入：已有的按规则 2 查 actor，owner 不变；新建的归 owner。replace 为假时只新建：同名的已存在
        Conflict（在写入的事务里查，并发的新建不会互相覆盖）。"""
        async with self._db.transaction() as s:
            if replace:
                row = await writable_in(s, StoredKind.SKILL, name, actor, owner)
            else:
                await creatable_in(s, StoredKind.SKILL, name, owner)
                row = None
            meta = await self._store_in(s, row, name, skill_md, attachments, actor, owner, verb)
        await self.materialize(name)
        return meta

    async def _store_in(
        self,
        s: AsyncSession,
        row: Item | None,
        name: str,
        skill_md: str,
        attachments: Mapping[str, SkillFile] | None,
        actor: str,
        owner: str | None,
        verb: str,
    ) -> SkillMeta:
        """在调用方的事务里写入 SKILL.md（校验 frontmatter）与附件（None 表示不变）、记一版；row 为 None 时新建，归 owner。
        生成目录由调用方在事务提交后做。"""
        description, _ = parse_frontmatter(skill_md, name)
        for rel in attachments or {}:
            if not skill_package.is_attachment(rel) or rel == SKILL_FILE:
                raise InvalidInput(f"{rel!r} cannot be an attachment of a skill")
        old_files = {} if row is None else await _attachments_in(s, row)
        before = None if row is None else document(row.content, old_files)
        if row is None:
            row = Item(kind=StoredKind.SKILL.value, name=name, owner=owner)
            s.add(row)
        row.content, row.description = skill_md, description
        new_files = old_files if attachments is None else dict(attachments)
        if attachments is not None:
            await s.flush()
            await s.execute(delete(ItemFile).where(ItemFile.item_id == row.id))
            s.add_all(
                ItemFile(item_id=row.id, path=rel, content=f.content, executable=f.executable)
                for rel, f in new_files.items()
            )
        record_in(
            s, Subject.SKILL, name, before, document(skill_md, new_files), actor, f"{verb} skill {name}"
        )
        await s.flush()
        return self._meta(row)

    async def export(self, name: str) -> bytes:
        """打成标准 .skill 文件（skill_package）：SKILL.md 与附件。"""
        return skill_package.pack(name, await self.files(name))

    async def import_package(
        self, data: bytes, *, actor: str, owner: str | None, replace: bool
    ) -> SkillImport:
        """导入标准 .skill 文件，新建的归 owner，记一版。SKILL.md 原样写入（标准里的 license、metadata 等其他
        frontmatter 键保留），但要满足与 write 相同的规则。同名 skill 已存在时：replace 为假报冲突；为真则覆盖（附件整体
        换掉，owner 不变，actor 要能改它）。"""
        package = skill_package.unpack(data)
        name = package.name
        skill_md = package.files[SKILL_FILE].content
        parse_frontmatter(skill_md, name)
        attachments = {rel: f for rel, f in package.files.items() if rel != SKILL_FILE}
        meta = await self._put(name, skill_md, attachments, actor, owner, "import", replace=replace)
        return SkillImport(meta, package.skipped)

    async def restore(self, name: str, rev: int, author: str) -> SkillMeta:
        """把第 rev 版的文档作为新的一版写入（归属不变：恢复的是内容），读版本与写入在同一个事务里。"""
        async with self._db.transaction() as s:
            row = await changeable_in(s, StoredKind.SKILL, name, author)
            files = parse_document(await revision_doc_in(s, Subject.SKILL, name, rev))
            skill_md = files.pop(SKILL_FILE).content
            meta = await self._store_in(s, row, name, skill_md, files, author, row.owner, "restore")
        await self.materialize(name)
        return meta

    async def delete(self, name: str, actor: str) -> None:
        async with self._db.transaction() as s:
            row = await changeable_in(s, StoredKind.SKILL, name, actor)
            before = document(row.content, await _attachments_in(s, row))
            await s.delete(row)
            record_in(s, Subject.SKILL, name, before, None, actor, f"delete skill {name}")
        await self.materialize(name)

    async def owned_names(self, owner: str) -> list[str]:
        async with self._db.transaction() as s:
            rows = await s.scalars(
                select(Item.name).where(Item.kind == StoredKind.SKILL.value, Item.owner == owner)
            )
            return list(rows)

    # ---- 生成目录 ----

    async def materialize(self, name: str) -> None:
        """按数据库里现在的状态重新生成这个 skill 的目录（已删除就删掉目录）。"""
        async with self._materialize_lock:
            async with self._db.transaction() as s:
                row = await item_in(s, StoredKind.SKILL, name)
                files = (
                    None
                    if row is None
                    else {SKILL_FILE: SkillFile(row.content), **await _attachments_in(s, row)}
                )
            await asyncio.to_thread(_materialize, self.skills_dir, name, files)

    async def remove_generated(self, names: list[str]) -> None:
        """删掉这些 skill 的生成目录（它们的行即将随 agent 删除）。"""
        async with self._materialize_lock:
            for name in names:
                await asyncio.to_thread(_materialize, self.skills_dir, name, None)

    async def materialize_all(self) -> None:
        """启动时：按数据库全量重新生成，删掉数据库里没有的目录与上次留下的临时目录。"""
        async with self._materialize_lock:
            async with self._db.transaction() as s:
                rows = list(await s.scalars(select(Item).where(Item.kind == StoredKind.SKILL.value)))
                skills = {
                    r.name: {SKILL_FILE: SkillFile(r.content), **await _attachments_in(s, r)} for r in rows
                }
            await asyncio.to_thread(_materialize_all, self.skills_dir, skills)
        log.info("skill directories generated", extra={"skills": len(skills), "root": str(self.skills_dir)})


async def _attachments_in(s, row: Item) -> dict[str, SkillFile]:  # noqa: ANN001
    files = await s.scalars(select(ItemFile).where(ItemFile.item_id == row.id).order_by(ItemFile.path))
    return {f.path: SkillFile(f.content, f.executable) for f in files}


def _valid_description(description: str) -> str:
    description = description.strip()
    if not description:
        raise InvalidInput("skill description must not be empty")
    return description


def _force_rmtree(path: Path) -> None:
    """删掉一个（可能只读的）生成目录。"""
    if not path.exists() and not path.is_symlink():
        return
    if path.is_symlink() or path.is_file():
        path.unlink()
        return
    for d, _, _ in os.walk(path):
        os.chmod(d, stat.S_IRWXU)
    shutil.rmtree(path)


def _materialize(root: Path, name: str, files: Mapping[str, SkillFile] | None) -> None:
    """生成（或删除）一个 skill 目录：先在临时目录里写好，再换上去，读的人看不到写了一半的目录。"""
    root.mkdir(parents=True, exist_ok=True)
    target = root / name
    if files is None:
        _force_rmtree(target)
        return
    tmp = Path(tempfile.mkdtemp(prefix=f"{_TMP_PREFIX}{name}.", dir=root))
    try:
        for rel, f in files.items():
            path = tmp / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f.content, encoding="utf-8")
            path.chmod(_EXEC_MODE if f.executable else _FILE_MODE)
        for d, _, _ in sorted(os.walk(tmp), key=lambda w: len(w[0]), reverse=True):
            os.chmod(d, _DIR_MODE)
        old = None
        if target.exists() or target.is_symlink():
            # 同一个上级里改名：换上级要改只读目录自己的 ..
            old = root / f"{_TMP_PREFIX}{name}.old.{uuid.uuid4().hex}"
            target.rename(old)
        tmp.rename(target)
        if old is not None:
            _force_rmtree(old)
    except BaseException:
        _force_rmtree(tmp)
        raise


def _materialize_all(root: Path, skills: Mapping[str, Mapping[str, SkillFile]]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for entry in root.iterdir():
        if entry.name not in skills:
            _force_rmtree(entry)
    for name, files in skills.items():
        _materialize(root, name, files)
