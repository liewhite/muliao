"""issue 领域接收的命令。"""

from muliao.platform.messaging import Command


class PostIssueEvent(Command):
    """把一个事件记进 issue 的时间线并按 issue 的规则投递（触发器领域的投递目标 issue:N）。发送方（如触发器）在自己的
    事务里用 issues.api.issue_exists_in 确认 issue 存在、记下自己的状态，同一事务发出它；issue 领域在自己的事务里记一条
    EVENT 动态、投给接手的 agent（没人接手时给 owner，同用户评论）。投递时 issue 已被删除就丢弃，记日志。"""

    TYPE = "issues.post_event"
    issue_id: int
    # 事件来源所属的 agent：时间线上的 actor
    agent_id: str
    # 来源的描述，时间线上显示（如 "trigger slack-live"）
    source: str
    body: str
