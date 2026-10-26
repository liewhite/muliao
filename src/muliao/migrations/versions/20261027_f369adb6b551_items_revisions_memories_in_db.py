"""条目、记忆与版本进数据库（#230）：新表 items、item_files、memories、revisions，删掉 secrets。

数据：
- 资源仓库（数据库同目录下的 resources/，git）里的 skill（SKILL.md 与文本附件）、MCP server 配置、记忆导入新表；
  原公共的 owner 为 NULL，私有的 owner 为该 agent。数据库里没有的 agent 的目录不导入（启动检查原本就报它们是残留）。
- 名字按种类全局唯一：私有项与公共项或别的私有项重名时，私有项改名为 `<agent 名>-<名字>`（agent 名不是合法 slug 时用
  它的 id），skill 的 frontmatter name 一并改。私有 MCP server 原来遮蔽它勾选的同名公共 server 时（私有的已启用），从
  agents.mcp_servers 里去掉那个公共 server 的名字：它原本就用不到那个公共 server，迁移后仍只用自己的（改名后的）。
- skill 与记忆正文里指向旧位置的路径（`resources/public/skills/`、`resources/agents/<id>/skills/`）改成新的生成目录
  `skills/`（数据目录下）。
- secrets 表并入 items（密文原样搬）：同一个 key 有多行（几个 agent 各存了一份，或私有遮蔽公共）时只留最近更新的那份、
  owner 置 NULL（公共）：上线前已核对过现有的两组同名 secret 值相同；合并后它对所有 agent 可见（#235 按角色分发后再收窄）。
- 每个导入的条目与记忆记一版（作者 muliao，before 为 NULL）。旧 git 历史不导入：资源仓库目录改名为 resources.imported
  留着（部署时另有备份），之后可人工删除；导入优先读 resources/，没有时读 resources.imported（改名之后、提交之前进程被杀，
  重跑迁移仍能导入）。

降级：从 items 重建 secrets（owner 原样），删掉新表，resources.imported 改回 resources；升级之后在数据库里做的修改
（skill、MCP、记忆、secret 的变化，去掉的 mcp_servers 勾选）不写回文件，会丢失。

Revision ID: f369adb6b551
Revises: 75cc40ae1d3d
Create Date: 2026-10-27 00:22:38.534283
"""

import json
import logging
import os
import re
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import frontmatter
import sqlalchemy as sa
from alembic import op

revision: str = "f369adb6b551"
down_revision: str | None = "75cc40ae1d3d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

log = logging.getLogger(__name__)

# 以下规则按写迁移时的代码抄下（迁移按写它时的样子执行，不引用应用代码）
_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_SUFFIXES = (
    "md", "txt", "py", "sh", "bash", "js", "mjs", "cjs", "ts", "json", "toml", "yaml", "yml",
    "sql", "ini", "cfg", "csv", "html", "css", "jq",
)  # fmt: skip
_NOT_ATTACHMENT_DIRS = ("node_modules", "venv", "site-packages", "__pycache__")
_MAX_FILE_BYTES = 256 * 1024
_AUTHOR = "muliao"
_IMPORTED = "resources.imported"


def _now() -> str:
    return datetime.now(UTC).replace(tzinfo=None).isoformat(" ")


def _is_attachment(rel: str) -> bool:
    parts = rel.split("/")
    *dirs, name = parts
    if any(p.startswith(".") for p in parts) or any(d in _NOT_ATTACHMENT_DIRS for d in dirs):
        return False
    stem, dot, suffix = name.rpartition(".")
    return bool(dot) and bool(stem) and suffix in _SUFFIXES


def _source(conn: sa.Connection) -> Path | None:
    database = conn.engine.url.database
    if not database or database == ":memory:":
        return None
    data = Path(database).parent
    for name in ("resources", _IMPORTED):
        if (data / name).is_dir():
            return data / name
    return None


def _fix_paths(text: str, plan: dict[tuple[str | None, str], str]) -> str:
    """正文里指向旧 skill 位置的路径改成新的生成目录（都在数据目录下），改了名的 skill 指向它的新名字。plan 里没有的
    （没导入的、已删除 agent 的）只换掉目录前缀。"""

    def private(m: re.Match[str]) -> str:
        return f"skills/{plan.get((m.group(1), m.group(2)), m.group(2))}/"

    text = re.sub(r"resources/agents/([0-9a-f]{12})/skills/([a-z0-9][a-z0-9_-]*)/", private, text)
    text = text.replace("resources/public/skills/", "skills/")
    return re.sub(r"resources/agents/[0-9a-f]{12}/skills/", "skills/", text)


def _skill(skill_dir: Path) -> tuple[str, str, dict[str, tuple[str, bool]]] | None:
    """(SKILL.md, 描述, 附件)；读不出来的跳过（原来就是坏的，agent 看不到它）。"""
    try:
        text = (skill_dir / "SKILL.md").read_text(encoding="utf-8")
        post = frontmatter.loads(text)
    except Exception as e:  # noqa: BLE001
        log.warning("skill not imported: %s: %s", skill_dir, e)
        return None
    description = post.get("description")
    if post.get("name") != skill_dir.name or not isinstance(description, str) or not description.strip():
        log.warning("skill not imported: %s: bad frontmatter", skill_dir)
        return None
    files: dict[str, tuple[str, bool]] = {}
    for path in sorted(skill_dir.rglob("*")):
        rel = path.relative_to(skill_dir).as_posix()
        if path.is_symlink() or not path.is_file() or rel == "SKILL.md" or not _is_attachment(rel):
            continue
        if path.stat().st_size > _MAX_FILE_BYTES:
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        files[rel] = (content, os.access(path, os.X_OK))
    return text, description.strip(), files


def _skill_doc(skill_md: str, files: dict[str, tuple[str, bool]]) -> str:
    every = {"SKILL.md": (skill_md, False), **files}
    return json.dumps(
        {rel: {"content": c, "executable": x} for rel, (c, x) in sorted(every.items())},
        ensure_ascii=False,
        indent=1,
    )


def _renamed(name: str, agent_id: str, agent_name: str) -> str:
    prefix = agent_name if _SLUG.fullmatch(agent_name) else agent_id
    new = f"{prefix}-{name}"
    if not _SLUG.fullmatch(new):
        new = f"{agent_id}-{name}"[:64]
    return new


def _plan(entries: list[tuple[str | None, str]], names: dict[str, str]) -> dict[tuple[str | None, str], str]:
    """(owner, 名字) → 迁移后的名字：公共的不改；与公共项或别的私有项重名的私有项改名。"""
    count: dict[str, int] = {}
    for _, name in entries:
        count[name] = count.get(name, 0) + 1
    plan = {}
    for owner, name in entries:
        plan[(owner, name)] = (
            name if owner is None or count[name] == 1 else _renamed(name, owner, names[owner])
        )
    if len(set(plan.values())) != len(plan):
        raise RuntimeError(f"cannot make item names unique: {plan}")
    return plan


def _revision(conn: sa.Connection, subject: str, name: str, after: str, message: str) -> None:
    conn.execute(
        sa.text(
            "INSERT INTO revisions (subject, name, before, after, author, message, created_at) "
            "VALUES (:s, :n, NULL, :a, :au, :m, :t)"
        ),
        {"s": subject, "n": name, "a": after, "au": _AUTHOR, "m": message, "t": _now()},
    )


def _insert_item(conn: sa.Connection, **values: object) -> int:
    values = {"description": "", "content": "", "secret": None, "enabled": True, **values}
    now = _now()
    values.setdefault("created_at", now)
    values.setdefault("updated_at", now)
    result = conn.execute(
        sa.text(
            "INSERT INTO items (kind, name, owner, description, content, secret, enabled, created_at, updated_at) "
            "VALUES (:kind, :name, :owner, :description, :content, :secret, :enabled, :created_at, :updated_at)"
        ),
        values,
    )
    return result.lastrowid  # type: ignore[return-value]


def _import_files(conn: sa.Connection, root: Path, agents: dict[str, str]) -> None:
    owners: list[tuple[str | None, Path]] = [(None, root / "public")]
    owners += [(a, root / "agents" / a) for a in sorted(agents)]
    for agent_dir in sorted((root / "agents").glob("*")) if (root / "agents").is_dir() else []:
        if agent_dir.name not in agents:
            log.warning("resources of a deleted agent not imported: %s", agent_dir)

    # skill
    # 读不出来的不导入，也不参与重名（不让同名的私有 skill 白白改名）
    loaded_skills = {
        (owner, d.name): loaded
        for owner, base in owners
        for d in sorted((base / "skills").glob("*"))
        if (d / "SKILL.md").is_file() and (loaded := _skill(d)) is not None
    }
    skill_plan = _plan(list(loaded_skills), agents)
    for (owner, old), (skill_md, description, files) in loaded_skills.items():
        skill_md = _fix_paths(skill_md, skill_plan)
        files = {rel: (_fix_paths(c, skill_plan), x) for rel, (c, x) in files.items()}
        name = skill_plan[(owner, old)]
        if name != old:
            post = frontmatter.loads(skill_md)
            post["name"] = name
            skill_md = frontmatter.dumps(post) + "\n"
            log.warning("private skill renamed: %s -> %s (owner %s)", old, name, owner)
        item = _insert_item(
            conn, kind="skill", name=name, owner=owner, description=description, content=skill_md
        )
        for rel, (content, executable) in files.items():
            conn.execute(
                sa.text(
                    "INSERT INTO item_files (item_id, path, content, executable) VALUES (:i, :p, :c, :x)"
                ),
                {"i": item, "p": rel, "c": content, "x": executable},
            )
        message = "import from resource repository" + (f" (renamed from {old})" if name != old else "")
        _revision(conn, "skill", name, _skill_doc(skill_md, files), message)

    # MCP server
    servers: dict[tuple[str | None, str], dict] = {}
    for owner, base in owners:
        for path in sorted((base / "mcp").glob("*.json")):
            try:
                servers[(owner, path.stem)] = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as e:
                log.warning("MCP server not imported: %s: %s", path, e)
    plan = _plan(list(servers), agents)
    for (owner, old), data in servers.items():
        name = plan[(owner, old)]
        enabled = bool(data.pop("enabled", True))
        _insert_item(conn, kind="mcp", name=name, owner=owner, content=json.dumps(data), enabled=enabled)
        doc = json.dumps({**data, "enabled": enabled}, indent=1)
        _revision(
            conn,
            "mcp",
            name,
            doc,
            "import from resource repository" + (f" (renamed from {old})" if name != old else ""),
        )
        if owner is not None and name != old:
            log.warning("private MCP server renamed: %s -> %s (owner %s)", old, name, owner)
            if enabled and (None, old) in servers:
                # 它原本遮蔽了同名的公共 server：去掉对那个公共 server 的勾选
                row = conn.execute(
                    sa.text("SELECT mcp_servers FROM agents WHERE id = :id"), {"id": owner}
                ).one()
                selected = [n for n in json.loads(row[0]) if n != old]
                conn.execute(
                    sa.text("UPDATE agents SET mcp_servers = :m WHERE id = :id"),
                    {"m": json.dumps(selected), "id": owner},
                )

    # 记忆
    for owner, base in owners:
        path = base / "MEMORY.md"
        content = _fix_paths(path.read_text(encoding="utf-8"), skill_plan) if path.is_file() else None
        if content is None and owner is None:
            continue
        content = content or ""
        conn.execute(
            sa.text("INSERT INTO memories (owner, content, updated_at) VALUES (:o, :c, :t)"),
            {"o": owner, "c": content, "t": _now()},
        )
        _revision(conn, "memory", owner or "", content, "import from resource repository")


def _import_secrets(conn: sa.Connection) -> None:
    rows = conn.execute(
        sa.text(
            'SELECT owner, "key", ciphertext, description, updated_at FROM secrets ORDER BY updated_at, id'
        )
    ).all()
    by_key: dict[str, list] = {}
    for row in rows:
        by_key.setdefault(row[1], []).append(row)
    for key, group in by_key.items():
        owner, _, ciphertext, description, updated_at = group[-1]
        if len(group) > 1:
            owner = None
            log.warning("secret %s had %d copies; kept the latest as a public secret", key, len(group))
        _insert_item(
            conn,
            kind="secret",
            name=key,
            owner=owner,
            description=description,
            secret=ciphertext,
            updated_at=updated_at,
        )
        # 与应用写的同一格式（UTCDateTime 读出带时区）：迁移后第一次修改的前后两版只在真的变了的地方不同
        set_at = datetime.fromisoformat(str(updated_at)).replace(tzinfo=UTC).isoformat()
        message = "import from vault" + (" (merged copies of several agents)" if len(group) > 1 else "")
        _revision(
            conn, "secret", key, json.dumps({"description": description, "value_set_at": set_at}), message
        )


def upgrade() -> None:
    op.create_table(
        "revisions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("subject", sa.String(length=16), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("before", sa.Text(), nullable=True),
        sa.Column("after", sa.Text(), nullable=True),
        sa.Column("author", sa.String(length=64), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sqlite_autoincrement=True,
    )
    with op.batch_alter_table("revisions", schema=None) as batch_op:
        batch_op.create_index("ix_revisions_subject", ["subject", "name", "id"], unique=False)

    op.create_table(
        "items",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("owner", sa.String(length=32), nullable=True),
        sa.Column("description", sa.Text(), server_default="", nullable=False),
        sa.Column("content", sa.Text(), server_default="", nullable=False),
        sa.Column("secret", sa.LargeBinary(), nullable=True),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("1"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["owner"], ["agents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("kind", "name", name="uq_items_kind_name"),
        sqlite_autoincrement=True,
    )
    with op.batch_alter_table("items", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_items_owner"), ["owner"], unique=False)

    op.create_table(
        "memories",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("owner", sa.String(length=32), nullable=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["owner"], ["agents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("owner"),
        sqlite_autoincrement=True,
    )
    op.create_table(
        "item_files",
        sa.Column("item_id", sa.Integer(), nullable=False),
        sa.Column("path", sa.String(length=500), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("executable", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(["item_id"], ["items.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("item_id", "path"),
    )

    conn = op.get_bind()
    agents = dict(conn.execute(sa.text("SELECT id, name FROM agents")).all())
    source = _source(conn)
    if source is not None:
        _import_files(conn, source, agents)
    # 没有资源仓库（新装、测试）或仓库里缺了记忆的 agent：记忆是空的
    for agent_id in agents:
        exists = conn.execute(sa.text("SELECT 1 FROM memories WHERE owner = :o"), {"o": agent_id}).first()
        if exists is None:
            conn.execute(
                sa.text("INSERT INTO memories (owner, content, updated_at) VALUES (:o, '', :t)"),
                {"o": agent_id, "t": _now()},
            )
            _revision(conn, "memory", agent_id, "", "import: no memory file, starts empty")
    _import_secrets(conn)

    with op.batch_alter_table("secrets", schema=None) as batch_op:
        batch_op.drop_index("uq_secrets_private", sqlite_where=sa.text("owner IS NOT NULL"))
        batch_op.drop_index("uq_secrets_public", sqlite_where=sa.text("owner IS NULL"))
    op.drop_table("secrets")

    # 最后一步：旧仓库改名留着（改名后、提交前被杀时重跑迁移从 resources.imported 读）
    if source is not None and source.name == "resources":
        source.rename(source.with_name(_IMPORTED))


def downgrade() -> None:
    op.create_table(
        "secrets",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("owner", sa.String(length=32), nullable=True),
        sa.Column("key", sa.String(length=100), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("secrets", schema=None) as batch_op:
        batch_op.create_index(
            "uq_secrets_private", ["owner", "key"], unique=True, sqlite_where=sa.text("owner IS NOT NULL")
        )
        batch_op.create_index(
            "uq_secrets_public", ["key"], unique=True, sqlite_where=sa.text("owner IS NULL")
        )
    conn = op.get_bind()
    conn.execute(
        sa.text(
            'INSERT INTO secrets (owner, "key", ciphertext, description, updated_at) '
            "SELECT owner, name, secret, description, updated_at FROM items WHERE kind = 'secret'"
        )
    )

    op.drop_table("item_files")
    op.drop_table("memories")
    with op.batch_alter_table("items", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_items_owner"))
    op.drop_table("items")
    with op.batch_alter_table("revisions", schema=None) as batch_op:
        batch_op.drop_index("ix_revisions_subject")
    op.drop_table("revisions")

    database = conn.engine.url.database
    if database and database != ":memory:":
        data = Path(database).parent
        if (data / _IMPORTED).is_dir() and not (data / "resources").exists():
            (data / _IMPORTED).rename(data / "resources")
