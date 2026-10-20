"""vault 领域的公开接口：别的领域只经这里使用 vault（docs/architecture.md 2.3）。"""

from muliao.vault.service import VAULT_REF, SecretInfo, Vault

__all__ = ["VAULT_REF", "SecretInfo", "Vault"]
