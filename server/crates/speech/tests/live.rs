//! 真调一次本机配置的转写服务（花钱、要联网，默认跳过）：`cargo test -p muliao-speech --test live -- --ignored`。
//! 录音用 macOS 的 `say` 现合成一段中文、`afconvert` 转成 wav。

use muliao_platform::ids::MediaType;
use std::process::Command;

use muliao_platform::config::{ConfigFile, DEFAULT_CONFIG_PATH, expand_home};
use muliao_speech::config::TranscriptionConfig;

#[tokio::test]
#[ignore = "calls the configured transcription service"]
async fn live_transcribe() {
    let mut file = ConfigFile::load(&expand_home(DEFAULT_CONFIG_PATH)).expect("the live config parses");
    let config: TranscriptionConfig = file.required().expect("[transcription] is configured");
    let t = muliao_speech::build(&config, reqwest::Client::new());
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

    let text = t.transcribe(&audio, &MediaType::parse("audio/wav").unwrap()).await.unwrap();
    eprintln!("transcribed {} bytes: {text:?}", audio.len());
    assert!(text.contains("天气"), "{text}");
}
