"""agent 可选用的工具组目录：组名、界面上的说明、是否等同全部权限、新建 agent 时是否默认开启，在这里唯一登记。

组名是 agent 配置的一部分（agents.tool_groups 存的就是它）；校验（AgentService）、界面（/api/agent-defaults）、给模型看的
可选组（get_agent）、组合根里每组的实现（runtime.runner.ToolGroups）都从这里取，不各存一份。依赖可选配置的组（web 依赖
[web]）只在配置了时出现（available）。等同全部权限的组见 docs/architecture.md「安全边界」：处理不可信输入的 agent 不该开。
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ToolGroupInfo:
    name: str
    label: str
    # 开了它就能间接做任何事（shell、改别的 agent、开临时 agent……），给不可信输入的 agent 开要谨慎
    full_access: bool
    # 新建 agent 时是否默认开启
    default: bool = True
    # 只在配置了 [web] 时存在
    needs_web: bool = False


WORKSPACE = "workspace"
PUBLIC_SKILLS = "public_skills"
MCP = "mcp"

CATALOG: tuple[ToolGroupInfo, ...] = (
    ToolGroupInfo(WORKSPACE, "文件与 shell", full_access=True),
    ToolGroupInfo("temp_agent", "临时 agent", full_access=True),
    ToolGroupInfo("delegation", "委派给其他 agent", full_access=True),
    ToolGroupInfo("notify", "推送通知", full_access=False),
    ToolGroupInfo("agents", "管理 agent 与公共指令", full_access=True),
    ToolGroupInfo(PUBLIC_SKILLS, "写公共 skill", full_access=False),
    ToolGroupInfo(MCP, "使用 MCP", full_access=False),
    ToolGroupInfo("mcp_admin", "管理 MCP", full_access=True),
    ToolGroupInfo("vault", "管理 vault", full_access=False),
    ToolGroupInfo("triggers", "管理触发器", full_access=True),
    ToolGroupInfo("web", "网页搜索与抓取", full_access=False, needs_web=True),
)

# 反思读的转录里可能有不可信输入：自我改进之外只开放该 agent 本来就有的写公共 skill
REFLECTION_GROUPS = frozenset({PUBLIC_SKILLS})


def available(web_configured: bool) -> tuple[ToolGroupInfo, ...]:
    """当前配置下存在的组，按目录顺序。"""
    return tuple(g for g in CATALOG if web_configured or not g.needs_web)
