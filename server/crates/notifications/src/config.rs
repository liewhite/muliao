//! 配置文件的 `[push]` 段。

use muliao_platform::config::ConfigSection;
use serde::Deserialize;

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct PushConfig {
    /// VAPID subject，Web Push 协议要求为 mailto: 或 https: URL
    pub vapid_subject: String,
}

impl ConfigSection for PushConfig {
    const NAME: &'static str = "push";

    fn check(&self) -> Vec<String> {
        if self.vapid_subject.starts_with("mailto:") || self.vapid_subject.starts_with("https://") {
            Vec::new()
        } else {
            vec!["push.vapid_subject must start with 'mailto:' or 'https://'".into()]
        }
    }
}

#[cfg(test)]
mod tests {
    use muliao_platform::config::ConfigFile;

    use super::*;

    #[test]
    fn the_vapid_subject_is_a_contact() {
        let parse = |v: &str| {
            ConfigFile::parse(format!("[push]\nvapid_subject = \"{v}\"\n"), "/tmp/c.toml".into())
                .and_then(|mut f| f.required::<PushConfig>())
        };
        assert!(parse("mailto:a@b.c").is_ok());
        assert!(parse("a@b.c").unwrap_err().to_string().contains("push.vapid_subject"));
    }
}
