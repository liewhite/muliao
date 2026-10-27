"""组合根：唯一一处创建并连接所有服务。"""

import asyncio
import logging
import os
import signal
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from zoneinfo import ZoneInfo

import anyio
import httpx

from muliao.agent_runtime.api import AgentRuntime, ModelRegistry, ShellLimits, Toolbox
from muliao.agent_runtime.llm import ModelResolver
from muliao.agent_runtime.mcp import McpConnector
from muliao.agents.api import (
    AgentLifecycle,
    AgentService,
    ItemCatalog,
    McpRegistry,
    MemoryStore,
    RoleService,
    SecretItems,
    SkillStore,
    Thinking,
)
from muliao.agents.evolution_tools import (
    memory_toolset,
    skills_toolset,
)
from muliao.agents.mcp_tools import mcp_admin_toolset
from muliao.agents.permissions import available_tools
from muliao.agents.service import AgentDefaults
from muliao.agents.tools import agents_toolset
from muliao.config import RuntimeConfig, Settings
from muliao.files.api import FileStore
from muliao.history.api import HistoryLog
from muliao.history.tools import history_toolset
from muliao.issues.api import SESSION_NOTE as ISSUE_SESSION_NOTE
from muliao.issues.api import IssueService
from muliao.issues.tools import issues_toolset
from muliao.jobs.api import JobRunner
from muliao.jobs.tools import workspace_toolset
from muliao.notifications.api import DELIVER_TASK, Notifications, PushSubscriptions, VapidKeys
from muliao.notifications.delivery import DeliverHandler
from muliao.notifications.tools import notify_toolset
from muliao.notifications.webpush import WebPushDelivery
from muliao.platform.bus import EventBus, EventFeed
from muliao.platform.checks import WorkWatcher
from muliao.platform.db import Database, WriteCheck, create_engine, migrate
from muliao.platform.messaging import Messaging
from muliao.platform.retention import TaskRetention
from muliao.platform.tables import EVENT_TASK
from muliao.platform.tasks import Retries, RetryPolicy, TaskAdmin, TaskQueue
from muliao.platform.worker import RunNow, Worker, register_handlers
from muliao.sessions.api import OWN_NOTES, Origin, OriginNotes, SessionDomain, SessionPorts, SessionService
from muliao.sessions.ask_user import ask_user_toolset
from muliao.sessions.compaction import Compactor
from muliao.sessions.consumer import Consumer
from muliao.sessions.delegation import ThreadChecks
from muliao.sessions.halt import SessionStopper
from muliao.sessions.live import LiveRuns
from muliao.sessions.reconcile import MailReconciler
from muliao.sessions.threads import Threads
from muliao.speech.api import Transcriber, build_transcriber
from muliao.triggers.api import EVENT_MAX_BYTES, TriggerService, Webhooks
from muliao.triggers.api import SESSION_NOTE as TRIGGER_SESSION_NOTE
from muliao.triggers.due import DueRunner
from muliao.triggers.once import OnceDue, OnceKind
from muliao.triggers.precheck import PrecheckRunner
from muliao.triggers.schedule import ScheduleDue, ScheduleKind
from muliao.triggers.stream import StreamKind, StreamRunner
from muliao.triggers.tools import triggers_toolset
from muliao.triggers.webhook import WebhookKind
from muliao.usage.api import UsageLog
from muliao.vault.api import Vault
from muliao.vault.tools import vault_toolset
from muliao.web.api import WebProvider, build_web_provider, web_toolset

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Container:
    """HTTP 层看到的组合根：各领域的门面（类型都由领域的 api 模块导出）、平台的展示事件流与「任务」页的操作，以及
    服务的生命周期。HTTP 层只经这些调用领域，拿不到领域内部的对象（消费者、队列的排任务与领取、
    总线的发布……）：窄接口（TaskAdmin、EventFeed）是只转发窄方法的包装对象，运行时
    也拿不到背后的完整对象。tests/test_boundaries 检查字段的声明类型，以及运行时的值恰好是声明的类型。"""

    # SSE：进程内的展示事件（只订阅）
    bus: EventFeed
    # 「任务」页：列出与人工重试
    tasks: TaskAdmin
    vault: Vault
    skills: SkillStore
    memory: MemoryStore
    agents: AgentService
    roles: RoleService
    # 条目池：全部条目、预览、转移、条目的版本历史与恢复
    catalog: ItemCatalog
    agent_lifecycle: AgentLifecycle
    sessions: SessionService
    notifications: Notifications
    mcp: McpRegistry
    triggers: TriggerService
    webhooks: Webhooks
    push_subscriptions: PushSubscriptions
    vapid: VapidKeys
    models: ModelRegistry
    files: FileStore
    usage: UsageLog
    issues: IssueService
    history: HistoryLog
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
    skills: SkillStore
    memory: MemoryStore
    agents: AgentService
    roles: RoleService
    catalog: ItemCatalog
    agent_lifecycle: AgentLifecycle
    sessions: SessionService
    notifications: Notifications
    mcp: McpRegistry
    triggers: TriggerService
    due_triggers: DueRunner
    # 流式触发器托管的常驻命令
    streams: StreamRunner
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
    consumer: Consumer
    # 停止会话的命令（sessions.halt.SessionStopper）
    stopper: SessionStopper
    models: ModelRegistry
    files: FileStore
    usage: UsageLog
    issues: IssueService
    history: HistoryLog
    # 联网搜索、语音转写共用的出站 HTTP 客户端
    http: httpx.AsyncClient
    transcriber: Transcriber | None

    @property
    def container(self) -> Container:
        return Container(
            bus=EventFeed(self.bus),
            tasks=TaskAdmin(self.queue),
            vault=self.vault,
            skills=self.skills,
            memory=self.memory,
            agents=self.agents,
            roles=self.roles,
            catalog=self.catalog,
            agent_lifecycle=self.agent_lifecycle,
            sessions=self.sessions,
            notifications=self.notifications,
            mcp=self.mcp,
            triggers=self.triggers,
            webhooks=self.webhooks,
            push_subscriptions=self.push_subscriptions,
            vapid=self.vapid,
            models=self.models,
            files=self.files,
            usage=self.usage,
            issues=self.issues,
            history=self.history,
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
                root.start_soon(_loop, "streams", self.streams.run_forever)
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
        # skill 的生成目录按数据库全量重新生成（上次进程留下的临时目录、被改动的副本都在这里收敛）
        await self.skills.materialize_all()
        await self.agents.verify_stored()
        await self.queue.check_kinds()
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


def retries_of(runtime: RuntimeConfig) -> Retries:
    """[runtime] 里的重试配置：两种策略共用退避参数，只有尝试次数不同。"""
    return Retries(
        transient=RetryPolicy(
            runtime.task_max_attempts, runtime.task_retry_base_seconds, runtime.task_retry_max_delay_seconds
        ),
        outage=RetryPolicy(
            runtime.outage_retry_max_attempts,
            runtime.task_retry_base_seconds,
            runtime.task_retry_max_delay_seconds,
        ),
    )


def assemble(
    settings: Settings,
    *,
    resolve_model: ModelResolver | None = None,
    model_checker: Callable[[str, Thinking], None] | None = None,
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
    # 「配置了哪些可选能力」只在这里判断一次，注册的工具与权限表核对都由它导出
    web_tools = [] if web is None else [web_toolset(web, settings.runtime.tool_output_max_chars)]
    tz = ZoneInfo(settings.timezone)
    db = Database(create_engine(settings.db_path), check_writes=check_writes)
    bus = EventBus()
    live = LiveRuns(bus)
    queue = TaskQueue(db, retries=retries_of(settings.runtime))
    # 领域事件与定向命令：订阅与命令处理者在下面各领域构造之后登记
    messaging = Messaging(queue)
    # 马上执行已提交的投递（Worker.run_now）：worker 由各领域的处理器组成，在最后构造、绑定
    run_now = RunNow()
    vault = Vault(SecretItems(db), settings.vault.key.get_secret_value())
    files = FileStore(db, settings.files_dir, settings.runtime.upload_max_bytes)
    skills = SkillStore(db, settings.skills_dir)
    memory = MemoryStore(db, settings.runtime.memory_max_chars)
    # MCP 的配置归 agent 领域，连接归运行时：新增时的连接验证注入给配置，运行时按 agent 可用的 server 连接
    mcp_connector = McpConnector(vault)
    mcp = McpRegistry(db, mcp_connector.probe)
    tools = available_tools(web_configured=bool(web_tools))
    agents = AgentService(
        db,
        settings.workspaces_dir,
        model_checker or models.check,
        AgentDefaults(settings.llm.default_model),
        tools,
    )
    roles = RoleService(db, tools)
    catalog = ItemCatalog(db, tools, skills, mcp)
    # 会话领域先于消费者构造的端口（会话忙不忙、停止会话、委派、后台任务的占用），别的领域与工具集要用它们
    ports = SessionPorts.build(db, queue, messaging)
    activity = ports.activity
    agent_lifecycle = AgentLifecycle(
        db, agents, skills, messaging, {"sessions": activity.blocks_agent_deletion_in}
    )
    notifications = Notifications(db, queue, bus)
    schedule_kind, once_kind = ScheduleKind(tz), OnceKind(tz)
    # 触发方式在这里注册：新增一种（如消息队列）= 一个 TriggerKind + 它的激活机制
    precheck = PrecheckRunner(
        vault, agents.workspace, settings.runtime.precheck_timeout_seconds, EVENT_MAX_BYTES
    )
    triggers = TriggerService(
        db,
        {
            k.name: k
            for k in (schedule_kind, once_kind, WebhookKind(settings.server.public_url), StreamKind())
        },
        tz,
        precheck,
        messaging,
        run_now,
    )
    # 按到期时间触发的方式共用一个扫描循环，各自给出到期时间与这次到期被用掉之后这一行怎么变
    due_triggers = DueRunner(db, triggers, [ScheduleDue(activity, schedule_kind, messaging), OnceDue()])
    streams = StreamRunner(db, triggers, activity, vault, agents.workspace, messaging, EVENT_MAX_BYTES)
    webhooks = Webhooks(db, triggers)
    issues = IssueService(db, messaging, tz, run_now, activity)
    push_subscriptions = PushSubscriptions(db)
    vapid = VapidKeys(settings.vapid_key_path)

    pusher = WebPushDelivery(push_subscriptions, vapid, settings.push.vapid_subject)

    usage = UsageLog(db, tz)
    history = HistoryLog(db, tz)
    runtime = AgentRuntime(
        agents=agents,
        skills=skills,
        memory=memory,
        tz=tz,
        resolve_model=resolve_model or models.resolve,
        files=files,
        usage=usage,
    )
    jobs = JobRunner(
        db,
        vault,
        agents.workspace,
        messaging,
        ports.work,
    )
    limits = ShellLimits(settings.runtime.tool_output_max_chars)
    # 运行可用的全部静态工具集；每个工具的权限在 agents.permissions 登记，一次运行开放哪些只按 agent 的分发规则算
    # （Toolbox.profile），不随运行状态变（工具列表稳定才命中缓存）：如 ask_user 在没有用户会回答的运行里照样列出，
    # 调用时被拒绝（sessions.ask_user）
    static_toolsets = [
        skills_toolset(skills),
        memory_toolset(memory),
        history_toolset(history, agents),
        ask_user_toolset(),
        workspace_toolset(vault, limits, files, jobs),
        ports.delegation_toolset(db, agents),
        notify_toolset(notifications),
        mcp_admin_toolset(mcp),
        vault_toolset(vault),
        triggers_toolset(triggers, agents, settings.runtime.precheck_timeout_seconds),
        issues_toolset(issues, agents),
        *web_tools,
    ]
    static_toolsets.append(agents_toolset(agents, roles, agent_lifecycle, catalog))
    toolbox = Toolbox(
        toolsets=static_toolsets,
        mcp=lambda: mcp_connector.toolset(mcp.index),
        rules_of=agents.rules,
    )
    _check_permissions(toolbox.tool_names(), web_configured=bool(web_tools))
    session_domain = SessionDomain.build(
        ports,
        db=db,
        queue=queue,
        live=live,
        messaging=messaging,
        run_now=run_now,
        runtime=runtime,
        toolbox=toolbox,
        files=files,
        agents=agents,
        notes=OriginNotes(
            {**OWN_NOTES, Origin.TRIGGER: TRIGGER_SESSION_NOTE, Origin.ISSUE: ISSUE_SESSION_NOTE}
        ),
        compaction_threshold_tokens=settings.runtime.compaction_threshold_tokens,
        compaction_keep_steps=settings.runtime.compaction_keep_steps,
    )
    # 跨领域的反应：各领域的订阅与命令处理者（删除 agent 与会话时各领域清理自己的数据）
    session_domain.subscribe(messaging)
    jobs.subscribe(messaging)
    notifications.subscribe(messaging)
    agent_lifecycle.subscribe(messaging)
    triggers.subscribe(messaging)
    issues.subscribe(messaging)
    # 各领域的任务种类在这里登记处理器；队列只接受登记过的种类（TaskQueue.accept）
    worker = Worker(
        queue,
        register_handlers(
            (EVENT_TASK, messaging),
            *session_domain.handlers(messaging),
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
        skills=skills,
        memory=memory,
        agents=agents,
        roles=roles,
        catalog=catalog,
        agent_lifecycle=agent_lifecycle,
        sessions=session_domain.sessions,
        notifications=notifications,
        mcp=mcp,
        triggers=triggers,
        due_triggers=due_triggers,
        streams=streams,
        webhooks=webhooks,
        push_subscriptions=push_subscriptions,
        vapid=vapid,
        worker=worker,
        messaging=messaging,
        threads=ports.threads,
        mail=session_domain.mail,
        jobs=jobs,
        thread_checks=ports.thread_checks,
        work=WorkWatcher([jobs, ports.thread_checks]),
        # 只清理处理它的领域声明了可删的任务（docs/architecture.md 3.2「保留」）
        retention=TaskRetention(queue, prunable=worker.prunable_kinds),
        compactor=session_domain.compactor,
        consumer=session_domain.consumer,
        stopper=ports.stopper,
        models=models,
        files=files,
        usage=usage,
        issues=issues,
        history=history,
        http=http,
        transcriber=transcriber,
    )


def _check_permissions(registered: list[str], *, web_configured: bool) -> None:
    """注册的工具与当前配置下的权限表（agents.permissions.available_tools）必须一一对应：没登记的工具谁也
    拿不到，表里多出的说明工具改了名；界面与 get_agent 算最终工具用的也是这张表，一致才说得准。"""
    expected = set(available_tools(web_configured=web_configured))
    duplicated = sorted({n for n in registered if registered.count(n) > 1})
    if set(registered) != expected or duplicated:
        raise RuntimeError(
            "registered tools do not match the permission table: "
            f"unregistered {sorted(expected - set(registered))}, without permission {sorted(set(registered) - expected)}, "
            f"duplicated {duplicated}"
        )
