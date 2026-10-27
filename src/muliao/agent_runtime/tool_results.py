"""内置工具结果交给模型的形式：结构化的返回值（pydantic 模型、列表、dict）一律序列化成一段 JSON 文本。

pydantic-ai 把列表结果按元素逐个映射（Anthropic 的 tool_result 里每个元素一个 text block），模型看到的是几个 JSON
对象首尾相接、不是合法 JSON；这里在工具执行完、结果写进历史之前就把它变成一段完整的 JSON，各 provider、界面与
历史看到的都是同一份。带图片等内容的列表（read_file 读图）原样交回，由 attachments.ToolImagesToFiles 处理。
工具说明里写明返回的是 JSON 数组还是对象、有哪些字段（tests/test_tool_schemas.py 核对）。"""

from dataclasses import dataclass
from typing import Any

from pydantic import TypeAdapter
from pydantic_ai import RunContext, ToolReturn
from pydantic_ai.messages import is_multi_modal_content
from pydantic_ai.toolsets import ToolsetTool, WrapperToolset

from muliao.platform.tooling import RunScope

_JSON = TypeAdapter(Any)


def as_model_text(value: Any) -> Any:
    """工具返回值交给模型的形式：文本、ToolReturn、带图片的内容列表原样；其余（结构化的值）序列化成一段 JSON 文本。"""
    if value is None or isinstance(value, str | ToolReturn):
        return value
    if isinstance(value, list) and any(is_multi_modal_content(v) for v in value):
        return value
    return _JSON.dump_json(value).decode()


@dataclass
class JsonResults(WrapperToolset[RunScope]):
    """套在内置工具集外：结构化的返回值序列化成一段 JSON 文本（as_model_text）。"""

    async def call_tool(
        self, name: str, tool_args: dict[str, Any], ctx: RunContext[RunScope], tool: ToolsetTool[RunScope]
    ) -> Any:
        return as_model_text(await super().call_tool(name, tool_args, ctx, tool))
