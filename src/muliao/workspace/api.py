"""工作区领域的公开接口（docs/architecture.md 2.1）。工作区领域拥有 agent 在自己工作目录里用的工具：文件工具
（read/write/edit_file、share_file）、run_shell（前台与后台）、job_status / job_kill（工具集 tools.workspace_toolset，同各领域的
tools 由组合根装配），以及后台任务（jobs 表；JobRunner：启动、结束、查询、运行中检查）。会话经占用与 WorkEnded /
WorkCheckIn（sessions.events）得知后台任务的结局，不调用这里。"""

from muliao.workspace.file_tools import ShellLimits
from muliao.workspace.runner import JobInfo, JobRunner

__all__ = ["JobInfo", "JobRunner", "ShellLimits"]
