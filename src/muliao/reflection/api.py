"""反思领域的公开接口（HTTP 层用）：反思记录与步数的查询。调度（订阅运行结束、投递反思）由组合根装配，不公开。"""

from muliao.reflection.service import ReflectionLog

__all__ = ["ReflectionLog"]
