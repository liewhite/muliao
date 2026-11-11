"""编辑的按字段乐观锁（#634，docs/architecture.md 2.4 第 18 行）。

界面的局部修改（PATCH）除了改过的字段，还带上这些字段在开始编辑时的值（`base`，与接口输出同一形状）；此刻的值已经
不同（编辑期间 agent 或别的页面改了同一个字段），就 409，由界面提示冲突，用户选用对方的或覆盖。只比 base 里的字段：
别的字段被改了互不影响（#575）。不带 base 的请求（agent 的工具、启用开关这类一点就改的）照常直接改。

比的是接口输出（用户看到的值），不是领域里的存法：同一字段的存法与输出可能不同（投递目标存的是解析后的目标、输出的是
文字地址），按输出比就不用为每个字段再写一份换算。比对与写入不在同一把锁里：两者相隔几毫秒，别人的修改正好落在中间
才会漏判，罕见且后果只是回到 #634 之前的「后写的覆盖」，有意不加锁（agent 直接改 agent.toml、access.toml 本来也不经锁）。
"""

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel

from muliao.errors import Conflict


def check_base(what: str, current: Mapping[str, Any], base: BaseModel) -> None:
    """base 里给出的每个字段（JSON 形状）都等于 current（此刻的接口输出，含全部可改字段）里的值，否则 Conflict。"""
    # 只取给出的字段，字段里的值按完整的 JSON 比（里层省略的缺省值补上，与输出一致）
    dumped = base.model_dump(mode="json")
    given = [field for field in type(base).model_fields if field in base.model_fields_set]
    stale = [field for field in given if current[field] != dumped[field]]
    if stale:
        raise Conflict(
            f"{what}: {', '.join(stale)} changed since you started editing; "
            "reload to see the latest value, then take it or save yours over it"
        )
