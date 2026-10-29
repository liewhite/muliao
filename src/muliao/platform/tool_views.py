"""工具调用在界面上的展示（#301）：每个工具可以给自己的参数与结果定一个展示形式（ToolRender），界面按展示块渲染；
没定的（包括全部 MCP 工具）照旧显示参数与结果的原文。

- 展示块是结构化的描述（ViewBlock），不是 HTML：界面每种块一个组件，样式与安全都在界面一处。
- render 挂在定义工具的地方（`@ts.tool(metadata=rendered_by(...))`，pydantic-ai 的 metadata 不交给模型），
  由 agent_runtime.toolbox.Toolbox.renders 收集并配上工具自己的参数校验（BoundRender），会话领域投影条目时调用
  （sessions.transcript）。历史与运行中的条目走同一份投影，所以历史消息同样按 render 展示。
- 参数先经工具自己的 schema 校验（与执行时同一个校验器，含默认值）再交给 call：不合 schema 的调用（会被退回）
  按原文展示。结构化结果按 render 声明的结果类型还原，还原不了（旧历史的结果不合现在的类型）按原文展示并记
  warning；render 自己认不出、不想画时返回 None，同样按原文展示。render 内部的错误不兜底。
"""

from __future__ import annotations

import difflib
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, TypeAdapter, ValidationError
from pydantic_core import SchemaValidator

from muliao.platform.text_edit import truncate

log = logging.getLogger(__name__)

# 输出模型：字段一律必填（没有就是 null），前端类型与实际输出一致


class TerminalBlock(BaseModel):
    """终端：`$ command` 提示行、输出、状态行（如退出码、超时、后台任务）。界面把相邻的终端块（调用的命令与结果的
    输出）画在同一个终端框里。"""

    kind: Literal["terminal"] = "terminal"
    command: str | None
    output: str | None
    status: str | None
    # 状态是失败（非零退出码、超时被杀）：状态行醒目显示
    failed: bool


class CodeBlock(BaseModel):
    """一段代码或文件内容（等宽），title 多为文件路径。"""

    kind: Literal["code"] = "code"
    title: str | None
    text: str


class DiffBlock(BaseModel):
    """一处修改：统一 diff 格式的文本（以 `+`/`-`/空格开头的行），title 多为文件路径。"""

    kind: Literal["diff"] = "diff"
    title: str | None
    text: str


class MarkdownBlock(BaseModel):
    kind: Literal["markdown"] = "markdown"
    title: str | None
    text: str


class FieldRow(BaseModel):
    label: str
    value: str


class FieldsBlock(BaseModel):
    """键值对（如 issue 的状态、指派人）。"""

    kind: Literal["fields"] = "fields"
    rows: list[FieldRow]


class TableBlock(BaseModel):
    kind: Literal["table"] = "table"
    columns: list[str]
    rows: list[list[str]]


class Link(BaseModel):
    title: str
    url: str
    summary: str | None


class LinksBlock(BaseModel):
    kind: Literal["links"] = "links"
    links: list[Link]


ViewBlock = Annotated[
    TerminalBlock | CodeBlock | DiffBlock | MarkdownBlock | FieldsBlock | TableBlock | LinksBlock,
    Field(discriminator="kind"),
]

View = list[ViewBlock]


def fields(*rows: tuple[str, object | None]) -> FieldsBlock:
    """键值块：值为 None 的行不显示（没给的参数），其余转成文字。"""
    return FieldsBlock(
        rows=[FieldRow(label=label, value=_text(value)) for label, value in rows if value is not None]
    )


def _text(value: object) -> str:
    if isinstance(value, list | tuple):
        return ", ".join(str(v) for v in value) if value else "(none)"
    return str(value)


def diff(title: str | None, old: str, new: str) -> DiffBlock:
    """把一处替换（原文 → 新文）画成统一 diff：只有改动的行带 `-`/`+`，上下文是两边相同的行。"""
    lines = difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=3)
    # 去掉 `---`/`+++` 文件头；`@@` 分段行留着（多处改动时分隔）。没有原文（只知道新文）时只有一段，行号也没有
    # 意义，连 `@@` 一起去掉
    body = [line for i, line in enumerate(lines) if i >= (2 if old else 3)]
    return DiffBlock(title=title, text="\n".join(body))


# 工具自己的 schema 校验过的参数（参数名 → 值，含默认值，与执行时交给工具函数的一致）
Args = Mapping[str, Any]


@dataclass(frozen=True)
class ToolRender[R]:
    """一个工具的展示规则。call：参数 → 展示块；result：结果 → 展示块，结果是 result_type 从 JSON 文本还原的值，
    没给 result_type 时是结果交给模型的文字。哪一边没给、或返回 None（认不出、不该画），那一边显示原文；返回
    空列表表示这一边不需要显示。"""

    call: Callable[[Args], Sequence[ViewBlock] | None] | None = None
    result: Callable[[R], Sequence[ViewBlock] | None] | None = None
    result_type: TypeAdapter[R] | None = None


@dataclass(frozen=True)
class BoundRender:
    """配上了工具参数校验器的展示规则（Toolbox.renders 给出）。"""

    tool: str
    render: ToolRender[Any]
    # 工具自己的参数校验（pydantic-ai 的 function_schema.validator）
    validator: SchemaValidator

    def call_view(self, args: dict[str, Any]) -> View | None:
        if self.render.call is None:
            return None
        try:
            valid = self.validator.validate_python(args)
        except ValidationError:
            # 参数不合工具的 schema：这次调用被退回了，按原文展示
            return None
        return _view(self.render.call(valid))

    def result_view(self, text: str) -> View | None:
        if self.render.result is None:
            return None
        if self.render.result_type is None:
            return _view(self.render.result(text))
        try:
            value = self.render.result_type.validate_json(text)
        except ValidationError as e:
            log.warning(
                "tool result does not fit its render type, shown raw",
                extra={"tool": self.tool, "error": str(e)[:500], "result": truncate(text, 200)},
            )
            return None
        return _view(self.render.result(value))


def _view(blocks: Sequence[ViewBlock] | None) -> View | None:
    return None if blocks is None else list(blocks)


# metadata 里挂 render 的键
_RENDER_KEY = "muliao_render"


def rendered_by(render: ToolRender[Any]) -> dict[str, Any]:
    """工具的 metadata：`@ts.tool(metadata=rendered_by(ToolRender(...)))`。"""
    return {_RENDER_KEY: render}


def render_in(metadata: Mapping[str, Any] | None) -> ToolRender[Any] | None:
    return None if metadata is None else metadata.get(_RENDER_KEY)


# 工具名 → 展示规则（Toolbox.renders）；没有的工具显示原文
ToolRenders = Mapping[str, BoundRender]
# 只要文字的投影（压缩的文字记录）不画展示块
NO_RENDERS: ToolRenders = {}


def truncated(view: View, max_chars: int) -> View:
    """展示块里的长文字截断（实时条目用，见 sessions.steps）：只截块本身的文字字段，键值、表格、链接的项本来就短。"""
    return [
        block.model_copy(
            update={
                name: truncate(value, max_chars)
                for name, value in block
                if isinstance(value, str) and name != "kind"
            }
        )
        for block in view
    ]
