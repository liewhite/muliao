"""会话领域自己命名的 lane（docs/architecture.md「任务队列即 actor 调度」）：lane 名在任务表里是全局的，会话领域命名的
都在这里定义，别的领域不拼它们（要问会话忙不忙，用 SessionActivity）。会话领域的任务种类也在这里定义（组合根为它们登记
处理器）。"""

from sqlalchemy import ColumnElement, literal

# 会话 lane 上的两种任务：一次运行（consumer.RunHandler）、一次手动压缩（compaction.CompactionHandler）
RUN_TASK = "run"
COMPACTION_TASK = "compaction"
# 停下一个 thread（它做的委派的发起方被停止了，sessions.halt）：不在 thread 自己的 lane 上，那里可能正有运行在跑
STOP_THREAD_TASK = "stop_thread"


_SESSION_LANE = "session:"


def session_lane(session_id: str) -> str:
    """会话 actor 的邮箱：运行、压缩都在这条 lane 上串行执行。"""
    return _SESSION_LANE + session_id


def session_lane_of(session_id: ColumnElement[str]) -> ColumnElement[str]:
    """session_lane 的 SQL 形式（按会话列关联任务的查询里用），同一个命名。"""
    return literal(_SESSION_LANE) + session_id


def stop_thread_lane(session_id: str) -> str:
    return f"sessions.stop:{session_id}"
