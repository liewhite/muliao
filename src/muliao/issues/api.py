"""issue 领域的公开接口：HTTP 适配层与组合根经这里使用它；组合根经 IssueDomain.build 装配。"""

from muliao.issues.activity import HAPPENINGS, Activity
from muliao.issues.assembly import IssueDomain
from muliao.issues.conversation import recipient as issue_recipient
from muliao.issues.rules import SESSION_NOTE
from muliao.issues.service import IssueDetail, IssueService, IssueWork, NewIssue, UserActions, issue_exists_in
from muliao.issues.tables import ActivityKind, IssuePriority, IssueStatus
from muliao.sessions.api import WorkState

__all__ = [
    "HAPPENINGS",
    "SESSION_NOTE",
    "Activity",
    "ActivityKind",
    "IssueDetail",
    "IssueDomain",
    "IssuePriority",
    "IssueService",
    "IssueStatus",
    "IssueWork",
    "NewIssue",
    "UserActions",
    "WorkState",
    "issue_exists_in",
    "issue_recipient",
]
