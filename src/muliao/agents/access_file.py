"""`platform/access.toml`（角色定义、各 agent 的角色与 allow / deny）的格式：读（read / parse）与平台自己写（plan 加各项
改动）都在这一处。

读：语法、字段、规则格式任何一处不合法，整份都不合法（readable 为 False，只剩内置角色的固定部分，没有任何 agent 的
规则），由读树的一方决定沿用什么（agents.assets，#550）——跳过写坏的一条（尤其 deny 或带 deny 的角色）等于放宽权限
（#573）。只引用了不存在的东西（没加载的 agent 目录、未知角色）只收窄：跳过那个引用、记一条问题，其余照常。
纯函数，问题交给调用方（读树的 agents.assets）。
写之前的校验只有 vet 一个：改完的文件合法，并且新加的规则至少匹配一项现有的工具或资源；文件工具改它（agents.asset_files）与
平台自己改它（plan）都调它。
写：界面上的角色与 agent 规则、建 / 改名 / 删 agent，都经 plan：现在的文件不合法时 Conflict（先修好它），改完过不了 vet 时
InvalidInput，算出新内容、什么都不写；AccessEdit.apply 才写入（期间被别处改过则 Conflict），后面的步骤失败时 revert 改回。用
tomlkit 读改写，人手写的注释与排版保留；调用方在 AssetSource.writing() 里串行调用。"""

from __future__ import annotations

import tomllib
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from fnmatch import fnmatchcase
from pathlib import Path
from typing import TYPE_CHECKING

import tomlkit
from pydantic import BaseModel, ConfigDict, ValidationError
from tomlkit.items import Table
from tomlkit.toml_document import TOMLDocument

from muliao.agents.kinds import Kind
from muliao.agents.naming import nfc, validate_slug
from muliao.agents.permissions import (
    BASE,
    Action,
    AgentRules,
    Role,
    Rules,
    ToolTable,
    fixed_rules,
    kind_of,
    matches_no_tool,
)
from muliao.errors import Conflict, InvalidInput
from muliao.platform.fs import write_atomic
from muliao.platform.values import validation_reason

if TYPE_CHECKING:
    from muliao.agents.assets import Snapshot

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
    """读出的一处问题。agent_id：与它相关的 agent（报给它），平台的为 None。invalid：形式不合法，整份因它作废。"""

    reason: str
    agent_id: str | None
    invalid: bool = False


@dataclass(frozen=True)
class Access:
    """读出的内容：全部角色（内置在前，按定义顺序；自建的按名称）、各 agent 的规则（按 id，没写的 agent 只有 base）。
    readable：整份合法；False 时 roles 只有内置角色的固定部分、rules 为空，problems 只有不合法的那几处。"""

    roles: dict[str, Role]
    rules: dict[str, AgentRules]
    problems: tuple[AccessProblem, ...]
    readable: bool


def read(path: Path, agents: Mapping[str, str], tools: ToolTable) -> Access:
    """读 path（没有这个文件就是空的）。agents：加载成功的 agent，名字 → id；tools：内置工具表（内置角色的固定规则、规则
    引用的工具）。同步 IO。"""
    if not path.is_file():
        return parse(None, agents, tools)
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as e:
        return _Parser(agents, tools).unreadable(str(e))
    return parse(text, agents, tools)


def parse(text: str | None, agents: Mapping[str, str], tools: ToolTable) -> Access:
    """文件内容 text（没有这个文件为 None）读成 Access。"""
    return _Parser(agents, tools).parse(text)


class _Parser:
    def __init__(self, agents: Mapping[str, str], tools: ToolTable):
        self.agents = agents
        self.tools = tools
        self.fixed = fixed_rules(tools)
        self.problems: list[AccessProblem] = []
        self.roles = self.builtin_roles()
        self.rules: dict[str, AgentRules] = {}

    def builtin_roles(self) -> dict[str, Role]:
        return {n: Role(n, BUILTIN_DESCRIPTIONS[n], Rules(), fixed) for n, fixed in self.fixed.items()}

    def problem(self, reason: str, agent_id: str | None = None) -> None:
        self.problems.append(AccessProblem(reason, agent_id))

    def invalid(self, reason: str) -> None:
        """形式不合法的一处：整份作废（报给平台，即 admin）。"""
        self.problems.append(AccessProblem(reason, None, invalid=True))

    def unreadable(self, error: str) -> Access:
        self.invalid(f"cannot parse: {error}")
        return self.done()

    def done(self) -> Access:
        invalid = [p for p in self.problems if p.invalid]
        if invalid:
            return Access(self.builtin_roles(), {}, tuple(invalid), readable=False)
        return Access(self.roles, self.rules, tuple(self.problems), readable=True)

    def parse(self, text: str | None) -> Access:
        if text is None:
            return self.done()
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError as e:
            return self.unreadable(str(e))
        extra = sorted(set(data) - {"roles", "agents"})
        if extra:
            self.invalid(f"unknown top-level tables {extra} (expected roles, agents)")
        for name, raw in sorted(self.table(data, "roles")):
            self.role(name, raw)
        for name, raw in sorted(self.table(data, "agents")):
            self.agent(name, raw)
        return self.done()

    def table(self, data: dict[str, object], key: str) -> list[tuple[str, object]]:
        value = data.get(key, {})
        if not isinstance(value, dict):
            self.invalid(f"{key} must be a table ([{key}.<name>] sections)")
            return []
        return list(value.items())

    def role(self, name: str, raw: object) -> None:
        what = f"role {name!r}"
        entry = self.entry(_RoleEntry, raw, what)
        try:
            validate_slug("role", name)
        except InvalidInput as e:
            self.invalid(str(e))
        if entry is None:
            return
        description = entry.description.strip() or BUILTIN_DESCRIPTIONS.get(name, "")
        if not description:
            self.invalid(f"{what}: description must not be empty")
        rules = Rules(self.patterns(entry.allow, what), self.patterns(entry.deny, what))
        self.roles[name] = Role(name, description, rules, self.fixed.get(name, Rules()))

    def agent(self, name: str, raw: object) -> None:
        what = f"agent {name!r}"
        entry = self.entry(_AgentEntry, raw, what)
        if entry is None:
            return
        agent_id = self.agents.get(nfc(name))
        allow, deny = self.patterns(entry.allow, what, agent_id), self.patterns(entry.deny, what, agent_id)
        # 角色定义与对它的引用在同一个文件里：引用了没定义的角色是文件自相矛盾（拼错、手删了定义），跳过它会丢掉
        # 那个角色的 deny，所以整份作废；写了隐含的 base 只是多余
        unknown = [r for r in entry.roles if r not in self.roles]
        if unknown:
            self.invalid(f"{what}: roles {unknown} are not defined in [roles]")
        if agent_id is None:
            # 那个 agent 本来就跑不了（目录没加载），这一节暂时不起作用
            self.problem(f"[agents.{name}]: no loaded agent directory has this name")
            return
        implicit = [r for r in entry.roles if r in self.roles and not self.roles[r].assignable]
        if implicit:
            self.problem(f"{what}: roles {implicit} are implicit for every agent; skipped", agent_id)
        kept = [r for r in dict.fromkeys(entry.roles) if r in self.roles and self.roles[r].assignable]
        self.rules[agent_id] = AgentRules.of(kept, allow, deny)

    def entry[T: BaseModel](self, model: type[T], raw: object, what: str) -> T | None:
        try:
            return model.model_validate(raw)
        except ValidationError as e:
            self.invalid(f"{what}: {validation_reason(e)}")
            return None

    def patterns(self, patterns: list[str], what: str, agent_id: str | None = None) -> tuple[str, ...]:
        """形式不对的整份作废；一个工具都匹配不上的只是引用了不存在的工具（留着也什么都不匹配），记一条问题；重复的
        去重。"""
        unique = tuple(dict.fromkeys(patterns))
        for p in unique:
            try:
                kind_of(p)
            except InvalidInput as e:
                self.invalid(f"{what}: {e}")
            else:
                if matches_no_tool(p, self.tools):
                    self.problem(
                        f"{what}: rule {p!r} matches no tool; tools are listed by get_agent, e.g. 'use:tool:issue_*'",
                        agent_id,
                    )
        return unique


# ---- 写 ----


@dataclass(frozen=True)
class AccessEdit:
    """对 access.toml 的一次改动：已校验、还没写（plan）。before 是改之前的内容（没有文件为 None）。"""

    path: Path
    before: str | None
    after: str

    def apply(self) -> None:
        """原子写入。文件已不是 plan 时读到的内容（期间被 shell 等改过）时 Conflict，不盖掉那次改动。同步 IO。"""
        current = self.path.read_text(encoding="utf-8") if self.path.is_file() else None
        if current != self.before:
            raise Conflict(f"{self.path} was changed meanwhile; retry")
        write_atomic(self.path, self.after)

    def revert(self) -> None:
        """把 apply 写下的改回 before（调用方后面的步骤失败时用，仍在同一个 writing() 里）。文件不是 after（没 apply 过、已
        改回、apply 时发现被别处改过）时不动它。同步 IO。"""
        current = self.path.read_text(encoding="utf-8") if self.path.is_file() else None
        if current != self.after:
            return
        if self.before is None:
            self.path.unlink(missing_ok=True)
        else:
            write_atomic(self.path, self.before)


def vet(before: str | None, after: str, tools: ToolTable, addressable: Sequence[str]) -> None:
    """access.toml 从 before（没有文件为 None）改成 after 之前的唯一校验，不过就 InvalidInput 说明每一处：
    - after 合法（与读树同一套规则：语法、字段、规则的形式、引用的角色都有定义、角色有描述……）；
    - 新加的规则（after 里有、before 整个文件里都没有的）至少匹配 addressable（Snapshot.addressable：注册的内置工具与按名字
      认的资源的 `kind:名`）里的一项，拼错的在写入时报出。已有的不查：资源删掉再建时分发不丢，没配置 [web] 时已有的
      `use:tool:web_*` 也不挡别的改动；比整个文件而不按节，agent 改名时它那一节换了名字也不算新加。不限种类的（`write:*`）
      与路径规则（目录可以还没建）不查。
    before 读不出（语法坏了）时它的规则按没有算：修好它时每条规则都要匹配得上。tools 是读法要的内置工具表。"""
    access = parse(after, {}, tools)
    if not access.readable:
        raise InvalidInput("; ".join(p.reason for p in access.problems))
    old = set(_rules_in(before))
    new = [p for p in dict.fromkeys(_rules_in(after)) if p not in old and kind_of(p) not in (Kind.PATH, None)]
    candidates = [f"{a.value}:{i}" for i in addressable for a in Action]
    bad = [p for p in new if not any(fnmatchcase(i, p) for i in candidates)]
    if bad:
        raise InvalidInput(
            f"rules {bad} match no existing tool, skill, MCP server, secret or shared memory; check the names "
            "(tools are listed by get_agent, e.g. 'use:tool:issue_*'; private memories belong to their agent and "
            "are not named in rules)"
        )


def _rules_in(text: str | None) -> Iterable[str]:
    """文件里写着的每一条规则（[roles.*] 与 [agents.*] 的 allow、deny 里的字符串），不管形式对不对；读不出时没有。"""
    if text is None:
        return
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return
    for section in ("roles", "agents"):
        entries = data.get(section)
        if not isinstance(entries, dict):
            continue
        for entry in entries.values():
            if not isinstance(entry, dict):
                continue
            for key in ("allow", "deny"):
                rules = entry.get(key)
                if isinstance(rules, list):
                    yield from (r for r in rules if isinstance(r, str))


def plan(snapshot: Snapshot, change: Callable[[TOMLDocument], None]) -> AccessEdit:
    """平台改 access.toml 的唯一入口：读出它（不存在就是空文档）、交给 change 改、按 vet 校验，返回还没写的改动。现在的文件
    不合法（与读树同一套规则）时 Conflict，由 admin 先修好它；改完过不了 vet 时 InvalidInput。要先写别的文件的调用方（建、
    改名 agent）先 plan 再动手：不合法时什么都没写。界面的预览也只 plan 不 apply，与写入同样校验。同步 IO。"""
    path = snapshot.access_path
    text = _valid_text(path, snapshot.tools)
    doc = tomlkit.parse(text) if text is not None else tomlkit.document()
    change(doc)
    out = tomlkit.dumps(doc)
    vet(text, out, snapshot.tools, snapshot.addressable())
    return AccessEdit(path, text, out)


def _valid_text(path: Path, tools: ToolTable) -> str | None:
    """合法的现有内容（没有文件为 None），否则 Conflict 列出不合法的每一处。"""
    try:
        text = path.read_text(encoding="utf-8") if path.is_file() else None
    except UnicodeDecodeError as e:
        raise Conflict(f"{path} is not UTF-8 text ({e}); fix it first") from e
    access = parse(text, {}, tools)
    if not access.readable:
        reasons = "; ".join(p.reason for p in access.problems)
        raise Conflict(f"{path} is invalid ({reasons}); fix it first")
    return text


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


def _distinct(what: str, values: Sequence[str]) -> None:
    """界面交来的列表不能有重复（读法遇到重复只是去重，平台自己不写进去）。"""
    if len(set(values)) != len(values):
        raise InvalidInput(f"{what} must be distinct, got {list(values)}")


def set_role(doc: TOMLDocument, name: str, description: str, allow: list[str], deny: list[str]) -> None:
    _distinct("rules", allow)
    _distinct("rules", deny)
    fields: dict[str, object] = {"description": description, "allow": allow}
    if deny:
        fields["deny"] = deny
    _section(doc, "roles")[name] = _rules_table(fields)


def delete_role(doc: TOMLDocument, name: str) -> None:
    roles = _section(doc, "roles")
    if name in roles:
        del roles[name]


def set_agent_rules(doc: TOMLDocument, agent: str, rules: AgentRules) -> None:
    """agent 的规则；全空时去掉这一节（只有 base）。roles 不能写 base（每个 agent 隐含），角色有没有定义由 vet 查。"""
    if BASE in rules.roles:
        raise InvalidInput(f"role {BASE!r} is implicit for every agent; do not list it in roles")
    _distinct("roles", rules.roles)
    _distinct("rules", rules.allow)
    _distinct("rules", rules.deny)
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
    """agent 改名：它的那一节跟着改名（规则不按名字指 agent 的私有记忆，别处不用改）。"""
    agents = _section(doc, "agents")
    if old in agents:
        agents[new] = agents[old]
        del agents[old]


def remove_agent(doc: TOMLDocument, agent: str) -> None:
    agents = _section(doc, "agents")
    if agent in agents:
        del agents[agent]
