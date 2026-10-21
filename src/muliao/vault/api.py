"""vault 领域的公开接口：别的领域只经这里使用 vault（docs/architecture.md 2.3）。"""

from muliao.vault.service import VAULT_REF, SecretInfo, Vault
from muliao.vault.tools import SECRET_TOOL_ARGS

__all__ = ["SECRET_TOOL_ARGS", "VAULT_REF", "SecretInfo", "Vault"]
