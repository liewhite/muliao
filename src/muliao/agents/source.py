"""AI 资源的读取入口（AssetSource）：把资源目录树（agents.assets）与数据库里的 agent 身份对上，给出当前的快照。

- 快照按树的指纹（每个文件的路径、修改时间、大小）与数据库里的 agent id 缓存：文件没变就不重读，直接改文件下次读到就
  生效（运行开始时组装指令、算工具与分发、解析 secret，都读它）。
- agent 的配置（描述、模型、思考档位、分组）只在快照里（Snapshot.loaded_agent），数据库只有身份（id、名字、归档、默认）。
  模型或思考档位在当前配置下不能运行的记成 Problem 报给它；它的运行在取模型时当场失败，不沿用旧值。
- 改写一个资源文件（界面编辑记忆、SKILL.md、MCP，文件工具 write_file / edit_file）只经写入口 write()：在锁里按读树的
  规则校验（agents.asset_files.vet，#500、#578）再原子写入，界面与文件工具对同一份内容结论一致。
- 平台的其余写入（vault_set 加密、导入 skill 整个目录、建 / 改名 / 删 agent、改 access.toml、删除与转移）在 writing() 里
  做：与读共用一把锁，读-改-写串行；agent.toml 的规则在 AgentFile 与 check_agent，access.toml 的在 access_file。
- 「资源文件变了」只由 watch() 看文件系统得出（平台自己写的、agent 用 shell 改的、用户在别处改的都算），不按谁写了逐处通知。
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from watchfiles import awatch

from muliao.agents import asset_files
from muliao.agents.active import agent_names_in, deleting_in
from muliao.agents.agent_file import AGENT_FILE, AgentFile, Thinking
from muliao.agents.assets import Problem, Snapshot, fingerprint, load_tree
from muliao.agents.kinds import StoredKind
from muliao.agents.permissions import GLOBAL_MEMORY, ToolTable
from muliao.errors import InvalidInput
from muliao.platform.db import Database
from muliao.platform.fs import real, write_atomic

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Written:
    """写入口写成的一份文件。"""

    # 实际存下的全文（记忆规整过空白，asset_files.vet）
    text: str
    # 写成时的修改时间
    mtime_ns: int


class AssetSource:
    def __init__(
        self,
        db: Database,
        root: Path,
        memory_max_chars: int,
        # (模型, 思考档位) 在当前配置下不能运行时报 InvalidInput（agent_runtime.llm.ModelRegistry.check）
        model_checker: Callable[[str, Thinking], None],
        # 资源文件有改动时调用（watch；组合根给前端发数据变了，schema.ChangedEvent）
        changed: Callable[[], None],
        # 内置工具表（组合根装好工具集后才 bind；快照带着它）
        tools: ToolTable,
    ):
        self._db = db
        self.root = root
        self.memory_max_chars = memory_max_chars
        self._check_model = model_checker
        self._changed = changed
        self._tools = tools
        self._key: object = None
        self._snapshot: Snapshot | None = None
        # 读（重新加载）与平台自己的读-改-写共用一把锁：写入期间不会读到写了一半的树
        self._lock = asyncio.Lock()

    async def snapshot(self) -> Snapshot:
        async with self._lock:
            return await self._current()

    async def _current(self) -> Snapshot:
        async with self._db.transaction() as s:
            known = (await agent_names_in(s)).by_id
            deleting = await deleting_in(s)
        key = (
            await asyncio.to_thread(fingerprint, self.root),
            tuple(sorted(known.items())),
            tuple(sorted(deleting)),
        )
        if key == self._key and self._snapshot is not None:
            return self._snapshot
        snapshot = await asyncio.to_thread(
            load_tree, self.root, known, deleting, self.memory_max_chars, self._snapshot, self._tools
        )
        snapshot = self._checked(snapshot)
        self._key, self._snapshot = key, snapshot
        if snapshot.problems:
            log.warning(
                "asset tree has problems",
                extra={"problems": [f"{p.path}: {p.reason}" for p in snapshot.problems]},
            )
        return snapshot

    def _checked(self, snapshot: Snapshot) -> Snapshot:
        """在当前配置下不能运行的模型与思考档位记成问题（agent 照样加载：界面能看能改，运行时报错）。"""
        problems = []
        for a in snapshot.agents.values():
            try:
                self._check_model(a.file.model, a.file.thinking)
            except InvalidInput as e:
                problems.append(
                    Problem(a.path / AGENT_FILE, f"{e}; the agent cannot run until this is fixed", a.id)
                )
        if not problems:
            return snapshot
        return dataclasses.replace(snapshot, problems=(*snapshot.problems, *problems))

    def contains(self, path: Path) -> bool:
        """path 是资源目录里的文件（按真实路径）。"""
        return asset_files.in_tree(self.root, path)

    def check_agent(self, data: AgentFile) -> None:
        """agent.toml 的内容在当前配置下能运行（模型与思考档位），否则 InvalidInput。格式与字段的规则在 AgentFile。"""
        self._check_model(data.model, data.thinking)

    async def write(
        self, texts: Mapping[Path, str], guard: Callable[[Path], None] | None = None
    ) -> dict[Path, Written]:
        """把资源目录里的这些文件写成 texts：改写资源文件的唯一入口。在锁里按此刻的树逐份校验（asset_files.vet，不合格是
        AssetRejected），再交给 guard 做调用方自己的核对（打开编辑时的版本、读过没有；不过就抛错），全部通过才逐个原子写入；
        有一份不过就一份都不写。返回每份实际存下的（Written）。"""
        async with self.writing() as snapshot:

            def write() -> dict[Path, Written]:
                stored = {
                    p: asset_files.vet(snapshot, p, t, self.memory_max_chars, self.check_agent)
                    for p, t in texts.items()
                }
                if guard is not None:
                    for p in stored:
                        guard(p)
                written = {}
                for p, t in stored.items():
                    write_atomic(p, t)
                    # 写成时就取，仍在锁里：之后别处的写入不会被记成这次的
                    written[p] = Written(t, p.stat().st_mtime_ns)
                return written

            return await asyncio.to_thread(write)

    async def ensure_global_memory(self) -> None:
        """全局记忆归平台、base 固定收录、不能删：资源目录里没有它（全新安装）时建一个空的。启动时调用。"""
        async with self.writing() as snapshot:
            path = snapshot.path_of(StoredKind.MEMORY, GLOBAL_MEMORY, None)
            if not path.exists():
                await asyncio.to_thread(write_atomic, path, "")
                log.info("empty global memory created", extra={"path": str(path)})

    @asynccontextmanager
    async def writing(self) -> AsyncIterator[Snapshot]:
        """平台自己改资源文件：与读串行（读-改-写不会互相覆盖），交出改之前的快照，退出时让缓存失效。锁不可重入：块内
        不要调 snapshot()（会卡死），只用交进来的快照。"""
        async with self._lock:
            try:
                yield await self._current()
            finally:
                self._key = None

    async def watch(self) -> None:
        """常驻：资源目录里的文件有改动（visible_change）就通知一次，密集的改动合成一批。awatch 在线程里等文件系统事件，
        常驻占 anyio 默认线程池的一个。"""
        # 文件系统报的是真实路径（如 macOS 的 /private/var）
        root = real(self.root)
        async for _batch in awatch(
            root, watch_filter=lambda _c, path: visible_change(root, path), debounce=200, step=50
        ):
            self._changed()


def visible_change(root: Path, path: str) -> bool:
    """资源目录 root 下 path 的改动算不算资源文件变了：隐藏的文件与目录（及其中的）不算，同 fingerprint；原子写入的临时
    文件也是隐藏的，rename 到的目标照常算。报来不在 root 下的照算（多通知一次无害）。"""
    p = Path(path)
    return not p.is_relative_to(root) or not any(part.startswith(".") for part in p.relative_to(root).parts)
