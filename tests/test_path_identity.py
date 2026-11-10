"""路径的身份与文本读写各只一个入口（#570）：大小写不同的写法是同一个文件（APFS 默认不分大小写），读写边界、资源
校验、edit_file 的合并都按 platform.fs.real 认；文件工具的文本读写保留换行符，不是 UTF-8 的交还模型。"""

import os
from pathlib import Path

import pytest

from muliao.errors import InvalidInput
from muliao.platform.fs import read_text, real, resolve_within
from tests.test_tools import run_tools


def _case_insensitive(directory: Path) -> bool:
    probe = directory / "case-probe"
    probe.write_text("")
    try:
        return (directory / "CASE-PROBE").exists()
    finally:
        probe.unlink()


@pytest.fixture
def folded(tmp_path: Path) -> Path:
    """大小写不敏感的目录（macOS 默认的 APFS）；别的文件系统上这些用例不适用。"""
    if not _case_insensitive(tmp_path):
        pytest.skip("the file system here is case-sensitive")
    return tmp_path


def test_real_names_a_file_the_way_the_disk_does(folded):
    (folded / "Dir").mkdir()
    (folded / "Dir" / "a.txt").write_text("x")
    disk = real(folded / "Dir" / "a.txt")
    assert real(folded / "DIR" / "A.TXT") == disk == real(str(folded).upper() + "/dir/A.txt")
    assert disk.name == "a.txt" and disk.parent.name == "Dir"
    # 不存在的部分照原样接在纠正过的上级后面
    assert real(folded / "dir" / "NEW" / "x") == real(folded / "Dir") / "NEW" / "x"


def test_real_keeps_a_link_itself_apart_from_what_it_points_to(folded):
    (folded / "target").mkdir()
    (folded / "Link").symlink_to(folded / "target")
    assert real(folded / "LINK", follow=False) == real(folded) / "Link"
    assert real(folded / "LINK") == real(folded / "target")


def test_a_link_loop_is_not_an_error_of_its_own(tmp_path):
    (tmp_path / "a").symlink_to(tmp_path / "b")
    (tmp_path / "b").symlink_to(tmp_path / "a")
    real(tmp_path / "a")
    with pytest.raises(InvalidInput):
        resolve_within(tmp_path / "missing-root", "../x")


def test_read_text_keeps_line_endings_and_refuses_non_utf8(tmp_path):
    crlf = tmp_path / "w.txt"
    crlf.write_bytes(b"a\r\nb\r\n")
    assert read_text(crlf) == "a\r\nb\r\n"
    gbk = tmp_path / "g.txt"
    gbk.write_bytes("中文".encode("gbk"))
    with pytest.raises(InvalidInput, match="not UTF-8 text"):
        read_text(gbk)


async def test_edit_file_keeps_crlf_and_refuses_non_utf8_without_failing_the_run(
    container, make_agent, models
):
    me = await make_agent("alice", model="tools")
    ws = container.agents.workspace(me.id)
    (ws / "w.txt").write_bytes(b"one\r\ntwo\r\n")
    (ws / "g.txt").write_bytes("中文\n".encode("gbk"))
    outputs = await run_tools(
        container,
        models,
        me.id,
        [
            ("edit_file", {"path": "w.txt", "old_text": "one\ntwo", "new_text": "1\n2\n3"}),
            ("edit_file", {"path": "g.txt", "old_text": "x", "new_text": "y"}),
            ("read_file", {"path": "w.txt"}),
        ],
    )
    assert (ws / "w.txt").read_bytes() == b"1\r\n2\r\n3\r\n"
    assert outputs[0].startswith("edited ")
    assert "not UTF-8 text" in outputs[1]
    assert outputs[2] == "     1\t1\n     2\t2\n     3\t3"


async def test_one_edit_file_call_sees_two_spellings_of_a_file_as_one(container, make_agent, models):
    me = await make_agent("alice", model="tools")
    ws = container.agents.workspace(me.id)
    if not _case_insensitive(ws):
        pytest.skip("the file system here is case-sensitive")
    (ws / "a.py").write_text("x = 1\ny = 2\n")
    outputs = await run_tools(
        container,
        models,
        me.id,
        [
            (
                "edit_file",
                {
                    "edits": [
                        {"path": "a.py", "old_text": "x = 1", "new_text": "x = 10"},
                        {"path": "A.py", "old_text": "y = 2", "new_text": "y = 20"},
                    ]
                },
            )
        ],
    )
    assert outputs[0].startswith("edited ")
    assert (ws / "a.py").read_text() == "x = 10\ny = 20\n"
    assert os.listdir(ws).count("a.py") == 1 and "A.py" not in os.listdir(ws)


async def test_asset_files_written_under_another_spelling_are_still_vetted(container, make_agent, models):
    me = await make_agent("root", model="tools", roles=("admin",))
    tree = container.settings.assets_dir
    if not _case_insensitive(tree):
        pytest.skip("the file system here is case-sensitive")
    shouted = Path(str(tree).upper()) / "ROOT" / "skills" / "new-one" / "SKILL.md"
    outputs = await run_tools(
        container, models, me.id, [("write_file", {"path": str(shouted), "content": "no frontmatter\n"})]
    )
    assert "frontmatter must contain a string 'description'" in outputs[0]
    assert not (tree / "root" / "skills" / "new-one").exists()
