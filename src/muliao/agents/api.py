"""agent 领域的公开接口：别的领域只经这里（与 agents.events）使用 agent 领域（docs/architecture.md 2.3）。

- 查询：AgentService 的 get / get_active / find_by_name / list，事务内的 agent_in / active_agent_in，返回 AgentInfo；
  名称映射 AgentService.names / 事务内的 agent_names_in（AgentNames，已删除的 agent 也有展示名）；
- 删除 agent（AgentLifecycle）；
- 资源：记忆（全局与 agent 的）、skill 的存取与标准 .skill 文件的导入导出（自我改进工具与 instructions 组装用），MCP server 的配置（McpServer 等；把配置连接成工具在 agent_runtime）、资源的版本历史（RevisionHistory，只读）；
- 归属规则：公共与 agent 私有的资源、secret 按同一套规则可见（ownership）；
- 工具权限（permissions：每个工具的权限 id 与读写、规则、内置角色 admin / agent）与角色（roles：内置加用户自建）。
"""

from muliao.agents.lifecycle import AgentLifecycle
from muliao.agents.mcp import McpConfig, McpRegistry, McpServer, McpTransport, mcp_config
from muliao.agents.memory import MemoryStore
from muliao.agents.ownership import Scope, owned_by, owner_of, scope_of, shadowing, visible, visible_owners
from muliao.agents.permissions import (
    TOOL_PERMISSIONS,
    Access,
    Permission,
    Resolved,
    Role,
    Rules,
)
from muliao.agents.resources import USER, CorruptResource, Revision, RevisionHistory
from muliao.agents.roles import RoleService, RoleSpec
from muliao.agents.service import (
    AgentInfo,
    AgentNames,
    AgentPatch,
    AgentService,
    AgentSpec,
    Thinking,
    active_agent_in,
    agent_in,
    agent_names_in,
)
from muliao.agents.skill_package import MAX_PACKAGE_BYTES
from muliao.agents.skills import SkillImport, SkillMeta, SkillStore

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
    "Thinking",
    "AgentInfo",
    "AgentNames",
    "CorruptResource",
    "AgentService",
    "MemoryStore",
    "Revision",
    "RevisionHistory",
    "Scope",
    "MAX_PACKAGE_BYTES",
    "SkillImport",
    "SkillMeta",
    "SkillStore",
    "Access",
    "Permission",
    "Resolved",
    "Role",
    "RoleService",
    "RoleSpec",
    "Rules",
    "TOOL_PERMISSIONS",
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
