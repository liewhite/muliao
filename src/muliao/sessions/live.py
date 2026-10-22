"""运行中的实时展示（会话领域）：当前这一段的运行事件在内存里留一份，经事件总线推给前端 SSE。只用于展示加速，数据库
是唯一事实源。"""

import uuid
from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import BaseModel, Field

from muliao.platform.bus import EventBus
from muliao.sessions.transcript import Item


class SavedEvent(BaseModel):
    """运行中又保存了一步（LiveRuns.step）：历史有了新内容，实时事件换成新的一段。"""

    type: Literal["saved"] = "saved"
    session_id: str


class _RunEventBase(BaseModel):
    type: Literal["run"] = "run"
    session_id: str
    agent: str
    # 这一段的编号与事件序号（LiveRuns）：快照与 SSE 事件据此合并、去重、发现缺口
    run_id: str
    seq: int


class RunItemEvent(_RunEventBase):
    """一条完整的会话条目（工具调用、工具结果），与历史同一个投影（transcript）；文字可能截断过。"""

    event: Literal["item"] = "item"
    item: Item


class RunDeltaEvent(_RunEventBase):
    """流式文字：接在同类的最后一条后面（或开始新的一条）。"""

    event: Literal["delta"] = "delta"
    item_type: Literal["assistant", "thinking"]
    text: str


RunEvent = Annotated[RunItemEvent | RunDeltaEvent, Field(discriminator="event")]


@dataclass(frozen=True)
class LiveRun:
    run_id: str
    # 这一段开始时会话最后一条已保存消息的 id：这之后保存的消息（这一段的回复）由这一段的事件展示，历史里先不显示
    after_message_id: int | None
    events: list[RunItemEvent | RunDeltaEvent]


class LiveRuns:
    """每个会话正在进行的运行里、当前这一段的运行事件，在内存里留一份，让中途打开会话页的人从这一段的开头看起。

    一段从一次模型请求保存之后开始，包括模型的回复与它调用的工具，到下一次请求保存（或运行结束）为止。消费者每保存
    一次请求就换一段（step），发 saved 事件让前端重新拉取历史；回复在工具执行前就已保存，但这一段的事件已经在展示它，
    会话接口不在历史里重复显示（after_message_id）。和事件总线一样只是展示加速：进程重启时没保存的部分本来也会
    重跑，缓存丢了无妨。事件带上这一段的 run_id 与递增的 seq，前端用它们把快照与之后经 SSE 收到的事件合并，并发现
    两者之间的缺口。没有会话的运行（临时 agent）不推事件（agent_runtime.runner）。生命周期只由消费者管
    （sessions.consumer，begin / step / end）。
    """

    def __init__(self, bus: EventBus):
        self._bus = bus
        self._runs: dict[str, LiveRun] = {}

    def begin(self, session_id: str, after_message_id: int | None) -> None:
        if session_id in self._runs:
            # 同一会话同时只有一次运行（会话的 lane 串行）；已有记录说明上一次漏了 end 或这个前提被打破
            raise RuntimeError(f"session {session_id} already has a live run")
        self._runs[session_id] = LiveRun(uuid.uuid4().hex, after_message_id, [])

    def step(self, session_id: str, after_message_id: int) -> None:
        """一次请求已经保存（事务已提交）：之后的事件属于新的一段。"""
        if session_id not in self._runs:
            raise RuntimeError(f"step saved for session {session_id} outside a begun run")
        self._runs[session_id] = LiveRun(uuid.uuid4().hex, after_message_id, [])
        self._bus.publish(SavedEvent(session_id=session_id))

    def end(self, session_id: str) -> None:
        self._runs.pop(session_id, None)

    def publish_item(self, session_id: str, agent: str, item: Item) -> None:
        run = self._current(session_id)
        self._publish(
            run,
            RunItemEvent(
                session_id=session_id, agent=agent, run_id=run.run_id, seq=len(run.events), item=item
            ),
        )

    def publish_delta(
        self, session_id: str, agent: str, item_type: Literal["assistant", "thinking"], text: str
    ) -> None:
        run = self._current(session_id)
        self._publish(
            run,
            RunDeltaEvent(
                session_id=session_id,
                agent=agent,
                run_id=run.run_id,
                seq=len(run.events),
                item_type=item_type,
                text=text,
            ),
        )

    def _current(self, session_id: str) -> LiveRun:
        run = self._runs.get(session_id)
        if run is None:
            # 会话的每次运行都由消费者先 begin；没有说明调用方漏了这一步
            raise RuntimeError(f"run event for session {session_id} outside a begun run")
        return run

    def _publish(self, run: LiveRun, event: RunItemEvent | RunDeltaEvent) -> None:
        run.events.append(event)
        self._bus.publish(event)

    def snapshot(self, session_id: str) -> LiveRun | None:
        run = self._runs.get(session_id)
        return None if run is None else LiveRun(run.run_id, run.after_message_id, list(run.events))
