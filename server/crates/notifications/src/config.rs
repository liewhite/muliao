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
