"""组装一次运行的 instructions，以及每次运行开头那段运行上下文。纯函数，便于测试。

两者的分工只有一条规则：**instructions 只放同一个会话的不同运行之间不会变的内容，随运行变化的放进运行上下文**。
原因是缓存前缀——instructions 渲染成提供方的 system，缓存断点就放在它末尾（llm.ANTHROPIC_CACHE_SETTINGS），
DeepSeek 等自动前缀缓存同样从这里开始匹配。instructions 里只要有一个每次运行都不同的值（最早是「当前时间」），
整段历史每次运行都要重新写入缓存（写缓存比普通输入还贵），同一个 agent 的不同会话、反思、压缩也只能复用工具定义
那一段。运行上下文放进这次运行新增的第一条请求（conventions.attach_run_context），它随消息入库、之后原样重放，
旧消息保持字节不变。
"""

from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from muliao.agents.api import SkillMeta
from muliao.platform.tooling import Audience, Origin

_ORIGIN_NOTES: dict[Origin, str] = {
    Origin.USER: "The user started this conversation in the app and may be reading along.",
    Origin.TRIGGER: (
        "This run was started by a trigger (a schedule, or an external event shown inside <event>); nobody is "
        "watching. Each firing starts a fresh conversation unless its sender continues this one, so keep anything "
        "later firings need in memory or workspace files."
    ),
    Origin.DELEGATION: "Another agent opened this conversation with you.",
}

_AUDIENCE_NOTES: dict[Audience, str] = {
    Audience.USER: "The user is notified with your final answer and may reply here.",
    Audience.AGENT: (
        "Another agent is waiting for your final answer, which is returned to it as a tool result; "
        "answer it directly and completely. The user is not notified."
    ),
    Audience.NONE: (
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
working out a non-trivial procedure likely to recur, save it with `write_skill`; when a skill proves wrong or \
incomplete, fix that part with `edit_skill`. Change skills, memory and instructions only through these tools, never \
with shell commands or file tools, so every change is versioned under your name.
- Your own instructions can be refined with `instructions_replace` when a standing rule proved wrong or the \
user keeps repeating one; keep such edits small. Every version is kept and the user can roll back.
- Secrets go in the vault (`vault_set`), referenced by key; never write secret values into files, memory or skills.
- Keep state you need across runs (cursors, caches) in files in your workspace."""


def build_run_context(*, now_local: datetime, audience: Audience | None) -> str:
    """一次运行开头交给模型的运行上下文：必须随运行变化、因而不能放进 instructions 的那几行（见模块说明）。

    - 当前时间：每次运行都不同。
    - 回答交给谁（Audience）：由这次运行处理的信件推导（sessions.kinds.KindPolicy.audience），同一个会话的不同
      运行会不同——用户在等回答的那次是 `user`，后台触发的那次是 `none`。
    会话来源（Origin）不在这里：它是会话创建时定下、整个会话不变的，留在 instructions 里。
    """
    lines = [f"[Current local time: {now_local.isoformat(timespec='minutes')} ({now_local.tzname()})]"]
    if audience is not None:
        lines.append(f"[{_AUDIENCE_NOTES[audience]}]")
    return "\n".join(lines)


def build_instructions(
    *,
    agent_name: str,
    agent_description: str,
    public_instructions: str,
    agent_instructions: str,
    workspace: Path,
    origin: Origin | None,
    memory: str,
    skills: Sequence[SkillMeta],
    memory_limit: int,
    summary: str | None,
) -> str:
    """这个 agent 在这个会话里的指令。参数只能是会话级、agent 级的稳定内容（见模块说明）。"""
    context = [
        f"- You are agent {agent_name!r}: {agent_description}",
        f"- Workspace (base for relative paths and shell cwd): {workspace}",
    ]
    if origin is not None:
        context.append(f"- {_ORIGIN_NOTES[origin]}")
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
