"""agent 领域的公开接口：别的领域只经这里（与 agents.events）使用 agent 领域（docs/architecture.md 2.3）。

- 查询：AgentService 的 get / get_active / find_by_name / list，事务内的 agent_in / active_agent_in，返回 AgentInfo；
  名称映射 AgentService.names / 事务内的 agent_names_in（AgentNames，已删除的 agent 也有展示名）；
- 删除 agent（AgentLifecycle）；
- 资源：指令、记忆、skill 的存取（自我改进工具与指令组装用），MCP server 的配置（McpServer 等；把配置连接成工具在 agent_runtime）、资源的版本历史（RevisionHistory，只读）；
- 归属规则：公共与 agent 私有的资源、secret 按同一套规则可见（ownership）；
- 工具组目录（tool_catalog）。
"""

from muliao.agents.lifecycle import AgentLifecycle
from muliao.agents.mcp import McpConfig, McpRegistry, McpServer, McpTransport, mcp_config
from muliao.agents.memory import MemoryStore
from muliao.agents.ownership import Scope, owned_by, owner_of, scope_of, shadowing, visible, visible_owners
from muliao.agents.prompts import PromptStore
from muliao.agents.resources import USER, CorruptResource, Revision, RevisionHistory
from muliao.agents.service import (
    AgentInfo,
    AgentNames,
    AgentPatch,
    AgentService,
    AgentSpec,
    active_agent_in,
    agent_in,
    agent_names_in,
)
from muliao.agents.skills import SkillMeta, SkillStore
from muliao.agents.tool_catalog import REFLECTION_GROUPS, ToolGroupInfo

__all__ = [
    "AgentLifecycle",
    "McpConfig",
    "McpRegistry",
    "McpServer",
    "McpTransport",
    "mcp_config",
    "USER",
    "AgentPatch",
    "AgentSpec",
    "REFLECTION_GROUPS",
    "AgentInfo",
    "AgentNames",
    "CorruptResource",
    "AgentService",
    "MemoryStore",
    "PromptStore",
    "Revision",
    "RevisionHistory",
    "Scope",
    "SkillMeta",
    "SkillStore",
    "ToolGroupInfo",
    "active_agent_in",
    "agent_in",
    "agent_names_in",
    "owned_by",
    "owner_of",
    "scope_of",
    "shadowing",
    "visible",
    "visible_owners",
]
