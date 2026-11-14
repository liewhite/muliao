//! 配置文件的 `[transcription]` 段（组合根取出，[`crate::build`] 按它选服务）。

use std::collections::BTreeMap;

use muliao_platform::config::{ConfigSection, Secret, at_least};
use serde::Deserialize;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum TranscriptionType {
    /// 阿里云百炼的录音文件识别
    Dashscope,
}

/// 语音转文字：界面录下的语音先转成文字，再作为消息发给 agent。不配置时界面没有语音输入。
#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct TranscriptionConfig {
    #[serde(rename = "type")]
    pub kind: TranscriptionType,
    pub api_key: Secret,
    /// 语音识别模型，如 qwen-audio-3.0-asr-flash
    pub model: String,
    /// 为空时用该类型的官方地址（dashscope：https://dashscope.aliyuncs.com/api/v1）
    pub base_url: Option<String>,
    /// 一段语音的字节上限：音频以 base64 内联在请求里，百炼限制编码后 10 MB
    #[serde(default = "default_max_audio_bytes")]
    pub max_bytes: u64,
    /// 即时热词：词 → 权重（1–5，50 为超级热词）
    #[serde(default)]
    pub vocabulary: BTreeMap<String, i64>,
    /// 语种提示（如 ["zh", "en"]）；为空时自动识别
    #[serde(default)]
    pub language_hints: Vec<String>,
}

fn default_max_audio_bytes() -> u64 {
    7 * 1024 * 1024
}

impl ConfigSection for TranscriptionConfig {
    const NAME: &'static str = "transcription";

    fn check(&self) -> Vec<String> {
        let mut e = Vec::new();
        at_least(&mut e, "transcription.api_key length", self.api_key.expose().len(), 1);
        at_least(&mut e, "transcription.model length", self.model.len(), 1);
        at_least(&mut e, "transcription.max_bytes", self.max_bytes, 1);
        e
    }
}
