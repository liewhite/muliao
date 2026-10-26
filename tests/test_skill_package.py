"""标准 .skill 文件的导入导出（agents.skill_package 与 SkillStore.export / import_package）。"""

import io
import stat
import zipfile

import pytest

from muliao.agents import skill_package
from muliao.agents.api import SkillMeta, Subject
from muliao.agents.skill_package import MAX_FILE_BYTES, SkillFile, is_attachment
from muliao.errors import Conflict, Forbidden, InvalidInput, NotFound, TooLarge
from tests.conftest import owned

SKILL_MD = "---\nname: digest\ndescription: When digesting\nlicense: MIT\n---\n\nRun `scripts/fetch.py`.\n"


def _zip(
    entries: dict[str, bytes | str], *, symlinks: tuple[str, ...] = (), executable: tuple[str, ...] = ()
) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            info = zipfile.ZipInfo(name)
            if name in symlinks:
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
            elif name in executable:
                info.external_attr = (stat.S_IFREG | 0o755) << 16
            archive.writestr(info, data)
    return buffer.getvalue()


def _members(data: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return {i.filename: archive.read(i) for i in archive.infolist()}


async def test_an_imported_skill_exports_the_same_files_and_keeps_extra_frontmatter(container, make_agent):
    skills = container.skills
    alpha = (await make_agent("alpha")).id
    package = _zip(
        {
            "digest/SKILL.md": SKILL_MD,
            "digest/scripts/fetch.py": "print('fetch')\n",
            "digest/run.sh": "echo hi\n",
        },
        executable=("digest/run.sh",),
    )

    result = await skills.import_package(package, actor="user", owner=alpha, replace=False)

    assert (result.meta.name, result.meta.description, result.meta.owner, result.skipped) == (
        "digest",
        "When digesting",
        alpha,
        [],
    )
    assert (await skills.files("digest"))["SKILL.md"].content == SKILL_MD
    exported = await skills.export("digest")
    assert _members(exported) == _members(package)
    with zipfile.ZipFile(io.BytesIO(exported)) as archive:
        modes = {i.filename: (i.external_attr >> 16) & 0o777 for i in archive.infolist()}
    assert (modes["digest/run.sh"], modes["digest/SKILL.md"]) == (0o755, 0o644)
    [revision] = await container.revisions.history(Subject.SKILL, "digest", 10)
    assert (revision.author, revision.message) == ("user", "import skill digest")


async def test_importing_over_an_existing_skill_needs_replace_and_then_swaps_its_files(container, make_agent):
    skills = container.skills
    beta = (await make_agent("beta")).id
    await skills.write(
        "digest", "old", "old body", actor=beta, owner=beta, attachments={"old.py": SkillFile("old = 1\n")}
    )
    package = _zip({"digest/SKILL.md": SKILL_MD, "digest/scripts/fetch.py": "new = 1\n"})

    with pytest.raises(Conflict):
        await skills.import_package(package, actor="user", owner=beta, replace=False)
    assert (await skills.read("digest")).meta.description == "old"

    await skills.import_package(package, actor="user", owner=beta, replace=True)

    assert set(await skills.files("digest")) == {"SKILL.md", "scripts/fetch.py"}
    assert not (skills.skills_dir / "digest" / "old.py").exists()
    # 用户覆盖时 owner 不变
    assert (await skills.read("digest")).meta == SkillMeta(
        "digest", "When digesting", beta, skills.skills_dir / "digest" / "SKILL.md"
    )
    # 不是 owner 也不是 admin 的 agent 覆盖不了
    alpha = (await make_agent("alpha")).id
    with pytest.raises(Forbidden):
        await skills.import_package(package, actor=alpha, owner=alpha, replace=True)


async def test_files_that_would_not_be_kept_are_skipped_and_listed(container, make_agent):
    alpha = (await make_agent("alpha")).id
    package = _zip(
        {
            "digest/SKILL.md": SKILL_MD,
            "digest/notes.md": "notes\n",
            "digest/tool": b"\x7fELF",
            "digest/latin1.txt": "caf\xe9".encode("latin-1"),
            "digest/big.json": "x" * (MAX_FILE_BYTES + 1),
            "digest/.env": "TOKEN=x",
            "digest/node_modules/pkg/index.js": "x",
            "digest/": "",
            "__MACOSX/digest/._SKILL.md": b"\0",
        }
    )

    result = await container.skills.import_package(package, actor="user", owner=alpha, replace=False)

    assert sorted(s.path for s in result.skipped) == [
        ".env",
        "__MACOSX/digest/._SKILL.md",
        "big.json",
        "latin1.txt",
        "node_modules/pkg/index.js",
        "tool",
    ]
    assert set(await container.skills.files("digest")) == {"SKILL.md", "notes.md"}


async def test_importing_for_an_unavailable_owner_is_refused(container, make_agent):
    alpha = (await make_agent("alpha")).id
    await container.agent_lifecycle.delete(alpha, author="user")
    with pytest.raises(NotFound):
        await container.skills.import_package(
            _zip({"digest/SKILL.md": SKILL_MD}), actor="user", owner=alpha, replace=False
        )


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
        ({"other/SKILL.md": SKILL_MD}, (), "does not match"),
        ({"digest/SKILL.md": "no frontmatter\n"}, (), "'name' and 'description'"),
        ({"digest/SKILL.md": "---\nname: digest\ndescription: '  '\n---\nx\n"}, (), "must not be empty"),
        ({"digest/SKILL.md": "---\nname: [\n---\n"}, (), "digest/SKILL.md"),
    ],
)
async def test_malformed_packages_are_rejected_without_writing_anything(
    container, make_agent, entries, symlinks, error
):
    alpha = (await make_agent("alpha")).id

    with pytest.raises(InvalidInput, match=error):
        await container.skills.import_package(
            _zip(entries, symlinks=symlinks), actor="user", owner=alpha, replace=False
        )

    assert await owned(container, "skill", alpha) == []


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


async def test_exporting_a_missing_skill_fails(container):
    with pytest.raises(NotFound):
        await container.skills.export("digest")


def test_attachment_rules():
    kept = {"a.py", "sub/deep/b.json", "o.yml", "p.jq", "venvx/m.py", "my_node_modules/n.js", "SKILL.md"}
    dropped = {
        "c.PY", "d.tmp", "noext", ".env", ".hidden.md", ".cache/e.md", "sub/.git-like/f.py", "node_modules/g.js",
        "sub/node_modules/h.js", "venv/i.py", "x/venv/j.py", "lib/site-packages/k.py", "__pycache__/l.py", "q.md.bak",
    }  # fmt: skip
    assert {p for p in kept | dropped if is_attachment(p)} == kept
