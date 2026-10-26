"""组装一次运行的 instructions，以及每次运行开头那段运行上下文。纯函数，便于测试。

两者的分工只有一条规则：**instructions 只放同一个会话的不同运行之间不会变的内容，随运行变化的放进运行上下文**。
原因是缓存前缀——instructions 渲染成提供方的 system，缓存断点就放在它末尾（llm.ANTHROPIC_CACHE_SETTINGS），
DeepSeek 等自动前缀缓存同样从这里开始匹配。instructions 里只要有一个每次运行都不同的值（最早是「当前时间」），
整段历史每次运行都要重新写入缓存（写缓存比普通输入还贵），同一个 agent 的不同会话、压缩也只能复用工具定义
那一段。运行上下文放进这次运行新增的第一条请求（conventions.attach_run_context），它随消息入库、之后原样重放，
旧消息保持字节不变。
"""

from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from muliao.agents.api import CorruptResource, SkillMeta
from muliao.platform.tooling import Audience

_AUDIENCE_NOTES: dict[Audience, str] = {
    Audience.USER: "The user is notified with your final answer and may reply here.",
    Audience.AGENT: (
        "Another agent is waiting for your final answer, which is returned to it as a tool result; "
        "answer it directly and completely. The user is not notified."
    ),
    Audience.NONE: (
        "The user is NOT notified automatically: send them a notification (`notify_user`, if you have it) "
        "only when there is something worth their attention."
    ),
}


SELF_IMPROVEMENT = """\
## Self-improvement
Everything you learn persists only as memory or skills.
- Memory: your private memory and the global memory shared by all agents (above), each limited to \
{memory_limit} characters. Record durable facts (user preferences, environment details, corrections, lessons \
learned) with `memory_edit` (empty `old_text` appends); fix or condense entries with it too. The global memory \
has its own tool (`memory_edit_public`, if you have it): only for facts and rules that hold for every agent. \
Never record secrets or one-off details.
- Skills are reusable procedures. Before a task that matches a skill in the index, call `read_skill`. After \
working out a non-trivial procedure likely to recur, save it with `write_skill`; when a skill proves wrong or \
incomplete, fix that part with `edit_skill`. Change skills and memory only through these tools, never with shell \
commands or file tools, so every change is versioned under your name and the user can roll it back.
- Secrets belong in the vault, referenced by key; never write secret values into files, memory or skills.
- Keep state you need across runs (cursors, caches) in files in your workspace."""


def build_run_context(*, now_local: datetime, audience: Audience) -> str:
    """一次运行开头交给模型的运行上下文：必须随运行变化、因而不能放进 instructions 的那几行（见模块说明）。

    - 当前时间：每次运行都不同。
    - 回答交给谁（Audience）：由这次运行处理的信件推导（sessions.kinds.KindPolicy.audience），同一个会话的不同
      运行会不同——用户在等回答的那次是 `user`，后台触发的那次是 `none`。
    会话来源的说明不在这里：它是会话创建时定下、整个会话不变的，留在 instructions 里（origin_note）。
    """
    return (
        f"[Current local time: {now_local.isoformat(timespec='minutes')} ({now_local.tzname()})]\n"
        f"[{_AUDIENCE_NOTES[audience]}]"
    )


def with_notices(run_context: str, notices: Sequence[str]) -> str:
    """运行上下文接上这次运行的工具组在开始时发现的问题（RunScope.notices，如连不上的 MCP server），一条一行。
    它们只有工具组连接之后才知道，所以在第一步之前单独接上（sessions.steps）。"""
    return "\n".join([*([run_context] if run_context else []), *(f"[{n}]" for n in notices)])


def build_instructions(
    *,
    agent_name: str,
    agent_description: str,
    global_memory: str,
    workspace: Path,
    origin_note: str,
    memory: str,
    skills: Sequence[SkillMeta],
    corrupt_skills: Sequence[CorruptResource],
    memory_limit: int,
    summary: str | None,
) -> str:
    """这个 agent 在这个会话里的指令。参数只能是会话级、agent 级的稳定内容（见模块说明）。origin_note 是会话来源的
    说明，由会话领域给出（sessions.origins）。"""
    context = [
        f"- You are agent {agent_name!r}: {agent_description}",
        f"- Workspace (base for relative paths and shell cwd): {workspace}",
        f"- {origin_note}",
    ]
    # 坏的 skill 逐项说明（不让整次运行失败，见 agents.resources.CorruptResource）：agent 可以重写或删掉它
    skill_lines = [f"- `{s.name}` ({s.scope}): {s.description}" for s in skills] + [
        f"- {c.describe()}" for c in corrupt_skills
    ] or ["(no skills yet)"]
    # 全局记忆在前、agent 自己的记忆在后：具体的可以细化或覆盖通用规则
    sections = [
        "## Global memory\nShared by every agent.\n"
        f"<global_memory>\n{global_memory.strip() or '(empty)'}\n</global_memory>",
        "## Runtime context\n" + "\n".join(context),
        f"## Memory\nYour private memory.\n<memory>\n{memory.strip() or '(empty)'}\n</memory>",
        "## Skill index\n" + "\n".join(skill_lines),
        SELF_IMPROVEMENT.format(memory_limit=memory_limit),
    ]
    if summary is not None:
        sections.append(
            "## Earlier in this session\nOlder messages of this session were compacted into this summary; "
            f"the messages after it follow in full.\n<summary>\n{summary}\n</summary>"
        )
    return "\n\n".join(s for s in sections if s)
