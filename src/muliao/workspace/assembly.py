"""工作区领域的装配与交给组合根的部件（platform.wiring.Parts）：后台任务、始终允许、文件与 shell 工具。以 agent 身份起命令
的入口（AgentCommands）与可写路径（Confiner）别的领域构造时就要用，仍由组合根先建好交进来。"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from pydantic_ai import FunctionToolset

from muliao.agents.api import AssetSource, SkillStore
from muliao.files.api import FileStore
from muliao.platform.checks import PeriodicCheck
from muliao.platform.db import Database
from muliao.platform.messaging import Messaging
from muliao.platform.tooling import RunScope
from muliao.platform.wiring import Parts
from muliao.sessions.api import SessionWork
from muliao.workspace.commands import AgentCommands
from muliao.workspace.file_tools import ShellLimits
from muliao.workspace.grants import ScriptGrants
from muliao.workspace.runner import JobRunner
from muliao.workspace.tools import workspace_toolset
from muliao.workspace.writable import Confiner


@dataclass(frozen=True)
class WorkspaceDomain(Parts):
    jobs: JobRunner
    script_grants: ScriptGrants
    _tools: FunctionToolset[RunScope]

    @classmethod
    def build(
        cls,
        *,
        db: Database,
        commands: AgentCommands,
        confiner: Confiner,
        workspace_of: Callable[[str], Path],
        work: SessionWork,
        files: FileStore,
        assets: AssetSource,
        skills: SkillStore,
        tool_output_max_chars: int,
    ) -> WorkspaceDomain:
        jobs = JobRunner(db, commands, workspace_of, work)
        script_grants = ScriptGrants(db)
        tools = workspace_toolset(
            commands,
            ShellLimits(tool_output_max_chars),
            files,
            jobs,
            confiner,
            assets,
            skills,
            script_grants,
        )
        return cls(jobs=jobs, script_grants=script_grants, _tools=tools)

    def toolsets(self) -> Sequence[FunctionToolset[RunScope]]:
        return (self._tools,)

    def subscribe(self, messaging: Messaging) -> None:
        self.jobs.subscribe(messaging)
        self.script_grants.subscribe(messaging)

    def checks(self) -> Sequence[PeriodicCheck]:
        return (self.jobs,)
