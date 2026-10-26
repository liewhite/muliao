"""vault：保存用户给的 secret，只能按 key 引用，没有读取明文的工具。"""

from pydantic import BaseModel
from pydantic_ai import FunctionToolset, RunContext

from muliao.agents.api import Scope, scope_of
from muliao.platform.tooling import TOOL_MAX_RETRIES, RunScope
from muliao.vault.service import Vault


class SecretKeyInfo(BaseModel):
    key: str
    scope: Scope
    description: str


# 工具名 → 明文参数名：agent_runtime 在消息落库、推送前据此脱敏（经 vault.api 导出）。
# 改工具名或参数名时一起改；tests/test_tool_schemas.py 断言它们在注册的工具 schema 里存在。
SECRET_TOOL_ARGS: dict[str, frozenset[str]] = {
    "vault_set": frozenset({"value"}),
    "vault_set_public": frozenset({"value"}),
}


def vault_toolset(vault: Vault) -> FunctionToolset[RunScope]:
    ts = FunctionToolset[RunScope](max_retries=TOOL_MAX_RETRIES)

    @ts.tool
    async def vault_list(ctx: RunContext[RunScope]) -> list[SecretKeyInfo]:
        """List secret keys you can use: your private ones and public ones. Values are never shown."""
        return [
            SecretKeyInfo(key=s.key, scope=scope_of(s.owner), description=s.description)
            for s in await vault.visible_to(ctx.deps.agent_id)
        ]

    @ts.tool
    async def vault_set(ctx: RunContext[RunScope], key: str, value: str, description: str = "") -> str:
        """Store a secret the user gave you, private to you, so it never has to appear in files, skills, memory or
        MCP configs. The value is redacted from the saved conversation. Keys are unique across private and
        public secrets.
        Reference it later as `${vault:KEY}` in MCP configs, or via `run_shell(secrets=[KEY])`.

        Args:
            key: Identifier like `SLACK_TOKEN`.
            value: The secret value.
            description: What it is for.
        """
        me = ctx.deps.agent_id
        await vault.set(me, key, value, description, me)
        return f"stored private secret {key}"

    @ts.tool
    async def vault_delete(ctx: RunContext[RunScope], key: str) -> str:
        """Delete one of your private secrets that is no longer needed.

        Args:
            key: The secret key.
        """
        me = ctx.deps.agent_id
        await vault.delete(me, key, me)
        return f"deleted private secret {key}"

    @ts.tool
    async def vault_set_public(ctx: RunContext[RunScope], key: str, value: str, description: str = "") -> str:
        """Store a public secret every agent (and public MCP servers) can reference as `${vault:KEY}`. The value
        is redacted from the saved conversation.

        Args:
            key: Identifier like `SLACK_TOKEN`.
            value: The secret value.
            description: What it is for.
        """
        await vault.set(None, key, value, description, ctx.deps.agent_id)
        return f"stored public secret {key}"

    @ts.tool
    async def vault_delete_public(ctx: RunContext[RunScope], key: str) -> str:
        """Delete a public secret that is no longer needed.

        Args:
            key: The secret key.
        """
        await vault.delete(None, key, ctx.deps.agent_id)
        return f"deleted public secret {key}"

    return ts
