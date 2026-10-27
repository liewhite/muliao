"""secret 的工具（vault_*）：保存用户给的 secret，只能按 key 引用，没有读取明文的工具。"""

from pydantic import BaseModel
from pydantic_ai import FunctionToolset, RunContext

from muliao.agents.secrets import Vault
from muliao.platform.tooling import TOOL_MAX_RETRIES, RunScope


class SecretKeyInfo(BaseModel):
    key: str
    # 是你自己的（否则是角色或你自己的 allow 分给你的）
    yours: bool
    description: str


# 工具名 → 明文参数名：agent_runtime 在消息落库、推送前据此脱敏（经 agents.api 导出）。
# 改工具名或参数名时一起改；tests/test_tool_schemas.py 断言它们在注册的工具 schema 里存在。
SECRET_TOOL_ARGS: dict[str, frozenset[str]] = {
    "vault_set": frozenset({"value"}),
}


def vault_toolset(vault: Vault) -> FunctionToolset[RunScope]:
    ts = FunctionToolset[RunScope](max_retries=TOOL_MAX_RETRIES)

    @ts.tool
    async def vault_list(ctx: RunContext[RunScope]) -> list[SecretKeyInfo]:
        """List the secret keys you can use: your own and those your roles or your own allow rules give you.
        Returns a JSON array, each item with `key`, `yours` and `description`. Values are never shown."""
        me = ctx.deps.agent_id
        return [
            SecretKeyInfo(key=s.key, yours=s.owner == me, description=s.description)
            for s in await vault.usable_by(me)
        ]

    @ts.tool
    async def vault_set(ctx: RunContext[RunScope], key: str, value: str, description: str = "") -> str:
        """Store a secret the user gave you so it never has to appear in files, skills, memory or MCP configs.
        A new key is yours; other agents get it only through a role. Changing an existing key needs you to own
        it (or be an admin). Keys are unique. The value is redacted from the saved conversation.
        Reference it later as `${vault:KEY}` in MCP configs, or via `run_shell(secrets=[KEY])`.

        Args:
            key: Identifier like `SLACK_TOKEN`.
            value: The secret value.
            description: What it is for.
        """
        me = ctx.deps.agent_id
        await vault.set(key, value, description, actor=me, owner=me)
        return f"stored secret {key}"

    @ts.tool
    async def vault_delete(ctx: RunContext[RunScope], key: str) -> str:
        """Delete a secret that is no longer needed (yours, or any if you are an admin).

        Args:
            key: The secret key.
        """
        await vault.delete(key, ctx.deps.agent_id)
        return f"deleted secret {key}"

    return ts
