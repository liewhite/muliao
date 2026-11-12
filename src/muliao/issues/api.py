"""issue 领域的公开接口：HTTP 适配层与组合根经这里使用它；组合根经 IssueDomain.build 装配。"""

from muliao.issues.activity import HAPPENINGS, Activity
from muliao.issues.assembly import IssueDomain
from muliao.issues.service import IssueDetail, IssueService, IssueWork, NewIssue, UserActions
from muliao.issues.tables import ActivityKind, IssuePriority, IssueStatus

__all__ = [
    "HAPPENINGS",
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
]
