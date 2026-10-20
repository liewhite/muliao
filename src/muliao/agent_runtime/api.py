"""agent 运行时的公开接口（docs/domain-design.md 4.9）：会话与反思共用的端口——逐步驱动一次运行、模型消息的约定
（会话历史里各种标记的读写）、工具组合、工作区工具的组成部分。别的领域只经这里使用它。"""

from muliao.agent_runtime.answers import Answer
from muliao.agent_runtime.attachments import copy_to_workspace, references
from muliao.agent_runtime.conventions import (
    RUN_EVENTS,
    RUN_STOPPED,
    STOPPED_TOOL_RESULT,
    TOOL_ATTACHMENTS,
    TOOL_THREADS,
    Attachment,
    answer_text,
    calls_in,
    event_indices,
    format_event,
    is_stopped,
    parse_event,
    results_in,
    shared_attachments,
    tool_attachments,
    tool_threads,
    unanswered_calls,
)
from muliao.agent_runtime.mcp import McpServer, McpTransport, mcp_config
from muliao.agent_runtime.redaction import redact_messages
from muliao.agent_runtime.runner import AgentRuntime, RunProfile, StepHooks, TextKind, ToolGroups
from muliao.agent_runtime.workspace_tools import SHELL_DESCRIPTION, ShellLimits, file_toolset, run_foreground

__all__ = [
    "McpServer",
    "McpTransport",
    "mcp_config",
    "RUN_EVENTS",
    "RUN_STOPPED",
    "SHELL_DESCRIPTION",
    "STOPPED_TOOL_RESULT",
    "TOOL_ATTACHMENTS",
    "TOOL_THREADS",
    "AgentRuntime",
    "Answer",
    "Attachment",
    "RunProfile",
    "ShellLimits",
    "StepHooks",
    "TextKind",
    "ToolGroups",
    "answer_text",
    "calls_in",
    "copy_to_workspace",
    "event_indices",
    "file_toolset",
    "format_event",
    "is_stopped",
    "parse_event",
    "redact_messages",
    "references",
    "results_in",
    "run_foreground",
    "shared_attachments",
    "tool_attachments",
    "tool_threads",
    "unanswered_calls",
]
