"""agent 领域的公开接口：别的领域只经这里（与 agents.events）使用 agent 领域（docs/architecture.md 2.3）。

- 查询：AgentService 的 get / get_active / find_by_name / list，事务内的 agent_in / active_agent_in，返回 AgentInfo；
- 资源：指令、记忆、skill 的存取（自我改进工具与指令组装用），资源仓库（MCP 配置）；
- 归属规则：公共与 agent 私有的资源、secret 按同一套规则可见（ownership）；
- 工具组目录（tool_catalog）。
"""

from muliao.agents.memory import MemoryStore
from muliao.agents.naming import validate_slug
from muliao.agents.ownership import Scope, owned_by, owner_of, scope_of, shadowing, visible, visible_owners
from muliao.agents.prompts import PromptStore
from muliao.agents.resources import USER, ResourceRepo
from muliao.agents.service import AgentInfo, AgentPatch, AgentService, AgentSpec, active_agent_in, agent_in
from muliao.agents.skills import SkillMeta, SkillStore
from muliao.agents.tool_catalog import REFLECTION_GROUPS

__all__ = [
    "USER",
    "AgentPatch",
    "AgentSpec",
    "REFLECTION_GROUPS",
    "AgentInfo",
    "AgentService",
    "MemoryStore",
    "PromptStore",
    "ResourceRepo",
    "Scope",
    "SkillMeta",
    "SkillStore",
    "active_agent_in",
    "agent_in",
    "owned_by",
    "owner_of",
    "scope_of",
    "shadowing",
    "validate_slug",
    "visible",
    "visible_owners",
]
