"""routing 领域的公开接口：事件按投递目标交给会话或 issue（docs/architecture.md「事件投递」）。

- 目标（Target：NewTo / LatestOf / IssueTo，存 id；TARGET 读存下的 JSON）与它的文字地址（ADDRESS_HELP；resolve_in 解析、
  describe 写回）；调用方当场指定续写的会话 Continue（与 Target 合称 Destination）；
- 在发送方事务里只读地把事件内容按 `@route` 行分到各目标（plan_in → Plan：各段 Segment，内容里写错的地址 Rejected）、变成要 send_in 的命令（routed_in → Routed）；
- 按行到达顺序分目标的发送方（流式）用的 `@route` 行读写（route_line / route_header）与合成（routed_text）；
- 发送方 Sender。
"""

from muliao.routing.router import Plan, Rejected, Routed, Segment, Sender, plan_in, resolve_in, routed_in
from muliao.routing.targets import (
    ADDRESS_HELP,
    DEFAULT,
    TARGET,
    Continue,
    Destination,
    IssueTo,
    LatestOf,
    NewTo,
    Target,
    describe,
    route_header,
    route_line,
    routed_text,
)

__all__ = [
    "ADDRESS_HELP",
    "DEFAULT",
    "TARGET",
    "Continue",
    "Destination",
    "IssueTo",
    "LatestOf",
    "NewTo",
    "Plan",
    "Rejected",
    "Routed",
    "Segment",
    "Sender",
    "Target",
    "describe",
    "plan_in",
    "resolve_in",
    "route_header",
    "route_line",
    "routed_in",
    "routed_text",
]
