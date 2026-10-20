"""vault 领域的公开接口：别的领域只经这里使用 vault（docs/domain-design.md 第 10 节）。"""

from muliao.vault.service import VAULT_REF, SecretInfo, Vault

__all__ = ["VAULT_REF", "SecretInfo", "Vault"]
