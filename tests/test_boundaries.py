"""最基本的几条边界（docs/architecture.md「依赖规则」），对源码做静态检查：

- 每个领域有 api.py；领域之间只 import 对方的 api 与 events，经 api 的依赖按 LAYERS 单向；
- platform 不 import 领域，领域不 import HTTP 层与组合根；HTTP 层只用领域的 api、events、tables；
- 工具拒绝只抛 DomainError；pydantic-ai 的运行类型只在 agent_runtime；路径身份只在 platform.fs 算。
"""

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).parent.parent / "src" / "muliao"
# 经 api 的依赖方向（docs/architecture.md「依赖规则」）：领域只能 import 更早一层的领域的 api，同层与反向都不行，所以依赖无环。
# events 模块不受方向限制（只放事件与请求的数据形状、端口类型）。新增领域要在这里定它在哪一层
LAYERS: list[set[str]] = [
    {"files", "agents", "usage", "speech", "web"},
    {"agent_runtime"},
    {"sessions"},
    {"workspace", "notifications", "issues"},
    {"triggers"},
    # 活动概览（热历史）要读会话与 issue 的动态
    {"history"},
]
DOMAINS = set().union(*LAYERS)
RANK = {d: rank for rank, layer in enumerate(LAYERS) for d in layer}
# 一个领域对外公开的模块
PUBLIC = {"api", "events"}
# HTTP 适配层另外可以 import 的：序列化领域交给它的行
HTTP_VISIBLE = PUBLIC | {"tables"}
# src/muliao 下不是领域的目录
NOT_DOMAINS = {"api", "platform", "migrations", "webui"}


def _module_of(path: Path, root: Path) -> list[str]:
    """path 这个模块所在的包（相对 import 从这里算起）：muliao/agents/x.py 与 muliao/agents/__init__.py 都在 muliao.agents。"""
    return ["muliao", *path.relative_to(root).parts[:-1]]


def _imports(path: Path, root: Path = SRC) -> list[tuple[int, str]]:
    """这个文件 import 的 muliao 模块（含相对 import，换算成绝对名）。`from x import y` 记成 x.y：y 可能是子模块。"""
    tree = ast.parse(path.read_text())
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level:
                package = _module_of(path, root)
                if node.level > len(package):
                    raise AssertionError(
                        f"{path}:{node.lineno} relative import goes above the muliao package"
                    )
                base = package[: len(package) - node.level + 1]
                module = ".".join(base + ([node.module] if node.module else []))
            else:
                module = node.module or ""
            found += [(node.lineno, f"{module}.{alias.name}") for alias in node.names]
        elif isinstance(node, ast.Import):
            found += [(node.lineno, alias.name) for alias in node.names]
    return [(line, name) for line, name in found if name.startswith("muliao.")]


def _violations() -> list[str]:
    problems = []
    for path in SRC.rglob("*.py"):
        parts = path.relative_to(SRC).parts
        owner = parts[0] if len(parts) > 1 else None
        for line, name in _imports(path):
            target = name.split(".")
            where = f"{path.relative_to(SRC)}:{line} imports {name}"
            if owner == "platform" and target[1] in DOMAINS | {"api", "container"}:
                problems.append(f"{where} (platform must not depend on a domain)")
            if owner in DOMAINS:
                if target[1] in ("api", "container"):
                    problems.append(
                        f"{where} (a domain must not depend on the HTTP layer or the composition root)"
                    )
                elif target[1] in DOMAINS and target[1] != owner:
                    if len(target) < 3 or target[2] not in PUBLIC:
                        problems.append(f"{where} (only {target[1]}.api and {target[1]}.events are public)")
                    elif target[2] == "api" and RANK[target[1]] >= RANK[owner]:
                        problems.append(
                            f"{where} (dependencies through api go one way: {owner} is in layer {RANK[owner]}, "
                            f"{target[1]} in layer {RANK[target[1]]})"
                        )
            if owner == "api" and target[1] in DOMAINS and (len(target) < 3 or target[2] not in HTTP_VISIBLE):
                problems.append(f"{where} (the HTTP layer uses {target[1]}.api, .events and .tables only)")
    return sorted(problems)


def test_every_domain_has_a_public_api_module():
    """src/muliao 下的目录都是领域（另列的除外），目录有没有 __init__.py 都算（没有它的命名空间包照样能 import）。"""
    packages = {p.name for p in SRC.iterdir() if p.is_dir() and p.name != "__pycache__"} - NOT_DOMAINS
    assert packages == DOMAINS
    assert sorted(d for d in DOMAINS if not (SRC / d / "api.py").exists()) == []


def test_domains_only_use_each_others_api():
    assert _violations() == []


# pydantic-ai 的运行与挂起类型只在 agent_runtime 里用：别的领域经它的 Finished / Paused、
# `{调用 id: 结果}` 与 defer_call。消息类型（pydantic_ai.messages）是历史的存储形态，不在此列
RUN_TYPES = {
    "AgentRun",
    "AgentRunResult",
    "ApprovalRequired",
    "CallDeferred",
    "DeferredToolRequests",
    "DeferredToolResults",
}


def test_only_agent_runtime_touches_the_model_librarys_run_types():
    problems = []
    for path in SRC.rglob("*.py"):
        if path.relative_to(SRC).parts[0] == "agent_runtime":
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "pydantic_ai":
                problems += [
                    f"{path.relative_to(SRC)}:{node.lineno} imports {a.name}"
                    for a in node.names
                    if a.name in RUN_TYPES
                ]
    assert problems == []


def test_relative_imports_are_resolved_to_the_module_they_name(tmp_path):
    root = tmp_path / "muliao"
    module = root / "workspace" / "x.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "from . import api\nfrom .runner import y\nfrom ..sessions.mailbox import z\nimport os\n"
    )
    assert _imports(module, root) == [
        (1, "muliao.workspace.api"),
        (2, "muliao.workspace.runner.y"),
        (3, "muliao.sessions.mailbox.z"),
    ]
    module.write_text("from .. import x\n")
    assert _imports(module, root) == [(1, "muliao.x")]
    module.write_text("from ... import x\n")
    with pytest.raises(AssertionError, match="above the muliao package"):
        _imports(module, root)


def test_tools_refuse_with_domain_errors_not_model_retry():
    """工具的拒绝一律抛 DomainError，由 agent_runtime.tool_errors 统一交还模型：在那里预算用完时改成 ToolFailed，
    不让整轮运行失败。工具自己抛 ModelRetry 会绕过这层，连续 3 次就把整轮打成失败（#145）。所以除了这层和把 MCP
    server 的 ModelRetry 换成 DomainError 的 mcp 模块，源码里不得出现 ModelRetry（裸名、属性、导入都算）。"""
    allowed = {SRC / "agent_runtime" / "tool_errors.py", SRC / "agent_runtime" / "mcp.py"}
    offenders = []
    for path in SRC.rglob("*.py"):
        if path in allowed or "webui" in path.parts:
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                (isinstance(node, ast.Name) and node.id == "ModelRetry")
                or (isinstance(node, ast.Attribute) and node.attr == "ModelRetry")
                or (isinstance(node, ast.alias) and node.name == "ModelRetry")
            ):
                offenders.append(f"{path.relative_to(SRC)}:{getattr(node, 'lineno', '?')}")
    assert offenders == []


def test_path_identity_is_computed_only_in_platform_fs():
    """「两个路径是不是同一个、在不在某目录下」只经 platform.fs.real（#570）：APFS 不分大小写与正规化形式，realpath /
    Path.resolve() 不纠正大小写，各处自己解析再按字符串比，`~/.MULIAO/CONFIG.toml` 就绕过了读写边界与审批。所以
    fs.py 以外不得调用 os.path.realpath、无参的 .resolve()。"""
    allowed = {SRC / "platform" / "fs.py"}
    offenders = []
    for path in SRC.rglob("*.py"):
        if path in allowed or "webui" in path.parts:
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            name = node.func.attr
            if name == "realpath" or (name == "resolve" and not node.args and not node.keywords):
                offenders.append(f"{path.relative_to(SRC)}:{node.lineno} {name}")
    assert offenders == []
