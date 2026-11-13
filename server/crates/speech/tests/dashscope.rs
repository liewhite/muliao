//! 百炼转写的离线测试：用 wiremock 扮演百炼。真调一次见 tests/live.rs（默认跳过）。

use muliao_platform::ids::MediaType;
use std::sync::Arc;

use muliao_platform::config::TranscriptionConfig;
use muliao_platform::errors::Error;
use muliao_platform::errors::ErrorKind;
use muliao_speech::Transcriber;
use wiremock::matchers::{body_partial_json, header, method, path};
use wiremock::{Mock, MockServer, ResponseTemplate};

fn transcriber(base_url: &str, extra: &str) -> Arc<dyn Transcriber> {
    let cfg: TranscriptionConfig = toml::from_str(&format!(
        "type = \"dashscope\"\napi_key = \"k\"\nmodel = \"asr\"\nbase_url = \"{base_url}/\"\n{extra}"
    ))
    .unwrap();
    muliao_speech::build(&cfg, reqwest::Client::new())
}

#[tokio::test]
async fn sends_the_audio_inline_with_its_format_and_returns_trimmed_text() {
    let server = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path("/services/aigc/multimodal-generation/generation"))
        .and(header("authorization", "Bearer k"))
        .and(header("x-dashscope-sse", "disable"))
        .and(body_partial_json(serde_json::json!({
            "model": "asr",
            "input": {"messages": [{"role": "user", "content": [
                {"type": "input_audio", "input_audio": {"data": "data:audio/webm;base64,AQID"}}
            ]}]},
            "parameters": {"format": "webm", "vocabulary": {"muliao": 5}, "language_hints": ["zh", "en"]},
        })))
        .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({
            "output": {"text": "  你好 muliao \n"}, "usage": {"seconds": 1}
        })))
        .expect(1)
        .mount(&server)
        .await;
    let t = transcriber(&server.uri(), "language_hints = [\"zh\", \"en\"]\n[vocabulary]\nmuliao = 5");
    assert_eq!(
        t.transcribe(&[1, 2, 3], &MediaType::parse("audio/webm;codecs=opus").unwrap()).await.unwrap(),
        "你好 muliao"
    );
}

#[tokio::test]
async fn optional_parameters_are_left_out_when_not_configured() {
    let server = MockServer::start().await;
    Mock::given(method("POST"))
        .respond_with(move |req: &wiremock::Request| {
            let body: serde_json::Value = serde_json::from_slice(&req.body).unwrap();
            assert_eq!(body["parameters"], serde_json::json!({"format": "mp4"}));
            ResponseTemplate::new(200).set_body_json(serde_json::json!({"output": {"text": ""}}))
        })
        .mount(&server)
        .await;
    // 听不到人说话：空字符串，不是错误
    assert_eq!(
        transcriber(&server.uri(), "").transcribe(b"x", &MediaType::parse("audio/mp4").unwrap()).await.unwrap(),
        ""
    );
}

#[tokio::test]
async fn bad_input_is_rejected_without_calling_the_service() {
    let server = MockServer::start().await;
    Mock::given(method("POST")).respond_with(ResponseTemplate::new(200)).expect(0).mount(&server).await;
    let t = transcriber(&server.uri(), "");
    for (audio, media) in [(&b"x"[..], "video/webm"), (&b""[..], "audio/webm")] {
        let err = t.transcribe(audio, &MediaType::parse(media).unwrap()).await.unwrap_err();
        assert!(matches!(err, Error::Domain(ref e) if e.kind == ErrorKind::InvalidInput), "{err}");
    }
}

#[tokio::test]
async fn upstream_failures_carry_the_reason() {
    let cases = [
        (
            ResponseTemplate::new(400).set_body_string(r#"{"code":"InvalidParameter","message":"format is required"}"#),
            "HTTP 400: {\"code\":\"InvalidParameter\"",
        ),
        (ResponseTemplate::new(200).set_body_json(serde_json::json!({"output": {}})), "unexpected response"),
    ];
    for (resp, want) in cases {
        let server = MockServer::start().await;
        Mock::given(method("POST")).respond_with(resp).mount(&server).await;
        let err =
            transcriber(&server.uri(), "").transcribe(b"x", &MediaType::parse("audio/ogg").unwrap()).await.unwrap_err();
        let Error::Upstream { message: reason, .. } = err else { panic!("{err}") };
        assert!(reason.starts_with("transcription failed (asr): "), "{reason}");
        assert!(reason.contains(want), "{reason}");
    }
    let port = std::net::TcpListener::bind("127.0.0.1:0").unwrap().local_addr().unwrap().port();
    let err = transcriber(&format!("http://127.0.0.1:{port}"), "")
        .transcribe(b"x", &MediaType::parse("audio/ogg").unwrap())
        .await
        .unwrap_err();
    assert!(matches!(err, Error::Upstream { .. }), "{err}");
}
