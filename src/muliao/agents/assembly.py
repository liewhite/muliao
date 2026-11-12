"""agent 领域交给组合根的部件（platform.wiring.Parts）：删除 agent 的流程与它删除文件的任务、管 agent 与 secret 的工具、资源目录的监听。
目录、资源、分发这些对象别的领域在构造时就要用，仍由组合根先建好交进来。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from pydantic_ai import FunctionToolset

from muliao.agents.distribution import Distribution
from muliao.agents.lifecycle import AGENT_REMOVAL_TASK, AgentLifecycle, AgentRemoval, ArchiveBlocker
from muliao.agents.secret_tools import vault_toolset
from muliao.agents.secrets import Vault
from muliao.agents.service import AgentService
from muliao.agents.source import AssetSource
from muliao.agents.tools import agents_toolset
from muliao.platform.db import Database
from muliao.platform.messaging import Messaging
from muliao.platform.tasks import TaskQueue
from muliao.platform.tooling import RunScope
from muliao.platform.wiring import Loop, Parts
from muliao.platform.worker import TaskHandler


@dataclass(frozen=True)
class AgentDomain(Parts):
    lifecycle: AgentLifecycle
    _removal: AgentRemoval
    _assets: AssetSource
    _tools: tuple[FunctionToolset[RunScope], ...]

    @classmethod
    def build(
        cls,
        *,
        db: Database,
        queue: TaskQueue,
        agents: AgentService,
        assets: AssetSource,
        vault: Vault,
        distribution: Distribution,
        messaging: Messaging,
        blockers: Mapping[str, ArchiveBlocker],
    ) -> AgentDomain:
        lifecycle = AgentLifecycle(db, assets, queue, messaging, blockers)
        return cls(
            lifecycle=lifecycle,
            _removal=AgentRemoval(db, agents, assets),
            _assets=assets,
            _tools=(vault_toolset(vault), agents_toolset(agents, distribution, lifecycle)),
        )

    def toolsets(self) -> Sequence[FunctionToolset[RunScope]]:
        return self._tools

    def handlers(self) -> Sequence[tuple[str, TaskHandler]]:
        """删除 agent 的文件与这一行。"""
        return ((AGENT_REMOVAL_TASK, self._removal),)

    def loops(self) -> Sequence[tuple[str, Loop]]:
        """资源目录的监听：服务器上直接改了资源文件，界面跟着刷新（AssetSource.watch）。"""
        return (("assets", self._assets.watch),)
