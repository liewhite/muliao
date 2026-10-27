"""加密保存的 secret。agent 只能通过 key 引用，没有读取明文的工具。

密文与描述存在 agent 领域的条目里（agents.api.SecretItems，key 按名字全局唯一，key 的语法也在那里）；这里管加解密与
${vault:KEY} 引用的解析。引用按使用者能用的 secret 解析（agents 的规则 1：自己的与分发给它的）：agent 运行的命令与
它连接的 MCP server 都用该 agent；用户（USER，如界面上新增 MCP 时的探测）能用全部。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from cryptography.fernet import Fernet

from muliao.agents.api import SECRET_KEY_SYNTAX, SecretItems, StoredSecret
from muliao.errors import InvalidInput, NotFound

log = logging.getLogger(__name__)

# 配置模板中的引用语法：${vault:KEY}
VAULT_REF = re.compile(rf"\$\{{vault:({SECRET_KEY_SYNTAX})\}}")


def fill(template: str, values: Mapping[str, str]) -> str:
    """把字符串中的 ${vault:KEY} 替换为 values 里的明文（values 须含全部引用，通常来自 resolve_many）。"""
    return VAULT_REF.sub(lambda m: values[m.group(1)], template)


@dataclass(frozen=True)
class SecretInfo:
    owner: str | None
    key: str
    description: str
    updated_at: datetime


def _info(s: StoredSecret) -> SecretInfo:
    return SecretInfo(s.owner, s.key, s.description, s.updated_at)


class Vault:
    def __init__(self, items: SecretItems, fernet_key: str):
        self._items = items
        self._fernet = Fernet(fernet_key.encode())

    async def set(self, key: str, value: str, description: str, *, actor: str, owner: str | None) -> None:
        """新建（归 owner）或改写（actor 要能改它）一个 secret。"""
        if not value:
            raise InvalidInput("secret value must not be empty")
        await self._items.put(
            key, self._fernet.encrypt(value.encode()), description, actor=actor, owner=owner
        )
        # 别的 agent 可能依赖它：记录谁改了哪个 key（不记值）
        log.info("secret set", extra={"owner": owner, "key": key, "author": actor})

    async def delete(self, key: str, actor: str) -> None:
        await self._items.delete(key, actor)
        log.info("secret deleted", extra={"key": key, "author": actor})

    async def get(self, key: str) -> SecretInfo:
        """一个 secret 的信息（不含值）。"""
        return _info(await self._items.get(key))

    async def usable_by(self, user: str) -> list[SecretInfo]:
        """user（agent 的 id，或 USER）能用的 secret（不含值），按 key。"""
        return [_info(s) for s in await self._items.usable(user)]

    async def resolve_many(self, user: str, keys: list[str]) -> dict[str, str]:
        """取 user（agent 的 id，或 USER）能用的 secret 的明文；任一 key 不能用即报错。"""
        if not keys:
            return {}
        rows = {s.key: s for s in await self._items.usable(user)}
        missing = [k for k in keys if k not in rows]
        if missing:
            raise NotFound(
                f"secret(s) {missing} not found among the secrets you can use (your own and those given to you "
                "by your roles or your own allow rules)"
            )
        return {k: self._fernet.decrypt(rows[k].ciphertext).decode() for k in keys}
