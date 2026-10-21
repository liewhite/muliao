"""工具参数的 schema：数组与对象参数不写成「可为空」。

可为空的数组/对象在 schema 里是 anyOf[array|object, null]，模型更容易把值写成 JSON 字符串（生产上 slack-hub 的
run_shell 有约 14% 的调用把 secrets 传成了 "[\\"KEY\\"]"，即使它自己的指令里写着正确格式）。不给与给空值相同时，
用不可为空的类型加空默认值；只有 None 本身有含义（「用默认」或「不改」）时才允许可为空，并在这里登记原因。
"""

from pydantic_ai import FunctionToolset

# (工具, 参数) -> None 的含义
NULL_MEANS_SOMETHING = {
    ("create_agent", "tool_groups"): "None = 全部工具组（新建 agent 的默认）",
    ("update_agent", "tool_groups"): "None = 不改",
    ("trigger_update", "config"): "None = 不改",
    ("trigger_create", "precheck"): "None = 不设前置检查（Precheck 没有「空检查」可以代替）",
    ("trigger_update", "precheck"): "None = 不改",
    # 枚举以 $ref 出现，被当作对象
    ("issue_update", "status"): "None = 不改",
    ("issue_update", "priority"): "None = 不改",
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
    toolsets = [t for g in container.consumer._tool_groups.groups.values() for t in g]  # noqa: SLF001
    toolsets += list(container.consumer._tool_groups.always)  # noqa: SLF001
    offending = [
        (tool.name, param)
        for ts in toolsets
        if isinstance(ts, FunctionToolset)
        for tool in ts.tools.values()
        for param in _nullable_containers(tool.function_schema.json_schema)
        if (tool.name, param) not in NULL_MEANS_SOMETHING
    ]
    assert offending == []
