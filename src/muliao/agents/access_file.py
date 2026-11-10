"""`platform/access.toml`（角色定义、各 agent 的角色与 allow / deny）的格式：读（read / parse）与平台自己写（edit 加各项
改动）都在这一处。

读：坏掉的单条规则、不认识的角色各自跳过并记一条问题，其余照常（不让一个笔误拿掉一个 agent 的全部角色）；整个文件读不
出来时 readable 为 False（只剩内置角色的固定部分，没有任何 agent 的规则），由读树的一方决定沿用什么（agents.assets）。
纯函数，问题交给调用方（读树的 agents.assets、写前校验的 agents.asset_files）。
写：界面上的角色与 agent 规则、建 / 改名 / 删 agent，用 tomlkit 读改写，人手写的注释与排版保留；调用方在
AssetSource.writing() 里串行调用。"""

from __future__ import annotations

import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

import tomlkit
from pydantic import BaseModel, ConfigDict, ValidationError
from tomlkit.items import Table
from tomlkit.toml_document import TOMLDocument

from muliao.agents.naming import nfc, validate_slug
from muliao.agents.permissions import BASE, FIXED_RULES, AgentRules, Role, Rules, check_patterns
from muliao.errors import InvalidInput
from muliao.platform.fs import write_atomic
from muliao.platform.values import validation_reason

# 内置角色没写描述时的描述
BUILTIN_DESCRIPTIONS: dict[str, str] = {
    BASE: "每个 agent 都有：全部非管理工具与全局记忆，加上这里收录的 skill、MCP server、secret、记忆",
    "admin": "全部工具（含管理 agent 与角色），并且能改任何 AI 资源",
}


# ---- 读 ----


class _RoleEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    description: str = ""
    allow: list[str] = []
    deny: list[str] = []


class _AgentEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    roles: list[str] = []
    allow: list[str] = []
    deny: list[str] = []


@dataclass(frozen=True)
class AccessProblem:
    """读出的一处问题。agent_id：与它相关的 agent（报给它），平台的为 None。"""

    reason: str
    agent_id: str | None


@dataclass(frozen=True)
class Access:
    """读出的内容：全部角色（内置在前，按定义顺序；自建的按名称）、各 agent 的规则（按 id，没写的 agent 只有 base）。
    readable：整个文件读出来了；False 时 roles 只有内置角色的固定部分、rules 为空，problems 里是读不出的原因。"""

    roles: dict[str, Role]
    rules: dict[str, AgentRules]
    problems: tuple[AccessProblem, ...]
    readable: bool


def read(path: Path, agents: Mapping[str, str]) -> Access:
    """读 path（没有这个文件就是空的）。agents：加载成功的 agent，名字 → id。同步 IO。"""
    if not path.is_file():
        return parse(None, agents)
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as e:
        return _Parser(agents).unreadable(str(e))
    return parse(text, agents)


def parse(text: str | None, agents: Mapping[str, str]) -> Access:
    """文件内容 text（没有这个文件为 None）读成 Access。"""
    return _Parser(agents).parse(text)


class _Parser:
    def __init__(self, agents: Mapping[str, str]):
        self.agents = agents
        self.problems: list[AccessProblem] = []
        self.roles: dict[str, Role] = {
            n: Role(n, BUILTIN_DESCRIPTIONS[n], Rules(), fixed) for n, fixed in FIXED_RULES.items()
        }
        self.rules: dict[str, AgentRules] = {}

    def problem(self, reason: str, agent_id: str | None = None) -> None:
        self.problems.append(AccessProblem(reason, agent_id))

    def done(self) -> Access:
        return Access(self.roles, self.rules, tuple(self.problems), readable=True)

    def unreadable(self, error: str) -> Access:
        self.problem(f"cannot parse: {error}")
        return Access(self.roles, self.rules, tuple(self.problems), readable=False)

    def parse(self, text: str | None) -> Access:
        if text is None:
            return self.done()
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError as e:
            return self.unreadable(str(e))
        extra = sorted(set(data) - {"roles", "agents"})
        if extra:
            self.problem(f"unknown top-level tables {extra} (expected roles, agents)")
        for name, raw in sorted(_table(data.get("roles"))):
            self.role(name, raw)
        for name, raw in sorted(_table(data.get("agents"))):
            self.agent(name, raw)
        return self.done()

    def role(self, name: str, raw: object) -> None:
        entry = self.entry(_RoleEntry, raw, f"role {name!r}")
        if entry is None:
            return
        try:
            validate_slug("role", name)
        except InvalidInput as e:
            self.problem(str(e))
            return
        description = entry.description.strip() or BUILTIN_DESCRIPTIONS.get(name, "")
        if not description:
            self.problem(f"role {name!r}: description must not be empty")
            return
        what = f"role {name!r}"
        rules = Rules(self.patterns(entry.allow, what), self.patterns(entry.deny, what))
        self.roles[name] = Role(name, description, rules, FIXED_RULES.get(name, Rules()))

    def agent(self, name: str, raw: object) -> None:
        what = f"agent {name!r}"
        entry = self.entry(_AgentEntry, raw, what)
        if entry is None:
            return
        agent_id = self.agents.get(nfc(name))
        if agent_id is None:
            self.problem(f"[agents.{name}]: no loaded agent directory has this name")
            return
        kept = []
        for r in dict.fromkeys(entry.roles):
            if r not in self.roles or not self.roles[r].assignable:
                self.problem(f"{what}: role {r!r} is unknown or implicit; skipped", agent_id)
            else:
                kept.append(r)
        self.rules[agent_id] = AgentRules.of(
            kept, self.patterns(entry.allow, what, agent_id), self.patterns(entry.deny, what, agent_id)
        )

    def entry[T: BaseModel](self, model: type[T], raw: object, what: str) -> T | None:
        try:
            return model.model_validate(raw)
        except ValidationError as e:
            self.problem(f"{what}: {validation_reason(e)}")
            return None

    def patterns(self, patterns: list[str], what: str, agent_id: str | None = None) -> tuple[str, ...]:
        kept = []
        for p in dict.fromkeys(patterns):
            try:
                check_patterns([p])
            except InvalidInput as e:
                self.problem(f"{what}: {e}; skipped", agent_id)
                continue
            kept.append(p)
        return tuple(kept)


def _table(value: object) -> list[tuple[str, object]]:
    return list(value.items()) if isinstance(value, dict) else []


# ---- 写 ----


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
