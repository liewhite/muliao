"""组合根：唯一一处创建并连接所有服务。"""

import asyncio
import logging
import os
import signal
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from zoneinfo import ZoneInfo

import anyio

from muliao import tool_catalog
from muliao.agent_lifecycle import AgentLifecycle
from muliao.agents import AgentDefaults, AgentService
from muliao.bus import EventBus, LiveRuns
from muliao.config import Settings
from muliao.db import Database, create_engine, migrate
from muliao.delivery.base import DeliverHandler
from muliao.delivery.webpush import PushSubscriptions, VapidKeys, WebPushDelivery
from muliao.errors import InvalidInput
from muliao.evolution.memory import MemoryStore
from muliao.evolution.prompts import PromptStore
from muliao.evolution.skills import SkillStore
from muliao.files import FileStore
from muliao.llm import ModelRegistry, ModelResolver
from muliao.models import TaskKind
from muliao.notifications import Notifications, Notifier
from muliao.platform.messaging import Messaging
from muliao.resources import ResourceRepo
from muliao.runtime.compaction import CompactionHandler, Compactor
from muliao.runtime.consumer import Consumer, RunHandler
from muliao.runtime.content import ContentBuilder
from muliao.runtime.conversation import Threads
from muliao.runtime.delegations import Delegations
from muliao.runtime.finish import RunFinisher
from muliao.runtime.halt import SessionHalt
from muliao.runtime.jobs import JobRunner
from muliao.runtime.mailbox import MailReconciler
from muliao.runtime.mcp import McpRegistry
from muliao.runtime.reflection import ReflectionHandler, ReflectionScheduler
from muliao.runtime.runner import AgentRuntime, RunProfile, ToolGroups
from muliao.runtime.sessions import SessionService
from muliao.runtime.steps import Steps
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
from muliao.runtime.tools.workspace import ShellLimits, session_workspace_toolset, workspace_toolset
from muliao.runtime.work import WorkWatcher
from muliao.runtime.worker import TaskHandler, Worker
from muliao.triggers.precheck import PrecheckRunner
from muliao.triggers.schedule import ScheduleKind, ScheduleRunner
from muliao.triggers.service import EVENT_MAX_BYTES, TriggerService
from muliao.triggers.webhook import WebhookKind, Webhooks
from muliao.vault import Vault
from muliao.web import WebProvider, build_web_provider

log = logging.getLogger(__name__)


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
    messaging: Messaging
    mail: MailReconciler
    jobs: JobRunner
    delegations: Delegations
    work: WorkWatcher
    compactor: Compactor
    reflection: ReflectionScheduler
    consumer: Consumer
    models: ModelRegistry
    files: FileStore
    # 默认就是 models.check，单独持有只为测试替换
    _model_checker: Callable[[str], None]

    @asynccontextmanager
    async def running(self) -> AsyncIterator[None]:
        """服务的整个生命周期：启动检查与恢复，然后在根任务组里跑后台循环（docs/domain-design.md「并发模型」）。
        退出时先取消根任务组（循环与正在进行的运行），它们全部退出之后，再按关停结束后台任务的进程并记为被中断
        （jobs.running 的退出）：这时已经不会有运行再开新的后台任务，结束的就是全部。"""
        try:
            await self._prepare()
            async with self.jobs.running(), anyio.create_task_group() as root:
                root.start_soon(_loop, "worker", self.worker.run_forever)
                root.start_soon(_loop, "schedules", self.schedules.run_forever)
                root.start_soon(_loop, "mail", self.mail.run_forever)
                root.start_soon(_loop, "checks", self.work.run_forever)
                try:
                    yield
                finally:
                    root.cancel_scope.cancel()
        finally:
            await self.db.engine.dispose()

    async def _prepare(self) -> None:
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
        available = set(self.agents.defaults.group_names)
        broken = [
            f"{a.name} ({a.id}): {sorted(set(a.tool_groups) - available)}"
            for a in await self.agents.list()
            if not set(a.tool_groups) <= available
        ]
        if broken:
            raise RuntimeError(
                "agents use tool groups unavailable with current config:\n" + "\n".join(broken)
            )


async def _loop(name: str, run_forever: Callable[[], Awaitable[None]]) -> None:
    """根任务组里的后台循环：意外退出属于 bug 或不可恢复的配置错误，停止进程，交给进程管理器重启并告警。
    不把异常抛给任务组：关停只走一条路径（SIGTERM → 服务器停止接收请求、排空连接 → lifespan 退出 → running
    的退出顺序），不另起一条从任务组内部炸开的关停。"""
    try:
        await run_forever()
    except Exception:
        log.critical("background loop %s died, shutting down", name, exc_info=True)
        os.kill(os.getpid(), signal.SIGTERM)
        return
    log.critical("background loop %s returned, shutting down", name)
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
    groups = tool_catalog.available(web_configured=bool(web_tools))
    tz = ZoneInfo(settings.timezone)
    db = Database(create_engine(settings.db_path))
    bus = EventBus()
    live = LiveRuns(bus)
    queue = TaskQueue(
        db,
        max_attempts=settings.runtime.task_max_attempts,
        retry_base_seconds=settings.runtime.task_retry_base_seconds,
    )
    # 领域事件与定向命令：订阅与命令处理者在下面各领域构造之后登记
    messaging = Messaging(queue)
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
        AgentDefaults(settings.llm.default_model, settings.runtime.default_reflect_every, groups),
    )
    agent_lifecycle = AgentLifecycle(db, agents, queue)
    sessions = SessionService(db, queue, live)
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
        live=live,
        tz=tz,
        resolve_model=resolve_model or models.resolve,
        files=files,
    )
    jobs = JobRunner(db, queue, vault, agents.workspace, settings.runtime.job_check_seconds)
    # 委派停下 thread 要用消费者，消费者停止会话又要停下委派：延迟绑定打破构造顺序上的环
    delegations = Delegations(db, queue, lambda session_id: consumer.stop_session(session_id))
    limits = ShellLimits(
        settings.runtime.shell_default_timeout,
        settings.runtime.shell_max_timeout,
        settings.runtime.tool_output_max_chars,
    )
    # 总是开放：自我改进，以及向用户提问（没有用户会回答的运行里由工具自己隐藏，见 runtime.tools.ask_user）
    always_tools = [
        skills_toolset(skills),
        memory_toolset(memory),
        instructions_toolset(prompts),
        ask_user_toolset(),
    ]
    # 临时 agent 只有文件与 shell（加上调用方的 MCP，以及配置了的网页搜索）：没有记忆、skill、委派，也不能再开临时 agent
    temp_profile = RunProfile(toolsets=[workspace_toolset(vault, limits, files), *web_tools, mcp.toolset()])
    # 每个组的实现，按目录取：目录里有、这里没有的组是代码不一致，在启动时报出
    implementations = {
        # 对话里用的这份带后台任务（只有会话运行会选用工具组；反思不开放它）
        "workspace": [session_workspace_toolset(vault, limits, files, jobs)],
        "temp_agent": (
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
        # stop_thread 用消费者停止会话，而消费者又用这组工具：通过延迟绑定的调用打破构造顺序上的环
        "delegation": (
            [
                delegation_toolset(
                    db,
                    agents,
                    threads,
                    lambda session_id: consumer.stop_session(session_id),
                    settings.runtime.job_check_seconds,
                )
            ]
        ),
        "notify": [notify_toolset(notifier)],
        "agents": [agents_toolset(agents, agent_lifecycle, prompts)],
        "public_skills": [public_skills_toolset(skills)],
        "mcp": [mcp.toolset()],
        "mcp_admin": [mcp_admin_toolset(mcp)],
        "vault": [vault_toolset(vault)],
        "triggers": ([triggers_toolset(triggers, agents, settings.runtime.precheck_timeout_seconds)]),
        "web": web_tools,
    }
    if missing := [g.name for g in tool_catalog.CATALOG if g.name not in implementations]:
        raise RuntimeError(f"tool groups in the catalog without an implementation: {missing}")
    tool_groups = ToolGroups(groups={g.name: implementations[g.name] for g in groups}, always=always_tools)
    compactor = Compactor(
        db,
        runtime,
        queue,
        threshold_tokens=settings.runtime.compaction_threshold_tokens,
        keep_steps=settings.runtime.compaction_keep_steps,
    )
    content = ContentBuilder(files, runtime)
    reflection = ReflectionScheduler(db, queue)
    consumer = Consumer(
        db,
        runtime,
        tool_groups,
        live,
        content,
        Steps(db, live, content, compactor),
        RunFinisher(queue, notifier, reflection, [jobs, delegations]),
        SessionHalt(db, queue, [jobs, delegations]),
        compactor,
    )
    handlers: dict[TaskKind, TaskHandler] = {
        TaskKind.EVENT: messaging,
        TaskKind.RUN: RunHandler(consumer, queue),
        TaskKind.COMPACTION: CompactionHandler(db, compactor),
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
        messaging=messaging,
        mail=MailReconciler(db, queue),
        jobs=jobs,
        delegations=delegations,
        work=WorkWatcher([jobs, delegations], settings.runtime.job_check_seconds),
        compactor=compactor,
        reflection=reflection,
        consumer=consumer,
        models=models,
        files=files,
        _model_checker=model_checker or models.check,
    )
