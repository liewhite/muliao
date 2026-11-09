"""agent 的分发规则（分发子域）：角色（不含隐含的 base）与它自己的 allow / deny，存在 access.toml 的 `[agents.<名>]`
（agents.assets 读）。最终规则（base + 角色 + allow / deny）按读出时角色的定义算。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from muliao.agents.permissions import BASE, Role, Rules


@dataclass(frozen=True)
class AgentRules:
    """agent 的分发规则：角色（不含每个 agent 隐含的 base）与它自己的 allow / deny（`action:kind:glob`）。"""

    roles: tuple[str, ...] = ()
    allow: tuple[str, ...] = ()
    deny: tuple[str, ...] = ()

    @classmethod
    def of(cls, roles: Sequence[str], allow: Sequence[str], deny: Sequence[str]) -> AgentRules:
        return cls(tuple(roles), tuple(allow), tuple(deny))

    @property
    def own(self) -> Rules:
        """它自己的 allow / deny（不含角色）。"""
        return Rules(self.allow, self.deny)

    def effective(self, known: Mapping[str, Role]) -> Rules:
        """最终规则：各来源（sources）的并集；known 是全部角色。"""
        return Rules.union(r for _, r in self.sources(known))

    def sources(self, known: Mapping[str, Role]) -> list[tuple[str | None, Rules]]:
        """最终规则的各个来源：(角色名, 它的规则)——base 在前、再按它的 roles——最后是 (None, 它自己的 allow / deny)。
        界面按来源解释（access.Provenance）用同一份。不存在的角色读树时已跳过（agents.assets），这里再遇到是代码错误。"""
        missing = [r for r in self.roles if r not in known]
        if missing:
            raise RuntimeError(f"agent references unknown roles {missing}")
        return [*((r, known[r].effective) for r in (BASE, *self.roles)), (None, self.own)]
