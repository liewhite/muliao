"""AI 资源的读取入口（AssetSource）：把资源目录树（agents.assets）与数据库里的 agent 身份对上，给出当前的快照。

- 快照按树的指纹（每个文件的路径、修改时间、大小）与数据库里的 agent id 缓存：文件没变就不重读，直接改文件下次读到就
  生效（运行开始时组装指令、算工具与分发、解析 secret，都读它）。
- 读到的 agent 配置（名字、描述、模型、思考档位、分组）同步到 agents 表的镜像列（agents.active.sync_mirror_in）；模型或
  思考档位在当前配置下不能运行的不同步、记成 Problem，agent 照旧用上次同步的值运行。
- 平台自己写资源（界面、vault_set、建 agent）时在 writing() 里做：读-改-写（access.toml、agent.toml）串行。
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from muliao.agents.active import Mirrored, known_agents_in, sync_mirror_in
from muliao.agents.assets import AGENT_FILE, Problem, Snapshot, fingerprint, load_tree
from muliao.agents.thinking import Thinking, check_thinking_level
from muliao.errors import InvalidInput
from muliao.platform.db import Database

log = logging.getLogger(__name__)


class AssetSource:
    def __init__(
        self,
        db: Database,
        root: Path,
        memory_max_chars: int,
        # (模型, 思考档位) 在当前配置下不能运行时报 InvalidInput（agent_runtime.llm.ModelRegistry.check）
        model_checker: Callable[[str, Thinking], None],
    ):
        self._db = db
        self.root = root
        self.memory_max_chars = memory_max_chars
        self._check_model = model_checker
        self._key: object = None
        self._snapshot: Snapshot | None = None
        self._read_lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()

    async def snapshot(self) -> Snapshot:
        async with self._read_lock:
            async with self._db.transaction() as s:
                known = await known_agents_in(s)
            key = (await asyncio.to_thread(fingerprint, self.root), frozenset(known))
            if key == self._key and self._snapshot is not None:
                return self._snapshot
            snapshot = await asyncio.to_thread(load_tree, self.root, known, self.memory_max_chars)
            snapshot = await self._synced(snapshot)
            self._key, self._snapshot = key, snapshot
            if snapshot.problems:
                log.warning(
                    "asset tree has problems",
                    extra={"problems": [f"{p.path}: {p.reason}" for p in snapshot.problems]},
                )
            return snapshot

    async def _synced(self, snapshot: Snapshot) -> Snapshot:
        """把 agent 配置同步进镜像列；不能运行的模型不同步，记成问题。"""
        mirrored, problems = [], []
        for a in snapshot.agents.values():
            try:
                check_thinking_level(a.thinking)
                self._check_model(a.model, a.thinking)
            except InvalidInput as e:
                problems.append(
                    Problem(a.path / AGENT_FILE, f"{e}; the agent keeps its previous model", a.id)
                )
                continue
            mirrored.append(Mirrored(a.id, a.name, a.description, a.model, a.thinking, a.group))
        async with self._db.transaction() as s:
            changed = await sync_mirror_in(s, mirrored)
        if changed:
            log.info("agent config synced from asset files", extra={"agent_ids": changed})
        if not problems:
            return snapshot
        return dataclasses.replace(snapshot, problems=(*snapshot.problems, *problems))

    def invalidate(self) -> None:
        """平台自己写过文件：下次读重新加载（指纹通常已变，这里只是不依赖文件时间的精度）。"""
        self._key = None

    @asynccontextmanager
    async def writing(self) -> AsyncIterator[Snapshot]:
        """平台自己改资源文件：串行（读-改-写不会互相覆盖），交出改之前的快照，退出时让缓存失效。"""
        async with self._write_lock:
            try:
                yield await self.snapshot()
            finally:
                self.invalidate()
