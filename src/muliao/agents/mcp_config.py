"""MCP server 的连接配置（McpConfig）与一个已保存的 server（McpServer）：资源文件 `mcp/<名>.json` 的格式（agents.assets 读，
agents.mcp 写），传输方式与字段的搭配在这里校验，读写同一条规则。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, ValidationError, model_validator

from muliao.errors import InvalidInput


class McpTransport(StrEnum):
    STDIO = "stdio"
    HTTP = "http"


class McpConfig(BaseModel):
    """一个 MCP server 的连接配置，传输方式与字段的搭配在这里校验，写入与读回同一条规则。字段全部
    必填：调用方输入与文件里可以省略的字段由 mcp_config、parse_mcp_file 补上。"""

    model_config = ConfigDict(extra="forbid", strict=True)

    transport: McpTransport
    command: str | None
    args: list[str]
    url: str | None
    env: dict[str, str]
    headers: dict[str, str]

    @model_validator(mode="after")
    def _fits_transport(self) -> McpConfig:
        if self.transport == McpTransport.STDIO:
            if not self.command or self.url or self.headers:
                raise ValueError("stdio MCP server requires 'command' and must not set 'url' or 'headers'")
        elif not self.url or self.command or self.args or self.env:
            raise ValueError("http MCP server requires 'url' and must not set 'command', 'args' or 'env'")
        elif not self.url.startswith(("http://", "https://")):
            raise ValueError("MCP url must start with http:// or https://")
        return self


def mcp_config(
    *,
    transport: McpTransport,
    command: str | None = None,
    args: list[str] | None = None,
    url: str | None = None,
    env: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
) -> McpConfig:
    """调用方（用户、agent）给出的新配置，可以省略与传输方式无关的字段。不合法是输入错误。"""
    fields = {
        "transport": transport,
        "command": command,
        "args": args or [],
        "url": url,
        "env": env or {},
        "headers": headers or {},
    }
    try:
        return McpConfig.model_validate(fields)
    except ValidationError as e:
        raise InvalidInput(f"invalid MCP server config: {e}") from e


@dataclass(frozen=True)
class McpServer:
    """一个已保存的 MCP server。env / headers / args / url 的值可以包含 ${vault:KEY}，按用它的 agent 能用的 secret 解析。"""

    owner: str | None
    name: str
    config: McpConfig
    enabled: bool

    @property
    def transport(self) -> McpTransport:
        return self.config.transport


def parse_mcp_file(text: str) -> tuple[McpConfig, bool]:
    """`mcp/<名>.json` 的内容：McpConfig 的字段（与传输方式无关的可以省略）加上可选的 `enabled`（缺省为真）。不合规则时
    InvalidInput。"""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        raise InvalidInput(f"not valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise InvalidInput("must be a JSON object")
    enabled = data.pop("enabled", True)
    if not isinstance(enabled, bool):
        raise InvalidInput("'enabled' must be true or false")
    fields = {"command": None, "args": [], "url": None, "env": {}, "headers": {}, **data}
    try:
        # JSON 模式：strict 下枚举只从 JSON 文本按值解析
        return McpConfig.model_validate_json(json.dumps(fields)), enabled
    except ValidationError as e:
        raise InvalidInput(f"invalid MCP server config: {e}") from e


def mcp_file(config: McpConfig, enabled: bool) -> str:
    """parse_mcp_file 的反面：写进 `mcp/<名>.json` 的文本（停用时才写 enabled）。"""
    data = config.model_dump(mode="json", exclude_defaults=False)
    if not enabled:
        data["enabled"] = False
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"
