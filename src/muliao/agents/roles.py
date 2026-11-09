"""写入分发规则（角色、agent 的 roles / allow / deny）之前的校验，按一份资源快照（agents.assets.Snapshot）查：形式合法、
引用的角色存在、新加的规则至少匹配一项现有资源。读树时对已有内容用同样的形式规则（坏的跳过并报出）。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from fnmatch import fnmatchcase

from muliao.agents.kinds import Kind, StoredKind
from muliao.agents.permissions import Action, Role, Rules, check_patterns, kind_of, pattern_of
from muliao.errors import InvalidInput


def check_assignable(roles: Mapping[str, Role], names: Sequence[str]) -> None:
    """agent 可以选的角色：存在、不重复，不含 base（每个 agent 隐含）。"""
    implicit = [n for n in names if n in roles and not roles[n].assignable]
    if implicit:
        raise InvalidInput(f"roles {implicit} are implicit for every agent; do not list them in roles")
    unknown = [n for n in names if n not in roles]
    if unknown or len(set(names)) != len(names):
        assignable = [r.name for r in roles.values() if r.assignable]
        raise InvalidInput(f"roles must be distinct names from {assignable}, got {list(names)}")


def check_new_items(items: Sequence[str], patterns: Sequence[str], before: Sequence[str]) -> None:
    """新加的针对某种资源的规则（不在 before 里的）至少匹配 items（全部资源的 `kind:name`）里的一项：拼错的在写入时报出。
    已有的规则不查——资源删掉再建时分发不丢，也不让改别的字段的写入失败。不限种类的（`write:*`）与路径规则（目录可以
    还没建）不查。"""
    new = [p for p in patterns if p not in before and kind_of(p) not in (Kind.TOOL, Kind.PATH, None)]
    if not new:
        return
    candidates = [f"{a.value}:{i}" for i in items for a in Action]
    bad = [p for p in new if not any(fnmatchcase(i, p) for i in candidates)]
    if bad:
        raise InvalidInput(
            f"rules {bad} match no existing skill, MCP server, secret or memory; check the names"
        )


def check_rules(items: Sequence[str], rules: Rules, before: Rules = Rules()) -> None:
    """写入一组规则之前的全部校验（agent 自己的 allow / deny 与角色的共用）。"""
    check_patterns(rules.allow)
    check_patterns(rules.deny)
    check_new_items(items, rules.allow, before.allow)
    check_new_items(items, rules.deny, before.deny)


def item_patterns(assets: Mapping[StoredKind, Mapping[str, object]]) -> list[str]:
    """快照里全部资源的 `kind:name`。"""
    return [pattern_of(Kind(k.value), n) for k, by_name in assets.items() for n in by_name]
