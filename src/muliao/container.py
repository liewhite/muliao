"""组合根：唯一一处创建并连接所有服务。"""

import asyncio
import logging
import os
import signal
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any
from zoneinfo import ZoneInfo

from muliao.agent_lifecycle import AgentLifecycle
from muliao.agents import AgentDefaults, AgentService
from muliao.bus import EventBus
from muliao.config import Settings
from muliao.db import Database, create_engine, migrate
from muliao.delivery.base import DeliverHandler, Notifier
from muliao.delivery.webpush import PushSubscriptions, VapidKeys, WebPushDelivery
from muliao.errors import InvalidInput
from muliao.evolution.memory import MemoryStore
from muliao.evolution.prompts import PromptStore
from muliao.evolution.skills import SkillStore
from muliao.llm import ModelRegistry, ModelResolver
from muliao.models import TaskKind
from muliao.resources import ResourceRepo
from muliao.runtime.compaction import CompactionHandler, CompactionScheduler
from muliao.runtime.conversation import Threads
from muliao.runtime.mcp import McpRegistry
from muliao.runtime.reflection import ReflectionHandler
from muliao.runtime.runner import AgentRuntime, RunProfile, ToolGroup, ToolGroups
from muliao.runtime.sessions import SessionService
from muliao.runtime.tasks import TaskQueue
from muliao.runtime.tools.agents import agents_toolset
from muliao.runtime.tools.delegation import delegation_toolset
from muliao.runtime.tools.evolution import (
    instructions_toolset,
    memory_toolset,
    public_skills_toolset,
    skills_toolset,
)
from muliao.runtime.tools.mcp import mcp_admin_toolset
from muliao.runtime.tools.notify import notify_toolset
from muliao.runtime.tools.temp_agent import clear_temp_dirs, temp_agent_toolset
from muliao.runtime.tools.triggers import triggers_toolset
from muliao.runtime.tools.vault import vault_toolset
from muliao.runtime.tools.workspace import ShellLimits, workspace_toolset
from muliao.runtime.turn import TurnHandler, TurnRunner
from muliao.runtime.worker import TaskHandler, Worker
from muliao.triggers.schedule import ScheduleKind, ScheduleRunner
from muliao.triggers.service import TriggerService
from muliao.triggers.webhook import WebhookKind, Webhooks
from muliao.vault import Vault

log = logging.getLogger(__name__)

# agent 可选用的工具组（名称是 agent 配置的一部分，在这里唯一声明；注册表必须与它完全一致）
TOOL_GROUPS = (
    "workspace",
    "temp_agent",
    "delegation",
    "notify",
    "agents",
    "public_skills",
    "mcp",
    "mcp_admin",
    "vault",
    "triggers",
)


@dataclass
class Container:
    settings: Settings
    db: Database
    bus: EventBus
    queue: TaskQueue
    vault: Vault
    resources: ResourceRepo
    skills: SkillStore
    memory: MemoryStore
    prompts: PromptStore
    agents: AgentService
    agent_lifecycle: AgentLifecycle
    sessions: SessionService
    notifier: Notifier
    mcp: McpRegistry
    triggers: TriggerService
    schedules: ScheduleRunner
    webhooks: Webhooks
    push_subscriptions: PushSubscriptions
    vapid: VapidKeys
    worker: Worker
    compaction: CompactionScheduler
    models: ModelRegistry
    # 默认就是 models.check，单独持有只为测试替换
    _model_checker: Callable[[str], None]
    _background: list[asyncio.Task[None]] = field(default_factory=list)

    def _spawn(self, name: str, coro: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(coro, name=name)
        task.add_done_callback(_crash_on_failure)
        self._background.append(task)

    async def start(self) -> None:
        await asyncio.to_thread(migrate, self.settings.db_path)
        # git 不可用或仓库损坏时在启动时暴露；上次进程留下的未提交修改与 git 锁在这里收敛（worker 还没开始）
        await self.resources.recover()
        await self._check_agent_models()
        await self._check_orphan_resources()
        # 在 worker 恢复任务之前：此时没有临时 agent 在运行
        await asyncio.to_thread(clear_temp_dirs, self.settings.temp_dir)
        self._spawn("worker", self.worker.run_forever())
        self._spawn("schedules", self.schedules.run_forever())

    async def _check_orphan_resources(self) -> None:
        """数据库里已删除、资源仓库里还留着目录的 agent（删除时提交失败所致）：记录出来，由人决定是否清理。"""
        known = {a.id for a in await self.agents.list()}
        orphans = [o for o in self.resources.agent_owners() if o not in known]
        if orphans:
            log.warning("resource directories of deleted agents remain", extra={"agent_ids": orphans})

    async def _check_agent_models(self) -> None:
        """已存 agent 的模型必须能被当前配置解析：配置改动（删 provider、改名）在启动时暴露，而不是散落到每个任务里失败。"""
        broken = []
        for agent in await self.agents.list():
            try:
                self._model_checker(agent.model)
            except InvalidInput as e:
                broken.append(f"{agent.name} ({agent.id}): {e}")
        if broken:
            raise RuntimeError(
                "agents reference models not resolvable with current config:\n" + "\n".join(broken)
            )

    async def stop(self) -> None:
        for task in self._background:
            task.cancel()
        await asyncio.gather(*self._background, return_exceptions=True)
        await self.db.engine.dispose()


def _crash_on_failure(task: asyncio.Task[None]) -> None:
    """后台循环意外退出属于 bug 或不可恢复的配置错误：停止进程，交给进程管理器重启并告警。"""
    if task.cancelled():
        return
    exc = task.exception()
    log.critical("background loop %s died, shutting down: %r", task.get_name(), exc, exc_info=exc)
    os.kill(os.getpid(), signal.SIGTERM)


def build_container(
    settings: Settings,
    *,
    resolve_model: ModelResolver | None = None,
    model_checker: Callable[[str], None] | None = None,
) -> Container:
    """resolve_model / model_checker 仅供测试替换；默认由配置中的 LLM providers 构造。"""
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    models = ModelRegistry(settings.llm)
    tz = ZoneInfo(settings.timezone)
    db = Database(create_engine(settings.db_path))
    bus = EventBus()
    queue = TaskQueue(
        db,
        max_attempts=settings.runtime.task_max_attempts,
        retry_base_seconds=settings.runtime.task_retry_base_seconds,
    )
    vault = Vault(db, settings.vault.key.get_secret_value())
    resources = ResourceRepo(settings.resources_dir)
    skills = SkillStore(resources)
    memory = MemoryStore(resources, settings.runtime.memory_max_chars)
    prompts = PromptStore(resources)
    agents = AgentService(
        db,
        settings.workspaces_dir,
        resources,
        memory,
        model_checker or models.check,
        prompts,
        AgentDefaults(settings.llm.default_model, settings.runtime.default_reflect_every, TOOL_GROUPS),
    )
    agent_lifecycle = AgentLifecycle(db, agents)
    sessions = SessionService(db, queue)
    threads = Threads(queue)
    notifier = Notifier(db, queue)
    mcp = McpRegistry(resources, vault)
    schedule_kind = ScheduleKind(tz)
    # 触发方式在这里注册：新增一种（如消息队列）= 一个 TriggerKind + 它的激活机制
    triggers = TriggerService(
        db, queue, {k.name: k for k in (schedule_kind, WebhookKind(settings.server.public_url))}, tz
    )
    schedules = ScheduleRunner(db, triggers, schedule_kind)
    webhooks = Webhooks(db, triggers)
    push_subscriptions = PushSubscriptions(db)
    vapid = VapidKeys(settings.vapid_key_path)

    pusher = WebPushDelivery(push_subscriptions, vapid, settings.push.vapid_subject)

    runtime = AgentRuntime(
        agents=agents,
        skills=skills,
        memory=memory,
        prompts=prompts,
        mcp=mcp,
        bus=bus,
        tz=tz,
        resolve_model=resolve_model or models.resolve,
    )
    workspace_tools = workspace_toolset(
        vault,
        ShellLimits(
            settings.runtime.shell_default_timeout,
            settings.runtime.shell_max_timeout,
            settings.runtime.tool_output_max_chars,
        ),
    )
    evolution_tools = [skills_toolset(skills), memory_toolset(memory), instructions_toolset(prompts)]
    # 临时 agent 只有文件与 shell（加上调用方的 MCP）：没有记忆、skill、委派，也不能再开临时 agent
    temp_profile = RunProfile(toolsets=[workspace_tools], include_mcp=True)
    tool_groups = ToolGroups(
        groups={
            "workspace": ToolGroup([workspace_tools]),
            "temp_agent": ToolGroup(
                [
                    temp_agent_toolset(
                        runtime,
                        agents,
                        temp_profile,
                        settings.temp_dir,
                        settings.runtime.temp_agent_request_limit,
                    )
                ]
            ),
            # ask_agent 用回合执行器跑子 agent，而执行器又用这组工具：通过延迟绑定的调用打破构造顺序上的环
            "delegation": ToolGroup(
                [
                    delegation_toolset(
                        db,
                        agents,
                        threads,
                        lambda session_id, prompt: turn_runner.run_inline(session_id, prompt),
                    )
                ]
            ),
            "notify": ToolGroup([notify_toolset(notifier)]),
            "agents": ToolGroup([agents_toolset(agents, agent_lifecycle, prompts)]),
            "public_skills": ToolGroup([public_skills_toolset(skills)]),
            "mcp": ToolGroup(uses_mcp=True),
            "mcp_admin": ToolGroup([mcp_admin_toolset(mcp)]),
            "vault": ToolGroup([vault_toolset(vault)]),
            "triggers": ToolGroup([triggers_toolset(triggers, agents)]),
        },
        always=evolution_tools,
    )
    if tuple(tool_groups.groups) != TOOL_GROUPS:
        raise RuntimeError(f"tool group registry {tuple(tool_groups.groups)} differs from {TOOL_GROUPS}")
    compaction = CompactionScheduler(queue, settings.runtime.compaction_threshold_tokens)
    turn_runner = TurnRunner(db, runtime, tool_groups, queue, notifier, compaction)
    handlers: dict[TaskKind, TaskHandler] = {
        TaskKind.TURN: TurnHandler(turn_runner),
        TaskKind.COMPACTION: CompactionHandler(
            db, runtime, compaction, settings.runtime.compaction_keep_turns
        ),
        TaskKind.REFLECTION: ReflectionHandler(db, runtime, tool_groups),
        TaskKind.DELIVER: DeliverHandler(pusher),
    }
    worker = Worker(
        queue,
        handlers,
        bus,
        concurrency=settings.runtime.worker_concurrency,
        task_timeout_seconds=settings.runtime.task_timeout_seconds,
    )

    return Container(
        settings=settings,
        db=db,
        bus=bus,
        queue=queue,
        vault=vault,
        resources=resources,
        skills=skills,
        memory=memory,
        prompts=prompts,
        agents=agents,
        agent_lifecycle=agent_lifecycle,
        sessions=sessions,
        notifier=notifier,
        mcp=mcp,
        triggers=triggers,
        schedules=schedules,
        webhooks=webhooks,
        push_subscriptions=push_subscriptions,
        vapid=vapid,
        worker=worker,
        compaction=compaction,
        models=models,
        _model_checker=model_checker or models.check,
    )
