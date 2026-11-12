//! secret 的加解密：Fernet（与 Python 版 cryptography.fernet 同一种密文，现有 `secrets/<KEY>.toml` 原样能读），主密钥在
//! 配置 `[vault].key`。secret 文件的格式、谁能用哪个 secret 是资源目录领域的事，这里只管密文与明文互换。

use std::fmt;

pub struct Vault {
    fernet: fernet::Fernet,
}

#[derive(Debug, thiserror::Error)]
pub enum VaultError {
    /// 主密钥不是 32 字节的 urlsafe base64：配置写错了，启动时报出
    #[error("[vault].key is not a valid Fernet key (32 url-safe base64-encoded bytes)")]
    BadKey,
    /// 密文解不开：文件被手改过，或是用别的主密钥加密的
    #[error("ciphertext cannot be decrypted with this vault key")]
    Undecryptable,
    #[error("decrypted secret is not UTF-8 text")]
    NotText,
}

impl Vault {
    pub fn new(key: &str) -> Result<Self, VaultError> {
        fernet::Fernet::new(key).map(|fernet| Self { fernet }).ok_or(VaultError::BadKey)
    }

    pub fn encrypt(&self, plaintext: &str) -> String {
        self.fernet.encrypt(plaintext.as_bytes())
    }

    pub fn decrypt(&self, ciphertext: &str) -> Result<String, VaultError> {
        let bytes = self.fernet.decrypt(ciphertext).map_err(|_| VaultError::Undecryptable)?;
        String::from_utf8(bytes).map_err(|_| VaultError::NotText)
    }
}

impl fmt::Debug for Vault {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("Vault(..)")
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn round_trip_and_wrong_key() {
        let a = Vault::new(&fernet::Fernet::generate_key()).unwrap();
        let b = Vault::new(&fernet::Fernet::generate_key()).unwrap();
        let token = a.encrypt("s3cret");
        assert_eq!(a.decrypt(&token).unwrap(), "s3cret");
        assert!(matches!(b.decrypt(&token), Err(VaultError::Undecryptable)));
        assert!(matches!(Vault::new("short"), Err(VaultError::BadKey)));
    }

    /// Python 版（cryptography.fernet）加密的密文能解开：固定的 key 与密文由 Python 生成。
    #[test]
    fn decrypts_python_tokens() {
        let vault = Vault::new("3QOHdQSfwdIhMog5wkCMiIBXFANh4YdV-XA9xY3RmyY=").unwrap();
        let token = "gAAAAABq9Y1e7m94nEu3NVsGGVXjkZUJeVhf_5V0YPAEO3gq262y5q8ikAyO1QOz1eNdo6gZc95ldIyfMjXhfYCUJhPc7YN5qB7B3s9T3SRO5_rq5EBzios=";
        assert_eq!(vault.decrypt(token).unwrap(), "python 加密的 secret");
    }
}
