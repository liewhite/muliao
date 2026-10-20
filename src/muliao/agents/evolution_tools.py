"""自我进化工具：私有 skill、memory 与自己的指令（每个 agent 总是可用），以及写公共 skill（单独的工具组）。turn 和反思共用。"""

from pydantic_ai import FunctionToolset, RunContext

from muliao.agent_runtime.scope import RunScope
from muliao.agent_runtime.tool_errors import TOOL_MAX_RETRIES
from muliao.agents.memory import MemoryStore
from muliao.agents.prompts import PromptStore
from muliao.agents.skills import SkillStore


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
        return f"saved private skill {meta.name} at {meta.path}"

    @ts.tool
    async def edit_skill(ctx: RunContext[RunScope], name: str, old_text: str, new_text: str) -> str:
        """Change part of one of your private skills' instructions without rewriting the whole skill.
        `old_text` must occur exactly once in them; an empty `old_text` appends `new_text` at the end.
        Use this (or `write_skill`) rather than editing SKILL.md with shell or file tools, so the change is versioned.

        Args:
            name: Skill name.
            old_text: Exact text currently in the skill's instructions, or empty to append.
            new_text: Replacement (or appended) text; empty deletes `old_text`.
        """
        meta = await store.edit(ctx.deps.agent_id, name, old_text, new_text, ctx.deps.agent_id)
        return f"edited private skill {meta.name}"

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
    """写公共 skill：它的描述进入每个 agent 的 instructions，等同于改所有 agent 共享的指令，所以单独成组。"""
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
        return f"saved public skill {meta.name} at {meta.path}"

    @ts.tool
    async def edit_public_skill(ctx: RunContext[RunScope], name: str, old_text: str, new_text: str) -> str:
        """Change part of a public skill's instructions without rewriting the whole skill.
        `old_text` must occur exactly once in them; an empty `old_text` appends `new_text` at the end.

        Args:
            name: Skill name.
            old_text: Exact text currently in the skill's instructions, or empty to append.
            new_text: Replacement (or appended) text; empty deletes `old_text`.
        """
        meta = await store.edit(None, name, old_text, new_text, ctx.deps.agent_id)
        return f"edited public skill {meta.name}"

    @ts.tool
    async def delete_public_skill(ctx: RunContext[RunScope], name: str) -> str:
        """Delete a public skill that is wrong or obsolete.

        Args:
            name: Skill name.
        """
        await store.delete(None, name, ctx.deps.agent_id)
        return f"deleted public skill {name}"

    return ts


def memory_toolset(store: MemoryStore) -> FunctionToolset[RunScope]:
    ts = FunctionToolset[RunScope](max_retries=TOOL_MAX_RETRIES)

    def _status(content: str) -> str:
        return f"memory updated ({len(content)}/{store.max_chars} chars)"

    @ts.tool
    async def memory_append(ctx: RunContext[RunScope], entry: str) -> str:
        """Append a durable fact to your private memory (user preferences, environment facts, lessons learned).
        Your whole memory is shown to you at the start of every run.

        Args:
            entry: One concise markdown line or short block.
        """
        return _status(await store.append(ctx.deps.agent_id, entry, ctx.deps.agent_id))

    @ts.tool
    async def memory_replace(ctx: RunContext[RunScope], old_text: str, new_text: str) -> str:
        """Correct, condense or delete part of your memory. `old_text` must occur exactly once;
        an empty `new_text` deletes it.

        Args:
            old_text: Exact text currently in memory.
            new_text: Replacement text.
        """
        return _status(await store.replace(ctx.deps.agent_id, old_text, new_text, ctx.deps.agent_id))

    return ts


def instructions_toolset(prompts: PromptStore) -> FunctionToolset[RunScope]:
    """改自己的指令：只影响自己，每次修改记一版（作者是自己），用户可在界面上回滚。"""
    ts = FunctionToolset[RunScope](max_retries=TOOL_MAX_RETRIES)

    @ts.tool
    async def instructions_replace(ctx: RunContext[RunScope], old_text: str, new_text: str) -> str:
        """Refine your own instructions (the agent-specific part of your system prompt), e.g. to fix a rule that
        proved wrong or add one the user keeps asking for. `old_text` must occur exactly once in them; an empty
        `old_text` appends `new_text` at the end. Keep changes small; the user can roll back any version.

        Args:
            old_text: Exact text currently in your instructions, or empty to append.
            new_text: Replacement (or appended) text; empty deletes `old_text`.
        """
        me = ctx.deps.agent_id
        changed = await prompts.edit(me, old_text, new_text, me)
        return "instructions updated" if changed else "instructions unchanged"

    return ts
