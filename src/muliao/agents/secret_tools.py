"""secret 的工具（vault_set、eth_sign）：保存用户给的 secret，只能按 key 引用，没有读取明文的工具；eth_sign 用 vault 里
的私钥在本进程内签名（agents.eth_signing），明文不出进程。"""

import logging
from typing import Any

from pydantic_ai import FunctionToolset, RunContext

from muliao.agents.eth_signing import Signature, SignKind, sign
from muliao.agents.secrets import Vault
from muliao.platform.tooling import TOOL_MAX_RETRIES, RunScope

log = logging.getLogger(__name__)


# 工具名 → 明文参数名：agent_runtime 在消息落库、推送前据此脱敏（经 agents.api 导出）。
# 改工具名或参数名时一起改；tests/test_tool_schemas.py 断言它们在注册的工具 schema 里存在。
SECRET_TOOL_ARGS: dict[str, frozenset[str]] = {
    "vault_set": frozenset({"value"}),
}


def vault_toolset(vault: Vault) -> FunctionToolset[RunScope]:
    ts = FunctionToolset[RunScope](max_retries=TOOL_MAX_RETRIES)

    @ts.tool
    async def vault_set(ctx: RunContext[RunScope], key: str, value: str, description: str = "") -> str:
        """Store a secret the user gave you so it never has to appear in files, skills, memory or MCP configs:
        it is encrypted into `secrets/<KEY>.toml` in your asset directory (a new key is yours; changing an existing
        one needs you to be able to write it). Keys are unique. The value is redacted from the saved conversation.
        Reference it later as `${vault:KEY}` in MCP configs, or via `run_shell(secrets=[KEY])`. To see keys, list
        the `secrets/` directories; to delete one, remove its file.

        Args:
            key: Identifier like `SLACK_TOKEN`.
            value: The secret value.
            description: What it is for.
        """
        me = ctx.deps.agent_id
        await vault.set(key, value, description, actor=me, owner=me)
        return f"stored secret {key}"

    @ts.tool
    async def eth_sign(
        ctx: RunContext[RunScope], key: str, kind: SignKind, payload: str | dict[str, Any]
    ) -> Signature:
        """Sign with an Ethereum private key stored in the vault, inside the service: the key never reaches a
        command, environment variable or file. Only signs, never broadcasts: send a signed transaction yourself
        (e.g. `eth_sendRawTransaction`). Every result includes the signer address; check it is the account you
        meant before using the signature. Signing a bare 32-byte hash is not supported.
        Returns a JSON object with `signer` and, depending on `kind`, `raw_transaction` and `tx_hash`, or
        `signature` and `message_hash` (the others are null).

        Args:
            key: Vault key holding the private key (one you can use).
            kind: `transaction`: payload is an object of transaction fields (must include `chainId`; legacy with
                `gasPrice` or EIP-1559 with `maxFeePerGas`/`maxPriorityFeePerGas`; numbers as integers or 0x hex);
                signed offline: nothing is looked up on chain or filled in, so give `nonce`, `gas` and the fee
                fields yourself (a missing one fails with `invalid transaction`); returns `raw_transaction` and
                `tx_hash`. `message`: EIP-191 personal_sign of payload, a string:
                text, or 0x-prefixed hex meaning raw bytes. `typed_data`: payload is the full EIP-712 object
                (`types` incl. `EIP712Domain`, `primaryType`, `domain`, `message`), e.g. a Safe transaction for
                its safeTxHash signature. message and typed_data return the 65-byte `signature` (r||s||v) and
                the 32-byte digest signed as `message_hash`.
            payload: What to sign, as described for `kind`.
        """
        me = ctx.deps.agent_id
        private_key = (await vault.resolve_many(me, [key]))[key]
        result = sign(private_key, key, kind, payload)
        # 审计：谁用哪个 key 签了什么（不记私钥）
        log.info(
            "eth signed",
            extra={
                "agent": me,
                "key": key,
                "sign_kind": kind.value,
                "signer": result.signer,
                "digest": result.tx_hash or result.message_hash,
            },
        )
        return result

    return ts
