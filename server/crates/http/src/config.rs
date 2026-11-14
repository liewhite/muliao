//! 配置文件的 `[server]`、`[auth]` 段：在哪个地址提供服务、对外地址、登录密码与 cookie 签名密钥。

use muliao_platform::config::{ConfigSection, Secret, at_least};
use serde::Deserialize;

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ServerConfig {
    #[serde(default = "default_host")]
    pub host: String,
    #[serde(default = "default_port")]
    pub port: u16,
    /// 对外访问地址：通知点击跳转、cookie 是否 Secure
    pub public_url: String,
}

fn default_host() -> String {
    "127.0.0.1".into()
}
fn default_port() -> u16 {
    8000
}

impl ConfigSection for ServerConfig {
    const NAME: &'static str = "server";

    fn check(&self) -> Vec<String> {
        Vec::new()
    }
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct AuthConfig {
    /// 至少 8 个字符
    pub password: Secret,
    /// cookie 签名密钥，至少 32 个字符
    pub secret_key: Secret,
}

impl ConfigSection for AuthConfig {
    const NAME: &'static str = "auth";

    fn check(&self) -> Vec<String> {
        let mut e = Vec::new();
        at_least(&mut e, "auth.password length", self.password.expose().chars().count(), 8);
        at_least(&mut e, "auth.secret_key length", self.secret_key.expose().chars().count(), 32);
        e
    }
}

#[cfg(test)]
mod tests {
    use muliao_platform::config::ConfigFile;

    use super::*;

    #[test]
    fn short_secrets_are_refused() {
        let text = "[auth]\npassword = \"1234\"\nsecret_key = \"0123456789abcdef0123456789abcdef\"\n";
        let mut f = ConfigFile::parse(text, "/tmp/c.toml".into()).unwrap();
        let err = f.required::<AuthConfig>().unwrap_err().to_string();
        assert!(err.contains("auth.password length") && !err.contains("1234"), "{err}");
        let mut f = ConfigFile::parse("[server]\npublic_url = \"http://x\"\n", "/tmp/c.toml".into()).unwrap();
        assert_eq!(f.required::<ServerConfig>().unwrap().port, 8000);
    }
}
