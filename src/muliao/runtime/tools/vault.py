"""vault：保存用户给的 secret，只能按 key 引用，没有读取明文的工具。"""

from pydantic import BaseModel
from pydantic_ai import FunctionToolset, RunContext

from muliao.ownership import Scope, owner_of, scope_of
from muliao.runtime.scope import RunScope
from muliao.runtime.tools.errors import TOOL_MAX_RETRIES
from muliao.vault import Vault


class SecretKeyInfo(BaseModel):
    key: str
    scope: Scope
    description: str


def vault_toolset(vault: Vault) -> FunctionToolset[RunScope]:
    ts = FunctionToolset[RunScope](max_retries=TOOL_MAX_RETRIES)

    @ts.tool
    async def vault_list(ctx: RunContext[RunScope]) -> list[SecretKeyInfo]:
        """List secret keys you can use: your private ones and public ones (a private key hides a public key
        of the same name). Values are never shown."""
        return [
            SecretKeyInfo(key=s.key, scope=scope_of(s.owner), description=s.description)
            for s in await vault.visible_to(ctx.deps.agent_id)
        ]

    @ts.tool
    async def vault_set(
        ctx: RunContext[RunScope], key: str, value: str, description: str, scope: Scope = "private"
    ) -> str:
        """Store a secret the user gave you so it never has to appear in files, skills, memory or MCP configs.
        The value is redacted from the saved conversation.
        Reference it later as `${vault:KEY}` in MCP configs, or via `run_shell(secrets=[KEY])`.

        Args:
            key: Identifier like `SLACK_TOKEN`.
            value: The secret value.
            description: What it is for.
            scope: `private` (only you) or `public` (every agent, and public MCP servers).
        """
        await vault.set(owner_of(scope, ctx.deps.agent_id), key, value, description)
        return f"stored {scope} secret {key}"

    @ts.tool
    async def vault_delete(ctx: RunContext[RunScope], key: str, scope: Scope = "private") -> str:
        """Delete a secret that is no longer needed.

        Args:
            key: The secret key.
            scope: `private` or `public`.
        """
        await vault.delete(owner_of(scope, ctx.deps.agent_id), key)
        return f"deleted {scope} secret {key}"

    return ts
