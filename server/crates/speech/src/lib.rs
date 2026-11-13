//! 语音转文字（配置了 `[transcription]` 才有）：界面录下的一段语音转成文字，由界面作为用户消息发给 agent（agent 只看到
//! 文字，录音不落盘）。
//!
//! [`Transcriber`] 是唯一接口，具体服务是它的实现，由配置 `[transcription].type` 选择（[`build`]）。用户说话时中英文混杂，
//! 用专门的语音识别模型（不用全模态对话模型：它会把语音当成提问来回答，还要先思考，慢）。
//!
//! 转写是用户在界面上同步等待的一次调用：服务出错或不可达时报 [`Error::Upstream`]（HTTP 层映射成 502），原因原样
//! 交给界面，由用户决定重录，不重试。

use muliao_platform::ids::MediaType;
use std::collections::BTreeMap;
use std::sync::Arc;
use std::time::{Duration, Instant};

use base64::Engine;
use futures::future::BoxFuture;
use muliao_platform::config::{TranscriptionConfig, TranscriptionType};
use muliao_platform::errors::{DomainError, Error};
use serde::Deserialize;

pub const DASHSCOPE_BASE_URL: &str = "https://dashscope.aliyuncs.com/api/v1";

/// 浏览器录音的 MIME 类型（去掉 `;codecs=` 参数后）→ 服务的音频格式参数。Chrome/Firefox 录成 webm 或 ogg（Opus），
/// iOS Safari 录成 mp4（AAC）
const AUDIO_FORMATS: &[(&str, &str)] = &[
    ("audio/webm", "webm"),
    ("audio/ogg", "ogg"),
    ("audio/mp4", "mp4"),
    ("audio/x-m4a", "m4a"),
    ("audio/m4a", "m4a"),
    ("audio/aac", "aac"),
    ("audio/mpeg", "mp3"),
    ("audio/mp3", "mp3"),
    ("audio/wav", "wav"),
    ("audio/x-wav", "wav"),
    ("audio/wave", "wav"),
    ("audio/flac", "flac"),
    ("audio/amr", "amr"),
];

/// 一段语音转写通常一两秒；服务挂起时让用户尽早看到失败，而不是一直转圈
const TIMEOUT: Duration = Duration::from_secs(60);

pub trait Transcriber: Send + Sync {
    /// 一段语音的字节上限，HTTP 层按它读取请求体
    fn max_bytes(&self) -> u64;

    /// 转写出的文字（首尾空白已去掉）；听不到人说话时为空字符串。
    fn transcribe<'a>(&'a self, audio: &'a [u8], media_type: &'a MediaType) -> BoxFuture<'a, Result<String, Error>>;
}

/// 按配置选实现。`client` 是共用的出站 HTTP 客户端（由组合根创建）。
pub fn build(cfg: &TranscriptionConfig, client: reqwest::Client) -> Arc<dyn Transcriber> {
    match cfg.kind {
        TranscriptionType::Dashscope => Arc::new(DashScope {
            client,
            url: format!(
                "{}/services/aigc/multimodal-generation/generation",
                cfg.base_url.as_deref().unwrap_or(DASHSCOPE_BASE_URL).trim_end_matches('/')
            ),
            authorization: format!("Bearer {}", cfg.api_key.expose()),
            model: cfg.model.clone(),
            max_bytes: cfg.max_bytes,
            vocabulary: cfg.vocabulary.clone(),
            language_hints: cfg.language_hints.clone(),
        }),
    }
}

/// 媒体类型对应的音频格式；不是支持的音频类型时报 InvalidInput。
pub fn audio_format(media_type: &MediaType) -> Result<&'static str, DomainError> {
    match AUDIO_FORMATS.iter().find(|(mime, _)| *mime == media_type.as_str()) {
        Some((_, format)) => Ok(format),
        None => {
            let supported: Vec<&str> = AUDIO_FORMATS.iter().map(|(m, _)| *m).collect();
            Err(DomainError::invalid(format!("unsupported audio type {media_type:?}; supported: {supported:?}")))
        }
    }
}

/// 百炼录音文件识别的同步接口（multimodal-generation，非流式）：音频以 data URI 内联，一次返回整段文字。
struct DashScope {
    client: reqwest::Client,
    url: String,
    authorization: String,
    model: String,
    max_bytes: u64,
    vocabulary: BTreeMap<String, i64>,
    language_hints: Vec<String>,
}

/// 响应里用到的字段：不合这个形状的响应是服务端故障，不当成听不到人说话
#[derive(Deserialize)]
struct Response {
    output: Output,
    usage: Option<serde_json::Value>,
}

#[derive(Deserialize)]
struct Output {
    text: String,
}

impl DashScope {
    fn body(&self, audio: &[u8], mime: &str, format: &str) -> serde_json::Value {
        // format 必填
        let mut parameters = serde_json::json!({"format": format});
        if !self.vocabulary.is_empty() {
            parameters["vocabulary"] = serde_json::json!(self.vocabulary);
        }
        if !self.language_hints.is_empty() {
            parameters["language_hints"] = serde_json::json!(self.language_hints);
        }
        let data = base64::engine::general_purpose::STANDARD.encode(audio);
        serde_json::json!({
            "model": self.model,
            "input": {"messages": [{"role": "user", "content": [
                {"type": "input_audio", "input_audio": {"data": format!("data:{mime};base64,{data}")}}
            ]}]},
            "parameters": parameters,
        })
    }

    fn failed(&self, reason: String, format: &str, size: usize) -> Error {
        tracing::warn!(model = %self.model, format, bytes = size, %reason, "transcription failed");
        Error::Upstream(format!("transcription failed ({}): {reason}", self.model))
    }
}

impl Transcriber for DashScope {
    fn max_bytes(&self) -> u64 {
        self.max_bytes
    }

    fn transcribe<'a>(&'a self, audio: &'a [u8], media_type: &'a MediaType) -> BoxFuture<'a, Result<String, Error>> {
        Box::pin(async move {
            let format = audio_format(media_type)?;
            if audio.is_empty() {
                return Err(DomainError::invalid("audio is empty").into());
            }
            let started = Instant::now();
            let sent = self
                .client
                .post(&self.url)
                .header(reqwest::header::AUTHORIZATION, &self.authorization)
                .header("X-DashScope-SSE", "disable")
                .json(&self.body(audio, media_type.as_str(), format))
                .timeout(TIMEOUT)
                .send()
                .await;
            let resp = sent.map_err(|e| self.failed(e.to_string(), format, audio.len()))?;
            let status = resp.status().as_u16();
            let bytes = resp.bytes().await.map_err(|e| self.failed(e.to_string(), format, audio.len()))?;
            let preview = |n: usize| String::from_utf8_lossy(&bytes).chars().take(n).collect::<String>();
            if status != 200 {
                return Err(self.failed(format!("HTTP {status}: {}", preview(500)), format, audio.len()));
            }
            let body: Response = serde_json::from_slice(&bytes).map_err(|e| {
                self.failed(format!("unexpected response {:?}: {e}", preview(200)), format, audio.len())
            })?;
            let text = body.output.text.trim().to_string();
            tracing::info!(
                model = %self.model,
                format,
                bytes = audio.len(),
                chars = text.chars().count(),
                seconds = started.elapsed().as_secs_f64(),
                usage = ?body.usage,
                "audio transcribed"
            );
            Ok(text)
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn audio_types_ignore_codecs_and_case() {
        let format = |raw: &str| audio_format(&MediaType::parse(raw).unwrap());
        assert_eq!(format("audio/webm;codecs=opus").unwrap(), "webm");
        assert_eq!(format("Audio/MP4").unwrap(), "mp4");
        assert!(format("video/webm").is_err());
    }
}
