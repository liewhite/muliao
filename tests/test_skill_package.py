"""标准 .skill 文件的导入导出（agents.skill_package 与 SkillStore.export / import_package）。"""

import asyncio
import io
import stat
import subprocess
import zipfile
from pathlib import Path

import pytest

from muliao.agents import skill_package
from muliao.agents.resources import MAX_EXTERNAL_FILE_BYTES, ResourceRepo, is_attachment
from muliao.agents.skills import SkillStore
from muliao.errors import Conflict, InvalidInput, NotFound, TooLarge

ALPHA, BETA = "a1a1a1a1a1a1", "b2b2b2b2b2b2"
SKILL_MD = "---\nname: digest\ndescription: When digesting\nlicense: MIT\n---\n\nRun `scripts/fetch.py`.\n"


async def any_owner(_owner: str | None) -> None:
    """只测 store 本身，owner 是否可用不在这里查。"""


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path: Path, seed_resources) -> ResourceRepo:
    return ResourceRepo(seed_resources(tmp_path / "resources"), any_owner)


@pytest.fixture
def skills(repo: ResourceRepo) -> SkillStore:
    return SkillStore(repo)


def _zip(entries: dict[str, bytes | str], *, symlinks: tuple[str, ...] = ()) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            info = zipfile.ZipInfo(name)
            if name in symlinks:
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(info, data)
    return buffer.getvalue()


def _members(data: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return {i.filename: archive.read(i) for i in archive.infolist()}


async def test_an_exported_skill_imports_as_the_same_skill_of_another_owner(
    skills: SkillStore, repo: ResourceRepo
):
    """导出的是 SKILL.md 与版本管理里的附件（留在历史外的不在包里）；导入到另一个 owner 一次提交、作者 user，内容一致。"""
    source = repo.owner_dir(ALPHA) / "skills" / "digest"
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text(SKILL_MD)
    (source / "scripts").mkdir()
    (source / "scripts" / "fetch.py").write_text("print('fetch')\n")
    (source / "run.sh").write_text("echo hi\n")
    (source / "run.sh").chmod(0o755)
    (source / ".venv" / "lib").mkdir(parents=True)
    (source / ".venv" / "lib" / "x.py").write_text("x = 1\n")
    (source / "tool").write_bytes(b"\x7fELF")

    package = await skills.export(ALPHA, "digest")

    members = _members(package)
    assert set(members) == {"digest/SKILL.md", "digest/scripts/fetch.py", "digest/run.sh"}
    assert members["digest/SKILL.md"].decode() == SKILL_MD
    # 可执行位写进条目权限（同 package_skill.py）
    with zipfile.ZipFile(io.BytesIO(package)) as archive:
        modes = {i.filename: (i.external_attr >> 16) & 0o777 for i in archive.infolist()}
    assert (modes["digest/run.sh"], modes["digest/SKILL.md"]) == (0o755, 0o644)
    commits = int(_git(repo.root, "rev-list", "--count", "HEAD"))

    result = await skills.import_package(None, package, "user", replace=False)

    assert (result.meta.name, result.meta.description, result.meta.owner) == (
        "digest",
        "When digesting",
        None,
    )
    assert result.skipped == []
    assert int(_git(repo.root, "rev-list", "--count", "HEAD")) == commits + 1
    assert _git(repo.root, "log", "-1", "--format=%an %s").strip() == "user import public skill digest"
    # 额外的 frontmatter 键（license）原样保留
    assert (repo.owner_dir(None) / "skills" / "digest" / "SKILL.md").read_text() == SKILL_MD
    assert _members(await skills.export(None, "digest")) == members
    assert skills.read(None, "digest").meta.description == "When digesting"


async def test_importing_over_an_existing_skill_needs_replace_and_then_swaps_its_versioned_files(
    skills: SkillStore, repo: ResourceRepo
):
    await skills.write(BETA, "digest", "old", "old body", BETA)
    target = repo.owner_dir(BETA) / "skills" / "digest"
    (target / "old.py").write_text("old = 1\n")
    (target / ".venv").mkdir()
    (target / ".venv" / "keep.py").write_text("keep = 1\n")
    await repo.recover()
    package = _zip({"digest/SKILL.md": SKILL_MD, "digest/scripts/fetch.py": "new = 1\n"})

    with pytest.raises(Conflict):
        await skills.import_package(BETA, package, "user", replace=False)
    assert skills.read(BETA, "digest").meta.description == "old"
    commits = int(_git(repo.root, "rev-list", "--count", "HEAD"))

    await skills.import_package(BETA, package, "user", replace=True)

    assert int(_git(repo.root, "rev-list", "--count", "HEAD")) == commits + 1
    tracked = set(_git(repo.root, "ls-files", f"agents/{BETA}/skills/digest").split())
    assert tracked == {
        f"agents/{BETA}/skills/digest/SKILL.md",
        f"agents/{BETA}/skills/digest/scripts/fetch.py",
    }
    assert not (target / "old.py").exists()
    # 留在历史外的东西不动
    assert (target / ".venv" / "keep.py").exists()
    assert skills.read(BETA, "digest").meta.description == "When digesting"


async def test_files_that_would_not_be_versioned_are_skipped_and_listed(
    skills: SkillStore, repo: ResourceRepo
):
    package = _zip(
        {
            "digest/SKILL.md": SKILL_MD,
            "digest/notes.md": "notes\n",
            "digest/tool": b"\x7fELF",
            "digest/latin1.txt": "caf\xe9".encode("latin-1"),
            "digest/big.json": "x" * (MAX_EXTERNAL_FILE_BYTES + 1),
            "digest/.env": "TOKEN=x",
            "digest/node_modules/pkg/index.js": "x",
            "digest/": "",
            "__MACOSX/digest/._SKILL.md": b"\0",
        }
    )

    result = await skills.import_package(ALPHA, package, "user", replace=False)

    assert sorted(s.path for s in result.skipped) == [
        ".env",
        "__MACOSX/digest/._SKILL.md",
        "big.json",
        "latin1.txt",
        "node_modules/pkg/index.js",
        "tool",
    ]
    skill_dir = repo.owner_dir(ALPHA) / "skills" / "digest"
    assert sorted(p.relative_to(skill_dir).as_posix() for p in skill_dir.rglob("*") if p.is_file()) == [
        "SKILL.md",
        "notes.md",
    ]


async def test_leftover_attachments_without_skill_md_count_as_an_existing_skill(
    skills: SkillStore, repo: ResourceRepo
):
    skill_dir = repo.owner_dir(ALPHA) / "skills" / "digest"
    skill_dir.mkdir(parents=True)
    (skill_dir / "old.py").write_text("old = 1\n")
    await repo.recover()
    package = _zip({"digest/SKILL.md": SKILL_MD})

    with pytest.raises(Conflict):
        await skills.import_package(ALPHA, package, "user", replace=False)
    await skills.import_package(ALPHA, package, "user", replace=True)
    assert not (skill_dir / "old.py").exists()


@pytest.mark.parametrize("link_at", ["skill", "subdir"])
@pytest.mark.parametrize("target", ["other owner", "outside"])
async def test_an_import_never_writes_through_a_symbolic_link(
    skills: SkillStore, repo: ResourceRepo, tmp_path: Path, link_at, target
):
    """agent 用 shell 在 skill 目录里建的链接不能把导入的文件引到别的 owner 名下或仓库外。"""
    await skills.write(BETA, "other", "other", "body", BETA)
    destination = (
        repo.owner_dir(BETA) / "skills" / "other" if target == "other owner" else tmp_path / "outside"
    )
    destination.mkdir(exist_ok=True)
    skills_dir = repo.owner_dir(ALPHA) / "skills"
    if link_at == "skill":
        skills_dir.mkdir(parents=True)
        (skills_dir / "digest").symlink_to(destination)
    else:
        (skills_dir / "digest").mkdir(parents=True)
        (skills_dir / "digest" / "scripts").symlink_to(destination)
    before = sorted(p.name for p in destination.iterdir())
    commits = _git(repo.root, "rev-list", "--count", "HEAD")
    package = _zip({"digest/SKILL.md": SKILL_MD, "digest/scripts/evil.py": "x = 1\n"})

    with pytest.raises(InvalidInput, match="symbolic link"):
        await skills.import_package(ALPHA, package, "user", replace=True)

    assert sorted(p.name for p in destination.iterdir()) == before
    assert _git(repo.root, "rev-list", "--count", "HEAD") == commits


async def test_a_planted_temporary_file_link_is_not_written_through(
    skills: SkillStore, repo: ResourceRepo, tmp_path: Path
):
    """写入用的临时文件（<路径>.tmp）如果是 agent 放的符号链接，不跟着它写到别处。"""
    outside = tmp_path / "outside.txt"
    outside.write_text("original")
    skill_dir = repo.owner_dir(ALPHA) / "skills" / "digest"
    skill_dir.mkdir(parents=True)
    (skill_dir / "a.md.tmp").symlink_to(outside)

    await skills.import_package(
        ALPHA, _zip({"digest/SKILL.md": SKILL_MD, "digest/a.md": "new\n"}), "user", replace=False
    )

    assert outside.read_text() == "original"
    assert not (skill_dir / "a.md").is_symlink()
    assert (skill_dir / "a.md").read_text() == "new\n"


async def test_replacing_turns_an_old_directory_into_a_file_of_the_same_name(
    skills: SkillStore, repo: ResourceRepo
):
    skill_dir = repo.owner_dir(ALPHA) / "skills" / "digest"
    await skills.import_package(
        ALPHA, _zip({"digest/SKILL.md": SKILL_MD, "digest/x.md/inner.md": "old\n"}), "user", replace=False
    )

    await skills.import_package(
        ALPHA, _zip({"digest/SKILL.md": SKILL_MD, "digest/x.md": "new\n"}), "user", replace=True
    )

    assert (skill_dir / "x.md").read_text() == "new\n"
    assert set(_git(repo.root, "ls-files", f"agents/{ALPHA}").split()) == {
        f"agents/{ALPHA}/skills/digest/SKILL.md",
        f"agents/{ALPHA}/skills/digest/x.md",
    }


@pytest.mark.parametrize(
    ("old", "new"),
    [({"x.md/inner.md": "old\n"}, {"x.md": "new\n"}), ({"x.md": "old\n"}, {"x.md/inner.md": "new\n"})],
)
async def test_a_failed_replace_that_swaps_a_file_and_a_directory_is_rolled_back(
    skills: SkillStore, repo: ResourceRepo, monkeypatch, old, new
):
    skill_dir = repo.owner_dir(ALPHA) / "skills" / "digest"
    files = lambda: {  # noqa: E731
        p.relative_to(skill_dir).as_posix(): p.read_text() for p in skill_dir.rglob("*") if p.is_file()
    }
    await skills.import_package(
        ALPHA,
        _zip({"digest/SKILL.md": SKILL_MD, **{f"digest/{k}": v for k, v in old.items()}}),
        "user",
        replace=False,
    )
    before = files()
    real = repo._git_on_paths

    async def failing_commit(paths, *args):  # noqa: ANN001, ANN202
        if args[0] == "commit":
            raise RuntimeError("disk full")
        return await real(paths, *args)

    monkeypatch.setattr(repo, "_git_on_paths", failing_commit)
    with pytest.raises(RuntimeError, match="disk full"):
        await skills.import_package(
            ALPHA,
            _zip({"digest/SKILL.md": SKILL_MD, **{f"digest/{k}": v for k, v in new.items()}}),
            "user",
            replace=True,
        )

    assert files() == before


async def test_exporting_a_skill_whose_directory_is_a_link_fails(skills: SkillStore, repo: ResourceRepo):
    await skills.write(BETA, "digest", "other", "body", BETA)
    (repo.owner_dir(ALPHA) / "skills").mkdir(parents=True)
    (repo.owner_dir(ALPHA) / "skills" / "digest").symlink_to(repo.owner_dir(BETA) / "skills" / "digest")
    with pytest.raises(InvalidInput, match="symbolic link"):
        await skills.export(ALPHA, "digest")


async def test_importing_for_an_unavailable_owner_is_refused_before_anything_else(
    skills: SkillStore, repo: ResourceRepo
):
    """同名 skill 已存在时也报不存在（不是冲突）。"""
    await skills.write(ALPHA, "digest", "old", "body", ALPHA)

    async def deleting(owner: str | None) -> None:
        if owner == ALPHA:
            raise NotFound("agent is being deleted")

    store = SkillStore(ResourceRepo(repo.root, deleting))
    with pytest.raises(NotFound):
        await store.import_package(ALPHA, _zip({"digest/SKILL.md": SKILL_MD}), "user", replace=False)


async def test_imported_file_names_are_written_and_committed_literally(
    skills: SkillStore, repo: ResourceRepo
):
    """名字带通配符的文件只提交它自己：不把同目录里留在历史外的文件带进来。"""
    skill_dir = repo.owner_dir(ALPHA) / "skills" / "digest"
    (skill_dir / ".venv").mkdir(parents=True)
    (skill_dir / ".venv" / "x.py").write_text("x = 1\n")

    await skills.import_package(
        ALPHA, _zip({"digest/SKILL.md": SKILL_MD, "digest/*.py": "star = 1\n"}), "user", replace=False
    )

    tracked = _git(repo.root, "ls-files", f"agents/{ALPHA}").splitlines()
    assert sorted(tracked) == [f"agents/{ALPHA}/skills/digest/*.py", f"agents/{ALPHA}/skills/digest/SKILL.md"]


@pytest.mark.parametrize(
    ("entries", "symlinks", "error"),
    [
        ({"digest/SKILL.md": SKILL_MD, "digest/../escape.md": "x"}, (), "'..'"),
        ({"digest/SKILL.md": SKILL_MD, "/etc/digest.md": "x"}, (), "absolute"),
        ({"digest/SKILL.md": SKILL_MD, "digest\\evil.md": "x"}, (), "backslash"),
        (
            {"digest/SKILL.md": SKILL_MD, "digest/link.md": "/etc/passwd"},
            ("digest/link.md",),
            "symbolic link",
        ),
        ({"digest/SKILL.md": SKILL_MD, "other/SKILL.md": SKILL_MD}, (), "exactly one top-level directory"),
        ({"digest/SKILL.md": SKILL_MD, "README.md": "x"}, (), "exactly one top-level directory"),
        (
            {"digest/SKILL.md": SKILL_MD, "digest/a.md": "x", "digest/a.md/b.md": "y"},
            (),
            "file and a directory",
        ),
        ({"digest/SKILL.md": SKILL_MD, "digest/skill.md": "x"}, (), "more than once"),
        (
            {"digest/SKILL.md": SKILL_MD, "digest/caf\u00e9.md": "x", "digest/cafe\u0301.md": "y"},
            (),
            "more than once",
        ),
        (
            {"digest/SKILL.md": SKILL_MD, "digest/A.md": "x", "digest/a.md/b.md": "y"},
            (),
            "file and a directory",
        ),
        ({"digest/notes.md": "x"}, (), "no usable digest/SKILL.md"),
        ({"Digest!/SKILL.md": SKILL_MD}, (), "invalid skill name"),
        ({"other/SKILL.md": SKILL_MD}, (), "does not match directory"),
        ({"digest/SKILL.md": "no frontmatter\n"}, (), "'name' and 'description'"),
        ({"digest/SKILL.md": "---\nname: digest\ndescription: '  '\n---\nx\n"}, (), "must not be empty"),
        ({"digest/SKILL.md": "---\nname: [\n---\n"}, (), "digest/SKILL.md"),
    ],
)
async def test_malformed_packages_are_rejected_without_writing_anything(
    skills: SkillStore, repo: ResourceRepo, entries, symlinks, error
):
    commits = _git(repo.root, "rev-list", "--count", "HEAD")

    with pytest.raises(InvalidInput, match=error):
        await skills.import_package(ALPHA, _zip(entries, symlinks=symlinks), "user", replace=False)

    assert _git(repo.root, "rev-list", "--count", "HEAD") == commits
    assert not (repo.owner_dir(ALPHA) / "skills").exists()


def test_a_file_that_is_not_a_zip_is_rejected():
    with pytest.raises(InvalidInput, match="not a .skill"):
        skill_package.unpack(b"plain text")


def test_duplicate_entries_are_rejected():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("digest/SKILL.md", SKILL_MD)
        with pytest.warns(UserWarning, match="Duplicate name"):
            archive.writestr("digest/SKILL.md", SKILL_MD)
    with pytest.raises(InvalidInput, match="more than once"):
        skill_package.unpack(buffer.getvalue())


def test_encrypted_entries_are_rejected():
    # zipfile 不写加密的条目：把本地头与中央目录里的通用标志位第 0 位（加密）置上
    data = bytearray(_zip({"digest/SKILL.md": SKILL_MD}))
    data[data.index(b"PK\x03\x04") + 6] |= 0x1
    data[data.index(b"PK\x01\x02") + 8] |= 0x1
    with pytest.raises(InvalidInput, match="encrypted"):
        skill_package.unpack(bytes(data))


def test_packages_over_the_file_count_or_unpacked_size_are_rejected_before_reading(monkeypatch):
    monkeypatch.setattr(skill_package, "MAX_PACKAGE_FILES", 2)
    with pytest.raises(TooLarge, match="files"):
        skill_package.unpack(_zip({"digest/SKILL.md": SKILL_MD, "digest/a.md": "a", "digest/b.md": "b"}))
    monkeypatch.setattr(skill_package, "MAX_UNPACKED_BYTES", len(SKILL_MD) + 5)
    with pytest.raises(TooLarge, match="bytes"):
        skill_package.unpack(_zip({"digest/SKILL.md": SKILL_MD, "digest/a.md": "x" * 6}))


async def test_exporting_a_missing_or_corrupt_skill_fails(skills: SkillStore, repo: ResourceRepo):
    with pytest.raises(NotFound):
        await skills.export(ALPHA, "digest")
    skill_dir = repo.owner_dir(ALPHA) / "skills" / "digest"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: other\ndescription: x\n---\n")
    with pytest.raises(InvalidInput, match="does not match directory"):
        await skills.export(ALPHA, "digest")


async def test_is_attachment_agrees_with_git(repo: ResourceRepo):
    """is_attachment 与交给 git 的 pathspec 是同一条规则的两种写法：对同一批路径判断一致。"""
    skill_dir = repo.owner_dir(ALPHA) / "skills" / "digest"
    paths = [
        "SKILL.md",
        "a.py",
        "sub/deep/b.json",
        "c.PY",
        "d.tmp",
        "noext",
        ".env",
        ".hidden.md",
        ".cache/e.md",
        "sub/.git-like/f.py",
        "node_modules/g.js",
        "sub/node_modules/h.js",
        "venv/i.py",
        "x/venv/j.py",
        "lib/site-packages/k.py",
        "__pycache__/l.py",
        "venvx/m.py",
        "my_node_modules/n.js",
        "o.yml",
        "p.jq",
        "q.md.bak",
    ]
    for rel in paths:
        target = skill_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x\n")

    await repo.recover()

    prefix = f"agents/{ALPHA}/skills/digest/"
    tracked = {
        p.removeprefix(prefix)
        for p in (await asyncio.to_thread(_git, repo.root, "ls-files", prefix)).splitlines()
    }
    assert {rel for rel in paths if is_attachment(rel)} == tracked
    assert {"a.py", "venvx/m.py", "my_node_modules/n.js"} <= tracked
