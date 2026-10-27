"""工具参数的 schema：数组与对象参数不写成「可为空」。

可为空的数组/对象在 schema 里是 anyOf[array|object, null]，模型更容易把值写成 JSON 字符串（生产上 slack-hub 的
run_shell 有约 14% 的调用把 secrets 传成了 "[\\"KEY\\"]"，即使它自己的指令里写着正确格式）。不给与给空值相同时，
用不可为空的类型加空默认值；只有 None 本身有含义（「用默认」或「不改」）时才允许可为空，并在这里登记原因。
"""

from tests.conftest import session_tools

# (工具, 参数) -> None 的含义
NULL_MEANS_SOMETHING = {
    ("update_agent", "roles"): "None = 不改",
    ("update_agent", "allow"): "None = 不改",
    ("update_agent", "deny"): "None = 不改",
    ("update_role", "allow"): "None = 不改",
    ("update_role", "deny"): "None = 不改",
    ("transfer_item", "owner"): "None = 转给平台",
    ("trigger_update", "config"): "None = 不改",
    ("trigger_create", "precheck"): "None = 不设前置检查（Precheck 没有「空检查」可以代替）",
    ("trigger_update", "precheck"): "None = 不改",
    # 枚举以 $ref 出现，被当作对象
    ("issue_update", "status"): "None = 不改",
    ("issue_update", "priority"): "None = 不改",
    ("issue_update", "depends_on"): "None = 不改（[] 是去掉全部依赖）",
}


def _nullable_containers(schema: dict) -> list[str]:
    found = []
    for name, prop in schema.get("properties", {}).items():
        variants = prop.get("anyOf", [])
        # 模型参数（pydantic BaseModel）以 $ref 引用出现，没有 type：同样是对象
        types = {"object" if "$ref" in v else v.get("type") for v in variants}
        if "null" in types and types & {"array", "object"}:
            found.append(name)
    return found


async def test_array_and_object_parameters_are_not_nullable_unless_null_means_something(container):
    offending = [
        (tool.name, param)
        for tool in session_tools(container).values()
        for param in _nullable_containers(tool.function_schema.json_schema)
        if (tool.name, param) not in NULL_MEANS_SOMETHING
    ]
    assert offending == []


async def test_redacted_secret_arguments_exist_in_the_registered_tool_schemas(container):
    """脱敏表若与工具定义脱节（改了工具名或参数名），secret 明文会悄悄进入 messages 表和 SSE。"""
    from muliao.agents.api import SECRET_TOOL_ARGS

    params = {
        tool.name: set(tool.function_schema.json_schema.get("properties", {}))
        for tool in session_tools(container).values()
    }
    assert SECRET_TOOL_ARGS
    for name, secret in SECRET_TOOL_ARGS.items():
        assert name in params, f"{name} is not a registered tool"
        assert secret <= params[name], f"{name} has no parameter {set(secret - params[name])}"


async def test_deferred_tools_and_thread_tools_are_registered_under_their_table_names(container):
    """挂起工具表（sessions.deferred）与续聊工具（KindPolicy.continued_by）按名字查：工具改了名而表没跟上，挂起判断
    与续聊提示会悄悄失效。"""
    from muliao.sessions.deferred import DEFERRED_TOOLS
    from muliao.sessions.kinds import SessionKind, policy_of

    names = set(session_tools(container))
    continued_by = {p.continued_by for k in SessionKind if (p := policy_of(k)).continued_by is not None}
    missing = (set(DEFERRED_TOOLS) | continued_by) - names
    assert not missing, missing


async def test_edit_file_single_change_parameters_mirror_the_fields_of_one_edit(container):
    """edit_file 的单处修改参数与 edits 里每一项是同一组定位方式：一边加了另一边没跟上，模型就会在两种写法里看到
    不同的能力。"""
    from muliao.workspace.file_tools import TextEdit

    params = set(session_tools(container)["edit_file"].function_schema.json_schema["properties"])
    assert params - {"edits"} == set(TextEdit.model_fields)


def _structured_return(tool) -> tuple[str, type] | None:
    """工具返回结构化结果时：("array" | "object", 元素/对象的类型)；返回文本等时为 None。"""
    import dataclasses
    import typing

    from pydantic import BaseModel
    from pydantic_ai import ToolReturn

    returned = typing.get_type_hints(tool.function)["return"]
    if returned is ToolReturn:
        # 文本结果加元数据（附件、子会话），不是结构化结果
        return None
    shape, item = (
        ("array", typing.get_args(returned)[0])
        if typing.get_origin(returned) is list
        else ("object", returned)
    )
    if isinstance(item, type) and (issubclass(item, BaseModel) or dataclasses.is_dataclass(item)):
        return shape, item
    return None


def _field_names(item: type) -> list[str]:
    import dataclasses

    return (
        [f.name for f in dataclasses.fields(item)]
        if dataclasses.is_dataclass(item)
        else list(item.model_fields)
    )


async def test_tools_returning_structured_results_say_it_is_json_and_name_every_field(container):
    """结构化结果交给模型时是一段 JSON（agent_runtime.tool_results）：说明里写明是 JSON 数组还是对象，并列出每个字段
    （`字段名`），模型不用先调一次才知道结果长什么样；加了字段而说明没跟上，这里会拦住。"""
    missing = {}
    structured = []
    for name, tool in session_tools(container).items():
        if (found := _structured_return(tool)) is None:
            continue
        shape, item = found
        structured.append(name)
        lacks = [f for f in _field_names(item) if f"`{f}`" not in tool.description]
        if f"JSON {shape}" not in tool.description:
            lacks.insert(0, f"JSON {shape}")
        if lacks:
            missing[name] = lacks
    assert {"issue_list", "get_agent", "history_search"} <= set(structured)
    assert missing == {}


async def test_structured_tool_results_reach_the_model_as_one_json_document():
    """pydantic-ai 把列表结果按元素逐个交给 provider（Anthropic 每个元素一个 text block，模型看到几个对象首尾相接）：
    内置工具的结构化结果先序列化成一段合法 JSON；文本与带图片的内容列表原样。"""
    import json
    from datetime import UTC, datetime

    from pydantic import BaseModel
    from pydantic_ai import BinaryContent

    from muliao.agent_runtime.tool_results import as_model_text

    class Row(BaseModel):
        id: int
        at: datetime

    rows = [
        Row(id=1, at=datetime(2026, 10, 27, tzinfo=UTC)),
        Row(id=2, at=datetime(2026, 10, 28, tzinfo=UTC)),
    ]
    assert json.loads(as_model_text(rows)) == [
        {"id": 1, "at": "2026-10-27T00:00:00Z"},
        {"id": 2, "at": "2026-10-28T00:00:00Z"},
    ]
    assert as_model_text([]) == "[]"
    assert json.loads(as_model_text(Row(id=3, at=datetime(2026, 1, 1, tzinfo=UTC)))) == {
        "id": 3,
        "at": "2026-01-01T00:00:00Z",
    }
    assert as_model_text("plain") == "plain"
    image = ["a.png", BinaryContent(b"\x89PNG", media_type="image/png")]
    assert as_model_text(image) is image


async def test_issue_create_single_parameters_mirror_the_fields_of_one_batch_item(container):
    """issue_create 的单个参数与 issues 里每一项是同一组字段（批里另有 key）。"""
    from muliao.issues.tools import NewIssueItem

    params = set(session_tools(container)["issue_create"].function_schema.json_schema["properties"])
    assert params - {"issues"} == set(NewIssueItem.model_fields) - {"key"}
