"""agent 领域的公开接口：别的领域只经这里（与 agents.events）使用 agent 领域（docs/architecture.md 2.3）。

- 查询：AgentService 的 get / get_active / find_by_name / list，事务内的 agent_in / active_agent_in / runnable_agent_in（归档的报
  AgentArchived），返回 AgentInfo；
  名称映射 AgentService.names / 事务内的 agent_names_in（AgentNames，已删除的 agent 也有展示名）；
- 删除与归档 agent（AgentLifecycle）；
- 条目与记忆：种类（kinds：Kind / StoredKind / Subject），记忆（全局与 agent 的）、skill 的存取与标准 .skill 文件的导入导出（自我改进工具与 instructions 组装用），MCP server 的配置（McpServer 等；把配置连接成工具在 agent_runtime）、secret 的密文存放（SecretItems，加解密在 vault）、条目池（ItemCatalog：全部条目、预览、转移、版本历史与恢复）、版本（Revisions，只读）；
- 条目的两条规则（access：能用 = 自己的 ∪ 规则放行的；能改 = owner 或 admin）；
- 分发规则（permissions：内置工具的登记、`kind:name` 规则、内置角色 base / admin）与角色（roles：内置加用户自建）。
"""

from muliao.agents.active import AgentArchived
from muliao.agents.catalog import CatalogEntry, ItemCatalog
from muliao.agents.items import Reach
from muliao.agents.kinds import Kind, StoredKind, Subject
from muliao.agents.lifecycle import AgentLifecycle
from muliao.agents.mcp import McpConfig, McpRegistry, McpServer, McpTransport, mcp_config
from muliao.agents.memory import MemoryStore
from muliao.agents.permissions import BASE, TOOLS, Access, Role, Rules, Tool
from muliao.agents.revisions import USER, RevisionInfo
from muliao.agents.role_service import RoleService, RoleSpec, RoleView
from muliao.agents.secrets import KEY_SYNTAX as SECRET_KEY_SYNTAX
from muliao.agents.secrets import SecretItems, StoredSecret
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
    runnable_agent_in,
)
from muliao.agents.skill_package import MAX_PACKAGE_BYTES
from muliao.agents.skills import SkillImport, SkillMeta, SkillStore

__all__ = [
    "AgentArchived",
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
    "AgentService",
    "CatalogEntry",
    "ItemCatalog",
    "Reach",
    "SECRET_KEY_SYNTAX",
    "StoredKind",
    "MemoryStore",
    "RevisionInfo",
    "Subject",
    "SecretItems",
    "StoredSecret",
    "MAX_PACKAGE_BYTES",
    "SkillImport",
    "SkillMeta",
    "SkillStore",
    "Access",
    "Kind",
    "Tool",
    "Role",
    "RoleService",
    "RoleSpec",
    "RoleView",
    "Rules",
    "BASE",
    "TOOLS",
    "active_agent_in",
    "agent_in",
    "agent_names_in",
    "runnable_agent_in",
]
