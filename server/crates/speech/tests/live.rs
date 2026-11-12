//! 真调一次本机配置的转写服务（花钱、要联网，默认跳过）：`cargo test -p muliao-speech --test live -- --ignored`。
//! 录音用 macOS 的 `say` 现合成一段中文、`afconvert` 转成 wav。

use std::process::Command;

use muliao_platform::config::{DEFAULT_CONFIG_PATH, Settings, expand_home};

#[tokio::test]
#[ignore = "calls the configured transcription service"]
async fn live_transcribe() {
    let settings = Settings::load(&expand_home(DEFAULT_CONFIG_PATH)).expect("the live config parses");
    let t = muliao_speech::build(
        settings.transcription.as_ref().expect("[transcription] is configured"),
        reqwest::Client::new(),
    );
    let dir = std::env::temp_dir().join(format!("muliao-speech-live-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    let (aiff, wav) = (dir.join("a.aiff"), dir.join("a.wav"));
    assert!(Command::new("say").arg("-o").arg(&aiff).arg("今天天气不错，我们去公园散步吧").status().unwrap().success());
    assert!(
        Command::new("afconvert")
            .args(["-f", "WAVE", "-d", "LEI16@16000"])
            .arg(&aiff)
            .arg(&wav)
            .status()
            .unwrap()
            .success()
    );
    let audio = std::fs::read(&wav).unwrap();
    std::fs::remove_dir_all(&dir).unwrap();

    let text = t.transcribe(&audio, "audio/wav").await.unwrap();
    eprintln!("transcribed {} bytes: {text:?}", audio.len());
    assert!(text.contains("天气"), "{text}");
}
