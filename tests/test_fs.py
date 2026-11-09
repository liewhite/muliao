"""platform.fs.write_atomic：整份替换、不留临时文件、保留可执行位、写进符号链接指向的文件（链接保留）。"""

import os

from muliao.platform.fs import write_atomic


def test_write_atomic_replaces_the_file_whole(tmp_path):
    f = tmp_path / "a" / "x.sh"
    write_atomic(f, "one")
    os.chmod(f, 0o755)
    write_atomic(f, "two")
    assert f.read_text() == "two" and os.access(f, os.X_OK)
    assert [p.name for p in f.parent.iterdir()] == ["x.sh"]


def test_write_atomic_writes_through_a_symlink(tmp_path):
    # 与原地写（Path.write_text）一样改链接指向的文件；rename 到链接本身会把链接换成普通文件，另一处看不到改动
    target = tmp_path / "real.md"
    target.write_text("old")
    link = tmp_path / "link.md"
    link.symlink_to(target)
    write_atomic(link, "new")
    assert link.is_symlink() and target.read_text() == "new"
