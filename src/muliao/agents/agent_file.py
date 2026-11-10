"""`<agent 名>/agent.toml` 的格式：读（parse）与平台自己写（dump）在这一处。名字不在文件里：目录名就是名字；目录与数据库
里 agent 的对应在读树（agents.assets）。"""

from __future__ import annotations

import tomllib
from collections.abc import Mapping
from pathlib import Path

import tomlkit
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator

from muliao.agents.thinking import Thinking
from muliao.errors import InvalidInput
from muliao.platform.values import validation_reason

AGENT_FILE = "agent.toml"


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
    doc["id"] = data.id
    doc["description"] = data.description
    doc["model"] = data.model
    doc["thinking"] = data.thinking
    if data.group is None:
        doc.pop("group", None)
    else:
        doc["group"] = data.group
    return tomlkit.dumps(doc)
