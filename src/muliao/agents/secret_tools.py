"""secret 的工具（vault_set）：保存用户给的 secret，只能按 key 引用，没有读取明文的工具。"""

from pydantic_ai import FunctionToolset, RunContext

from muliao.agents.secrets import Vault
from muliao.platform.tooling import TOOL_MAX_RETRIES, RunScope

# 工具名 → 明文参数名：agent_runtime 在消息落库、推送前据此脱敏（经 agents.api 导出）。
# 改工具名或参数名时一起改；tests/test_tool_schemas.py 断言它们在注册的工具 schema 里存在。
SECRET_TOOL_ARGS: dict[str, frozenset[str]] = {
    "vault_set": frozenset({"value"}),
}


def vault_toolset(vault: Vault) -> FunctionToolset[RunScope]:
    ts = FunctionToolset[RunScope](max_retries=TOOL_MAX_RETRIES)

    @ts.tool
    async def vault_set(
        ctx: RunContext[RunScope],
        key: str,
        value: str,
        description: str = "",
        allow_shell: bool | None = None,
    ) -> str:
        """Store a secret the user gave you so it never has to appear in files, skills, memory or MCP configs:
        it is encrypted into `secrets/<KEY>.toml` in your asset directory (a new key is yours; changing an existing
        one needs you to be able to write it). Keys are unique. The value is redacted from the saved conversation.
        Reference it later as `${vault:KEY}` in MCP configs, or via `run_shell(secrets=[KEY])` /
        `skill_run(secrets=[KEY])`. To see keys, list the `secrets/` directories; to delete one, remove its file.

        Args:
            key: Identifier like `SLACK_TOKEN`.
            value: The secret value.
            description: What it is for.
            allow_shell: false keeps the key out of commands you write yourself (run_shell, trigger prechecks and
                streams): only skill scripts run with skill_run and MCP configs can use it. Use it
                for keys that must only be used through a script wrapping the operation. Omitted: a new secret
                allows shell, an existing one keeps its setting.
        """
        me = ctx.deps.agent_id
        shell = await vault.set(key, value, description, actor=me, owner=me, allow_shell=allow_shell)
        return f"stored secret {key} (allow_shell: {str(shell).lower()})"

    return ts
