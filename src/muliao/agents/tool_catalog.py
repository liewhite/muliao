"""agent 可选用的工具组目录：组名、界面上的说明，在这里唯一登记。新建 agent 未指定时开启当前配置下的全部组。

组名是 agent 配置的一部分（agents.tool_groups 存的就是它）；校验（AgentService）、界面（/api/agent-defaults）、给模型看的
可选组（get_agent，连同每组实际注册的工具）、组合根里每组的实现（agent_runtime.runner.ToolGroups）都从这里取，不各存一份。
依赖可选配置的组（web 依赖 [web]）只在配置了时出现（available）。
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ToolGroupInfo:
    name: str
    label: str
    # 只在配置了 [web] 时存在
    needs_web: bool = False


# 组名常量：组合根按它登记每组的实现，别处引用组名也用它，不写字面量
WORKSPACE = "workspace"
TEMP_AGENT = "temp_agent"
DELEGATION = "delegation"
NOTIFY = "notify"
AGENTS = "agents"
PUBLIC_SKILLS = "public_skills"
MCP = "mcp"
MCP_ADMIN = "mcp_admin"
VAULT = "vault"
TRIGGERS = "triggers"
ISSUES = "issues"
WEB = "web"

CATALOG: tuple[ToolGroupInfo, ...] = (
    ToolGroupInfo(WORKSPACE, "文件与 shell"),
    ToolGroupInfo(TEMP_AGENT, "临时 agent"),
    ToolGroupInfo(DELEGATION, "委派给其他 agent"),
    ToolGroupInfo(NOTIFY, "推送通知"),
    ToolGroupInfo(AGENTS, "管理 agent"),
    ToolGroupInfo(PUBLIC_SKILLS, "写公共 skill"),
    ToolGroupInfo(MCP, "使用 MCP"),
    ToolGroupInfo(MCP_ADMIN, "管理 MCP"),
    ToolGroupInfo(VAULT, "管理 vault"),
    ToolGroupInfo(TRIGGERS, "管理触发器"),
    ToolGroupInfo(ISSUES, "issue 看板"),
    ToolGroupInfo(WEB, "网页搜索与抓取", needs_web=True),
)


def available(web_configured: bool) -> tuple[ToolGroupInfo, ...]:
    """当前配置下存在的组，按目录顺序。"""
    return tuple(g for g in CATALOG if web_configured or not g.needs_web)
