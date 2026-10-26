"""加密保存的 secret，分公共与 agent 私有。agent 只能通过 key 引用，没有读取明文的工具。

密文与描述存在 agent 领域的条目里（agents.api.SecretItems，key 按名字全局唯一）；这里管加解密、key 的语法与
${vault:KEY} 引用的解析。引用按使用者的视角解析（ownership.sees）：agent 运行的命令用该 agent 的视角，MCP server
的配置用 server 归属者的视角——公共 server 只能引用公共 secret。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from cryptography.fernet import Fernet

from muliao.agents.api import SecretItems, StoredSecret
from muliao.errors import InvalidInput, NotFound

log = logging.getLogger(__name__)

# secret key 的语法：存储时校验与模板里的引用共用这一份。长度上限只在存储时检查：引用了超长 key 的模板照样认作引用，
# 解析时按「不存在」报错，而不是当成字面文本原样代入
_KEY_SYNTAX = r"[A-Za-z_][A-Za-z0-9_]*"
_KEY_MAX = 100
_KEY = re.compile(rf"^{_KEY_SYNTAX}$")
# 配置模板中的引用语法：${vault:KEY}
VAULT_REF = re.compile(rf"\$\{{vault:({_KEY_SYNTAX})\}}")


def fill(template: str, values: Mapping[str, str]) -> str:
    """把字符串中的 ${vault:KEY} 替换为 values 里的明文（values 须含全部引用，通常来自 resolve_many）。"""
    return VAULT_REF.sub(lambda m: values[m.group(1)], template)


@dataclass(frozen=True)
class SecretInfo:
    owner: str | None
    key: str
    description: str
    updated_at: datetime


def validate_key(key: str) -> str:
    if not _KEY.fullmatch(key) or len(key) > _KEY_MAX:
        raise InvalidInput(
            f"invalid secret key {key!r}: must match {_KEY.pattern} and be at most {_KEY_MAX} characters"
        )
    return key


def _info(s: StoredSecret) -> SecretInfo:
    return SecretInfo(s.owner, s.key, s.description, s.updated_at)


class Vault:
    def __init__(self, items: SecretItems, fernet_key: str):
        self._items = items
        self._fernet = Fernet(fernet_key.encode())

    async def set(self, owner: str | None, key: str, value: str, description: str, author: str) -> None:
        validate_key(key)
        if not value:
            raise InvalidInput("secret value must not be empty")
        await self._items.put(owner, key, self._fernet.encrypt(value.encode()), description, author)
        # 公共 secret 所有 agent 都依赖：记录谁在什么作用域改了哪个 key（不记值）
        log.info("secret set", extra={"owner": owner, "key": key, "author": author})

    async def delete(self, owner: str | None, key: str, author: str) -> None:
        await self._items.delete(owner, key, author)
        log.info("secret deleted", extra={"owner": owner, "key": key, "author": author})

    async def visible_to(self, agent_id: str | None) -> list[SecretInfo]:
        """agent_id 视角下的 secret（不含值），即它私有的与公共的；None 时只有公共的。私有在前，各按 key。"""
        return [_info(s) for s in await self._items.visible(agent_id)]

    async def resolve_many(self, agent_id: str | None, keys: list[str]) -> dict[str, str]:
        """按 agent_id 的视角取明文（None 为只看公共）；任一 key 不可见即报错。"""
        if not keys:
            return {}
        rows = {s.key: s for s in await self._items.visible(agent_id)}
        missing = [k for k in keys if k not in rows]
        if missing:
            where = "public secrets" if agent_id is None else "private or public secrets"
            raise NotFound(f"secret(s) {missing} not found in {where}")
        return {k: self._fernet.decrypt(rows[k].ciphertext).decode() for k in keys}
