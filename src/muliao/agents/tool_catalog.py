"""agent 可选用的工具组目录：组名、界面上的说明、新建 agent 时是否默认开启，在这里唯一登记。

组名是 agent 配置的一部分（agents.tool_groups 存的就是它）；校验（AgentService）、界面（/api/agent-defaults）、给模型看的
可选组（get_agent，连同每组实际注册的工具）、组合根里每组的实现（agent_runtime.runner.ToolGroups）都从这里取，不各存一份。
依赖可选配置的组（web 依赖 [web]）只在配置了时出现（available）。
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ToolGroupInfo:
    name: str
    label: str
    # 新建 agent 时是否默认开启
    default: bool = True
    # 只在配置了 [web] 时存在
    needs_web: bool = False


WORKSPACE = "workspace"
AGENTS = "agents"
PUBLIC_SKILLS = "public_skills"
MCP = "mcp"

CATALOG: tuple[ToolGroupInfo, ...] = (
    ToolGroupInfo(WORKSPACE, "文件与 shell"),
    ToolGroupInfo("temp_agent", "临时 agent"),
    ToolGroupInfo("delegation", "委派给其他 agent"),
    ToolGroupInfo("notify", "推送通知"),
    ToolGroupInfo(AGENTS, "管理 agent 与公共指令"),
    ToolGroupInfo(PUBLIC_SKILLS, "写公共 skill"),
    ToolGroupInfo(MCP, "使用 MCP"),
    ToolGroupInfo("mcp_admin", "管理 MCP"),
    ToolGroupInfo("vault", "管理 vault"),
    ToolGroupInfo("triggers", "管理触发器"),
    ToolGroupInfo("issues", "issue 看板"),
    ToolGroupInfo("web", "网页搜索与抓取", needs_web=True),
)

# 反思读的转录里可能有不可信输入：自我改进之外只开放该 agent 本来就有的写公共 skill
REFLECTION_GROUPS = frozenset({PUBLIC_SKILLS})


def available(web_configured: bool) -> tuple[ToolGroupInfo, ...]:
    """当前配置下存在的组，按目录顺序。"""
    return tuple(g for g in CATALOG if web_configured or not g.needs_web)
