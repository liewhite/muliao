"""AI 资源的目录树（#498）：可编辑的 AI 资源都是文件，放在一棵目录树里，文件在谁的目录下就归谁。

```
<root>/
  platform/                     归平台（owner 为 None）
    access.toml                 角色定义、各 agent 的角色与 allow / deny（分发规则的唯一来源）
    memory/<名>.md              全局记忆（global）与共享记忆
    skills/<名>/SKILL.md …      公共 skill
    mcp/<名>.json               MCP server（mcp_config.parse_mcp_file）
    secrets/<KEY>.toml          secret：description、Fernet 密文 ciphertext（agents.secrets 加解密）与 allow_shell
  <agent 名>/                   归这个 agent
    agent.toml                  id、description、model、thinking、group（AgentFile）
    memory.md                   私有记忆，记忆名就是 agent 名
    skills/<名>/  mcp/<名>.json  secrets/<KEY>.toml
```

这里只管文件：布局、把整棵树读成一份不可变的快照（load_tree，纯同步 IO），以及平台自己写（原子写入用 platform.fs.write_atomic）时的删除、挪动。读坏的文件不加载、记成
Problem（路径与原因）。名字冲突的（同一种类的同名资源在不止一处）：其中一处在平台目录时只加载平台那份，其余各记一条；
否则全部不加载、每处各记一条（_Reader.resolved）。运行时把与这个 agent 相关的
报给它（agents.source.AssetSource）。agent 目录按 agent.toml 里的 id 对应数据库里的 agent，目录名必须就是数据库里它的名字
（名字只认数据库，改名只走平台）；对不上（id 不认识、重复、目录名不同）的整个目录不加载。

隐藏文件与目录（`.` 开头，如 .git、.DS_Store）不读。
"""

from __future__ import annotations

import logging
import os
import shutil
import tomllib
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, ValidationError

from muliao.agents.agent_rules import AgentRules
from muliao.agents.kinds import StoredKind, label
from muliao.agents.mcp_config import McpConfig, parse_mcp_file
from muliao.agents.naming import file_name, validate_slug
from muliao.agents.permissions import ADMIN, BASE, FIXED_RULES, Role, Rules, check_patterns
from muliao.agents.secrets_format import SecretFile, parse_secret_file
from muliao.agents.skill_package import SKILL_FILE, parse_frontmatter
from muliao.agents.thinking import Thinking
from muliao.errors import Conflict, InvalidInput, NotFound

log = logging.getLogger(__name__)

PLATFORM = "platform"
ACCESS_FILE = "access.toml"
AGENT_FILE = "agent.toml"
PRIVATE_MEMORY_FILE = "memory.md"
MEMORY_DIR = "memory"
SKILLS_DIR = "skills"
MCP_DIR = "mcp"
SECRETS_DIR = "secrets"

# 内置角色没写描述时的描述
BUILTIN_DESCRIPTIONS: dict[str, str] = {
    BASE: "每个 agent 都有：全部非管理工具与全局记忆，加上这里收录的 skill、MCP server、secret、记忆",
    "admin": "全部工具（含管理 agent 与角色），并且能改任何 AI 资源",
}


class AgentFile(BaseModel):
    """`<agent 名>/agent.toml`。名字不在这里：目录名就是名字。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    id: str
    description: str
    model: str
    thinking: Thinking = "default"
    group: str | None = None


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
class AgentDir:
    """一个加载成功的 agent 目录。"""

    id: str
    name: str
    description: str
    model: str
    thinking: Thinking
    group: str | None
    path: Path


@dataclass(frozen=True)
class Asset:
    """一项资源。path：记忆、MCP、secret 是那个文件，skill 是它的目录。description：skill 与 secret 的描述（记忆、MCP
    为空）。种类专有的内容：记忆的正文 text，MCP 的 mcp（配置与是否启用），secret 的文件内容 secret（密文与 allow_shell）。"""

    kind: StoredKind
    name: str
    owner: str | None
    path: Path
    description: str
    updated_at: datetime
    text: str = ""
    mcp: tuple[McpConfig, bool] | None = None
    secret: SecretFile | None = None


@dataclass(frozen=True)
class Problem:
    """读树时发现的一处问题：哪个文件（或目录）、为什么没加载。owner 是它所在目录的 agent 的 id（平台目录与根目录为
    None），据此报给相关的 agent。"""

    path: Path
    reason: str
    owner: str | None


@dataclass(frozen=True)
class Snapshot:
    """某一时刻整棵树的内容（只读）。"""

    root: Path
    # 数据库里的 agent（id → 名字，含正在删除的）：目录读坏、没加载的 agent 也知道自己的目录该叫什么（own_dir）
    names: Mapping[str, str]
    # 加载成功的 agent 目录，按 id
    agents: Mapping[str, AgentDir]
    # 每种资源按名字
    assets: Mapping[StoredKind, Mapping[str, Asset]]
    # 全部角色：内置在前（按定义顺序），自建的按名称
    roles: Mapping[str, Role]
    # access.toml 里各 agent 的规则，按 id；没写的 agent 只有 base
    rules: Mapping[str, AgentRules]
    problems: tuple[Problem, ...]
    # 磁盘上有、但读坏了或名字冲突而没加载的资源名：新建同名的会再添一处冲突，按已占用处理（check_free）
    unloaded: Mapping[StoredKind, frozenset[str]]
    # 被平台同名的那份遮住、没加载的 agent 目录里的副本（#502），按名字：平台那份转给 agent 之前要先处理掉它们
    shadowed: Mapping[StoredKind, Mapping[str, tuple[Path, ...]]]

    def rules_of(self, agent_id: str) -> AgentRules:
        return self.rules.get(agent_id, AgentRules())

    def get(self, kind: StoredKind, name: str) -> Asset | None:
        return self.assets[kind].get(name)

    def check_free(self, kind: StoredKind, name: str) -> None:
        """新建之前：同名的已加载，或在磁盘上但没加载（读坏、冲突），都是 Conflict。"""
        if name in self.assets[kind]:
            raise Conflict(f"{label(kind)} {name!r} already exists; names are unique, choose another name")
        if name in self.unloaded[kind]:
            raise Conflict(
                f"a {label(kind)} named {name!r} is already on disk but not loaded (broken or duplicated; see the "
                "reported problems); fix or remove it first, or choose another name"
            )

    def asset(self, kind: StoredKind, name: str) -> Asset:
        found = self.get(kind, name)
        if found is None:
            raise NotFound(f"{label(kind)} {name!r} not found")
        return found

    def of_kind(self, kind: StoredKind) -> list[Asset]:
        return sorted(self.assets[kind].values(), key=lambda a: a.name)

    def owner_dir(self, owner: str | None) -> Path:
        """owner（agent 的 id，None 为平台）的目录。agent 没有加载成功的目录时 NotFound。"""
        if owner is None:
            return self.root / PLATFORM
        agent = self.agents.get(owner)
        if agent is None:
            raise NotFound(f"agent {owner!r} has no directory in {self.root}")
        return agent.path

    def owner_label(self, owner: str | None) -> str:
        if owner is None:
            return "the platform"
        agent = self.agents.get(owner)
        return f"agent {agent.name if agent is not None else owner!r}"

    def path_of(self, kind: StoredKind, name: str, owner: str | None) -> Path:
        """owner 名下这一项资源该在的位置（新建、转移时用）。"""
        base = self.owner_dir(owner)
        if kind == StoredKind.MEMORY:
            return base / PRIVATE_MEMORY_FILE if owner is not None else base / MEMORY_DIR / f"{name}.md"
        if kind == StoredKind.SKILL:
            return base / SKILLS_DIR / name
        if kind == StoredKind.MCP:
            return base / MCP_DIR / f"{name}.json"
        return base / SECRETS_DIR / f"{name}.toml"

    def own_dir(self, agent_id: str) -> Path | None:
        """agent 的目录该在的位置（目录名就是数据库里的名字），不论它现在加载成功没有；不认识的 agent 为 None。"""
        name = self.names.get(agent_id)
        return None if name is None else self.root / name

    def administers(self, agent_id: str) -> bool:
        """agent 管整棵树（有 admin 角色）：平台目录、access.toml 与每个 agent 的目录都能写，平台的问题报给它。"""
        return ADMIN in self.rules_of(agent_id).roles

    def problems_for(self, agent_id: str) -> list[Problem]:
        """报给这个 agent 的问题：它自己目录里的；平台的（platform/、根目录、access.toml）只报给能写它们的 admin
        （administers），免得几十个 agent 同时去改同一个平台文件。别的照样记在服务日志里。"""
        admin = self.administers(agent_id)
        return [p for p in self.problems if p.owner == agent_id or (p.owner is None and admin)]

    @property
    def access_file(self) -> Path:
        return self.root / PLATFORM / ACCESS_FILE


def updated(path: Path) -> datetime:
    return datetime.fromtimestamp(path.stat().st_mtime, UTC)


def _visible(entries: list[Path]) -> list[Path]:
    return sorted((p for p in entries if not p.name.startswith(".")), key=lambda p: p.name)


def _nfc(name: str) -> str:
    return unicodedata.normalize("NFC", name)


class _Reader:
    """读一棵树：一次性的状态（收集到的资源与问题）。"""

    def __init__(self, root: Path, known: Mapping[str, str], memory_max_chars: int):
        self.root = root
        self.known = known
        self.memory_max_chars = memory_max_chars
        self.problems: list[Problem] = []
        # 每种资源：名字 → 找到的各处（冲突时不止一处）
        self.found: dict[StoredKind, dict[str, list[Asset]]] = {k: {} for k in StoredKind}
        self.unloaded: dict[StoredKind, set[str]] = {k: set() for k in StoredKind}
        # 平台目录里读坏了的名字：仍归平台占着，agent 的副本照样被遮住（不让副本趁平台那份坏了接管）
        self.broken_platform: dict[StoredKind, set[str]] = {k: set() for k in StoredKind}
        self.shadowed: dict[StoredKind, dict[str, tuple[Path, ...]]] = {k: {} for k in StoredKind}

    def problem(self, path: Path, reason: str, owner: str | None) -> None:
        self.problems.append(Problem(path, reason, owner))

    def add(self, asset: Asset) -> None:
        self.found[asset.kind].setdefault(asset.name, []).append(asset)

    def read(self) -> Snapshot:
        agents: dict[str, AgentDir] = {}
        if self.root.is_dir():
            claimed: dict[str, list[AgentDir]] = {}
            for entry in _visible(list(self.root.iterdir())):
                if entry.name == PLATFORM:
                    continue
                if not entry.is_dir():
                    self.problem(
                        entry, "unexpected file; the root holds only platform/ and agent directories", None
                    )
                    continue
                agent = self.agent_dir(entry)
                if agent is not None:
                    claimed.setdefault(agent.id, []).append(agent)
            for agent_id, dirs in claimed.items():
                if len(dirs) > 1:
                    for d in dirs:
                        self.problem(
                            d.path / AGENT_FILE,
                            f"id {agent_id} is also claimed by {[x.name for x in dirs if x is not d]}; "
                            "none of these directories is loaded",
                            None,
                        )
                    continue
                agents[agent_id] = dirs[0]
            for agent in agents.values():
                self.owner_assets(agent.path, agent.id, agent.name)
            platform = self.root / PLATFORM
            if platform.is_dir():
                self.owner_assets(platform, None, None)
        reported = {p.owner for p in self.problems}
        for agent_id, name in self.known.items():
            # 目录在、只是名字或内容不对的，上面已经报给它了
            if agent_id not in agents and agent_id not in reported:
                self.problem(
                    self.root / name,
                    f"agent {name!r} ({agent_id}) has no directory with an {AGENT_FILE} carrying its id; it keeps "
                    "its last known name, description and model, without private memory and own assets",
                    agent_id,
                )
        access = self.root / PLATFORM / ACCESS_FILE
        text = None
        if access.is_file():
            try:
                text = access.read_text(encoding="utf-8")
            except UnicodeDecodeError as e:
                self.problem(access, f"cannot parse: {e}; only the fixed parts of built-in roles apply", None)
        roles, rules = self.access(agents, text)
        return Snapshot(
            self.root,
            dict(self.known),
            agents,
            {kind: self.resolved(kind) for kind in StoredKind},
            roles,
            rules,
            tuple(self.problems),
            {k: frozenset(v) for k, v in self.unloaded.items()},
            {k: dict(v) for k, v in self.shadowed.items()},
        )

    def resolved(self, kind: StoredKind) -> dict[str, Asset]:
        """同名的只在一处才加载。平台目录优先（#502）：平台的那份照常加载（读坏了也照样占着名字），agent 目录里同名的
        不加载、报给那个 agent，免得 agent 在自己目录里复制一份公共资源就让所有人都用不了它；agent 之间同名地位相同，
        全部不加载。"""
        out: dict[str, Asset] = {}
        for name, places in sorted(self.found[kind].items()):
            platform = [a for a in places if a.owner is None]
            broken = name in self.broken_platform[kind]
            if len(places) == 1 and not broken:
                out[name] = places[0]
                continue
            if len(platform) == 1 or broken:
                copies = [a for a in places if a.owner is not None]
                if platform:
                    out[name] = platform[0]
                    where = str(platform[0].path.relative_to(self.root))
                else:
                    where = f"{PLATFORM}/ (broken, see the problem reported for it)"
                self.shadowed[kind][name] = tuple(a.path for a in copies)
                for a in copies:
                    self.shadow(kind, name, a, where)
                continue
            for a in places:
                others = [str(p.path.relative_to(self.root)) for p in places if p is not a]
                self.problem(
                    a.path,
                    f"{label(kind)} name {name!r} is also used by {others}; names are unique per kind, so none "
                    "of them is loaded until all but one are renamed or removed",
                    a.owner,
                )
            self.unloaded[kind].add(name)
        return out

    def shadow(self, kind: StoredKind, name: str, copy: Asset, where: str) -> None:
        """agent 目录里的 copy 与平台的同名，不加载，报给它。私有记忆的名字就是 agent 名、改不了：报给它不要动，另报给
        admin 处理平台那份。"""
        if kind != StoredKind.MEMORY:
            self.problem(
                copy.path,
                f"{label(kind)} name {name!r} is already used by the platform's {where}; the platform's one is "
                "loaded and this one is not: rename or remove it",
                copy.owner,
            )
            return
        self.problem(
            copy.path,
            f"your private memory is not loaded: the platform has a memory with the same name {name!r} ({where}); "
            "do not rename or remove this file, an admin has to rename or move the platform's one",
            copy.owner,
        )
        self.problem(
            self.root / PLATFORM / MEMORY_DIR / f"{name}.md",
            f"memory {name!r} has the name of agent {name!r}, whose private memory is therefore not loaded: rename "
            "it, or move it into that agent's directory if it is that agent's memory",
            None,
        )

    def agent_dir(self, path: Path) -> AgentDir | None:
        name = _nfc(path.name)
        file = path / AGENT_FILE
        if not file.is_file():
            self.problem(path, f"no {AGENT_FILE}: not an agent directory", None)
            return None
        try:
            data = parse_agent_file(file.read_text(encoding="utf-8"))
        except (InvalidInput, UnicodeDecodeError) as e:
            self.problem(file, str(e), None)
            return None
        if data.id not in self.known:
            self.problem(file, f"id {data.id!r} is not an existing agent", None)
            return None
        if self.known[data.id] != name:
            # 名字只认数据库：目录被手动改了名（或拷错了地方），不猜，整个不加载
            self.problem(
                path,
                f"this is agent {self.known[data.id]!r}'s directory (id {data.id}) under another name; agents are "
                f"renamed only in the app — move it back to {self.known[data.id]!r}",
                data.id,
            )
            return None
        if not data.description.strip():
            self.problem(file, "description must not be empty", data.id)
            return None
        return AgentDir(data.id, name, data.description, data.model, data.thinking, data.group, path)

    def owner_assets(self, base: Path, owner: str | None, agent_name: str | None) -> None:
        expected = {SKILLS_DIR, MCP_DIR, SECRETS_DIR}
        expected |= {AGENT_FILE, PRIVATE_MEMORY_FILE} if owner is not None else {ACCESS_FILE, MEMORY_DIR}
        for entry in _visible(list(base.iterdir())):
            if entry.name not in expected:
                self.problem(entry, f"not a recognized entry here (expected {sorted(expected)})", owner)
        if owner is not None:
            memory = base / PRIVATE_MEMORY_FILE
            if memory.is_file():
                assert agent_name is not None
                self.memory(memory, agent_name, owner)
        else:
            for f in self.files(base / MEMORY_DIR, ".md", owner):
                self.memory(f, f.stem, None)
        for d in self.dirs(base / SKILLS_DIR, owner):
            self.skill(d, owner)
        for f in self.files(base / MCP_DIR, ".json", owner):
            self.mcp(f, owner)
        for f in self.files(base / SECRETS_DIR, ".toml", owner):
            self.secret(f, owner)

    def files(self, directory: Path, suffix: str, owner: str | None) -> list[Path]:
        if not directory.is_dir():
            return []
        out = []
        for f in _visible(list(directory.iterdir())):
            if f.is_file() and f.suffix == suffix:
                out.append(f)
            else:
                self.problem(f, f"expected only *{suffix} files in {directory.name}/", owner)
        return out

    def dirs(self, directory: Path, owner: str | None) -> list[Path]:
        if not directory.is_dir():
            return []
        out = []
        for d in _visible(list(directory.iterdir())):
            if d.is_dir():
                out.append(d)
            else:
                self.problem(d, f"expected only skill directories in {directory.name}/", owner)
        return out

    def guarded(
        self, kind: StoredKind, name: str, path: Path, owner: str | None, load: Callable[[], Asset]
    ) -> None:
        try:
            self.add(load())
        except (InvalidInput, OSError, UnicodeDecodeError) as e:
            self.problem(path, str(e), owner)
            self.unloaded[kind].add(name)
            if owner is None:
                self.broken_platform[kind].add(name)

    def memory(self, path: Path, name: str, owner: str | None) -> None:
        def load() -> Asset:
            if owner is None:
                # 界面新建的要是 slug；agent 删除或转移时挪来的私有记忆带着 agent 名（可以是中文），能当文件名就行
                file_name(label(StoredKind.MEMORY), name)
            text = path.read_text(encoding="utf-8").strip()
            if len(text) > self.memory_max_chars:
                # 照常加载（截断更糟），但要让它缩短
                self.problem(
                    path,
                    f"memory is {len(text)} chars, over the limit of {self.memory_max_chars}; condense it",
                    owner,
                )
            return Asset(StoredKind.MEMORY, name, owner, path, "", updated(path), text=text)

        self.guarded(StoredKind.MEMORY, name, path, owner, load)

    def skill(self, path: Path, owner: str | None) -> None:
        def load() -> Asset:
            name = validate_slug(label(StoredKind.SKILL), path.name)
            md = path / SKILL_FILE
            if not md.is_file():
                raise InvalidInput(f"skill directory has no {SKILL_FILE}")
            description, _ = parse_frontmatter(md.read_text(encoding="utf-8"), name)
            return Asset(StoredKind.SKILL, name, owner, path, description, updated(md))

        self.guarded(StoredKind.SKILL, path.name, path, owner, load)

    def mcp(self, path: Path, owner: str | None) -> None:
        def load() -> Asset:
            name = validate_slug(label(StoredKind.MCP), path.stem)
            config, enabled = parse_mcp_file(path.read_text(encoding="utf-8"))
            return Asset(StoredKind.MCP, name, owner, path, "", updated(path), mcp=(config, enabled))

        self.guarded(StoredKind.MCP, path.stem, path, owner, load)

    def secret(self, path: Path, owner: str | None) -> None:
        def load() -> Asset:
            key = path.stem
            secret = parse_secret_file(key, path.read_text(encoding="utf-8"))
            return Asset(
                StoredKind.SECRET, key, owner, path, secret.description, updated(path), secret=secret
            )

        self.guarded(StoredKind.SECRET, path.stem, path, owner, load)

    def access(
        self, agents: Mapping[str, AgentDir], text: str | None
    ) -> tuple[dict[str, Role], dict[str, AgentRules]]:
        """access.toml 的内容 text（没有这个文件为 None）：坏掉的单条规则、不认识的角色各自跳过并记一条，其余照常（不让
        一个笔误拿掉一个 agent 的全部角色）。整个文件读不出来时只剩内置角色的固定部分，没有任何 agent 的规则。"""
        path = self.root / PLATFORM / ACCESS_FILE
        data: dict[str, object] = {}
        if text is not None:
            try:
                data = tomllib.loads(text)
            except tomllib.TOMLDecodeError as e:
                self.problem(path, f"cannot parse: {e}; only the fixed parts of built-in roles apply", None)
        extra = sorted(set(data) - {"roles", "agents"})
        if extra:
            self.problem(path, f"unknown top-level tables {extra} (expected roles, agents)", None)
        roles: dict[str, Role] = {
            n: Role(n, BUILTIN_DESCRIPTIONS[n], Rules(), fixed) for n, fixed in FIXED_RULES.items()
        }
        for name, raw in sorted(_table(data.get("roles"))):
            entry = self.entry(_RoleEntry, raw, path, f"role {name!r}")
            if entry is None:
                continue
            try:
                validate_slug("role", name)
            except InvalidInput as e:
                self.problem(path, str(e), None)
                continue
            fixed = FIXED_RULES.get(name, Rules())
            description = entry.description.strip() or BUILTIN_DESCRIPTIONS.get(name, "")
            if not description:
                self.problem(path, f"role {name!r}: description must not be empty", None)
                continue
            rules = Rules(
                self.patterns(entry.allow, path, f"role {name!r}"),
                self.patterns(entry.deny, path, f"role {name!r}"),
            )
            roles[name] = Role(name, description, rules, fixed)
        by_name = {a.name: a.id for a in agents.values()}
        rules_by_id: dict[str, AgentRules] = {}
        for name, raw in sorted(_table(data.get("agents"))):
            what = f"agent {name!r}"
            entry = self.entry(_AgentEntry, raw, path, what)
            if entry is None:
                continue
            agent_id = by_name.get(_nfc(name))
            if agent_id is None:
                self.problem(path, f"[agents.{name}]: no loaded agent directory has this name", None)
                continue
            kept = []
            for r in dict.fromkeys(entry.roles):
                if r not in roles or not roles[r].assignable:
                    self.problem(path, f"{what}: role {r!r} is unknown or implicit; skipped", agent_id)
                else:
                    kept.append(r)
            rules_by_id[agent_id] = AgentRules.of(
                kept,
                self.patterns(entry.allow, path, what, agent_id),
                self.patterns(entry.deny, path, what, agent_id),
            )
        return roles, rules_by_id

    def entry[T: BaseModel](self, model: type[T], raw: object, path: Path, what: str) -> T | None:
        try:
            return model.model_validate(raw)
        except ValidationError as e:
            self.problem(path, f"{what}: {_brief(e)}", None)
            return None

    def patterns(
        self, patterns: list[str], path: Path, what: str, owner: str | None = None
    ) -> tuple[str, ...]:
        kept = []
        for p in dict.fromkeys(patterns):
            try:
                check_patterns([p])
            except InvalidInput as e:
                self.problem(path, f"{what}: {e}; skipped", owner)
                continue
            kept.append(p)
        return tuple(kept)


def _table(value: object) -> list[tuple[str, object]]:
    return list(value.items()) if isinstance(value, dict) else []


def _brief(e: Exception) -> str:
    if isinstance(e, ValidationError):
        return "; ".join(
            f"{'.'.join(str(x) for x in err['loc']) or '(root)'}: {err['msg']}" for err in e.errors()
        )
    return str(e)


def parse_agent_file(text: str) -> AgentFile:
    """agent.toml 的格式（不含与数据库的对应）；不合规则时 InvalidInput。"""
    try:
        return AgentFile.model_validate(tomllib.loads(text))
    except (tomllib.TOMLDecodeError, ValidationError) as e:
        raise InvalidInput(f"invalid {AGENT_FILE}: {_brief(e)}") from e


def access_problems(snapshot: Snapshot, text: str) -> list[str]:
    """access.toml 若写成 text，按读树的同一套规则会报的问题（对照 snapshot 里的 agent 与角色）。"""
    reader = _Reader(snapshot.root, snapshot.names, 0)
    reader.access(snapshot.agents, text)
    return [p.reason for p in reader.problems]


def load_tree(root: Path, known: Mapping[str, str], memory_max_chars: int) -> Snapshot:
    """读整棵树。known 是数据库里的 agent（id → 名字，含正在删除的）：agent 目录按 agent.toml 的 id 对上它们。同步 IO，
    调用方放到线程里。"""
    return _Reader(root, known, memory_max_chars).read()


def fingerprint(root: Path) -> tuple[tuple[str, int, int], ...]:
    """树的指纹：每个文件与目录的相对路径、修改时间、大小。任何一个文件改了、增删、改名都会变；快照按它缓存。"""
    out: list[tuple[str, int, int]] = []
    if not root.is_dir():
        return ()
    for current, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in [*dirs, *files]:
            if name.startswith("."):
                continue
            path = os.path.join(current, name)
            try:
                st = os.stat(path)
            except FileNotFoundError:
                continue
            out.append((os.path.relpath(path, root), st.st_mtime_ns, st.st_size))
    return tuple(sorted(out))


# ---- 写入（平台自己写的：界面、vault_set、建 agent、迁移） ----


def remove(path: Path) -> None:
    """删掉一个文件或目录（不存在不算错）。"""
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def move(src: Path, dst: Path) -> None:
    """改名或换 owner：目标已存在是冲突（不覆盖）。"""
    if dst.exists() and not os.path.samefile(src, dst):  # 大小写不敏感的文件系统上只改大小写是同一个
        raise InvalidInput(f"{dst} already exists")
    dst.parent.mkdir(parents=True, exist_ok=True)
    src.rename(dst)
