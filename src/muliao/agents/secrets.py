"""secret：AI 资源的一种，一个文件（`secrets/<KEY>.toml`，在 owner 的资源目录下，格式见 agents.secrets_format），值以 Fernet
密文存放，主密钥在服务端配置。agent 只能通过 key 引用，没有读取明文的工具。

这里管加解密、存取，以及 ${vault:KEY} 引用的解析。对外只有不含值的 SecretInfo，与按使用者解析出的明文（resolve_many）。
每个 secret 带 allow_shell（#528，防误操作）：false 的不让 agent 自己写的命令（run_shell、触发器的 precheck 与 stream）拿到，只给
skill 里封装好的脚本（skill_run）与 MCP 配置。哪些入口算 shell 由调用方在 resolve_many 的 shell 参数里说。
引用按使用者能用的 secret 解析（agents.access 的 Grants.can：自己的与分发给它的）：agent 运行的命令与它连接的 MCP server
都用该 agent；用户（USER，如界面上新增 MCP 时的探测）能用全部。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime

from cryptography.fernet import Fernet, InvalidToken

from muliao.agents import assets
from muliao.agents.access import usable, writable
from muliao.agents.kinds import StoredKind
from muliao.agents.secrets_format import KEY_SYNTAX, VAULT_REF, SecretFile, secret_file, validate_key
from muliao.agents.source import AssetSource
from muliao.errors import InvalidInput, NotFound
from muliao.platform.fs import write_atomic

log = logging.getLogger(__name__)

__all__ = ["KEY_SYNTAX", "VAULT_REF", "SecretInfo", "Vault", "fill", "validate_key"]

_KIND = StoredKind.SECRET


def fill(template: str, values: Mapping[str, str]) -> str:
    """把字符串中的 ${vault:KEY} 替换为 values 里的明文（values 须含全部引用，通常来自 resolve_many）。"""
    return VAULT_REF.sub(lambda m: values[m.group(1)], template)


@dataclass(frozen=True)
class SecretInfo:
    """一个 secret 的信息，不含值。"""

    owner: str | None
    key: str
    description: str
    allow_shell: bool
    updated_at: datetime


def _file(a: assets.Asset) -> SecretFile:
    assert a.secret is not None, "a secret asset always carries its file content"
    return a.secret


def _info(a: assets.Asset) -> SecretInfo:
    return SecretInfo(a.owner, a.name, a.description, _file(a).allow_shell, a.updated_at)


class Vault:
    def __init__(self, source: AssetSource, fernet_key: str):
        self._source = source
        self._fernet = Fernet(fernet_key.encode())

    async def set(
        self,
        key: str,
        value: str,
        description: str,
        *,
        actor: str,
        owner: str | None,
        allow_shell: bool | None = None,
    ) -> bool:
        """新建（归 owner）或改写（actor 要能改它，owner 不变）这个 secret 的值与描述：加密后写进文件。
        allow_shell 为 None：已有的保持原值，新建的允许（只改值时不把设成不允许的悄悄放开）。返回最终的 allow_shell。"""
        if not value:
            raise InvalidInput("secret value must not be empty")
        validate_key(key)
        ciphertext = self._fernet.encrypt(value.encode())
        async with self._source.writing() as snapshot:
            existing = writable(snapshot, _KIND, key, actor)
            if allow_shell is None:
                allow_shell = True if existing is None else _file(existing).allow_shell
            path = existing.path if existing is not None else snapshot.path_of(_KIND, key, owner)
            content = secret_file(SecretFile(description, ciphertext, allow_shell))
            await asyncio.to_thread(write_atomic, path, content)
        # 别的 agent 可能依赖它：记录谁改了哪个 key（不记值）
        log.info("secret set", extra={"owner": owner, "key": key, "actor": actor})
        return allow_shell

    async def delete(self, key: str) -> None:
        """用户在界面上删。"""
        async with self._source.writing() as snapshot:
            asset = snapshot.asset(_KIND, key)
            await asyncio.to_thread(assets.remove, asset.path)
        log.info("secret deleted", extra={"key": key})

    async def get(self, key: str) -> SecretInfo:
        """一个 secret 的信息（不含值）。"""
        return _info((await self._source.snapshot()).asset(_KIND, key))

    async def resolve_many(self, user: str, keys: list[str], *, shell: bool) -> dict[str, str]:
        """取 user（agent 的 id，或 USER）能用的 secret 的明文；任一 key 不能用或解不开即报错。shell：明文要交给 agent
        自己写的命令，这时 allow_shell 为 false 的也报错。"""
        if not keys:
            return {}
        found = {a.name: a for a in usable(await self._source.snapshot(), _KIND, user)}
        missing = [k for k in keys if k not in found]
        if missing:
            raise NotFound(
                f"secret(s) {missing} not found among the secrets you can use (your own and those given to you "
                "by your roles or your own allow rules)"
            )
        if shell and (denied := [k for k in keys if not _file(found[k]).allow_shell]):
            raise InvalidInput(
                f"secret(s) {denied} are not allowed in shell commands (allow_shell = false): use them through a "
                "skill script that wraps the operation, with skill_run(secrets=[...])"
            )
        values = {}
        for k in keys:
            ciphertext = _file(found[k]).ciphertext
            try:
                values[k] = self._fernet.decrypt(ciphertext).decode()
            except InvalidToken:
                raise InvalidInput(
                    f"secret {k!r} cannot be decrypted ({found[k].path} was changed by hand?); set it again with "
                    "vault_set"
                ) from None
        return values
