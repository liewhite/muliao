"""组装每次运行的 instructions。纯函数，便于测试。"""

from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from muliao.evolution.skills import SkillMeta
from muliao.models import Origin

_ORIGIN_NOTES: dict[Origin, str] = {
    Origin.USER: "The user started this conversation in the app and may be reading along.",
    Origin.CRON: (
        "This run was started by a schedule; nobody is watching. The user is NOT notified automatically: "
        "call `notify_user` only when there is something worth their attention."
    ),
    Origin.WEBHOOK: "This run was started by an external event. The user is notified with your final answer.",
    Origin.TELEGRAM: "The user is talking to you through Telegram; your final answer is sent back there.",
    Origin.DELEGATION: (
        "Another agent delegated this task to you. The user is notified with your final answer "
        "and may reply in this session."
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
    agent_instructions: str,
    workspace: Path,
    now_local: datetime,
    origin: Origin | None,
    memory: str,
    skills: Sequence[SkillMeta],
    memory_limit: int,
    summary: str | None,
) -> str:
    context = [
        f"- You are agent `{agent_name}`: {agent_description}",
        f"- Current local time: {now_local.isoformat(timespec='minutes')} ({now_local.tzname()})",
        f"- Workspace (base for relative paths and shell cwd): {workspace}",
    ]
    if origin is not None:
        context.append(f"- {_ORIGIN_NOTES[origin]}")
    skill_lines = [f"- `{s.name}` ({s.scope}): {s.description}" for s in skills] or ["(no skills yet)"]
    sections = [
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
