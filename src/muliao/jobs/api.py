"""后台任务领域的公开接口：agent 在后台跑的 shell 命令（JobRunner：启动、结束、查询、运行中检查）。会话经占用与
WorkEnded / WorkCheckIn（sessions.events）得知它们的结局，不调用这里。"""

from muliao.jobs.runner import JobInfo, JobRunner

__all__ = ["JobInfo", "JobRunner"]
