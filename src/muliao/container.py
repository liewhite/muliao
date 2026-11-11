"""组合根：唯一一处创建并连接所有服务。"""

import asyncio
import logging
import os
import signal
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

import anyio
import httpx

from muliao.agent_runtime.api import AgentRuntime, ModelRegistry, Toolbox
from muliao.agent_runtime.llm import ModelResolver
from muliao.agent_runtime.mcp import McpConnector
from muliao.agents.api import (
    AgentDomain,
    AgentLifecycle,
    AgentService,
    AssetCatalog,
    AssetSource,
    Distribution,
    McpRegistry,
    MemoryStore,
    SkillStore,
    Thinking,
    ToolTable,
    Vault,
)
from muliao.agents.service import AgentDefaults
from muliao.config import RuntimeConfig, Settings
from muliao.files.api import FileStore
from muliao.history.api import HistoryLog
from muliao.history.tools import history_toolset
from muliao.issues.api import IssueDomain, IssueService
from muliao.notifications.api import NotificationDomain, Notifications, PushSubscriptions, VapidKeys
from muliao.platform.bus import EventBus, EventFeed
from muliao.platform.checks import WorkWatcher
from muliao.platform.db import Database, WriteCheck, create_engine, migrate
from muliao.platform.messaging import Messaging
from muliao.platform.retention import TaskRetention
from muliao.platform.sandbox import sandbox_for
from muliao.platform.tables import EVENT_TASK
from muliao.platform.tasks import Retries, RetryPolicy, TaskAdmin, TaskQueue
from muliao.platform.wiring import Loop, Parts, Toolsets
from muliao.platform.worker import RunNow, Worker, register_handlers
from muliao.schema import ChangedEvent
from muliao.sessions.api import OWN_NOTES, OriginNotes, SessionDomain, SessionPorts, SessionService
from muliao.sessions.compaction import Compactor
from muliao.sessions.consumer import Consumer
from muliao.sessions.delegation import Delegations, ThreadChecks
from muliao.sessions.halt import SessionStopper
from muliao.sessions.live import LiveSegments
from muliao.sessions.reconcile import MailReconciler
from muliao.sessions.threads import Threads
from muliao.sessions.wakeups import WakeUps
from muliao.speech.api import Transcriber, build_transcriber
from muliao.triggers.api import TriggerDomain, TriggerService
from muliao.triggers.due import DueRunner
from muliao.usage.api import UsageLog
from muliao.web.api import WebProvider, build_web_provider
from muliao.web.tools import web_toolset
from muliao.workspace.api import (
    AgentCommands,
    Confiner,
    JobAdmin,
    JobRunner,
    ScriptGrantAdmin,
    ScriptGrants,
    Sites,
    WorkspaceDomain,
    WorkspaceFiles,
)

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
    # 会话页等待项里的后台任务：查看与结束
    jobs: JobAdmin
    # agent 页的「文件」：工作目录的只读浏览
    workspace_files: WorkspaceFiles
    # 设置页的「始终允许」：查看与撤销
    script_grants: ScriptGrantAdmin
    # 「页面」：agent 在工作目录 site/ 下写的网页
    sites: Sites
    vault: Vault
    skills: SkillStore
    memory: MemoryStore
    agents: AgentService
    # 分发：角色、agent 的规则与能用的、预览
    distribution: Distribution
    # 资源池：全部资源、转移 owner
    catalog: AssetCatalog
    agent_lifecycle: AgentLifecycle
    sessions: SessionService
    notifications: Notifications
    mcp: McpRegistry
    triggers: TriggerService
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
    live: LiveSegments
    queue: TaskQueue
    # AI 资源的目录树（agents.assets）：当前快照
    assets: AssetSource
    vault: Vault
    skills: SkillStore
    memory: MemoryStore
    agents: AgentService
    distribution: Distribution
    catalog: AssetCatalog
    agent_lifecycle: AgentLifecycle
    sessions: SessionService
    notifications: Notifications
    mcp: McpRegistry
    triggers: TriggerService
    # 定时与一次性触发器的到期扫描（测试自己驱动 fire_due）
    due: DueRunner
    push_subscriptions: PushSubscriptions
    vapid: VapidKeys
    worker: Worker
    messaging: Messaging
    threads: Threads
    delegations: Delegations
    mail: MailReconciler
    jobs: JobRunner
    script_grants: ScriptGrants
    confiner: Confiner
    commands: AgentCommands
    thread_checks: ThreadChecks
    wakeups: WakeUps
    retention: TaskRetention
    # 服务的全部常驻循环（名字，入口）：平台自己的与各领域声明的（Parts.loops），running 里各自启动
    loops: Sequence[tuple[str, Loop]]
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
            jobs=JobAdmin(self.jobs),
            workspace_files=WorkspaceFiles(self.agents.workspace),
            script_grants=ScriptGrantAdmin(self.script_grants),
            sites=Sites(self.agents.workspace),
            vault=self.vault,
            skills=self.skills,
            memory=self.memory,
            agents=self.agents,
            distribution=self.distribution,
            catalog=self.catalog,
            agent_lifecycle=self.agent_lifecycle,
            sessions=self.sessions,
            notifications=self.notifications,
            mcp=self.mcp,
            triggers=self.triggers,
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
                for name, loop in self.loops:
                    root.start_soon(_loop, name, loop)
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
        await self.assets.ensure_global_memory()
        await self.agents.report_leftovers()
        await self.queue.check_kinds()
        await asyncio.to_thread(self.files.recover)
        # 上次运行留下的「运行中」后台任务：进程已随服务结束，记为被中断并通知 agent（worker 处理这些事件之前）
        await self.jobs.recover()
        # 所有循环开始之前恢复上次进程遗留的 running task（Worker.recover）
        await self.worker.recover()


async def _loop(name: str, run_forever: Loop) -> None:
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
    tz = ZoneInfo(settings.timezone)
    bus = EventBus()

    # 写过表的事务提交后：前端正在显示的页面重新拉取（不分哪类数据，#646）
    db = Database(
        create_engine(settings.db_path),
        check_writes=check_writes,
        after_commit=lambda: bus.publish(ChangedEvent()),
    )
    live = LiveSegments(bus)
    queue = TaskQueue(db, retries=retries_of(settings.runtime))
    # 领域事件与定向命令：订阅与命令处理者在下面各领域构造之后登记
    # 提交后马上执行投递（Worker.run_now）：worker 由各领域的处理器组成，在最后构造、绑定
    run_now = RunNow()
    messaging = Messaging(queue, run_now)
    check_model = model_checker or models.check
    # 内置工具表：资源树、分发、资源池要用，而工具集要用它们构造，所以先建、装好工具集后 bind（agents.ToolTable）
    tool_table = ToolTable()
    assets = AssetSource(
        db,
        settings.assets_dir,
        settings.runtime.memory_max_chars,
        check_model,
        changed=lambda: bus.publish(ChangedEvent()),
        tools=tool_table,
    )
    vault = Vault(assets, settings.vault.key.get_secret_value())
    files = FileStore(db, settings.files_dir, settings.runtime.upload_max_bytes)
    skills = SkillStore(assets)
    memory = MemoryStore(assets)
    agents = AgentService(
        db,
        assets,
        settings.workspaces_dir,
        AgentDefaults(settings.llm.default_model),
    )
    distribution = Distribution(assets, agents.get_active)
    # agent 的命令与文件工具只能写它的可写路径（#416）：当前系统没有写沙箱时在这里拒绝启动
    home = Path.home()
    confiner = Confiner(
        sandbox_for(settings.runtime.write_sandbox, home),
        agents.workspace,
        distribution.write_scope,
        home,
        settings.private_files,
    )
    # MCP 的配置归 agent 领域，连接归运行时：新增时的连接验证注入给配置，运行时按 agent 可用的 server 连接；
    # stdio server 的进程与 agent 的命令关在同一个写沙箱里（#499）
    mcp_connector = McpConnector(vault, confiner.of)
    # 以 agent 身份起命令的唯一入口：run_shell、后台任务、前置检查与 stream 共用
    commands = AgentCommands(vault, agents.workspace, confiner)
    mcp = McpRegistry(assets, mcp_connector.probe)
    catalog = AssetCatalog(assets)
    max_chars = settings.runtime.tool_output_max_chars
    # 会话领域先于消费者构造的端口（会话忙不忙、停止会话、委派、后台任务的占用），别的领域与工具集要用它们
    ports = SessionPorts.build(db, queue, messaging, tz, agents, max_chars)
    agent_domain = AgentDomain.build(
        db=db,
        agents=agents,
        assets=assets,
        vault=vault,
        distribution=distribution,
        messaging=messaging,
        blockers=ports.deletion_blockers,
    )
    notification_domain = NotificationDomain.build(
        db=db,
        queue=queue,
        bus=bus,
        vapid_key_path=settings.vapid_key_path,
        vapid_subject=settings.push.vapid_subject,
    )
    issue_domain = IssueDomain.build(
        db=db, messaging=messaging, tz=tz, activity=ports.activity, stop_session=ports.stopper, agents=agents
    )
    trigger_domain = TriggerDomain.build(
        db=db,
        tz=tz,
        messaging=messaging,
        commands=commands,
        activity=ports.activity,
        agents=agents,
        precheck_timeout_seconds=settings.runtime.precheck_timeout_seconds,
    )
    workspace_domain = WorkspaceDomain.build(
        db=db,
        commands=commands,
        confiner=confiner,
        workspace_of=agents.workspace,
        messaging=messaging,
        work=ports.work,
        files=files,
        assets=assets,
        skills=skills,
        tool_output_max_chars=max_chars,
    )
    usage = UsageLog(db, tz)
    history = HistoryLog(db, tz)
    runtime = AgentRuntime(
        agents=agents,
        assets=assets,
        skills=skills,
        memory=memory,
        tz=tz,
        resolve_model=resolve_model or models.resolve,
        files=files,
        usage=usage,
    )
    # 各领域交给组合根的部件（platform.wiring.Parts，docs/architecture.md 2.4 第 17 行）：下面每种部件遍历一次这个列表。
    # 会话领域的第二段（消费者）要用组好的工具箱，所以最后加入；它的工具集在第一段（SessionPorts）
    domains: list[Parts] = [
        ports,
        agent_domain,
        workspace_domain,
        notification_domain,
        issue_domain,
        trigger_domain,
        Toolsets(history_toolset(history, agents, ports.recall, issue_domain.issues, max_chars)),
        # 「配置了哪些可选能力」只在这里判断一次：没配置 [web] 就不注册网页工具，工具表里也就没有它们
        *([] if web is None else [Toolsets(web_toolset(web, max_chars))]),
    ]
    # 一次运行开放哪些工具只按 agent 的分发规则算（Toolbox.profile；每个工具注册时自己声明读写、是否管理类等，
    # platform.tooling.declared），不随运行状态变（工具列表稳定才命中缓存）：如 ask_user 在没有用户会回答的运行里照样列出，
    # 调用时被拒绝（sessions.ask_user）
    toolbox = Toolbox(
        toolsets=[t for d in domains for t in d.toolsets()],
        mcp=lambda: mcp_connector.toolset(mcp.index),
        rules_of=distribution.rules,
    )
    tool_table.bind(toolbox.specs())
    session_domain = SessionDomain.build(
        ports,
        db=db,
        queue=queue,
        live=live,
        messaging=messaging,
        runtime=runtime,
        toolbox=toolbox,
        files=files,
        agents=agents,
        notes=OriginNotes({**OWN_NOTES, **trigger_domain.origin_notes, **issue_domain.origin_notes}),
        compaction_threshold_tokens=settings.runtime.compaction_threshold_tokens,
        compaction_keep_steps=settings.runtime.compaction_keep_steps,
    )
    if session_domain.toolsets():
        raise RuntimeError("SessionDomain is built after the toolbox: its toolsets belong in SessionPorts")
    domains.append(session_domain)
    # 跨领域的反应：各领域的订阅与命令处理者（删除 agent 与会话时各领域清理自己的数据）；漏了的当场报错
    for d in domains:
        d.subscribe(messaging)
    messaging.check_wired()
    # 各领域的任务种类与处理器；队列只接受登记过的种类（TaskQueue.accept）
    worker = Worker(
        queue,
        register_handlers((EVENT_TASK, messaging), *(h for d in domains for h in d.handlers())),
        bus,
    )
    run_now.bind(worker)
    queue.accept(worker.kinds)
    # 只清理处理它的领域声明了可删的任务（docs/architecture.md 3.2「保留」）
    retention = TaskRetention(queue, prunable=worker.prunable_kinds)
    loops: list[tuple[str, Loop]] = [
        ("worker", worker.run_forever),
        ("checks", WorkWatcher([c for d in domains for c in d.checks()]).run_forever),
        ("retention", retention.run_forever),
        *(loop for d in domains for loop in d.loops()),
    ]

    return Assembly(
        settings=settings,
        db=db,
        bus=bus,
        live=live,
        queue=queue,
        assets=assets,
        vault=vault,
        skills=skills,
        memory=memory,
        agents=agents,
        distribution=distribution,
        catalog=catalog,
        agent_lifecycle=agent_domain.lifecycle,
        sessions=session_domain.sessions,
        notifications=notification_domain.notifications,
        mcp=mcp,
        triggers=trigger_domain.triggers,
        due=trigger_domain.due,
        push_subscriptions=notification_domain.push_subscriptions,
        vapid=notification_domain.vapid,
        worker=worker,
        messaging=messaging,
        threads=ports.threads,
        delegations=ports.delegations,
        mail=session_domain.mail,
        jobs=workspace_domain.jobs,
        script_grants=workspace_domain.script_grants,
        confiner=confiner,
        commands=commands,
        thread_checks=ports.thread_checks,
        wakeups=ports.wakeups,
        retention=retention,
        loops=loops,
        compactor=session_domain.compactor,
        consumer=session_domain.consumer,
        stopper=ports.stopper,
        models=models,
        files=files,
        usage=usage,
        issues=issue_domain.issues,
        history=history,
        http=http,
        transcriber=transcriber,
    )
