"""agent 运行时的公开接口（docs/architecture.md 2.1）：会话运行的端口——逐步驱动一次运行（结局为 Finished / Paused，工具用 defer_call 挂起；不交出模型库的运行类型）、运行时写下的消息约定
（工具交回给用户的文件与图片；会话自己的约定在 sessions.conventions）、工具组合（MCP 的配置归 agent 领域，见 agents.api）。
具体的工具在各自的领域（如 workspace 的文件与 shell、web）。别的领域只经这里使用它。"""

from muliao.agent_runtime.attachments import copy_to_workspace, references
from muliao.agent_runtime.conventions import (
    TOOL_ATTACHMENTS,
    Attachment,
    attachment_of,
    attachment_of_ref,
    result_images,
    shared_attachments,
    tool_attachments,
)
from muliao.agent_runtime.instructions import with_notices
from muliao.agent_runtime.llm import ModelRegistry
from muliao.agent_runtime.outcome import Finished, Paused, defer_call
from muliao.agent_runtime.redaction import redact_messages
from muliao.agent_runtime.runner import AgentRuntime, StepHooks
from muliao.agent_runtime.toolbox import RunProfile, Toolbox

__all__ = [
    "attachment_of",
    "attachment_of_ref",
    "ModelRegistry",
    "TOOL_ATTACHMENTS",
    "AgentRuntime",
    "Attachment",
    "Finished",
    "Paused",
    "RunProfile",
    "StepHooks",
    "Toolbox",
    "copy_to_workspace",
    "defer_call",
    "redact_messages",
    "references",
    "result_images",
    "shared_attachments",
    "tool_attachments",
    "with_notices",
]
