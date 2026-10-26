"""自我进化工具：私有 skill 与记忆（含全局记忆，每个 agent 总是可用），以及写公共 skill（单独的工具组）。"""

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
    return f"{verb} {meta.scope} skill {meta.name} at {meta.path.parent}{skipped_note}{_ATTACHMENTS_NOTE}"


def skills_toolset(store: SkillStore) -> FunctionToolset[RunScope]:
    ts = FunctionToolset[RunScope](max_retries=TOOL_MAX_RETRIES)

    @ts.tool
    async def read_skill(ctx: RunContext[RunScope], name: str) -> str:
        """Load the full instructions of a skill listed in your skill index.

        Args:
            name: Skill name.
        """
        skill = await store.resolve(ctx.deps.agent_id, name)
        return (
            f"# skill {skill.meta.name} ({skill.meta.scope}, read-only dir: {skill.meta.path.parent})\n\n"
            f"{skill.body}"
        )

    @ts.tool
    async def write_skill(
        ctx: RunContext[RunScope],
        name: str,
        description: str,
        content: str,
        attachments_dir: str | None = None,
    ) -> str:
        """Create or replace one of your private skills: a reusable procedure for a recurring kind of task.
        Skill names are unique across private and public skills.

        Args:
            name: Lowercase slug, e.g. `slack-digest`.
            description: One sentence on when to use this skill; shown in your skill index.
            content: Markdown instructions: steps, commands, pitfalls, examples.
            attachments_dir: Directory (workspace-relative or absolute) whose text files replace all the
                skill's attachments (scripts etc. next to SKILL.md); omit to keep them.
        """
        me = ctx.deps.agent_id
        files, skipped = await _attachments(ctx, attachments_dir)
        meta = await store.write(me, name, description, content, me, attachments=files)
        return _saved("saved", meta, skipped)

    @ts.tool
    async def edit_skill(
        ctx: RunContext[RunScope],
        name: str,
        old_text: str = "",
        new_text: str = "",
        description: str | None = None,
        attachments_dir: str | None = None,
    ) -> str:
        """Change one of your private skills without rewriting the whole skill: part of its instructions,
        its description, its attachments, or several. Instructions: `old_text` must occur exactly once in them
        and is replaced by `new_text`; an empty `old_text` appends `new_text` at the end; leave both empty to
        keep the instructions. At least one change must be given.
        The skill directory on disk is a read-only copy: change skills only with this or `write_skill`.

        Args:
            name: Skill name.
            old_text: Exact text currently in the skill's instructions, or empty to append.
            new_text: Replacement (or appended) text; empty deletes `old_text`.
            description: New one-sentence description shown in your skill index; omit to keep the current one.
            attachments_dir: Directory (workspace-relative or absolute) whose text files replace all the
                skill's attachments (scripts etc. next to SKILL.md); omit to keep them.
        """
        me = ctx.deps.agent_id
        return await _edit(ctx, store, me, name, old_text, new_text, description, attachments_dir)

    @ts.tool
    async def delete_skill(ctx: RunContext[RunScope], name: str) -> str:
        """Delete one of your private skills that is wrong or obsolete.

        Args:
            name: Skill name.
        """
        await store.delete(ctx.deps.agent_id, name, ctx.deps.agent_id)
        return f"deleted private skill {name}"

    return ts


def public_skills_toolset(store: SkillStore) -> FunctionToolset[RunScope]:
    """写公共 skill：它的描述进入每个 agent 的 skill 索引，所以单独成组。"""
    ts = FunctionToolset[RunScope](max_retries=TOOL_MAX_RETRIES)

    @ts.tool
    async def write_public_skill(
        ctx: RunContext[RunScope],
        name: str,
        description: str,
        content: str,
        attachments_dir: str | None = None,
    ) -> str:
        """Create or replace a public skill that every agent sees in its skill index. Only for procedures any
        agent can reuse; prefer a private skill (`write_skill`) otherwise.

        Args:
            name: Lowercase slug, e.g. `slack-digest`.
            description: One sentence on when to use this skill; shown in every agent's skill index.
            content: Markdown instructions: steps, commands, pitfalls, examples.
            attachments_dir: Directory (workspace-relative or absolute) whose text files replace all the
                skill's attachments (scripts etc. next to SKILL.md); omit to keep them.
        """
        files, skipped = await _attachments(ctx, attachments_dir)
        meta = await store.write(None, name, description, content, ctx.deps.agent_id, attachments=files)
        return _saved("saved", meta, skipped)

    @ts.tool
    async def edit_public_skill(
        ctx: RunContext[RunScope],
        name: str,
        old_text: str = "",
        new_text: str = "",
        description: str | None = None,
        attachments_dir: str | None = None,
    ) -> str:
        """Change a public skill without rewriting the whole skill: part of its instructions, its description,
        its attachments, or several. Instructions: `old_text` must occur exactly once in them and is replaced by
        `new_text`; an empty `old_text` appends `new_text` at the end; leave both empty to keep the instructions.
        At least one change must be given.

        Args:
            name: Skill name.
            old_text: Exact text currently in the skill's instructions, or empty to append.
            new_text: Replacement (or appended) text; empty deletes `old_text`.
            description: New one-sentence description shown in every agent's skill index; omit to keep the current one.
            attachments_dir: Directory (workspace-relative or absolute) whose text files replace all the
                skill's attachments (scripts etc. next to SKILL.md); omit to keep them.
        """
        return await _edit(ctx, store, None, name, old_text, new_text, description, attachments_dir)

    @ts.tool
    async def delete_public_skill(ctx: RunContext[RunScope], name: str) -> str:
        """Delete a public skill that is wrong or obsolete.

        Args:
            name: Skill name.
        """
        await store.delete(None, name, ctx.deps.agent_id)
        return f"deleted public skill {name}"

    return ts


async def _edit(
    ctx: RunContext[RunScope],
    store: SkillStore,
    owner: str | None,
    name: str,
    old: str,
    new: str,
    description: str | None,
    attachments_dir: str | None,
) -> str:
    """edit_skill 与 edit_public_skill 共用：规则在 SkillStore.edit。"""
    files, skipped = await _attachments(ctx, attachments_dir)
    meta = await store.edit(
        owner, name, ctx.deps.agent_id, old=old, new=new, description=description, attachments=files
    )
    if files is None:
        return f"edited {meta.scope} skill {meta.name}"
    return _saved("edited", meta, skipped)


def memory_toolset(store: MemoryStore) -> FunctionToolset[RunScope]:
    """记忆：自己的与所有 agent 共享的全局记忆，各用各的工具（全局记忆的是 *_public，权限 memory.public:*）。全局记忆
    由有权限的 agent 的自进化维护，每次修改记一版（作者是改的 agent），用户可在界面上回滚。"""
    ts = FunctionToolset[RunScope](max_retries=TOOL_MAX_RETRIES)

    def _status(which: str, content: str) -> str:
        return f"{which} updated ({len(content)}/{store.max_chars} chars)"

    @ts.tool
    async def memory_edit(ctx: RunContext[RunScope], new_text: str, old_text: str = "") -> str:
        """Add to or change your private memory (user preferences, environment facts, lessons learned).
        Your memory and the global memory are both shown to you at the start of every run.
        `old_text` must occur exactly once in your memory and is replaced by `new_text` (an empty `new_text`
        deletes it); an empty `old_text` appends `new_text` as a new entry at the end.

        Args:
            new_text: Replacement text, or the entry to append (one concise markdown line or short block);
                empty deletes `old_text`.
            old_text: Exact text currently in your memory, or empty to append.
        """
        me = ctx.deps.agent_id
        return _status("memory", await store.edit(me, old_text, new_text, me))

    @ts.tool
    async def memory_edit_public(ctx: RunContext[RunScope], new_text: str, old_text: str = "") -> str:
        """Add to or change the global memory every agent sees: only facts and rules that hold for all agents.
        `old_text` must occur exactly once in it and is replaced by `new_text` (an empty `new_text` deletes it);
        an empty `old_text` appends `new_text` as a new entry at the end.

        Args:
            new_text: Replacement text, or the entry to append (one concise markdown line or short block);
                empty deletes `old_text`.
            old_text: Exact text currently in the global memory, or empty to append.
        """
        return _status("global memory", await store.edit(None, old_text, new_text, ctx.deps.agent_id))

    return ts
