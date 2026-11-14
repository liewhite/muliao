//! 真调一次本机配置的网页服务（花钱、要联网，默认跳过）：`cargo test -p muliao-web --test live -- --ignored`。

use muliao_platform::config::{ConfigFile, DEFAULT_CONFIG_PATH, expand_home};
use muliao_web::config::WebConfig;

#[tokio::test]
#[ignore = "calls the configured web service"]
async fn live_search_and_fetch() {
    let mut file = ConfigFile::load(&expand_home(DEFAULT_CONFIG_PATH)).expect("the live config parses");
    let config: WebConfig = file.required().expect("[web] is configured");
    let web = muliao_web::build(&config, reqwest::Client::new());

    let hits = web.search("Rust programming language", 3).await.unwrap();
    assert!(!hits.is_empty());
    eprintln!("search: {:?}", hits.iter().map(|h| &h.url).collect::<Vec<_>>());

    let page = web.fetch("https://www.rust-lang.org/").await.unwrap();
    eprintln!("fetch via service: title {:?}, {} chars", page.title, page.content.len());
    assert!(page.content.contains("Rust"));

    // 抓取服务拿不到 raw.githubusercontent.com 的纯文本（#189），走直连
    let page = web.fetch("https://raw.githubusercontent.com/rust-lang/rust/master/README.md").await.unwrap();
    eprintln!("fetch raw: {}", page.content.lines().next().unwrap_or_default());
    assert!(page.content.contains("Rust"));
}
