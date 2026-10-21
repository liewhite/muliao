"""反思领域的公开接口（HTTP 层用）：反思记录与步数的查询，反思的任务种类（REFLECTION_TASK）。调度（订阅运行结束、
投递反思）由组合根装配，不公开。"""

from muliao.reflection.service import REFLECTION_TASK, ReflectionLog

__all__ = ["REFLECTION_TASK", "ReflectionLog"]
