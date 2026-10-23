"""自我进化工具：私有 skill 与记忆（含全局记忆，每个 agent 总是可用），以及写公共 skill（单独的工具组）。"""

from pydantic_ai import FunctionToolset, RunContext

from muliao.agents.memory import MemoryStore
from muliao.agents.resources import ATTACHMENT_SUFFIXES, MAX_EXTERNAL_FILE_BYTES
from muliao.agents.skills import SkillStore
from muliao.platform.tooling import TOOL_MAX_RETRIES, RunScope

# 写完 skill 时告诉模型附件怎么放（哪些进历史由 agents.resources 决定，这里从同一份定义生成）：只在写 skill 时
# 用得上，不放进每次请求都带的工具说明
_ATTACHMENTS_NOTE = (
    "; scripts and other text files it needs go in that directory (write them with write_file). Files ending in "
    f"{', '.join('.' + s for s in ATTACHMENT_SUFFIXES)} of at most {MAX_EXTERNAL_FILE_BYTES // 1024} KB are "
    "versioned with it (not inside hidden, node_modules, venv or site-packages directories); anything else, "
    "binaries included, is not: write in the skill how to install it (version and source)"
)


def skills_toolset(store: SkillStore) -> FunctionToolset[RunScope]:
    ts = FunctionToolset[RunScope](max_retries=TOOL_MAX_RETRIES)

    @ts.tool
    async def read_skill(ctx: RunContext[RunScope], name: str) -> str:
        """Load the full instructions of a skill listed in your skill index.
        Your private skill shadows a public skill with the same name.

        Args:
            name: Skill name.
        """
        skill = store.resolve(ctx.deps.agent_id, name)
        return (
            f"# skill {skill.meta.name} ({skill.meta.scope}, dir: {skill.meta.path.parent})\n\n{skill.body}"
        )

    @ts.tool
    async def write_skill(ctx: RunContext[RunScope], name: str, description: str, content: str) -> str:
        """Create or replace one of your private skills: a reusable procedure for a recurring kind of task.

        Args:
            name: Lowercase slug, e.g. `slack-digest`.
            description: One sentence on when to use this skill; shown in your skill index.
            content: Markdown instructions: steps, commands, pitfalls, examples.
        """
        meta = await store.write(ctx.deps.agent_id, name, description, content, ctx.deps.agent_id)
        return f"saved private skill {meta.name} at {meta.path}{_ATTACHMENTS_NOTE}"

    @ts.tool
    async def edit_skill(
        ctx: RunContext[RunScope],
        name: str,
        old_text: str = "",
        new_text: str = "",
        description: str | None = None,
    ) -> str:
        """Change one of your private skills without rewriting the whole skill: part of its instructions,
        its description, or both. Instructions: `old_text` must occur exactly once in them and is replaced by
        `new_text`; an empty `old_text` appends `new_text` at the end; leave both empty to keep the instructions.
        At least one of the two changes must be given.
        Use this (or `write_skill`) rather than editing SKILL.md with shell or file tools, so the change is versioned.

        Args:
            name: Skill name.
            old_text: Exact text currently in the skill's instructions, or empty to append.
            new_text: Replacement (or appended) text; empty deletes `old_text`.
            description: New one-sentence description shown in your skill index; omit to keep the current one.
        """
        return await _edit(store, ctx.deps.agent_id, name, old_text, new_text, description, ctx.deps.agent_id)

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
    async def write_public_skill(ctx: RunContext[RunScope], name: str, description: str, content: str) -> str:
        """Create or replace a public skill that every agent sees in its skill index. Only for procedures any
        agent can reuse; prefer a private skill (`write_skill`) otherwise.

        Args:
            name: Lowercase slug, e.g. `slack-digest`.
            description: One sentence on when to use this skill; shown in every agent's skill index.
            content: Markdown instructions: steps, commands, pitfalls, examples.
        """
        meta = await store.write(None, name, description, content, ctx.deps.agent_id)
        return f"saved public skill {meta.name} at {meta.path}{_ATTACHMENTS_NOTE}"

    @ts.tool
    async def edit_public_skill(
        ctx: RunContext[RunScope],
        name: str,
        old_text: str = "",
        new_text: str = "",
        description: str | None = None,
    ) -> str:
        """Change a public skill without rewriting the whole skill: part of its instructions, its description,
        or both. Instructions: `old_text` must occur exactly once in them and is replaced by `new_text`; an empty
        `old_text` appends `new_text` at the end; leave both empty to keep the instructions.
        At least one of the two changes must be given.

        Args:
            name: Skill name.
            old_text: Exact text currently in the skill's instructions, or empty to append.
            new_text: Replacement (or appended) text; empty deletes `old_text`.
            description: New one-sentence description shown in every agent's skill index; omit to keep the current one.
        """
        return await _edit(store, None, name, old_text, new_text, description, ctx.deps.agent_id)

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
    store: SkillStore, owner: str | None, name: str, old: str, new: str, description: str | None, author: str
) -> str:
    """edit_skill 与 edit_public_skill 共用：规则在 SkillStore.edit。"""
    meta = await store.edit(owner, name, author, old=old, new=new, description=description)
    return f"edited {meta.scope} skill {meta.name}"


def memory_toolset(store: MemoryStore) -> FunctionToolset[RunScope]:
    """记忆：自己的与所有 agent 共享的全局记忆，各用各的工具（全局记忆的是 *_public，权限 memory.public:*）。全局记忆
    由有权限的 agent 的自进化维护，每次修改记一版（作者是改的 agent），用户可在界面上回滚。"""
    ts = FunctionToolset[RunScope](max_retries=TOOL_MAX_RETRIES)

    def _status(which: str, content: str) -> str:
        return f"{which} updated ({len(content)}/{store.max_chars} chars)"

    @ts.tool
    async def memory_append(ctx: RunContext[RunScope], entry: str) -> str:
        """Append a durable fact to your private memory (user preferences, environment facts, lessons learned).
        Your memory and the global memory are both shown to you at the start of every run.

        Args:
            entry: One concise markdown line or short block.
        """
        me = ctx.deps.agent_id
        return _status("memory", await store.append(me, entry, me))

    @ts.tool
    async def memory_replace(ctx: RunContext[RunScope], old_text: str, new_text: str) -> str:
        """Correct, condense or delete part of your private memory. `old_text` must occur exactly once; an empty
        `new_text` deletes it.

        Args:
            old_text: Exact text currently in your memory.
            new_text: Replacement text.
        """
        me = ctx.deps.agent_id
        return _status("memory", await store.replace(me, old_text, new_text, me))

    @ts.tool
    async def memory_append_public(ctx: RunContext[RunScope], entry: str) -> str:
        """Append to the global memory every agent sees: only facts and rules that hold for all agents.

        Args:
            entry: One concise markdown line or short block.
        """
        return _status("global memory", await store.append(None, entry, ctx.deps.agent_id))

    @ts.tool
    async def memory_replace_public(ctx: RunContext[RunScope], old_text: str, new_text: str) -> str:
        """Correct, condense or delete part of the global memory. `old_text` must occur exactly once; an empty
        `new_text` deletes it.

        Args:
            old_text: Exact text currently in the global memory.
            new_text: Replacement text.
        """
        return _status("global memory", await store.replace(None, old_text, new_text, ctx.deps.agent_id))

    return ts
