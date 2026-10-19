"""组装每次运行的 instructions。纯函数，便于测试。"""

from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from muliao.evolution.skills import SkillMeta
from muliao.models import Origin, ReplyTo

_ORIGIN_NOTES: dict[Origin, str] = {
    Origin.USER: "The user started this conversation in the app and may be reading along.",
    Origin.CRON: "This run was started by a schedule; nobody is watching.",
    Origin.EVENT: (
        "This run was started by an external event (inside <event>); nobody is watching. "
        "A new event starts a fresh conversation unless its sender continues this one, so keep anything "
        "unrelated events need in memory or workspace files."
    ),
    Origin.DELEGATION: "Another agent opened this conversation with you.",
}

_REPLY_NOTES: dict[ReplyTo, str] = {
    ReplyTo.USER: "The user is notified with your final answer and may reply here.",
    ReplyTo.AGENT: (
        "Another agent is waiting for your final answer, which is returned to it as a tool result; "
        "answer it directly and completely. The user is not notified."
    ),
    ReplyTo.NONE: (
        "The user is NOT notified automatically: call `notify_user` only when there is something worth "
        "their attention."
    ),
}


SELF_IMPROVEMENT = """\
## Self-improvement
- Your memory is private and limited to {memory_limit} characters. Record durable facts (user preferences, \
environment details, corrections, lessons learned) with `memory_append`; fix or condense with `memory_replace`. \
Never record secrets or one-off details.
- Skills are reusable procedures. Before a task that matches a skill in the index, call `read_skill`. After \
working out a non-trivial procedure likely to recur, or finding a skill wrong or incomplete, save it with \
`write_skill` (private unless every agent would benefit).
- Secrets go in the vault (`vault_set`), referenced by key; never write secret values into files, memory or skills.
- Keep state you need across runs (cursors, caches) in files in your workspace."""


def build_instructions(
    *,
    agent_name: str,
    agent_description: str,
    public_instructions: str,
    agent_instructions: str,
    workspace: Path,
    now_local: datetime,
    origin: Origin | None,
    reply_to: ReplyTo | None,
    memory: str,
    skills: Sequence[SkillMeta],
    memory_limit: int,
    summary: str | None,
) -> str:
    context = [
        f"- You are agent {agent_name!r}: {agent_description}",
        f"- Current local time: {now_local.isoformat(timespec='minutes')} ({now_local.tzname()})",
        f"- Workspace (base for relative paths and shell cwd): {workspace}",
    ]
    if origin is not None:
        context.append(f"- {_ORIGIN_NOTES[origin]}")
    if reply_to is not None:
        context.append(f"- {_REPLY_NOTES[reply_to]}")
    skill_lines = [f"- `{s.name}` ({s.scope}): {s.description}" for s in skills] or ["(no skills yet)"]
    # 公共 prompt 在前、agent 自己的指令在后：具体的指令可以细化或覆盖通用规则
    sections = [
        public_instructions.strip(),
        agent_instructions.strip(),
        "## Runtime context\n" + "\n".join(context),
        f"## Memory\n<memory>\n{memory.strip() or '(empty)'}\n</memory>",
        "## Skill index\n" + "\n".join(skill_lines),
        SELF_IMPROVEMENT.format(memory_limit=memory_limit),
    ]
    if summary is not None:
        sections.append(
            "## Earlier in this session\nOlder messages of this session were compacted into this summary; "
            f"the messages after it follow in full.\n<summary>\n{summary}\n</summary>"
        )
    return "\n\n".join(s for s in sections if s)


TEMP_AGENT_INSTRUCTIONS = """\
You are a temporary helper agent working for agent {parent_name!r} on a single task. You have no memory and \
nothing you do is remembered: your final answer is returned to that agent as a tool result, so make it complete \
and self-contained. Do not ask questions back; if something is missing, say what and do what you can.

## Runtime context
- Current local time: {now} ({tz})
- Temporary workspace (base for relative paths and shell cwd, deleted when you finish): {workspace}
- Secrets are referenced by key as the agent you work for would (`run_shell(secrets=[KEY])`)."""


def build_temp_instructions(*, parent_name: str, workspace: Path, now_local: datetime, extra: str) -> str:
    base = TEMP_AGENT_INSTRUCTIONS.format(
        parent_name=parent_name,
        now=now_local.isoformat(timespec="minutes"),
        tz=now_local.tzname(),
        workspace=workspace,
    )
    return f"{extra.strip()}\n\n{base}" if extra.strip() else base
