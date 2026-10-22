"""agent 运行时的公开接口（docs/architecture.md 2.1）：会话与反思共用的端口——逐步驱动一次运行（结局为 Finished / Paused，工具用 defer_call 挂起；不交出模型库的运行类型）、模型消息的约定
（会话历史里各种标记的读写）、工具组合、工作区工具的组成部分（MCP 的配置归 agent 领域，见 agents.api）。别的领域只经这里使用它。"""

from muliao.agent_runtime.answers import Answer
from muliao.agent_runtime.attachments import copy_to_workspace, references
from muliao.agent_runtime.conventions import (
    INTERRUPTED_TOOL_RESULT,
    RUN_CONTEXT,
    RUN_EVENTS,
    RUN_STOPPED,
    STOPPED_TOOL_RESULT,
    TOOL_ATTACHMENTS,
    TOOL_THREADS,
    Attachment,
    answer_text,
    attach_run_context,
    calls_in,
    event_indices,
    format_event,
    is_stopped,
    made_up_result,
    parse_event,
    results_in,
    run_context_index,
    shared_attachments,
    tool_attachments,
    tool_threads,
    unanswered_calls,
)
from muliao.agent_runtime.instructions import with_notices
from muliao.agent_runtime.llm import ModelRegistry
from muliao.agent_runtime.outcome import Finished, Paused, defer_call
from muliao.agent_runtime.redaction import redact_messages
from muliao.agent_runtime.runner import AgentRuntime, RunProfile, StepHooks, ToolGroups
from muliao.agent_runtime.workspace_tools import ShellLimits, file_toolset, run_foreground, shell_description

__all__ = [
    "INTERRUPTED_TOOL_RESULT",
    "ModelRegistry",
    "RUN_CONTEXT",
    "RUN_EVENTS",
    "RUN_STOPPED",
    "STOPPED_TOOL_RESULT",
    "TOOL_ATTACHMENTS",
    "TOOL_THREADS",
    "AgentRuntime",
    "Answer",
    "Attachment",
    "Finished",
    "Paused",
    "RunProfile",
    "ShellLimits",
    "StepHooks",
    "ToolGroups",
    "answer_text",
    "attach_run_context",
    "calls_in",
    "copy_to_workspace",
    "defer_call",
    "event_indices",
    "file_toolset",
    "format_event",
    "is_stopped",
    "made_up_result",
    "parse_event",
    "redact_messages",
    "references",
    "results_in",
    "run_context_index",
    "run_foreground",
    "shell_description",
    "shared_attachments",
    "tool_attachments",
    "tool_threads",
    "unanswered_calls",
    "with_notices",
]
