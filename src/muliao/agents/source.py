"""AI 资源的读取入口（AssetSource）：把资源目录树（agents.assets）与数据库里的 agent 身份对上，给出当前的快照。

- 快照按树的指纹（每个文件的路径、修改时间、大小）与数据库里的 agent id 缓存：文件没变就不重读，直接改文件下次读到就
  生效（运行开始时组装指令、算工具与分发、解析 secret，都读它）。
- agent 的配置（描述、模型、思考档位、分组）只在快照里（Snapshot.loaded_agent），数据库只有身份（id、名字、归档、默认）。
  模型或思考档位在当前配置下不能运行的记成 Problem 报给它；它的运行在取模型时当场失败，不沿用旧值。
- 平台自己写资源（界面、vault_set、建 / 改名 / 删 agent）时在 writing() 里做：与读共用一把锁，读-改-写串行。
- 文件工具写资源目录里的文件之前问 contains / vet（agents.asset_files）：按读树的规则校验格式（#500）。
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from muliao.agents import asset_files
from muliao.agents.active import agent_names_in
from muliao.agents.agent_file import AGENT_FILE
from muliao.agents.assets import Problem, Snapshot, fingerprint, load_tree
from muliao.agents.kinds import StoredKind
from muliao.agents.permissions import GLOBAL_MEMORY
from muliao.agents.thinking import Thinking, check_thinking_level
from muliao.errors import InvalidInput
from muliao.platform.db import Database
from muliao.platform.fs import write_atomic

log = logging.getLogger(__name__)


class AssetSource:
    def __init__(
        self,
        db: Database,
        root: Path,
        memory_max_chars: int,
        # (模型, 思考档位) 在当前配置下不能运行时报 InvalidInput（agent_runtime.llm.ModelRegistry.check）
        model_checker: Callable[[str, Thinking], None],
        # 平台自己改完资源文件之后调用（组合根给前端发 agents 的数据变了，schema.ChangedEvent）
        changed: Callable[[], None],
    ):
        self._db = db
        self.root = root
        self.memory_max_chars = memory_max_chars
        self._check_model = model_checker
        self._changed = changed
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
        key = (await asyncio.to_thread(fingerprint, self.root), tuple(sorted(known.items())))
        if key == self._key and self._snapshot is not None:
            return self._snapshot
        snapshot = await asyncio.to_thread(load_tree, self.root, known, self.memory_max_chars, self._snapshot)
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
                check_thinking_level(a.thinking)
                self._check_model(a.model, a.thinking)
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

    async def vet(self, path: Path, text: str) -> None:
        """文件工具把资源目录里的 path 写成 text 之前：不合格式时 InvalidInput（agents.asset_files）。"""
        snapshot = await self.snapshot()
        await asyncio.to_thread(
            asset_files.vet, snapshot, path, text, self.memory_max_chars, self._check_model
        )

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
                # 失败的写也可能改过文件（再改回去），一律通知
                self._changed()
