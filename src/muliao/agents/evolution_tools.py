"""自我进化工具：skill 与记忆（自己的与全局记忆）。"""

import asyncio
from pathlib import Path

from pydantic_ai import FunctionToolset, RunContext

from muliao.agents.memory import MemoryStore
from muliao.agents.skill_package import ATTACHMENT_SUFFIXES, MAX_FILE_BYTES, SkillFile, Skipped
from muliao.agents.skills import SkillMeta, SkillStore, attachments_from
from muliao.platform.tooling import TOOL_MAX_RETRIES, RunScope

# 写完 skill 时告诉模型附件怎么放（规则在 skill_package，这里从同一份定义生成）：只在写 skill 时用得上，
# 不放进每次请求都带的工具说明
_ATTACHMENTS_NOTE = (
    ". That directory is a read-only copy generated from the database: to add or change scripts and other "
    "text files of the skill, put them in a directory in your workspace and pass it as `attachments_dir` "
    "(to change one file: `cp -R <dir> <copy> && chmod -R u+w <copy>` (the copy keeps the read-only modes), edit, "
    "pass the copy). Files ending in "
    f"{', '.join('.' + s for s in ATTACHMENT_SUFFIXES)} of at most {MAX_FILE_BYTES // 1024} KB are kept "
    "(not inside hidden, node_modules, venv or site-packages directories); anything else, binaries included, "
    "is not: write in the skill how to install it (version and source)."
)


async def _attachments(
    ctx: RunContext[RunScope], directory: str | None
) -> tuple[dict[str, SkillFile] | None, list[Skipped]]:
    if directory is None:
        return None, []

    def collect() -> tuple[dict[str, SkillFile], list[Skipped]]:
        path = Path(directory).expanduser()
        return attachments_from(path if path.is_absolute() else ctx.deps.workspace / path)

    return await asyncio.to_thread(collect)


def _saved(verb: str, meta: SkillMeta, skipped: list[Skipped]) -> str:
    skipped_note = "".join(f"; skipped {s.path}: {s.reason}" for s in skipped)
    return f"{verb} skill {meta.name} at {meta.path.parent}{skipped_note}{_ATTACHMENTS_NOTE}"


def skills_toolset(store: SkillStore) -> FunctionToolset[RunScope]:
    """skill：读任何 skill；写、改、删按规则 2（自己的，admin 可改任何的），新建的归自己。分给别的 agent 靠角色。"""
    ts = FunctionToolset[RunScope](max_retries=TOOL_MAX_RETRIES)

    @ts.tool
    async def read_skill(ctx: RunContext[RunScope], name: str) -> str:
        """Load the full instructions of a skill listed in your skill index.

        Args:
            name: Skill name.
        """
        me = ctx.deps.agent_id
        skill = await store.resolve(me, name)
        yours = "yours, " if skill.meta.owner == me else ""
        note = (
            ""
            if skill.usable
            else "Note: this skill is not in your skill index (not yours and none of your roles includes it); "
            "it is shown anyway.\n\n"
        )
        return f"# skill {skill.meta.name} ({yours}read-only dir: {skill.meta.path.parent})\n\n{note}{skill.body}"

    @ts.tool
    async def write_skill(
        ctx: RunContext[RunScope],
        name: str,
        description: str,
        content: str,
        reason: str,
        attachments_dir: str | None = None,
    ) -> str:
        """Create or replace a skill: a reusable procedure for a recurring kind of task. A new skill is yours and
        only you have it in your skill index; other agents get it only through a role. Replacing an existing one
        needs you to own it (or be an admin). Skill names are unique.

        Args:
            name: Lowercase slug, e.g. `slack-digest`.
            description: One sentence on when to use this skill; shown in the skill index.
            content: Markdown instructions: steps, commands, pitfalls, examples.
            reason: Why, kept in the version history as this skill's timeline: the defect it fixes (user
                correction, error, detour) or the recurring pattern it captures, the source (session id, the
                user's words), and how it relates to earlier changes (new / rewrites / overturns what).
            attachments_dir: Directory (workspace-relative or absolute) whose text files replace all the
                skill's attachments (scripts etc. next to SKILL.md); omit to keep them.
        """
        me = ctx.deps.agent_id
        files, skipped = await _attachments(ctx, attachments_dir)
        meta = await store.write(
            name, description, content, actor=me, owner=me, attachments=files, reason=reason
        )
        return _saved("saved", meta, skipped)

    @ts.tool
    async def edit_skill(
        ctx: RunContext[RunScope],
        name: str,
        reason: str,
        old_text: str = "",
        new_text: str = "",
        description: str | None = None,
        attachments_dir: str | None = None,
    ) -> str:
        """Change a skill you own (or any skill, if you are an admin) without rewriting it: part of its
        instructions, its description, its attachments, or several. Instructions: `old_text` must occur exactly
        once in them and is replaced by `new_text`; an empty `old_text` appends `new_text` at the end; leave both
        empty to keep the instructions. At least one change must be given.
        The skill directory on disk is a read-only copy: change skills only with this or `write_skill`.

        Args:
            name: Skill name.
            reason: Why, kept in the version history as this skill's timeline: the defect it fixes (user
                correction, error, detour) or the recurring pattern it captures, the source (session id, the
                user's words), and how it relates to earlier changes (new / rewrites / overturns what).
            old_text: Exact text currently in the skill's instructions, or empty to append.
            new_text: Replacement (or appended) text; empty deletes `old_text`.
            description: New one-sentence description shown in the skill index; omit to keep the current one.
            attachments_dir: Directory (workspace-relative or absolute) whose text files replace all the
                skill's attachments (scripts etc. next to SKILL.md); omit to keep them.
        """
        files, skipped = await _attachments(ctx, attachments_dir)
        meta = await store.edit(
            name,
            ctx.deps.agent_id,
            old=old_text,
            new=new_text,
            description=description,
            attachments=files,
            reason=reason,
        )
        if files is None:
            return f"edited skill {meta.name}"
        return _saved("edited", meta, skipped)

    @ts.tool
    async def delete_skill(ctx: RunContext[RunScope], name: str, reason: str) -> str:
        """Delete a skill you own (or any skill, if you are an admin) that is wrong or obsolete.

        Args:
            name: Skill name.
            reason: Why it is wrong or obsolete (and where its content went, if merged elsewhere); kept in
                the version history.
        """
        await store.delete(name, ctx.deps.agent_id, reason=reason)
        return f"deleted skill {name}"

    return ts


def memory_toolset(store: MemoryStore) -> FunctionToolset[RunScope]:
    """记忆：自己的与所有 agent 共享的全局记忆，各用各的工具（全局记忆的 memory_edit_public 是管理类工具，只经 admin
    角色给出）。全局记忆由 admin 的自进化维护，每次修改记一版（改的人记为那个 agent），用户可在界面上回滚。"""
    ts = FunctionToolset[RunScope](max_retries=TOOL_MAX_RETRIES)

    def _status(which: str, content: str) -> str:
        return f"updated {which} ({len(content)}/{store.max_chars} chars)"

    @ts.tool
    async def memory_edit(ctx: RunContext[RunScope], new_text: str, reason: str, old_text: str = "") -> str:
        """Add to or change your private memory (user preferences, environment facts, lessons learned).
        Your memory and the global memory are both shown to you at the start of every run.
        `old_text` must occur exactly once in your memory and is replaced by `new_text` (an empty `new_text`
        deletes it); an empty `old_text` appends `new_text` as a new entry at the end.

        Args:
            new_text: Replacement text, or the entry to append (one concise markdown line or short block);
                empty deletes `old_text`.
            reason: Why, kept in the version history as your memory's timeline: the defect it fixes (user
                correction, error, detour) or the fact it records, the source (session id, the user's words),
                and how it relates to earlier changes (new / rewrites / overturns what).
            old_text: Exact text currently in your memory, or empty to append.
        """
        me = ctx.deps.agent_id
        return _status("memory", await store.edit(me, old_text, new_text, me, reason=reason))

    @ts.tool
    async def memory_edit_public(
        ctx: RunContext[RunScope], new_text: str, reason: str, old_text: str = ""
    ) -> str:
        """Add to or change the global memory every agent sees: only facts and rules that hold for all agents.
        `old_text` must occur exactly once in it and is replaced by `new_text` (an empty `new_text` deletes it);
        an empty `old_text` appends `new_text` as a new entry at the end.

        Args:
            new_text: Replacement text, or the entry to append (one concise markdown line or short block);
                empty deletes `old_text`.
            reason: Why, kept in the version history: what it fixes or lifts (from which agents), the source,
                and how it relates to earlier changes (new / rewrites / overturns what).
            old_text: Exact text currently in the global memory, or empty to append.
        """
        actor = ctx.deps.agent_id
        return _status("global memory", await store.edit(None, old_text, new_text, actor, reason=reason))

    return ts
