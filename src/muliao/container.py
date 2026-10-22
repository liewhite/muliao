"""组合根：唯一一处创建并连接所有服务。"""

import asyncio
import logging
import os
import signal
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from zoneinfo import ZoneInfo

import anyio
import httpx
from pydantic_ai import FunctionToolset
from pydantic_ai.toolsets import AbstractToolset, DynamicToolset

from muliao.agent_runtime.llm import ModelRegistry, ModelResolver
from muliao.agent_runtime.mcp import MCP_TOOL_NAMES, McpConnector
from muliao.agent_runtime.runner import AgentRuntime, RunProfile, ToolGroups
from muliao.agent_runtime.temp_agent import clear_temp_dirs, temp_agent_toolset
from muliao.agent_runtime.web import WebProvider, build_web_provider
from muliao.agent_runtime.web_tools import web_toolset
from muliao.agent_runtime.workspace_tools import ShellLimits, workspace_toolset
from muliao.agents import tool_catalog
from muliao.agents.active import ActiveOwners
from muliao.agents.evolution_tools import (
    instructions_toolset,
    memory_toolset,
    public_skills_toolset,
    skills_toolset,
)
from muliao.agents.lifecycle import AgentLifecycle
from muliao.agents.mcp import McpRegistry
from muliao.agents.mcp_tools import mcp_admin_toolset
from muliao.agents.memory import MemoryStore
from muliao.agents.prompts import PromptStore
from muliao.agents.resources import ResourceRepo, RevisionHistory
from muliao.agents.service import AgentDefaults, AgentService
from muliao.agents.skills import SkillStore
from muliao.agents.tools import agents_toolset
from muliao.config import Settings
from muliao.files.store import FileStore
from muliao.issues.api import IssueService
from muliao.issues.tools import issues_toolset
from muliao.jobs.runner import JobRunner
from muliao.jobs.tools import session_workspace_toolset
from muliao.notifications.delivery import DeliverHandler
from muliao.notifications.service import DELIVER_TASK, Notifications
from muliao.notifications.tools import notify_toolset
from muliao.notifications.webpush import PushSubscriptions, VapidKeys, WebPushDelivery
from muliao.platform.bus import EventBus, EventFeed
from muliao.platform.checks import WorkWatcher
from muliao.platform.db import Database, WriteCheck, create_engine, migrate
from muliao.platform.messaging import Messaging
from muliao.platform.retention import TaskRetention
from muliao.platform.tables import EVENT_TASK
from muliao.platform.tasks import Commit, TaskAdmin, TaskQueue
from muliao.platform.tooling import RunScope
from muliao.platform.worker import RunNow, Worker, register_handlers
from muliao.reflection.service import REFLECTION_TASK, ReflectionHandler, ReflectionLog, ReflectionScheduler
from muliao.sessions.agent_history import AgentHistory
from muliao.sessions.api import SessionActivity
from muliao.sessions.ask_user import ask_user_toolset
from muliao.sessions.compaction import CompactionHandler, Compactor
from muliao.sessions.consumer import Consumer, RunHandler
from muliao.sessions.content import ContentBuilder
from muliao.sessions.delegation import ThreadChecks
from muliao.sessions.delegation_tools import delegation_toolset
from muliao.sessions.deletion import AgentSessionsCleanup
from muliao.sessions.finish import RunFinisher
from muliao.sessions.halt import SessionHalt, SessionStopper
from muliao.sessions.lanes import COMPACTION_TASK, RUN_TASK
from muliao.sessions.live import LiveRuns
from muliao.sessions.reconcile import MailReconciler
from muliao.sessions.service import SessionService
from muliao.sessions.steps import Steps
from muliao.sessions.stopping import StoppableRuns
from muliao.sessions.threads import Threads
from muliao.sessions.work import SessionWork, handle_work_commands
from muliao.speech.api import Transcriber, build_transcriber
from muliao.triggers.due import DueRunner
from muliao.triggers.once import OnceDue, OnceKind
from muliao.triggers.precheck import PrecheckRunner
from muliao.triggers.schedule import ScheduleDue, ScheduleKind
from muliao.triggers.service import EVENT_MAX_BYTES, TriggerService
from muliao.triggers.tools import triggers_toolset
from muliao.triggers.webhook import WebhookKind, Webhooks
from muliao.usage.service import UsageLog
from muliao.vault.service import Vault
from muliao.vault.tools import vault_toolset

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Container:
    """HTTP 层看到的组合根：各领域的门面（类型都由领域的 api 模块导出）、平台的展示事件流与「任务」页的操作，以及
    服务的生命周期。HTTP 层只经这些调用领域，拿不到领域内部的对象（消费者、队列的排任务与领取、资源仓库的直接写入、
    总线的发布……）：窄接口（TaskAdmin、RevisionHistory、EventFeed、ReflectionLog）是只转发窄方法的包装对象，运行时
    也拿不到背后的完整对象。tests/test_boundaries 检查字段的声明类型，以及运行时的值恰好是声明的类型。"""

    # SSE：进程内的展示事件（只订阅）
    bus: EventFeed
    # 「任务」页：列出与人工重试
    tasks: TaskAdmin
    vault: Vault
    # 资源的版本历史（只读；写入经各 store）
    resources: RevisionHistory
    skills: SkillStore
    memory: MemoryStore
    prompts: PromptStore
    agents: AgentService
    agent_lifecycle: AgentLifecycle
    sessions: SessionService
    notifications: Notifications
    mcp: McpRegistry
    triggers: TriggerService
    webhooks: Webhooks
    push_subscriptions: PushSubscriptions
    vapid: VapidKeys
    reflection: ReflectionLog
    models: ModelRegistry
    files: FileStore
    usage: UsageLog
    issues: IssueService
    # 语音转文字；没配置 [transcription] 时为 None，界面不显示语音输入
    transcriber: Transcriber | None
    # 服务的整个生命周期（Assembly.running）
    running: Callable[[], AbstractAsyncContextManager[None]]


@dataclass
class Assembly:
    """组合根装配出的全部对象。服务入口（__main__）只取它的 container 交给 HTTP 层；测试夹具直接持有它，以驱动
    worker、消费者等循环与查库。"""

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
    notifications: Notifications
    mcp: McpRegistry
    # 「使用 MCP」组（临时 agent 也用它）
    mcp_tools: DynamicToolset[RunScope]
    triggers: TriggerService
    due_triggers: DueRunner
    webhooks: Webhooks
    push_subscriptions: PushSubscriptions
    vapid: VapidKeys
    worker: Worker
    messaging: Messaging
    threads: Threads
    mail: MailReconciler
    jobs: JobRunner
    thread_checks: ThreadChecks
    work: WorkWatcher
    retention: TaskRetention
    compactor: Compactor
    reflection: ReflectionScheduler
    reflection_log: ReflectionLog
    consumer: Consumer
    # 停止会话的命令（sessions.halt.SessionStopper）
    stopper: SessionStopper
    models: ModelRegistry
    files: FileStore
    usage: UsageLog
    issues: IssueService
    # 联网搜索、语音转写共用的出站 HTTP 客户端
    http: httpx.AsyncClient
    transcriber: Transcriber | None

    @property
    def container(self) -> Container:
        return Container(
            bus=EventFeed(self.bus),
            tasks=TaskAdmin(self.queue),
            vault=self.vault,
            resources=RevisionHistory(self.resources),
            skills=self.skills,
            memory=self.memory,
            prompts=self.prompts,
            agents=self.agents,
            agent_lifecycle=self.agent_lifecycle,
            sessions=self.sessions,
            notifications=self.notifications,
            mcp=self.mcp,
            triggers=self.triggers,
            webhooks=self.webhooks,
            push_subscriptions=self.push_subscriptions,
            vapid=self.vapid,
            reflection=self.reflection_log,
            models=self.models,
            files=self.files,
            usage=self.usage,
            issues=self.issues,
            transcriber=self.transcriber,
            running=self.running,
        )

    @asynccontextmanager
    async def running(self) -> AsyncIterator[None]:
        """服务的整个生命周期：启动检查与恢复，然后在根任务组里跑后台循环（docs/architecture.md「并发模型」）。
        退出时先取消根任务组（循环与正在进行的运行），它们全部退出之后，再按关停结束后台任务的进程并记为被中断
        （jobs.running 的退出）：这时已经不会有运行再开新的后台任务，结束的就是全部。"""
        try:
            await self._prepare()
            async with self.jobs.running(), anyio.create_task_group() as root:
                root.start_soon(_loop, "worker", self.worker.run_forever)
                root.start_soon(_loop, "due", self.due_triggers.run_forever)
                root.start_soon(_loop, "mail", self.mail.run_forever)
                root.start_soon(_loop, "checks", self.work.run_forever)
                root.start_soon(_loop, "retention", self.retention.run_forever)
                try:
                    yield
                finally:
                    root.cancel_scope.cancel()
        finally:
            try:
                await self.db.engine.dispose()
            finally:
                await self.http.aclose()

    async def _prepare(self) -> None:
        await asyncio.to_thread(migrate, self.settings.db_path)
        # git 不可用或仓库损坏时在启动时暴露；上次进程留下的未提交修改与 git 锁在这里收敛（worker 还没开始）
        await self.resources.recover()
        await self.agents.verify_stored()
        await self.queue.check_kinds()
        # 在 worker 恢复任务之前：此时没有临时 agent 在运行
        await asyncio.to_thread(clear_temp_dirs, self.settings.temp_dir)
        await asyncio.to_thread(self.files.recover)
        # 上次运行留下的「运行中」后台任务：进程已随服务结束，记为被中断并通知 agent（worker 处理这些事件之前）
        await self.jobs.recover()
        # 所有循环开始之前恢复上次进程遗留的 running task（Worker.recover）
        await self.worker.recover()


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


def assemble(
    settings: Settings,
    *,
    resolve_model: ModelResolver | None = None,
    model_checker: Callable[[str], None] | None = None,
    web: WebProvider | None = None,
    transcriber: Transcriber | None = None,
    check_writes: WriteCheck | None = None,
) -> Assembly:
    """resolve_model / model_checker / web / transcriber 仅供测试替换；默认由配置中的 LLM providers、[web] 与 [transcription] 构造。check_writes 是
    测试打开的开发期断言（schema.single_domain_writes）。"""
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    models = ModelRegistry(settings.llm)
    # 联网搜索与语音转写共用的出站 HTTP 客户端；不设默认超时，每个调用必须自己传 timeout。容器关停时关闭
    http = httpx.AsyncClient(timeout=None)
    if web is None and settings.web is not None:
        web = build_web_provider(settings.web, http)
    if transcriber is None and settings.transcription is not None:
        transcriber = build_transcriber(settings.transcription, http)
    # 「配置了哪些可选能力」只在这里判断一次，工具组名称、注册表与临时 agent 的工具都由它导出
    web_tools = [] if web is None else [web_toolset(web, settings.runtime.tool_output_max_chars)]
    groups = tool_catalog.available(web_configured=bool(web_tools))
    tz = ZoneInfo(settings.timezone)
    db = Database(create_engine(settings.db_path), check_writes=check_writes)
    bus = EventBus()
    live = LiveRuns(bus)
    queue = TaskQueue(
        db,
        max_attempts=settings.runtime.task_max_attempts,
        retry_base_seconds=settings.runtime.task_retry_base_seconds,
    )
    # 领域事件与定向命令：订阅与命令处理者在下面各领域构造之后登记
    messaging = Messaging(queue)
    # 马上执行已提交的投递（Worker.run_now）：worker 由各领域的处理器组成，在最后构造、绑定
    run_now = RunNow()
    vault = Vault(db, settings.vault.key.get_secret_value())
    files = FileStore(db, settings.files_dir, settings.runtime.upload_max_bytes)
    # 写私有资源之前，资源仓库在它的锁里检查 owner 还可用（agents.active）
    resources = ResourceRepo(settings.resources_dir, ActiveOwners(db))
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
    activity = SessionActivity(queue)
    agent_history = AgentHistory(db)
    reflection = ReflectionScheduler(db, queue, agents, agent_history)
    agent_lifecycle = AgentLifecycle(
        db,
        agents,
        messaging,
        {"sessions": activity.blocks_agent_deletion_in, "reflection": reflection.blocks_agent_deletion_in},
    )
    # 停止会话不依赖消费者：正在进行的运行的登记与 SessionHalt 先构造，消费者用同一份登记运行
    runs: StoppableRuns[Commit] = StoppableRuns(db.exclusive)
    halt = SessionHalt(db, queue, messaging)
    stopper = SessionStopper(runs, halt)
    threads = Threads(queue)
    notifications = Notifications(db, queue, bus)
    # MCP 的配置归 agent 领域，连接归运行时：新增时的连接验证注入给配置，「使用 MCP」组按配置的可用 server 连接
    mcp_connector = McpConnector(vault)
    mcp = McpRegistry(resources, mcp_connector.probe)
    mcp_tools = mcp_connector.toolset(mcp.index)
    schedule_kind, once_kind = ScheduleKind(tz), OnceKind(tz)
    # 触发方式在这里注册：新增一种（如消息队列）= 一个 TriggerKind + 它的激活机制
    precheck = PrecheckRunner(
        vault, agents.workspace, settings.runtime.precheck_timeout_seconds, EVENT_MAX_BYTES
    )
    triggers = TriggerService(
        db,
        {k.name: k for k in (schedule_kind, once_kind, WebhookKind(settings.server.public_url))},
        tz,
        precheck,
        messaging,
        run_now,
    )
    # 按到期时间触发的方式共用一个扫描循环，各自给出到期时间与这次到期被用掉之后这一行怎么变
    due_triggers = DueRunner(db, triggers, [ScheduleDue(activity, schedule_kind, messaging), OnceDue()])
    webhooks = Webhooks(db, triggers)
    issues = IssueService(db, messaging, tz, run_now, activity)
    push_subscriptions = PushSubscriptions(db)
    vapid = VapidKeys(settings.vapid_key_path)

    pusher = WebPushDelivery(push_subscriptions, vapid, settings.push.vapid_subject)

    usage = UsageLog(db, tz)
    runtime = AgentRuntime(
        agents=agents,
        skills=skills,
        memory=memory,
        prompts=prompts,
        tz=tz,
        resolve_model=resolve_model or models.resolve,
        files=files,
        usage=usage,
    )
    jobs = JobRunner(
        db,
        vault,
        agents.workspace,
        settings.runtime.job_check_seconds,
        messaging,
        SessionWork(db),
    )
    thread_checks = ThreadChecks(db, queue)
    # 跨领域的反应：会话领域的命令处理者与各领域的订阅
    handle_work_commands(messaging, queue)
    jobs.subscribe(messaging)
    limits = ShellLimits(
        settings.runtime.shell_default_timeout,
        settings.runtime.shell_max_timeout,
        settings.runtime.tool_output_max_chars,
    )
    # 总是开放：自我改进，以及向用户提问（工具列表保持稳定以命中缓存；没有用户会回答的运行里调用被拒绝，见 sessions.ask_user）
    always_tools = [
        skills_toolset(skills),
        memory_toolset(memory),
        instructions_toolset(prompts),
        ask_user_toolset(),
    ]
    # 临时 agent 只有文件与 shell（加上调用方的 MCP，以及配置了的网页搜索）：没有记忆、skill、委派，也不能再开临时 agent
    temp_profile = RunProfile(toolsets=[workspace_toolset(vault, limits, files), *web_tools, mcp_tools])
    # 每个组的实现，按目录的组名常量登记：与目录的组不一一对应是代码不一致，在启动时报出
    implementations = {
        # 对话里用的这份带后台任务（只有会话运行会选用工具组；反思不开放它）
        tool_catalog.WORKSPACE: [session_workspace_toolset(vault, limits, files, jobs)],
        tool_catalog.TEMP_AGENT: [temp_agent_toolset(runtime, agents, temp_profile, settings.temp_dir)],
        tool_catalog.DELEGATION: [
            delegation_toolset(db, agents, threads, stopper, settings.runtime.job_check_seconds)
        ],
        tool_catalog.NOTIFY: [notify_toolset(notifications)],
        tool_catalog.PUBLIC_SKILLS: [public_skills_toolset(skills)],
        tool_catalog.MCP: [mcp_tools],
        tool_catalog.MCP_ADMIN: [mcp_admin_toolset(mcp)],
        tool_catalog.VAULT: [vault_toolset(vault)],
        tool_catalog.TRIGGERS: [
            triggers_toolset(triggers, agents, settings.runtime.precheck_timeout_seconds)
        ],
        tool_catalog.ISSUES: [issues_toolset(issues, agents)],
        tool_catalog.WEB: web_tools,
    }
    # get_agent 给模型列出每组实际包含的工具（从注册的工具集取，不另写一份）；MCP 的工具每次运行按可见的 server 构建
    group_tools = {
        name: [tool for ts in toolsets for tool in _function_tools(name, ts)]
        for name, toolsets in implementations.items()
        if name != tool_catalog.MCP
    }
    group_tools[tool_catalog.MCP] = [MCP_TOOL_NAMES]
    implementations[tool_catalog.AGENTS] = [agents_toolset(agents, agent_lifecycle, prompts, group_tools)]
    _check_implementations(implementations)
    tool_groups = ToolGroups(groups={g.name: implementations[g.name] for g in groups}, always=always_tools)
    compactor = Compactor(
        db,
        runtime,
        queue,
        threshold_tokens=settings.runtime.compaction_threshold_tokens,
        keep_steps=settings.runtime.compaction_keep_steps,
    )
    content = ContentBuilder(files, runtime)
    sessions = SessionService(db, queue, live, messaging, stopper, compactor, run_now)
    # 删除 agent 与会话时各领域清理自己的数据
    notifications.subscribe(messaging)
    agent_lifecycle.subscribe(messaging)
    sessions.subscribe(messaging)
    AgentSessionsCleanup(db, queue, messaging, stopper).subscribe(messaging)
    vault.subscribe(messaging)
    triggers.subscribe(messaging)
    issues.subscribe(messaging)
    reflection.subscribe(messaging)
    consumer = Consumer(
        db,
        runtime,
        tool_groups,
        live,
        content,
        Steps(db, live, content, compactor),
        RunFinisher(queue, messaging),
        runs,
        halt,
        compactor,
    )
    # 各领域的任务种类在这里登记处理器；队列只接受登记过的种类（TaskQueue.accept）
    worker = Worker(
        queue,
        register_handlers(
            (EVENT_TASK, messaging),
            (RUN_TASK, RunHandler(consumer, queue, messaging)),
            (COMPACTION_TASK, CompactionHandler(db, compactor)),
            (REFLECTION_TASK, ReflectionHandler(db, runtime, tool_groups, agents, agent_history)),
            (DELIVER_TASK, DeliverHandler(db, pusher)),
        ),
        bus,
    )
    run_now.bind(worker)
    queue.accept(worker.kinds)

    return Assembly(
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
        notifications=notifications,
        mcp=mcp,
        mcp_tools=mcp_tools,
        triggers=triggers,
        due_triggers=due_triggers,
        webhooks=webhooks,
        push_subscriptions=push_subscriptions,
        vapid=vapid,
        worker=worker,
        messaging=messaging,
        threads=threads,
        mail=MailReconciler(db, queue),
        jobs=jobs,
        thread_checks=thread_checks,
        work=WorkWatcher([jobs, thread_checks], settings.runtime.job_check_seconds),
        # 只清理投递类的任务（docs/architecture.md 3.2「保留」）
        retention=TaskRetention(queue, prunable=(EVENT_TASK, DELIVER_TASK)),
        compactor=compactor,
        reflection=reflection,
        reflection_log=ReflectionLog(db, agent_history),
        consumer=consumer,
        stopper=stopper,
        models=models,
        files=files,
        usage=usage,
        issues=issues,
        http=http,
        transcriber=transcriber,
    )


def _check_implementations(implementations: Mapping[str, object]) -> None:
    """组合根登记的组必须与目录的组一一对应：缺实现的组选了没工具，多出的组选不到、被静默丢掉。"""
    catalog = {g.name for g in tool_catalog.CATALOG}
    if set(implementations) != catalog:
        raise RuntimeError(
            "tool group implementations do not match the catalog: "
            f"missing {sorted(catalog - set(implementations))}, unknown {sorted(set(implementations) - catalog)}"
        )


def _function_tools(group: str, toolset: AbstractToolset[RunScope]) -> list[str]:
    """一个静态工具集里注册的工具名；静态的组只由 FunctionToolset 组成，别的是代码不一致。"""
    if not isinstance(toolset, FunctionToolset):
        raise RuntimeError(
            f"tool group {group} has a toolset whose tools are not known up front: {toolset!r}"
        )
    return list(toolset.tools)
