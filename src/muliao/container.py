"""组合根：唯一一处创建并连接所有服务。"""

import asyncio
import logging
import os
import signal
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from muliao.agents import AgentService
from muliao.bus import EventBus
from muliao.config import Settings
from muliao.db import Database, create_engine, migrate
from muliao.delivery.base import DeliverHandler, Delivery, Notifier
from muliao.delivery.telegram import TelegramDelivery
from muliao.delivery.webpush import PushSubscriptions, VapidKeys, WebPushDelivery
from muliao.errors import InvalidInput
from muliao.evolution.memory import MemoryStore
from muliao.evolution.skills import SkillStore
from muliao.llm import ModelRegistry, ModelResolver
from muliao.models import DeliveryChannel, TaskKind
from muliao.runtime.compaction import CompactionHandler, CompactionTrigger
from muliao.runtime.mcp import McpRegistry
from muliao.runtime.reflection import ReflectionHandler
from muliao.runtime.runner import AgentRuntime, RunProfile
from muliao.runtime.sessions import SessionService
from muliao.runtime.tasks import TaskQueue
from muliao.runtime.tools.evolution import memory_toolset, skills_toolset
from muliao.runtime.tools.system import (
    agents_toolset,
    cron_toolset,
    mcp_admin_toolset,
    notify_toolset,
    vault_toolset,
)
from muliao.runtime.tools.workspace import ShellLimits, workspace_toolset
from muliao.runtime.turn import TurnHandler
from muliao.runtime.worker import TaskHandler, Worker
from muliao.telegram import TelegramClient
from muliao.triggers.cron import CronService
from muliao.triggers.telegram import TelegramTrigger
from muliao.triggers.webhook import WebhookService
from muliao.vault import Vault

log = logging.getLogger(__name__)


@dataclass
class Container:
    settings: Settings
    db: Database
    bus: EventBus
    queue: TaskQueue
    vault: Vault
    skills: SkillStore
    memory: MemoryStore
    agents: AgentService
    sessions: SessionService
    notifier: Notifier
    mcp: McpRegistry
    cron: CronService
    webhooks: WebhookService
    push_subscriptions: PushSubscriptions
    vapid: VapidKeys
    worker: Worker
    http: httpx.AsyncClient
    telegram: TelegramTrigger | None
    _model_checker: Callable[[str], None]
    _background: list[asyncio.Task[None]] = field(default_factory=list)

    def _spawn(self, name: str, coro: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(coro, name=name)
        task.add_done_callback(_crash_on_failure)
        self._background.append(task)

    async def start(self) -> None:
        await asyncio.to_thread(migrate, self.settings.db_path)
        await self._check_agent_models()
        if self.settings.telegram is not None:
            # 配置错误在启动时暴露，而不是在第一条消息到来时让轮询崩溃
            await self.agents.get(self.settings.telegram.agent)
        self._spawn("worker", self.worker.run_forever())
        self._spawn("cron", self.cron.run_forever())
        if self.telegram is not None:
            self._spawn("telegram", self.telegram.run_forever())

    async def _check_agent_models(self) -> None:
        """已存 agent 的模型必须能被当前配置解析：配置改动（删 provider、改名）在启动时暴露，而不是散落到每个任务里失败。"""
        broken = []
        for agent in await self.agents.list():
            try:
                self._model_checker(agent.model)
            except InvalidInput as e:
                broken.append(f"{agent.name}: {e}")
        if broken:
            raise RuntimeError(
                "agents reference models not resolvable with current config:\n" + "\n".join(broken)
            )

    async def stop(self) -> None:
        for task in self._background:
            task.cancel()
        await asyncio.gather(*self._background, return_exceptions=True)
        await self.http.aclose()
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
    skills = SkillStore(settings.public_skills_dir, settings.agents_dir)
    memory = MemoryStore(settings.agents_dir, settings.runtime.memory_max_chars)
    agents = AgentService(
        db,
        settings.agents_dir,
        memory,
        model_checker or models.check,
        protected=frozenset([settings.telegram.agent]) if settings.telegram else frozenset(),
    )
    sessions = SessionService(db, queue)
    notifier = Notifier(db, queue)
    mcp = McpRegistry(db, vault)
    cron = CronService(db, sessions, queue, tz)
    webhooks = WebhookService(db, sessions)
    push_subscriptions = PushSubscriptions(db)
    vapid = VapidKeys(settings.vapid_key_path)
    http = httpx.AsyncClient()

    deliveries: dict[DeliveryChannel, Delivery] = {
        DeliveryChannel.WEBPUSH: WebPushDelivery(push_subscriptions, vapid, settings.push.vapid_subject),
    }
    telegram: TelegramTrigger | None = None
    if settings.telegram is not None:
        client = TelegramClient(settings.telegram.bot_token.get_secret_value(), http)
        deliveries[DeliveryChannel.TELEGRAM] = TelegramDelivery(client)
        telegram = TelegramTrigger(
            client, sessions, notifier, settings.telegram.agent, set(settings.telegram.allowed_chat_ids)
        )

    evolution_tools = [skills_toolset(skills), memory_toolset(memory)]
    turn_profile = RunProfile(
        toolsets=[
            workspace_toolset(
                vault,
                ShellLimits(
                    settings.runtime.shell_default_timeout,
                    settings.runtime.shell_max_timeout,
                    settings.runtime.tool_output_max_chars,
                ),
            ),
            *evolution_tools,
            agents_toolset(agents, sessions),
            notify_toolset(notifier),
            mcp_admin_toolset(mcp),
            vault_toolset(vault),
            cron_toolset(cron),
        ],
        include_mcp=True,
    )
    reflection_profile = RunProfile(toolsets=evolution_tools, include_mcp=False)
    runtime = AgentRuntime(
        agents=agents,
        skills=skills,
        memory=memory,
        mcp=mcp,
        bus=bus,
        tz=tz,
        resolve_model=resolve_model or models.resolve,
    )
    handlers: dict[TaskKind, TaskHandler] = {
        TaskKind.TURN: TurnHandler(
            db,
            runtime,
            turn_profile,
            queue,
            notifier,
            CompactionTrigger(queue, settings.runtime.compaction_threshold_tokens),
        ),
        TaskKind.COMPACTION: CompactionHandler(db, runtime, settings.runtime.compaction_keep_turns),
        TaskKind.REFLECTION: ReflectionHandler(db, runtime, reflection_profile),
        TaskKind.DELIVER: DeliverHandler(deliveries),
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
        skills=skills,
        memory=memory,
        agents=agents,
        sessions=sessions,
        notifier=notifier,
        mcp=mcp,
        cron=cron,
        webhooks=webhooks,
        push_subscriptions=push_subscriptions,
        vapid=vapid,
        worker=worker,
        http=http,
        telegram=telegram,
        _model_checker=model_checker or models.check,
    )
