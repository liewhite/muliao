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
    from muliao.vault.api import SECRET_TOOL_ARGS

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
    from muliao.agent_runtime.workspace_tools import TextEdit

    params = set(session_tools(container)["edit_file"].function_schema.json_schema["properties"])
    assert params - {"edits"} == set(TextEdit.model_fields)


async def test_issue_create_single_parameters_mirror_the_fields_of_one_batch_item(container):
    """issue_create 的单个参数与 issues 里每一项是同一组字段（批里另有 key）。"""
    from muliao.issues.tools import NewIssueItem

    params = set(session_tools(container)["issue_create"].function_schema.json_schema["properties"])
    assert params - {"issues"} == set(NewIssueItem.model_fields) - {"key"}
