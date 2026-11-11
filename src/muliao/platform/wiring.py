"""各领域交给组合根的部件：一个领域有哪些工具集、事件订阅、任务处理器、
常驻循环、运行中检查，由它自己的装配对象声明（继承 Parts，覆盖自己有的，没有的默认为空）；组合根把各领域放进一个
列表，每种部件遍历一次，不列举各领域有什么。

漏装配不静默：每个事件都要有订阅者，装配结束时由 Messaging.check_wired 核对，缺了当场报错（漏放进列表的领域，
它的订阅就会缺）。"""

from collections.abc import Awaitable, Callable, Sequence

from pydantic_ai import FunctionToolset

from muliao.platform.checks import PeriodicCheck
from muliao.platform.messaging import Messaging
from muliao.platform.tooling import RunScope
from muliao.platform.worker import TaskHandler

# 常驻循环的入口：服务的根任务组里一直跑，退出就是 bug（container._loop 停止进程）
type Loop = Callable[[], Awaitable[None]]


class Parts:
    def toolsets(self) -> Sequence[FunctionToolset[RunScope]]:
        """agent 用的工具集（进工具箱，一次运行开放哪些按分发规则算）。"""
        return ()

    def subscribe(self, messaging: Messaging) -> None:
        """登记这个领域的事件订阅。"""

    def handlers(self) -> Sequence[tuple[str, TaskHandler]]:
        """这个领域的任务种类与处理器（交给 worker）。"""
        return ()

    def loops(self) -> Sequence[tuple[str, Loop]]:
        """常驻循环（名字，入口），各自在服务的根任务组里启动。"""
        return ()

    def checks(self) -> Sequence[PeriodicCheck]:
        """运行中检查：platform.checks.WorkWatcher 每个节拍让它们各看一遍到期的工作。"""
        return ()


class Toolsets(Parts):
    """只交出工具集的领域（如 history、web）。"""

    def __init__(self, *toolsets: FunctionToolset[RunScope]):
        self._toolsets = toolsets

    def toolsets(self) -> Sequence[FunctionToolset[RunScope]]:
        return self._toolsets
