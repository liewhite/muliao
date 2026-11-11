"""skill 的 SKILL.md 与目录：frontmatter 的规则（description、name、approval），以及读树与写入口按它读、校验一个
skill 目录（agents.kinds 的 KindSpec 登记）。只读与校验，不管写入。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import frontmatter
import yaml

from muliao.agents.formats import Draft, Loaded, modified
from muliao.errors import InvalidInput
from muliao.platform.fs import resolve_within

SKILL_FILE = "SKILL.md"


# ---- SKILL.md 的 frontmatter（资源目录里的 skill 与界面的编辑都按它） ----


@dataclass(frozen=True)
class Frontmatter:
    description: str
    # 执行前要用户批准的脚本（相对 skill 目录的路径，skill_run 执行前挂起等批准，#530）
    approval: tuple[str, ...]
    post: frontmatter.Post


def parse_frontmatter(text: str, name: str) -> Frontmatter:
    """SKILL.md 的 frontmatter 规则：description 是非空字符串；有 name 时必须与 skill（目录名）同名；有 approval 时是
    相对 skill 目录的脚本路径列表。不合规则时 InvalidInput。"""
    where = f"{name}/{SKILL_FILE}"
    post = loads_skill_md(text, name, where)
    description = post.get("description")
    if not isinstance(description, str):
        raise InvalidInput(f"{where}: frontmatter must contain a string 'description'")
    return Frontmatter(valid_description(description), _approval(post.get("approval", []), where), post)


def _approval(value: object, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(p, str) and p.strip() for p in value):
        raise InvalidInput(
            f"{where}: 'approval' must be a list of script paths relative to the skill directory, "
            f"e.g. [scripts/deploy.sh], got {value!r}"
        )
    for p in value:
        if p.startswith("/"):
            raise InvalidInput(f"{where}: approval path {p!r} must be relative to the skill directory")
    return tuple(p.strip() for p in value)


def approval_scripts(directory: Path, approval: tuple[str, ...]) -> tuple[Path, ...]:
    """frontmatter 的 approval 换成目录里的脚本文件（身份，platform.fs.real，skill_run 按它认要批准的脚本）。哪一项
    不是目录里存在的文件就 InvalidInput：写错的一项若照样加载，那个脚本就静默地不用批准。"""
    where = f"{directory.name}/{SKILL_FILE}"
    found = []
    for p in approval:
        try:
            script = resolve_within(directory, p)
        except InvalidInput as e:
            raise InvalidInput(f"{where}: approval: {e}") from e
        if not script.is_file():
            raise InvalidInput(
                f"{where}: approval path {p!r} is not a file in the skill directory (looked for {script}); "
                "create the script first, or fix the path"
            )
        found.append(script)
    return tuple(found)


def loads_skill_md(text: str, name: str, where: str) -> frontmatter.Post:
    """解析 SKILL.md；frontmatter 里有 name 时必须与 skill 同名。where 是报错里对这份文件的称呼。"""
    try:
        post = frontmatter.loads(text)
    except (yaml.YAMLError, ValueError) as e:
        raise InvalidInput(f"{where}: cannot parse frontmatter: {e}") from e
    fm_name = post.get("name")
    if fm_name is not None and fm_name != name:
        raise InvalidInput(f"{where}: frontmatter name {fm_name!r} does not match {name!r}")
    return post


def valid_description(description: str) -> str:
    description = description.strip()
    if not description:
        raise InvalidInput("skill description must not be empty")
    return description


# ---- 资源目录里的 skill（读树与写入口，agents.kinds 的 KindSpec 登记） ----


@dataclass(frozen=True)
class SkillDir:
    """skill 的内容：描述与执行前要批准的脚本（frontmatter 里列的，读树时已换成目录里存在的文件的身份，
    approval_scripts）。"""

    description: str
    approval: tuple[Path, ...]


def load(path: Path, name: str, _memory_max_chars: int) -> Loaded[SkillDir]:
    """读树：path 是 skill 目录，修改时间取 SKILL.md 的。"""
    md = path / SKILL_FILE
    if not md.is_file():
        raise InvalidInput(f"skill directory has no {SKILL_FILE}")
    fm = parse_frontmatter(md.read_text(encoding="utf-8"), name)
    return Loaded(SkillDir(fm.description, approval_scripts(path, fm.approval)), modified(md))


def vet(draft: Draft) -> str:
    """写入口：SKILL.md 按读树的规则校验；附件要 skill 已有 SKILL.md，否则写出来的是一个读树时加载不了的目录。"""
    if draft.inner == (SKILL_FILE,):
        approval_scripts(draft.item, parse_frontmatter(draft.text, draft.name).approval)
        return draft.text
    if not (draft.item / SKILL_FILE).is_file():
        raise InvalidInput(
            f"{draft.name}/ has no {SKILL_FILE}; write {draft.name}/{SKILL_FILE} first, then its other files"
        )
    return draft.text
