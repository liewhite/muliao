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
from muliao.bus import EventBus, LiveRuns
from muliao.config import Settings
from muliao.db import Database, create_engine, migrate
from muliao.delivery.base import DeliverHandler, Notifications, Notifier
from muliao.delivery.webpush import PushSubscriptions, VapidKeys, WebPushDelivery
from muliao.errors import InvalidInput
from muliao.evolution.memory import MemoryStore
from muliao.evolution.prompts import PromptStore
from muliao.evolution.skills import SkillStore
from muliao.files import FileStore
from muliao.llm import ModelRegistry, ModelResolver
from muliao.models import TaskKind
from muliao.resources import ResourceRepo
from muliao.runtime.compaction import CompactionHandler, CompactionScheduler
from muliao.runtime.consumer import Consumer, RunHandler
from muliao.runtime.conversation import Threads
from muliao.runtime.jobs import JobRunner
from muliao.runtime.mailbox import MailReconciler
from muliao.runtime.mcp import McpRegistry
from muliao.runtime.reflection import ReflectionHandler
from muliao.runtime.reports import ReportWatcher
from muliao.runtime.runner import AgentRuntime, RunProfile, ToolGroup, ToolGroups
from muliao.runtime.sessions import SessionService
from muliao.runtime.tasks import TaskQueue
from muliao.runtime.tools.agents import agents_toolset
from muliao.runtime.tools.ask_user import ask_user_toolset
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
from muliao.runtime.tools.web import web_toolset
from muliao.runtime.tools.workspace import ShellLimits, workspace_toolset
from muliao.runtime.worker import TaskHandler, Worker
from muliao.triggers.precheck import PrecheckRunner
from muliao.triggers.schedule import ScheduleKind, ScheduleRunner
from muliao.triggers.service import EVENT_MAX_BYTES, TriggerService
from muliao.triggers.webhook import WebhookKind, Webhooks
from muliao.vault import Vault
from muliao.web import WebProvider, build_web_provider

log = logging.getLogger(__name__)

# agent 可选用的工具组（名称是 agent 配置的一部分，在这里唯一声明；注册表必须与它完全一致）。
# 依赖可选配置的组（web 依赖 [web]）只在配置了时追加在后面，见 build_container 里的 optional_groups
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
    live: LiveRuns
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
    notifications: Notifications
    mcp: McpRegistry
    triggers: TriggerService
    schedules: ScheduleRunner
    webhooks: Webhooks
    push_subscriptions: PushSubscriptions
    vapid: VapidKeys
    worker: Worker
    mail: MailReconciler
    jobs: JobRunner
    reports: ReportWatcher
    compaction: CompactionScheduler
    consumer: Consumer
    models: ModelRegistry
    files: FileStore
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
        await self._check_agent_tool_groups()
        await self._check_orphan_resources()
        # 在 worker 恢复任务之前：此时没有临时 agent 在运行
        await asyncio.to_thread(clear_temp_dirs, self.settings.temp_dir)
        await asyncio.to_thread(self.files.recover)
        # 上次运行留下的「运行中」后台任务：进程已随服务结束，记为被中断并通知 agent（worker 处理这些事件之前）
        await self.jobs.recover()
        self._spawn("worker", self.worker.run_forever())
        self._spawn("schedules", self.schedules.run_forever())
        self._spawn("mail", self.mail.run_forever())
        self._spawn("checks", self._check_running_work())

    async def _check_running_work(self) -> None:
        """运行中的检查：每隔 job_check_seconds，给还在跑的后台任务、结果要交回的 thread 各投一封检查信件。"""
        while True:
            await asyncio.sleep(self.settings.runtime.job_check_seconds)
            await self.jobs.check()
            await self.reports.check()

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

    async def _check_agent_tool_groups(self) -> None:
        """已存 agent 选用的工具组必须都可用：去掉 [web] 配置后，选了 web 的 agent 在启动时暴露，而不是每轮都失败。"""
        available = set(self.agents.defaults.tool_groups)
        broken = [
            f"{a.name} ({a.id}): {sorted(set(a.tool_groups) - available)}"
            for a in await self.agents.list()
            if not set(a.tool_groups) <= available
        ]
        if broken:
            raise RuntimeError(
                "agents use tool groups unavailable with current config:\n" + "\n".join(broken)
            )

    async def stop(self) -> None:
        # 后台任务的进程不随服务退出（自成进程组），主动杀掉；下次启动时记为被中断
        await self.jobs.stop()
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
    web: WebProvider | None = None,
) -> Container:
    """resolve_model / model_checker / web 仅供测试替换；默认由配置中的 LLM providers 与 [web] 构造。"""
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    models = ModelRegistry(settings.llm)
    if web is None and settings.web is not None:
        web = build_web_provider(settings.web)
    # 「配置了哪些可选能力」只在这里判断一次，工具组名称、注册表与临时 agent 的工具都由它导出
    web_tools = [] if web is None else [web_toolset(web, settings.runtime.tool_output_max_chars)]
    optional_groups = {"web": ToolGroup(web_tools)} if web_tools else {}
    tool_group_names = (*TOOL_GROUPS, *optional_groups)
    tz = ZoneInfo(settings.timezone)
    db = Database(create_engine(settings.db_path))
    bus = EventBus()
    live = LiveRuns(bus)
    queue = TaskQueue(
        db,
        max_attempts=settings.runtime.task_max_attempts,
        retry_base_seconds=settings.runtime.task_retry_base_seconds,
    )
    vault = Vault(db, settings.vault.key.get_secret_value())
    files = FileStore(db, settings.files_dir, settings.runtime.upload_max_bytes)
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
        AgentDefaults(settings.llm.default_model, settings.runtime.default_reflect_every, tool_group_names),
    )
    agent_lifecycle = AgentLifecycle(db, agents)
    sessions = SessionService(db, queue)
    threads = Threads(queue)
    notifier = Notifier(db, queue)
    mcp = McpRegistry(resources, vault)
    schedule_kind = ScheduleKind(tz)
    # 触发方式在这里注册：新增一种（如消息队列）= 一个 TriggerKind + 它的激活机制
    precheck = PrecheckRunner(
        vault, agents.workspace, settings.runtime.precheck_timeout_seconds, EVENT_MAX_BYTES
    )
    triggers = TriggerService(
        db,
        queue,
        {k.name: k for k in (schedule_kind, WebhookKind(settings.server.public_url))},
        tz,
        precheck,
        notifier,
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
        live=live,
        tz=tz,
        resolve_model=resolve_model or models.resolve,
        files=files,
    )
    jobs = JobRunner(db, queue, vault, agents.workspace, settings.runtime.job_check_seconds)
    workspace_tools = workspace_toolset(
        vault,
        ShellLimits(
            settings.runtime.shell_default_timeout,
            settings.runtime.shell_max_timeout,
            settings.runtime.tool_output_max_chars,
        ),
        files,
        jobs,
    )
    # 总是开放：自我改进，以及向用户提问（没有用户会回答的运行里由工具自己隐藏，见 runtime.tools.ask_user）
    always_tools = [
        skills_toolset(skills),
        memory_toolset(memory),
        instructions_toolset(prompts),
        ask_user_toolset(),
    ]
    # 临时 agent 只有文件与 shell（加上调用方的 MCP，以及配置了的网页搜索）：没有记忆、skill、委派，也不能再开临时 agent
    temp_profile = RunProfile(toolsets=[workspace_tools, *web_tools], include_mcp=True)
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
            # ask_agent 用消费者跑子 agent，而消费者又用这组工具：通过延迟绑定的调用打破构造顺序上的环
            "delegation": ToolGroup(
                [
                    delegation_toolset(
                        db,
                        agents,
                        threads,
                        lambda session_id, prompt: consumer.run_inline(session_id, prompt),
                        lambda session_id: consumer.stop_session(session_id),
                        settings.runtime.job_check_seconds,
                    )
                ]
            ),
            "notify": ToolGroup([notify_toolset(notifier)]),
            "agents": ToolGroup([agents_toolset(agents, agent_lifecycle, prompts)]),
            "public_skills": ToolGroup([public_skills_toolset(skills)]),
            "mcp": ToolGroup(uses_mcp=True),
            "mcp_admin": ToolGroup([mcp_admin_toolset(mcp)]),
            "vault": ToolGroup([vault_toolset(vault)]),
            "triggers": ToolGroup(
                [triggers_toolset(triggers, agents, settings.runtime.precheck_timeout_seconds)]
            ),
            **optional_groups,
        },
        always=always_tools,
    )
    if tuple(tool_groups.groups) != tool_group_names:
        raise RuntimeError(f"tool group registry {tuple(tool_groups.groups)} differs from {tool_group_names}")
    compaction = CompactionScheduler(queue, settings.runtime.compaction_threshold_tokens)
    consumer = Consumer(
        db, runtime, tool_groups, queue, notifier, compaction, files, live, bus, jobs.stop_session
    )
    handlers: dict[TaskKind, TaskHandler] = {
        TaskKind.RUN: RunHandler(consumer, queue),
        TaskKind.COMPACTION: CompactionHandler(
            db, runtime, compaction, settings.runtime.compaction_keep_turns
        ),
        TaskKind.REFLECTION: ReflectionHandler(db, runtime, tool_groups),
        TaskKind.DELIVER: DeliverHandler(db, pusher),
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
        live=live,
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
        notifications=Notifications(db),
        mcp=mcp,
        triggers=triggers,
        schedules=schedules,
        webhooks=webhooks,
        push_subscriptions=push_subscriptions,
        vapid=vapid,
        worker=worker,
        mail=MailReconciler(db, queue),
        jobs=jobs,
        reports=ReportWatcher(db, queue),
        compaction=compaction,
        consumer=consumer,
        models=models,
        files=files,
        _model_checker=model_checker or models.check,
    )
