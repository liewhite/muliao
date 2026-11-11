"""Skill：AI 资源的一种，一个目录（`skills/<名>/`，在 owner 的资源目录下，agents.assets）：SKILL.md（YAML frontmatter：
description，可选 name 须与目录同名）加任意附件。agent 直接用文件工具读、改、执行里面的文件；这里是平台自己的读写：
skill 索引、界面的编辑页（只改 SKILL.md；整个目录的只读浏览是 workspace.browse）、标准 .skill 文件的导入导出与删除。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import frontmatter

from muliao.agents import assets, skill_package
from muliao.agents.access import usable
from muliao.agents.kinds import StoredKind, spec_of
from muliao.agents.skill_package import SKILL_FILE, SkillFile, Skipped, parse_frontmatter
from muliao.agents.source import AssetSource
from muliao.errors import Conflict, NotFound
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
class SkillScripts:
    # skill 的目录（脚本在里面）
    directory: Path
    # 执行前要用户批准的脚本（SKILL.md frontmatter 的 approval，读树时已换成文件的身份，platform.fs.real）
    approval: tuple[Path, ...]

    def needs_approval(self, script: Path) -> bool:
        """这个脚本（身份，platform.fs.real）在要批准的列表里。"""
        return script in self.approval

    def fingerprint(self) -> str:
        """skill 目录里除 SKILL.md 以外全部文件的内容指纹（sha256，十六进制）：「始终允许」按它认同一份脚本（#531），
        改了脚本或它用的辅助文件就不再算同一份，改说明不算。按相对路径排序，路径与内容一起算；跳过 `__pycache__`
        （运行 Python 脚本时自动生成，Python 只在源文件对得上时用它）与隐藏的文件和目录（`.cache`、`.venv`、`.DS_Store` 这类脚本或系统
        自己写的，算进来跑一次就对不上；代价是它们里面的代码改了不作废始终允许，脚本要用的代码不放隐藏处）；符号链接跟进去算目标的内容（链到目录的也是：
        脚本可能 source 里面的文件），同一个目录只算一次（链接成环也不会走不完）。阻塞 IO，在线程里调。"""
        root = self.directory
        digest = hashlib.sha256()
        seen: set[tuple[int, int]] = set()
        for dirpath, dirnames, filenames in os.walk(root, followlinks=True):
            here = Path(dirpath)
            st = here.stat()
            if (st.st_dev, st.st_ino) in seen:
                dirnames[:] = []
                continue
            seen.add((st.st_dev, st.st_ino))
            dirnames[:] = sorted(d for d in dirnames if d != "__pycache__" and not d.startswith("."))
            digest.update(f"{here.relative_to(root).as_posix()}/\0".encode())
            for name in sorted(filenames):
                path = here / name
                rel = path.relative_to(root).as_posix()
                if rel == SKILL_FILE or name.startswith(".") or not path.is_file():
                    continue
                digest.update(f"{rel}\0".encode())
                digest.update(hashlib.sha256(path.read_bytes()).digest())
        return digest.hexdigest()


@dataclass(frozen=True)
class Skill:
    meta: SkillMeta
    body: str
    # SKILL.md 的版本（assets.version_of）：整体改写时交回
    version: str


@dataclass(frozen=True)
class SkillImport:
    meta: SkillMeta
    # 包里没有导入的文件与原因（见 skill_package）
    skipped: list[Skipped]


def _meta(a: assets.Asset) -> SkillMeta:
    return SkillMeta(a.name, a.description, a.owner, a.path / SKILL_FILE)


@dataclass(frozen=True)
class SkillFiles:
    # 相对 skill 目录的路径 → 内容，含 SKILL.md
    files: dict[str, SkillFile]
    # 没有收进来的文件与原因（不是 UTF-8 文本的）
    skipped: list[Skipped]


@dataclass(frozen=True)
class SkillExport:
    # 标准 .skill 文件
    package: bytes
    # 没有打进包的文件与原因
    skipped: list[Skipped]


def _read_files(directory: Path) -> SkillFiles:
    """skill 目录里的全部 UTF-8 文本文件（导出用）；二进制文件不进 .skill 包，列进 skipped。"""
    files: dict[str, SkillFile] = {}
    skipped: list[Skipped] = []
    for current, dirs, names in os.walk(directory):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for name in sorted(names):
            path = Path(current) / name
            if name.startswith(".") or path.is_symlink() or not path.is_file():
                continue
            rel = path.relative_to(directory).as_posix()
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                skipped.append(Skipped(rel, "not UTF-8 text"))
                continue
            files[rel] = SkillFile(text, os.access(path, os.X_OK))
    return SkillFiles(files, skipped)


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

    async def scripts(self, name: str, agent: str) -> SkillScripts:
        """agent 能用的这个 skill 的脚本在哪、哪些要批准（skill_run 用）；不存在或它不能用都是 NotFound。"""
        found = {a.name: a for a in usable(await self._source.snapshot(), _KIND, agent)}
        if name not in found:
            raise NotFound(f"skill {name!r} not found among the skills you can use (see your skill index)")
        return SkillScripts(found[name].path, assets.content_of(found[name], assets.SkillDir).approval)

    async def read(self, name: str) -> Skill:
        asset = (await self._source.snapshot()).asset(_KIND, name)
        text = await asyncio.to_thread((asset.path / SKILL_FILE).read_text, encoding="utf-8")
        return Skill(_meta(asset), frontmatter.loads(text).content, assets.version_of(text))

    async def directory(self, name: str) -> Path:
        """skill 的目录（界面只读浏览它的文件用）。"""
        return (await self._source.snapshot()).asset(_KIND, name).path

    async def write(
        self, name: str, description: str, body: str, *, owner: str | None, version: str | None
    ) -> SkillMeta:
        """新建或整体改写 SKILL.md（附件不变；frontmatter 里别的键保留），用户在界面上编辑。已有的 owner 不变；
        新建的归 owner。version 是打开编辑时 SKILL.md 的版本（新建为 None），期间被别处改过就 Conflict（assets.check_version）。"""
        description = skill_package.valid_description(description)
        snapshot = await self._source.snapshot()
        existing = snapshot.existing(_KIND, name)
        md = (existing.path if existing is not None else snapshot.path_of(_KIND, name, owner)) / SKILL_FILE
        current = await asyncio.to_thread(md.read_text, encoding="utf-8") if existing is not None else None
        post = frontmatter.Post("") if current is None else frontmatter.loads(current)
        post.content = body.strip() + "\n"
        post["name"], post["description"] = name, description

        def unchanged(path: Path) -> None:
            # 此刻文件里的（写入口在锁里、紧挨着写之前调用）：期间被删、被抢先建、被改过都对不上
            now = path.read_text(encoding="utf-8") if path.is_file() else None
            assets.check_version(now, version, f"skill {name!r}")

        await self._source.write({md: frontmatter.dumps(post) + "\n"}, unchanged)
        return _meta((await self._source.snapshot()).asset(_KIND, name))

    async def files(self, name: str) -> SkillFiles:
        """SKILL.md 与全部文本附件；二进制文件不在内，列在 skipped。"""
        asset = (await self._source.snapshot()).asset(_KIND, name)
        return await asyncio.to_thread(_read_files, asset.path)

    async def export(self, name: str) -> SkillExport:
        """打成标准 .skill 文件（skill_package）：SKILL.md 与文本附件；没打进去的同导入一样逐个列出原因。"""
        read = await self.files(name)
        return SkillExport(skill_package.pack(name, read.files), read.skipped)

    async def import_package(self, data: bytes, *, owner: str | None, replace: bool) -> SkillImport:
        """导入标准 .skill 文件，新建的归 owner。SKILL.md 原样写入（标准里的 license、metadata 等其他 frontmatter 键保留）。
        同名 skill 已存在时：replace 为假报冲突；为真则整个目录换掉（owner 不变）。"""
        package = skill_package.unpack(data)
        name = package.name
        spec_of(_KIND).check_name(name)
        parse_frontmatter(package.files[SKILL_FILE].content, name)
        async with self._source.writing() as snapshot:
            existing = snapshot.existing(_KIND, name)
            if existing is not None and not replace:
                raise Conflict(f"skill {name!r} already exists; import it with replace to overwrite it")
            directory = existing.path if existing is not None else snapshot.path_of(_KIND, name, owner)
            await asyncio.to_thread(_replace_dir, directory, package.files)
        meta = _meta((await self._source.snapshot()).asset(_KIND, name))
        return SkillImport(meta, package.skipped)
