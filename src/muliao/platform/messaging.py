"""领域事件与定向命令（docs/architecture.md 3.3、3.4）。两者都是 outbox：在发送方的本地事务里为接收方插入一条投递
任务（EVENT_TASK），状态变化与「一定会送达」原子地提交；worker 执行投递，失败按队列规则重试，最终失败在「任务」页
可见。

- 事件（publish_in）：发送方不知道谁在订阅。每个订阅者一条投递任务，默认每个订阅者一条 lane（按发布顺序逐条处理）。
- 定向命令（send_in）：接收方由发送方指定，只有一个处理者。用于「先在自己的事务里提交了状态，又必须保证一条指定的
  命令执行」；其余命令直接同步调用对方接口。
- 发送方的事务提交之后，在它的任务里马上执行这些投递（Worker.run_now，经 db.when_committed_in）：发送方返回时效果通常已经生效（如新建
  的会话已经存在），不靠各发送方自己收集；发送方因此要等整条投递链做完。此刻不可领取（lane 被挡住、已被领取）、领取时出错
  的由 worker 照常执行。处理要等别的工作结束的接收方登记为 slow（停下 thread、删除 agent 的会话与目录），只交给 worker。
- 处理者返回 Commit：它对自己领域的写入与「这条投递完成」在同一个事务里提交（TaskQueue.complete），所以对自己领域
  是恰好一次；对别的领域只能调用命令（在 execute 里、带幂等键），不能在 Commit 里调用。
- 消息的数据不合法、接收方没有登记，都是存下的数据与代码不一致（errors.CorruptState，例如跨版本部署），投递直接
  失败，不重试。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from muliao.errors import CorruptState
from muliao.platform.db import when_committed_in
from muliao.platform.ids import new_id
from muliao.platform.tables import EVENT_TASK, Task
from muliao.platform.tasks import Commit, TaskQueue

log = logging.getLogger(__name__)


class Message(BaseModel):
    """事件或命令的数据。TYPE 是它在投递任务里的名字，跨版本保持不变。"""

    model_config = ConfigDict(frozen=True, extra="forbid")
    TYPE: ClassVar[str]


class DomainEvent(Message):
    """一个领域宣布自己发生了什么。"""


class Command(Message):
    """请另一个领域做一件事（定向命令，持久化投递）。"""


@dataclass(frozen=True)
class Delivery:
    """一次投递：消息 id 是幂等键的来源（同一事件投给不同订阅者时 id 相同）。"""

    message_id: str


type Handler[M: Message] = Callable[[M, Delivery], Awaitable[Commit]]


@dataclass(frozen=True)
class _Receiver:
    name: str
    message_type: type[Message]
    handler: Handler[Any]
    lane: Callable[[Message], str]
    slow: bool


class Messaging:
    # 事件与定向命令的投递完成后没有别的数据引用它
    prunable = True

    def __init__(self, queue: TaskQueue, run_now: Callable[[int], Awaitable[None]]):
        self._queue = queue
        # 马上执行一个已提交的投递任务（Worker.run_now，经组合根的 RunNow 端口）
        self._run_now = run_now
        self._subscribers: dict[str, list[_Receiver]] = {}
        self._commands: dict[str, _Receiver] = {}
        self._receivers: dict[str, _Receiver] = {}
        # TYPE 在全部消息类型里唯一：投递任务里只记 TYPE
        self._types: dict[str, type[Message]] = {}

    # ---- 登记（组合根，启动前） ----

    def subscribe[E: DomainEvent](
        self,
        event_type: type[E],
        name: str,
        handler: Handler[E],
        *,
        lane: Callable[[E], str] | None = None,
        slow: bool = False,
    ) -> None:
        """name 在全部接收方里唯一（它写在投递任务里）。lane 不给时这个订阅者的投递都在一条 lane 上。slow：处理要等
        别的工作结束（停下会话、删目录），不在发送方的任务里马上执行，只交给 worker，发送方不等它。"""
        receiver = self._register(name, event_type, handler, lane, slow)
        self._subscribers.setdefault(event_type.TYPE, []).append(receiver)

    def handle[C: Command](
        self,
        command_type: type[C],
        name: str,
        handler: Handler[C],
        *,
        lane: Callable[[C], str] | None = None,
        slow: bool = False,
    ) -> None:
        """name、lane、slow 同 subscribe。"""
        if command_type.TYPE in self._commands:
            raise RuntimeError(f"command {command_type.TYPE} already has a handler")
        self._commands[command_type.TYPE] = self._register(name, command_type, handler, lane, slow)

    def _register(
        self,
        name: str,
        message_type: type[Message],
        handler: Handler[Any],
        lane: Callable[[Any], str] | None,
        slow: bool,
    ) -> _Receiver:
        if name in self._receivers:
            raise RuntimeError(f"receiver name {name!r} is registered twice")
        known = self._types.setdefault(message_type.TYPE, message_type)
        if known is not message_type:
            raise RuntimeError(
                f"message type {message_type.TYPE!r} is used by {known.__name__} and {message_type.__name__}"
            )
        receiver = _Receiver(
            name=name,
            message_type=message_type,
            handler=handler,
            lane=lane if lane is not None else (lambda _m, n=name: f"sub:{n}"),
            slow=slow,
        )
        self._receivers[name] = receiver
        return receiver

    # ---- 发送（在发送方的事务里） ----

    def publish_in(self, s: AsyncSession, event: DomainEvent) -> None:
        """为每个订阅者插入一条投递任务，提交后马上执行（slow 的只交给 worker）。没有订阅者时什么都不插。"""
        message_id = new_id()
        for receiver in self._subscribers.get(event.TYPE, []):
            self._enqueue(s, receiver, event, message_id)

    def send_in(self, s: AsyncSession, command: Command) -> None:
        """插入一条投递任务，提交后马上执行（slow 的只交给 worker）。"""
        receiver = self._commands.get(command.TYPE)
        if receiver is None:
            raise RuntimeError(f"command {command.TYPE} has no handler")
        self._enqueue(s, receiver, command, new_id())

    def _enqueue(self, s: AsyncSession, receiver: _Receiver, message: Message, message_id: str) -> None:
        task = self._queue.enqueue(
            s,
            kind=EVENT_TASK,
            lane=receiver.lane(message),
            agent_id=None,
            session_id=None,
            payload={
                "type": message.TYPE,
                "receiver": receiver.name,
                "id": message_id,
                "data": message.model_dump(mode="json"),
            },
        )
        if not receiver.slow:
            when_committed_in(s, lambda: self._run_now(task.id))

    # ---- 投递（TaskHandler） ----

    async def execute(self, task: Task) -> Commit:
        payload = task.payload
        if not {"type", "receiver", "id", "data"} <= payload.keys():
            raise CorruptState(f"delivery task {task.id} has a malformed payload: {sorted(payload)}")
        receiver = self._receivers.get(payload["receiver"])
        if receiver is None or receiver.message_type.TYPE != payload["type"]:
            raise CorruptState(
                f"delivery task {task.id} is for {payload['receiver']!r} / {payload['type']!r}, "
                "which this version does not register"
            )
        try:
            message = receiver.message_type.model_validate(payload["data"])
        except ValidationError as e:
            raise CorruptState(f"delivery task {task.id} carries an invalid {payload['type']}: {e}") from e
        log.info(
            "delivering message",
            extra={
                "task_id": task.id,
                "type": payload["type"],
                "receiver": receiver.name,
                "message_id": payload["id"],
            },
        )
        return await receiver.handler(message, Delivery(message_id=payload["id"]))

    async def failed_in(self, s: AsyncSession, task: Task) -> None:
        """投递最终失败只需可见（任务记录与日志）；人工重试按原 id 保持顺序（TaskQueue.retry）。"""
