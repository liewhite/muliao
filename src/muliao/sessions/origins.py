"""会话来源（Origin）：会话创建时定下、整个会话不变（存在 sessions.origin），以及每种来源在指令里怎么向 agent 说明。
说明是会话级的稳定内容，进 instructions（缓存前缀）：会话领域把它作为文字交给 agent_runtime，运行时不认识来源。"""

from enum import StrEnum


class Origin(StrEnum):
    """会话来源：用户在网页发起、触发器（定时、外部事件……）、agent 之间委派、issue 看板唤醒被指派的 agent。"""

    USER = "user"
    TRIGGER = "trigger"
    DELEGATION = "delegation"
    ISSUE = "issue"


_NOTES: dict[Origin, str] = {
    Origin.USER: "The user started this conversation in the app and may be reading along.",
    Origin.TRIGGER: (
        "This run was started by a trigger (a schedule, or an external event shown inside <event>); nobody is "
        "watching. Each firing starts a fresh conversation unless its sender continues this one, so keep anything "
        "later firings need in memory or workspace files."
    ),
    Origin.DELEGATION: "Another agent opened this conversation with you.",
    Origin.ISSUE: (
        "This conversation is dedicated to one issue on the muliao issue board that is assigned to you, and is "
        "continued each time the issue needs you again: while it is `todo` or `in_progress`, every change to it "
        "(comments, status, assignee, edits, a dependency finishing) except those you make here arrives here as a short message "
        "carrying only that new activity. Read the full issue (description, dependencies, whole timeline) with "
        "`issue_show` before you start, and again whenever the message is not enough. An issue that depends on "
        "others is delivered only once all of them are `done` or `cancelled`; a cancelled dependency may mean "
        "this issue is no longer needed. Work on it here, then record the outcome on the issue with the `issue_*` "
        "tools: comment with what you did or found (`issue_comment`), and move it on with `issue_update`, e.g. "
        "status `in_progress` while working, `blocked` with a comment explaining why. When you have finished, do "
        "not set `done` yourself: set status `in_review` and assignee `user`, and the user moves it to `done` "
        "after reviewing. Hand it to another agent by changing the assignee. Nobody is watching this "
        "conversation: the user reads the issue, not this conversation."
    ),
}


def origin_note(origin: Origin) -> str:
    """这个来源的会话在指令「Runtime context」里的那一行说明。"""
    return _NOTES[origin]
