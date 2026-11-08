"""自我进化工具：skill 与记忆。"""

import asyncio
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field
from pydantic_ai import FunctionToolset, RunContext

from muliao.agents.memory import MemoryInfo, MemoryStore
from muliao.agents.skill_package import ATTACHMENT_SUFFIXES, MAX_FILE_BYTES, SKILL_FILE, SkillFile, Skipped
from muliao.agents.skills import FileChange, SkillMeta, SkillStore, attachments_from
from muliao.errors import InvalidInput
from muliao.platform.text_edit import TextChange
from muliao.platform.tool_views import Args, MarkdownBlock, ToolRender, ViewBlock, diff, fields, rendered_by
from muliao.platform.tooling import TOOL_MAX_RETRIES, RunScope

# 写完 skill 时告诉模型附件怎么放（规则在 skill_package，这里从同一份定义生成）：只在写 skill 时用得上，
# 不放进每次请求都带的工具说明
_ATTACHMENTS_NOTE = (
    ". That directory is a read-only copy generated from the database: to add or change one script or other "
    "text file of the skill, use `edit_skill` with `file` (the path in that directory); to reorganize or "
    "delete attachments, put the full set in a directory in your workspace and pass it as `attachments_dir`. "
    "Files ending in "
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


_READ_SKILL_RENDER = ToolRender[str](
    call=lambda a: [fields(("skill", a["name"]))],
    result=lambda text: [MarkdownBlock(title=None, text=text)],
)
_WRITE_SKILL_RENDER = ToolRender[str](
    call=lambda a: [
        fields(("skill", a["name"]), ("description", a["description"]), ("reason", a["reason"])),
        MarkdownBlock(title=None, text=a["content"]),
    ],
)


class TextReplace(BaseModel):
    # memory_edit / edit_skill 一次调用里的一处修改（类 docstring 会进 schema，所以用注释）；规则与单处的 old_text/new_text 相同
    model_config = ConfigDict(extra="forbid")

    old_text: str = Field(
        default="", description="Exact text to replace, or empty to append new_text at the end."
    )
    new_text: str = Field(description="Replacement (or appended) text; empty deletes old_text.")


class FileTextReplace(TextReplace):
    # edit_skill 一次调用里的一处修改：可以指定改哪个文件
    file: str | None = Field(
        default=None,
        description="File to change, a path in the skill directory (e.g. `scripts/run.py`); defaults to the "
        "top-level `file`.",
    )


def _changes(old_text: str, new_text: str, edits: tuple[TextReplace, ...]) -> tuple[TextChange, ...]:
    """单处的 old_text/new_text 与 `edits` 列表二选一，统一成按顺序的修改；单处两个都空是不改正文。"""
    if edits:
        if old_text or new_text:
            raise InvalidInput("pass either one change (old_text/new_text) or edits, not both")
        return tuple(TextChange(e.old_text, e.new_text) for e in edits)
    return (TextChange(old_text, new_text),) if old_text or new_text else ()


def _file_changes(
    file: str, old_text: str, new_text: str, edits: tuple[FileTextReplace, ...]
) -> tuple[FileChange, ...]:
    """edit_skill 的修改：同 _changes，每处带上改的文件（edits 里没给 file 的用顶层的 file）。"""
    changes = _changes(old_text, new_text, edits)
    if edits:
        return tuple(FileChange(c, e.file or file) for e, c in zip(edits, changes, strict=True))
    return tuple(FileChange(c, file) for c in changes)


def _change(a: Args) -> list[ViewBlock]:
    """每处 old_text → new_text 一个 diff（空 old_text 是追加）；没有改正文（只改别的）不显示。两种写法都给了（会被退回）时
    两种都画。"""
    edits: tuple[TextReplace, ...] = a["edits"]
    single = [(a["old_text"], a["new_text"])] if a["old_text"] or a["new_text"] else []
    return [diff(None, old, new) for old, new in [*single, *((e.old_text, e.new_text) for e in edits)]]


def _file_change(a: Args) -> list[ViewBlock]:
    """edit_skill 的各处修改，同 _change；改的不是 SKILL.md 时以文件名为 diff 的标题。"""
    file: str = a["file"]
    edits: tuple[FileTextReplace, ...] = a["edits"]
    single = [(file, a["old_text"], a["new_text"])] if a["old_text"] or a["new_text"] else []
    rest = [(e.file or file, e.old_text, e.new_text) for e in edits]
    return [diff(None if f == SKILL_FILE else f, old, new) for f, old, new in [*single, *rest]]


_SKILL_EDIT_RENDER = ToolRender[str](
    call=lambda a: [
        fields(("skill", a["name"]), ("description", a["description"]), ("reason", a["reason"])),
        *_file_change(a),
    ],
)
_MEMORY_EDIT_RENDER = ToolRender[str](
    call=lambda a: [fields(("memory", a["name"]), ("reason", a["reason"])), *_change(a)]
)


def skills_toolset(store: SkillStore) -> FunctionToolset[RunScope]:
    """skill：读任何 skill；写、改、删按规则 1 的 write（自己的、规则放行 write 的，admin 靠 write:*），新建的归自己。分给别的 agent 靠角色。"""
    ts = FunctionToolset[RunScope](max_retries=TOOL_MAX_RETRIES)

    @ts.tool(metadata=rendered_by(_READ_SKILL_RENDER))
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

    @ts.tool(metadata=rendered_by(_WRITE_SKILL_RENDER))
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

    @ts.tool(metadata=rendered_by(_SKILL_EDIT_RENDER))
    async def edit_skill(
        ctx: RunContext[RunScope],
        name: str,
        reason: str,
        old_text: str = "",
        new_text: str = "",
        edits: tuple[FileTextReplace, ...] = (),
        file: str = SKILL_FILE,
        description: str | None = None,
        attachments_dir: str | None = None,
    ) -> str:
        """Change a skill you own (or any skill, if you are an admin) without rewriting it: part of its
        instructions or of one of its attachments (scripts etc.), its description, or several. Text: in `file`
        (default SKILL.md, the instructions), `old_text` must occur exactly once and is replaced by `new_text`;
        an empty `old_text` appends `new_text` at the end, and creates the attachment if it does not exist yet
        (executable if it starts with `#!`; existing files keep their mode). Leave both empty to change no text.
        At least one change must be given.
        To change several places in one call, in one file or in several, pass `edits` instead of
        old_text/new_text: they are applied in order, each `old_text` found in the text as the earlier edits left
        it; if any fails nothing is written, and all of them make one version.
        The skill directory on disk is a read-only copy: change skills only with this or `write_skill`.
        To delete or reorganize attachments, use `attachments_dir` instead.

        Args:
            name: Skill name.
            reason: Why, kept in the version history as this skill's timeline: the defect it fixes (user
                correction, error, detour) or the recurring pattern it captures, the source (session id, the
                user's words), and how it relates to earlier changes (new / rewrites / overturns what).
            old_text: Exact text currently in `file`, or empty to append.
            new_text: Replacement (or appended) text; empty deletes `old_text`.
            edits: Several changes, each {old_text, new_text} with the same rules and optionally its own
                `file`, applied in order (omit old_text/new_text when using it).
            file: File whose text old_text/new_text (and edits without a `file`) change: `SKILL.md` (the
                instructions, without the frontmatter) or an attachment's path in the skill directory, e.g.
                `scripts/run.py`.
            description: New one-sentence description shown in the skill index; omit to keep the current one.
            attachments_dir: Directory (workspace-relative or absolute) whose text files replace all the
                skill's attachments (scripts etc. next to SKILL.md); omit to keep them. Not together with
                text changes to attachments.
        """
        files, skipped = await _attachments(ctx, attachments_dir)
        meta = await store.edit(
            name,
            ctx.deps.agent_id,
            changes=_file_changes(file, old_text, new_text, edits),
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
    """记忆：memory_edit 改能改（write）的任何一块（自己的私有记忆、角色给了 write 的共享记忆，admin 的全部），调用时查；
    memory_create / memory_delete 是管理类工具，只经 admin 角色给出。每次修改记一版（改的人记为那个 agent），用户可在
    界面上回滚。"""
    ts = FunctionToolset[RunScope](max_retries=TOOL_MAX_RETRIES)

    def _status(verb: str, memory: MemoryInfo) -> str:
        return f"{verb} memory {memory.name} ({len(memory.content)}/{store.max_chars} chars)"

    @ts.tool(metadata=rendered_by(_MEMORY_EDIT_RENDER))
    async def memory_edit(
        ctx: RunContext[RunScope],
        name: str,
        reason: str,
        new_text: str = "",
        old_text: str = "",
        edits: tuple[TextReplace, ...] = (),
    ) -> str:
        """Add to or change a memory block marked editable in your instructions. `old_text` must occur exactly
        once in the block and is replaced by `new_text` (an empty `new_text` deletes it); an empty `old_text`
        appends `new_text` as a new entry at the end. To change several places in one call, pass `edits`
        instead: they are applied in order, each `old_text` found in the text as the earlier edits left it; if
        any fails nothing is written, all of them make one version, and the size limit is checked once at the end.

        Memory is in every run's context, so it holds only one-line conclusions that must constrain every run:
        boundaries and red lines, the user's decisions and corrections, rejected approaches, a one-line warning
        about a pitfall, ids and paths that no command can look up. Not memory: steps, commands, API parameters,
        the details of a pitfall and its workaround (write or edit a skill; memory keeps at most "X: see skill
        Y"); what you did, current status, commit ids, one-off details (issue comments / work history); anything
        a command shows or a tool description or skill already says; secrets (vault). If an entry on the same
        topic exists, rewrite it instead of appending.

        Args:
            name: Name of the memory block, as in `<memory name="...">` in your instructions.
            new_text: Replacement text, or the entry to append: one concise line (a conclusion, not a
                procedure); empty deletes `old_text`.
            reason: Why, kept in the version history as this memory's timeline: the defect it fixes (user
                correction, error, detour) or the fact it records, the source (session id, the user's words),
                and how it relates to earlier changes (new / rewrites / overturns what).
            old_text: Exact text currently in the block, or empty to append.
            edits: Several changes, each {old_text, new_text} with the same rules, applied in order (omit
                old_text/new_text when using it).
        """
        changes = _changes(old_text, new_text, edits)
        if not changes:
            raise InvalidInput(
                "nothing to change: give new_text (with old_text to replace or delete) or edits"
            )
        return _status("updated", await store.edit(name, changes, ctx.deps.agent_id, reason=reason))

    @ts.tool
    async def memory_create(
        ctx: RunContext[RunScope], name: str, reason: str, content: str = "", description: str = ""
    ) -> str:
        """Create a shared memory block, owned by the platform, for knowledge that several agents need but that
        is not global. Nobody uses it until a role includes it: give the role `*:memory:<name>` (members read and
        edit it) with `update_role`.

        Args:
            name: Lowercase slug, e.g. `trading`.
            reason: Why, kept in the version history: what it gathers (from which agents) and the source.
            content: Initial text (markdown).
            description: One sentence on what belongs in it, shown on the assets page.
        """
        memory = await store.create(
            name, content, actor=ctx.deps.agent_id, description=description, reason=reason
        )
        return _status("created", memory)

    @ts.tool
    async def memory_delete(ctx: RunContext[RunScope], name: str, reason: str) -> str:
        """Delete a memory block that is obsolete (e.g. merged into another). The global memory cannot be deleted.

        Args:
            name: Name of the memory block.
            reason: Why, and where its content went; kept in the version history.
        """
        await store.delete(name, ctx.deps.agent_id, reason=reason)
        return f"deleted memory {name}"

    return ts
