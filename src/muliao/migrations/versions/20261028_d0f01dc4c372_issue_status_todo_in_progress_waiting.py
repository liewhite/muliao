"""issue 状态去掉 todo、加 waiting（#258）；RunEnded 事件带上会话来源

只动数据，不改表结构：
- issues.status：todo 且还有没完成（不是 done / cancelled）上游的改成 waiting，其余 todo 改成 in_progress。时间线里旧的
  status 动态（change 里的 "todo"）是历史，不改。
- 还没投递完的 session.run_ended 事件（tasks 里 kind=event 的投递任务）补上会话的 origin / origin_ref：RunEnded 新加了这两个
  必填字段，升级前已入队的投递按新模型解析会失败。

降级：waiting 改回 todo（in_progress 保持不变，分不出原来是不是 todo）；删掉 unsettled 动态；补上的事件字段删掉。

Revision ID: d0f01dc4c372
Revises: 20676599edfd
Create Date: 2026-10-28 00:38:54.290384
"""

from collections.abc import Sequence

from alembic import op

revision: str = "d0f01dc4c372"
down_revision: str | None = "20676599edfd"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_UNFINISHED_UPSTREAM = (
    "EXISTS (SELECT 1 FROM issue_dependencies d JOIN issues u ON u.id = d.depends_on_id "
    "WHERE d.issue_id = issues.id AND u.status NOT IN ('done', 'cancelled'))"
)

# 没成功的都补：关停时还在执行的（running）启动后由 worker 复位重做，failed 的可以在任务页人工重试。会话已删除的不补
# （解析失败、投递记为失败，在任务页可见；不替它编造来源）
_RUN_ENDED = (
    "kind = 'event' AND status IN ('pending', 'running', 'failed') "
    "AND json_extract(payload, '$.type') = 'session.run_ended' "
    "AND EXISTS (SELECT 1 FROM sessions WHERE id = json_extract(tasks.payload, '$.data.session_id'))"
)


def upgrade() -> None:
    op.execute(f"UPDATE issues SET status = 'waiting' WHERE status = 'todo' AND {_UNFINISHED_UPSTREAM}")
    op.execute("UPDATE issues SET status = 'in_progress' WHERE status = 'todo'")
    op.execute(
        "UPDATE tasks SET payload = json_set(payload, "
        "'$.data.origin', (SELECT origin FROM sessions WHERE id = json_extract(tasks.payload, '$.data.session_id')), "
        "'$.data.origin_ref', (SELECT origin_ref FROM sessions WHERE id = json_extract(tasks.payload, '$.data.session_id'))"
        f") WHERE {_RUN_ENDED}"
    )


def downgrade() -> None:
    op.execute("UPDATE issues SET status = 'todo' WHERE status = 'waiting'")
    # 旧代码不认识 unsettled（StrEnumType 遇到未知取值报错）：它只是请 agent 表态的提示，删掉不丢业务事实
    op.execute("DELETE FROM issue_activities WHERE kind = 'unsettled'")
    op.execute(
        f"UPDATE tasks SET payload = json_remove(payload, '$.data.origin', '$.data.origin_ref') WHERE {_RUN_ENDED}"
    )
