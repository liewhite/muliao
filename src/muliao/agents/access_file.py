"""平台自己改 access.toml（界面上的角色与 agent 规则、建 / 改名 / 删 agent）：用 tomlkit 读改写，人手写的注释与排版保留。
读与校验在 agents.assets（读树）；调用方在 AssetSource.writing() 里串行调用。"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import tomlkit
from tomlkit.items import Table
from tomlkit.toml_document import TOMLDocument

from muliao.agents.agent_rules import AgentRules
from muliao.platform.fs import write_atomic


def edit(path: Path, change: Callable[[TOMLDocument], None]) -> None:
    """读出 path（不存在就是空文档）、交给 change 改、原子写回。同步 IO。"""
    doc = tomlkit.parse(path.read_text(encoding="utf-8")) if path.is_file() else tomlkit.document()
    change(doc)
    write_atomic(path, tomlkit.dumps(doc))


def _section(doc: TOMLDocument, name: str) -> Table:
    if name not in doc:
        doc[name] = tomlkit.table(is_super_table=True)
    table = doc[name]
    assert isinstance(table, Table)
    return table


def _rules_table(fields: dict[str, object]) -> Table:
    table = tomlkit.table()
    for key, value in fields.items():
        if isinstance(value, list):
            array = tomlkit.array()
            array.extend(value)
            if len(value) > 2:
                array.multiline(True)
            table[key] = array
        else:
            table[key] = value
    return table


def set_role(doc: TOMLDocument, name: str, description: str, allow: list[str], deny: list[str]) -> None:
    fields: dict[str, object] = {"description": description, "allow": allow}
    if deny:
        fields["deny"] = deny
    _section(doc, "roles")[name] = _rules_table(fields)


def delete_role(doc: TOMLDocument, name: str) -> None:
    roles = _section(doc, "roles")
    if name in roles:
        del roles[name]


def set_agent_rules(doc: TOMLDocument, agent: str, rules: AgentRules) -> None:
    """agent 的规则；全空时去掉这一节（只有 base）。"""
    agents = _section(doc, "agents")
    fields = {
        k: list(v) for k, v in (("roles", rules.roles), ("allow", rules.allow), ("deny", rules.deny)) if v
    }
    if not fields:
        if agent in agents:
            del agents[agent]
        return
    agents[agent] = _rules_table(fields)


def rename_agent(doc: TOMLDocument, old: str, new: str) -> None:
    agents = _section(doc, "agents")
    if old in agents:
        agents[new] = agents[old]
        del agents[old]


def remove_agent(doc: TOMLDocument, agent: str) -> None:
    agents = _section(doc, "agents")
    if agent in agents:
        del agents[agent]
