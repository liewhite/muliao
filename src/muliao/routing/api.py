"""routing 领域的公开接口：事件按投递目标交给会话或 issue（docs/architecture.md「事件投递」）。

- 目标（Target：NewTo / LatestOf / IssueTo，存 id；TARGET 读存下的 JSON）与它的文字地址（ADDRESS_HELP；resolve_in 解析、
  describe 写回）；
- 在发送方事务里只读地把事件内容按 `@route` 行分到各目标（plan_in）、变成要 send_in 的命令（routed_in → Routed）；
- 发送方 Sender。
"""

from muliao.routing.router import Routed, Sender, describe, plan_in, resolve_in, routed_in
from muliao.routing.targets import ADDRESS_HELP, DEFAULT, TARGET, IssueTo, LatestOf, NewTo, Target

__all__ = [
    "ADDRESS_HELP",
    "DEFAULT",
    "TARGET",
    "IssueTo",
    "LatestOf",
    "NewTo",
    "Routed",
    "Sender",
    "Target",
    "describe",
    "plan_in",
    "resolve_in",
    "routed_in",
]
