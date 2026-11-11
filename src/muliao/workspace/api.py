"""工作区领域的公开接口（docs/architecture.md 2.1）。工作区领域拥有 agent 在自己工作目录里用的工具：文件工具
（read/write/edit_file、share_file）、run_shell（前台与后台）、job_status / job_kill（工具集 tools.workspace_toolset，
由 WorkspaceDomain 交给组合根），后台任务（jobs 表；JobRunner：启动、结束、查询、运行中检查），以及界面对工作目录的只读浏览
（WorkspaceFiles；同一套 list_dir / file_in 也给界面浏览 skill 目录）与 agent 自定义页面（Sites，工作目录下 site/），用户对 skill 脚本的始终允许（ScriptGrants，#531），还有 agent 能写哪里（Confiner：命令与文件工具的可写路径），以及以 agent 身份起命令的唯一入口（AgentCommands：secret、工作目录、写沙箱与被拒时的说明，触发器的前置检查与 stream 也用它）。会话经占用与 WorkEnded /
WorkCheckIn（sessions.events）得知后台任务的结局，不调用这里。"""

from muliao.workspace.assembly import WorkspaceDomain
from muliao.workspace.browse import Entry, WorkspaceFile, WorkspaceFiles, file_in, list_dir
from muliao.workspace.commands import AgentCommands, AgentShell
from muliao.workspace.file_tools import ShellLimits
from muliao.workspace.grants import GrantInfo, ScriptGrantAdmin, ScriptGrants
from muliao.workspace.runner import JobAdmin, JobInfo, JobRunner
from muliao.workspace.sites import SitePage, Sites
from muliao.workspace.writable import Confiner

__all__ = [
    "AgentCommands",
    "AgentShell",
    "Confiner",
    "Entry",
    "file_in",
    "list_dir",
    "JobAdmin",
    "JobInfo",
    "JobRunner",
    "GrantInfo",
    "ScriptGrantAdmin",
    "ScriptGrants",
    "ShellLimits",
    "SitePage",
    "Sites",
    "WorkspaceDomain",
    "WorkspaceFile",
    "WorkspaceFiles",
]
