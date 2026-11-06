"""工作区领域的公开接口（docs/architecture.md 2.1）。工作区领域拥有 agent 在自己工作目录里用的工具：文件工具
（read/write/edit_file、share_file）、run_shell（前台与后台）、job_status / job_kill（工具集 tools.workspace_toolset，同各领域的
tools 由组合根装配），后台任务（jobs 表；JobRunner：启动、结束、查询、运行中检查），以及界面对工作目录的只读浏览
（WorkspaceFiles）与 agent 自定义页面（Sites，工作目录下 site/），还有 agent 能写哪里（Confiner：命令与文件工具的可写路径）。会话经占用与 WorkEnded /
WorkCheckIn（sessions.events）得知后台任务的结局，不调用这里。"""

from muliao.workspace.browse import Entry, WorkspaceFiles
from muliao.workspace.file_tools import ShellLimits
from muliao.workspace.runner import JobAdmin, JobInfo, JobRunner
from muliao.workspace.sites import SitePage, Sites
from muliao.workspace.writable import Confiner, denial_note

__all__ = [
    "Confiner",
    "denial_note",
    "Entry",
    "JobAdmin",
    "JobInfo",
    "JobRunner",
    "ShellLimits",
    "SitePage",
    "Sites",
    "WorkspaceFiles",
]
