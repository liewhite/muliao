"""agent 领域的公开接口：别的领域只经这里（与 agents.events）使用 agent 领域（docs/architecture.md 2.3）。领域内分目录、分发、
资源三个子域（docs/architecture.md 2.1）；AI 资源都是资源目录里的文件（agents.assets），AssetSource 给出当前快照。

- 查询：AgentService 的 get / get_active / find_by_name / list，事务内的 agent_in / active_agent_in / runnable_agent_in（归档的报
  AgentArchived），返回 AgentInfo；
  名称映射 AgentService.names / 事务内的 agent_names_in（AgentNames，已删除的 agent 也有展示名）；
- 删除与归档 agent（AgentLifecycle）；
- AI 资源：种类（kinds：Kind / StoredKind），资源目录（AssetSource：当前快照与读树时发现的问题 Problem），记忆（MemoryStore、一次运行放进指令的 MemoryBlock、MemoryInfo）、skill 的索引、界面编辑与标准 .skill 文件的导入导出，MCP server 的配置（McpServer 等；把配置连接成工具在 agent_runtime）、secret（Vault：存取、加解密与 ${vault:KEY} 引用的解析 VAULT_REF / fill，对外不出密文；SECRET_TOOL_ARGS：要脱敏的工具参数）、资源池（ItemCatalog：全部资源、转移 owner）；
- 分发（Distribution：角色的增删改、agent 的规则 AgentRules 与最终规则、能用的 Reach、预览），内置工具的登记与
  `action:kind:name` 规则（permissions）；AI 资产的规则在它的 access（能 use / write = 自己 own 的 ∪ 规则放行的）。
"""

from muliao.agents.access import Reach, Source, WriteScope
from muliao.agents.active import AgentArchived, AgentNames, agent_names_in
from muliao.agents.agent_rules import AgentRules
from muliao.agents.assets import Problem
from muliao.agents.catalog import CatalogEntry, ItemCatalog
from muliao.agents.distribution import Distribution, RoleSpec, RoleView, UsedAsset
from muliao.agents.ids import USER
from muliao.agents.kinds import Kind, StoredKind
from muliao.agents.lifecycle import AgentLifecycle
from muliao.agents.mcp import McpRegistry
from muliao.agents.mcp_config import McpConfig, McpServer, McpTransport, mcp_config
from muliao.agents.memory import MemoryBlock, MemoryInfo, MemoryStore
from muliao.agents.permissions import BASE, TOOLS, Access, Role, Rules, Tool, ToolSpec
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
    named_agent_in,
    runnable_agent_in,
)
from muliao.agents.skill_package import MAX_PACKAGE_BYTES
from muliao.agents.skills import SkillImport, SkillMeta, SkillStore
from muliao.agents.source import AssetSource

__all__ = [
    "Source",
    "WriteScope",
    "UsedAsset",
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
    "MemoryBlock",
    "MemoryInfo",
    "MemoryStore",
    "AssetSource",
    "Problem",
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
    "named_agent_in",
    "agent_names_in",
    "runnable_agent_in",
]
