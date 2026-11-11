"""`<agent 名>/agent.toml` 的格式：读（parse）与平台自己写（dump）在这一处。名字不在文件里：目录名就是名字；目录与数据库
里 agent 的对应在读树（agents.assets）。"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

import tomlkit
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from muliao.errors import InvalidInput
from muliao.platform.values import validation_reason

AGENT_FILE = "agent.toml"

# agent 的思考档位，运行时交给 pydantic_ai 的 ModelSettings.thinking（agent_runtime.llm.model_settings）："default" 不指定
# 档位，按模型与服务端的默认；"off" 关闭；其余是 effort 档位。各 provider 怎么换算、模型不支持时怎么处理都由 pydantic_ai
# 与服务端决定，这里不映射。档位之外，运行时在会思考的请求上还要回思考的摘要（llm._thinking_shown），"default" 也带。
Thinking = Literal["default", "off", "low", "medium", "high", "xhigh"]

GROUP_MAX = 64


class AgentFile(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: str
    description: str
    model: str
    thinking: Thinking = "default"
    group: str | None = None

    @field_validator("description")
    @classmethod
    def _described(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("description must not be empty")
        return value

    @field_validator("group")
    @classmethod
    def _group(cls, value: str | None) -> str | None:
        """不分组就是没有这个键；有就是去过首尾空白、非空、不超过 GROUP_MAX 个字符的名字。"""
        if value is not None and (not value or value != value.strip() or len(value) > GROUP_MAX):
            raise ValueError(
                f"group must be 1-{GROUP_MAX} characters without leading or trailing whitespace, got {value!r}"
            )
        return value


# 能改的配置字段（id 把目录与 agent 绑在一起，不改）：新建与修改 agent 时按名字取（agents.service）
CONFIG_FIELDS: tuple[str, ...] = tuple(f for f in AgentFile.model_fields if f != "id")


def parse(text: str) -> AgentFile:
    """不合格式时 InvalidInput。"""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise InvalidInput(f"invalid {AGENT_FILE}: {e}") from e
    return of(data)


def of(data: Mapping[str, object]) -> AgentFile:
    """按这些字段的 AgentFile（平台自己写时也经这里）；不合规则时 InvalidInput。"""
    try:
        return AgentFile.model_validate(data)
    except ValidationError as e:
        raise InvalidInput(f"invalid {AGENT_FILE}: {validation_reason(e)}") from e


def dump(path: Path, data: AgentFile) -> str:
    """path 要写成 data 时的全文：已有的文件在原文上改（保留注释与别的排版），没有就新建。同步 IO。"""
    doc = tomlkit.parse(path.read_text(encoding="utf-8")) if path.is_file() else tomlkit.document()
    # 每个字段都写（按 AgentFile 的字段，加字段不用改这里）；值为 None 的（不分组）就是没有这个键
    for key, value in data.model_dump().items():
        if value is None:
            doc.pop(key, None)
        else:
            doc[key] = value
    return tomlkit.dumps(doc)
