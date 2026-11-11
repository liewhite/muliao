"""agent 领域的公开接口：别的领域只经这里（与 agents.events）使用 agent 领域（docs/architecture.md 2.3）。领域内分目录、分发、
资源三个子域（docs/architecture.md 2.1）；AI 资源都是资源目录里的文件（agents.assets），AssetSource 给出当前快照。

- 查询：AgentService 的 get / get_active / find_by_name / list，事务内的 agent_in / active_agent_in / runnable_agent_in（归档的报
  AgentArchived），返回 AgentInfo（身份）；要按情况分别处理、不想接异常的投递方用 availability_in（Availability）；
  配置（描述、模型、思考档位、分组）只在快照：AssetSource.snapshot().loaded_agent 给出 AgentDir，界面与 get_agent 用
  AgentService.view / find_view / list_views（AgentView：身份 + 配置 + 分发规则）；
  名称映射 AgentService.names / 事务内的 agent_names_in（AgentNames，已删除的 agent 也有展示名）；
- 删除与归档 agent（AgentLifecycle；别的领域拦下删除的检查 DeletionBlocker）；组合根经 AgentDomain.build 装配它与 agent 领域的工具、常驻循环；
- AI 资源：种类（kinds：Kind / StoredKind），资源目录（AssetSource：当前快照与读树时发现的问题，改写资源文件的写入口 write 与它的结果 Written、拒绝 AssetRejected；讲怎么改资源文件的公共 skill 名 ASSETS_SKILL），记忆（MemoryStore、一次运行放进指令的 MemoryBlock、MemoryInfo）、skill 的索引、界面编辑与标准 .skill 文件的导入导出，MCP server 的配置（McpServer 等；把配置连接成工具在 agent_runtime）、secret（Vault：存取、加解密与 ${vault:KEY} 引用的解析 VAULT_REF / fill，对外不出密文）、资源池（AssetCatalog：全部资源与各自的详情 CatalogAsset.detail、删除、转移 owner）；
- 分发（Distribution：角色的增删改、agent 的规则 AgentRules 与最终规则、能用的 Reach、预览），内置工具表（ToolTable：组合根装好工具集后 bind，
  ToolSpec）与 `action:kind:name` 规则（permissions）；AI 资产的规则在它的 access（能 use / write = 自己 own 的 ∪ 规则放行的）。
"""

from muliao.agents.access import Reach, Source, WriteScope
from muliao.agents.active import AgentArchived, AgentNames, Availability, agent_names_in, availability_in
from muliao.agents.agent_file import Thinking
from muliao.agents.assembly import AgentDomain
from muliao.agents.asset_files import SKILL as ASSETS_SKILL
from muliao.agents.asset_files import AssetRejected
from muliao.agents.assets import AgentDir, McpFile, MemoryText, SkillDir
from muliao.agents.catalog import AssetCatalog, CatalogAsset, SecretDetail
from muliao.agents.distribution import Distribution, RoleSpec, RoleView, UsedAsset
from muliao.agents.kinds import Kind, StoredKind
from muliao.agents.lifecycle import AgentLifecycle, DeletionBlocker
from muliao.agents.mcp import McpRegistry
from muliao.agents.mcp_config import McpConfig, McpServer, McpTransport, mcp_config
from muliao.agents.memory import MemoryBlock, MemoryInfo, MemoryStore
from muliao.agents.naming import USER
from muliao.agents.permissions import BASE, AgentRules, Rules, ToolSpec, ToolTable
from muliao.agents.secrets import VAULT_REF, Vault, fill
from muliao.agents.service import (
    AgentInfo,
    AgentPatch,
    AgentService,
    AgentSpec,
    AgentView,
    active_agent_in,
    agent_in,
    runnable_agent_in,
)
from muliao.agents.skill_package import MAX_PACKAGE_BYTES
from muliao.agents.skills import SkillImport, SkillMeta, SkillScripts, SkillStore
from muliao.agents.source import AssetSource, Written

__all__ = [
    "Source",
    "WriteScope",
    "UsedAsset",
    "AgentArchived",
    "Availability",
    "availability_in",
    "AgentLifecycle",
    "AgentDomain",
    "DeletionBlocker",
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
    "AgentDir",
    "AgentView",
    "AgentNames",
    "AgentRules",
    "AgentService",
    "CatalogAsset",
    "SecretDetail",
    "McpFile",
    "MemoryText",
    "SkillDir",
    "AssetCatalog",
    "Reach",
    "VAULT_REF",
    "fill",
    "StoredKind",
    "MemoryBlock",
    "MemoryInfo",
    "MemoryStore",
    "AssetSource",
    "AssetRejected",
    "Written",
    "ASSETS_SKILL",
    "Vault",
    "MAX_PACKAGE_BYTES",
    "SkillImport",
    "SkillMeta",
    "SkillScripts",
    "SkillStore",
    "Kind",
    "ToolSpec",
    "ToolTable",
    "Distribution",
    "RoleSpec",
    "RoleView",
    "Rules",
    "BASE",
    "active_agent_in",
    "agent_in",
    "agent_names_in",
    "runnable_agent_in",
]
