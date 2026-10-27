"""agent 领域的公开接口：别的领域只经这里（与 agents.events）使用 agent 领域（docs/architecture.md 2.3）。领域内分目录、分发、
条目、记忆、版本五个子域（docs/architecture.md 2.1）。

- 查询：AgentService 的 get / get_active / find_by_name / list，事务内的 agent_in / active_agent_in / runnable_agent_in（归档的报
  AgentArchived），返回 AgentInfo；
  名称映射 AgentService.names / 事务内的 agent_names_in（AgentNames，已删除的 agent 也有展示名）；
- 删除与归档 agent（AgentLifecycle）；
- 条目与记忆：种类（kinds：Kind / StoredKind / Subject），记忆（全局与 agent 的）、skill 的存取与标准 .skill 文件的导入导出（自我改进工具与 instructions 组装用），MCP server 的配置（McpServer 等；把配置连接成工具在 agent_runtime）、secret（Vault：存取、加解密与 ${vault:KEY} 引用的解析 VAULT_REF / fill，对外不出密文；SECRET_TOOL_ARGS：要脱敏的工具参数）、条目池（ItemCatalog：全部条目、转移、版本历史与恢复）、版本（RevisionInfo：ItemCatalog.history 与 MemoryStore.history 读出，角色与 agent 规则的版本由 knowledge-upkeep 的 revisions.py 只读查询）；
- 分发（Distribution：角色的增删改、agent 的规则 AgentRules 与最终规则、能用的 Reach、预览），内置工具的登记与
  `kind:name` 规则（permissions）；条目的两条规则在它的 access（能用 = 自己的 ∪ 规则放行的；能改 = owner 或 admin）。
"""

from muliao.agents.access import Reach
from muliao.agents.active import AgentArchived, AgentNames, agent_names_in
from muliao.agents.agent_rules import AgentRules
from muliao.agents.catalog import CatalogEntry, ItemCatalog
from muliao.agents.distribution import Distribution, RoleSpec, RoleView
from muliao.agents.ids import USER
from muliao.agents.kinds import Kind, StoredKind, Subject
from muliao.agents.lifecycle import AgentLifecycle
from muliao.agents.mcp import McpConfig, McpRegistry, McpServer, McpTransport, mcp_config
from muliao.agents.memory import MemoryStore
from muliao.agents.permissions import BASE, TOOLS, Access, Role, Rules, Tool, ToolSpec
from muliao.agents.revisions import RevisionInfo
from muliao.agents.secret_tools import SECRET_TOOL_ARGS
from muliao.agents.secrets import VAULT_REF, SecretInfo, Vault, fill
from muliao.agents.service import (
    AgentInfo,
    AgentPatch,
    AgentService,
    AgentSpec,
    Thinking,
    active_agent_in,
    agent_in,
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
    "AgentRules",
    "AgentService",
    "CatalogEntry",
    "ItemCatalog",
    "Reach",
    "SECRET_TOOL_ARGS",
    "VAULT_REF",
    "fill",
    "StoredKind",
    "MemoryStore",
    "RevisionInfo",
    "Subject",
    "SecretInfo",
    "Vault",
    "MAX_PACKAGE_BYTES",
    "SkillImport",
    "SkillMeta",
    "SkillStore",
    "Access",
    "Kind",
    "Tool",
    "ToolSpec",
    "Role",
    "Distribution",
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
