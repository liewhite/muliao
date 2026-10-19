"""自我进化工具：skill 与 memory。turn 和反思共用。"""

from pydantic_ai import FunctionToolset, RunContext

from muliao.evolution.memory import MemoryStore
from muliao.evolution.skills import SkillStore
from muliao.ownership import Scope, owner_of
from muliao.runtime.scope import RunScope
from muliao.runtime.tools.errors import TOOL_MAX_RETRIES


def skills_toolset(store: SkillStore) -> FunctionToolset[RunScope]:
    ts = FunctionToolset[RunScope](max_retries=TOOL_MAX_RETRIES)

    @ts.tool
    async def read_skill(ctx: RunContext[RunScope], name: str) -> str:
        """Load the full instructions of a skill listed in your skill index.
        Your private skill shadows a public skill with the same name.

        Args:
            name: Skill name.
        """
        skill = store.resolve(ctx.deps.agent_name, name)
        return (
            f"# skill {skill.meta.name} ({skill.meta.scope}, dir: {skill.meta.path.parent})\n\n{skill.body}"
        )

    @ts.tool
    async def write_skill(
        ctx: RunContext[RunScope], name: str, description: str, content: str, scope: Scope = "private"
    ) -> str:
        """Create or replace a skill: a reusable procedure for a recurring kind of task.
        Use `private` for procedures specific to you, `public` for ones any agent can reuse.

        Args:
            name: Lowercase slug, e.g. `slack-digest`.
            description: One sentence on when to use this skill; shown in every agent's skill index.
            content: Markdown instructions: steps, commands, pitfalls, examples.
            scope: `private` (only you) or `public` (all agents).
        """
        meta = store.write(owner_of(scope, ctx.deps.agent_name), name, description, content)
        return f"saved {scope} skill {meta.name} at {meta.path}"

    @ts.tool
    async def delete_skill(ctx: RunContext[RunScope], name: str, scope: Scope = "private") -> str:
        """Delete a skill that is wrong or obsolete.

        Args:
            name: Skill name.
            scope: `private` or `public`.
        """
        store.delete(owner_of(scope, ctx.deps.agent_name), name)
        return f"deleted {scope} skill {name}"

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
        return _status(store.append(ctx.deps.agent_name, entry))

    @ts.tool
    async def memory_replace(ctx: RunContext[RunScope], old_text: str, new_text: str) -> str:
        """Correct, condense or delete part of your memory. `old_text` must occur exactly once;
        an empty `new_text` deletes it.

        Args:
            old_text: Exact text currently in memory.
            new_text: Replacement text.
        """
        return _status(store.replace(ctx.deps.agent_name, old_text, new_text))

    return ts
