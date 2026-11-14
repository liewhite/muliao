//! DeepSeek 搜索与抓取（含直连备用）的离线测试：用 wiremock 扮演 DeepSeek 与目标站点。

use std::sync::Arc;

use muliao_platform::errors::Error;
use muliao_web::config::WebConfig;
use muliao_web::{Page, WebProvider};
use wiremock::matchers::{body_json, header, method, path};
use wiremock::{Mock, MockServer, ResponseTemplate};

fn provider_at(base_url: &str) -> Arc<dyn WebProvider> {
    let cfg: WebConfig =
        toml::from_str(&format!("type = \"deepseek\"\napi_key = \"k\"\nbase_url = \"{base_url}/\"")).unwrap();
    muliao_web::build(&cfg, reqwest::Client::new())
}

async fn provider(server: &MockServer) -> Arc<dyn WebProvider> {
    provider_at(&server.uri())
}

fn contract_error(status: u16, message: &str) -> ResponseTemplate {
    ResponseTemplate::new(status).set_body_json(serde_json::json!({"error": {"message": message}}))
}

#[tokio::test]
async fn search_maps_results() {
    let server = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path("/v1/tools/search"))
        .and(header("authorization", "Bearer k"))
        .and(body_json(serde_json::json!({"query": "rust", "count": 3})))
        .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({"results": [
            {"title": "T", "url": "https://x", "summary": "S", "site_name": null, "date": "2026-01-01T00:00:00Z"}
        ]})))
        .mount(&server)
        .await;
    let hits = provider(&server).await.search("rust", 3).await.unwrap();
    assert_eq!(hits.len(), 1);
    assert_eq!(hits[0].title, "T");
    assert_eq!(hits[0].site_name, None);
    assert_eq!(hits[0].published.as_deref(), Some("2026-01-01T00:00:00Z"));
}

#[tokio::test]
async fn search_errors_are_classified_by_status_and_contract() {
    let cases = [
        (contract_error(400, "query too long"), "rejected"),
        (contract_error(413, "too large"), "rejected"),
        (contract_error(401, "bad key"), "failed"),
        (contract_error(404, "not found"), "failed"),
        (contract_error(429, "slow down"), "transient"),
        (ResponseTemplate::new(502).set_body_string("<html>bad gateway</html>"), "transient"),
        // 状态码像「输入无效」但不是契约错误体：网关或配置问题
        (ResponseTemplate::new(400).set_body_string("nginx"), "failed"),
        (ResponseTemplate::new(200).set_body_string("not json"), "failed"),
    ];
    for (resp, want) in cases {
        let server = MockServer::start().await;
        Mock::given(method("POST")).respond_with(resp).mount(&server).await;
        let err = provider(&server).await.search("q", 1).await.unwrap_err();
        let got = match &err {
            Error::Domain(_) => "rejected",
            Error::Unavailable(_) => "transient",
            Error::Upstream(_) => "failed",
            Error::Internal(_) => "internal",
        };
        assert_eq!(got, want, "{err}");
    }
    // 连不上：一个刚释放、没人在听的端口
    let port = std::net::TcpListener::bind("127.0.0.1:0").unwrap().local_addr().unwrap().port();
    let p = provider_at(&format!("http://127.0.0.1:{port}"));
    let r = p.search("q", 1).await;
    assert!(r.as_ref().is_err_and(Error::is_transient), "{r:?}");
}

#[tokio::test]
async fn fetch_returns_the_service_page() {
    let server = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path("/v1/tools/fetch"))
        .respond_with(ResponseTemplate::new(200).set_body_json(serde_json::json!({"title": "T", "content": "C"})))
        .mount(&server)
        .await;
    let page = provider(&server).await.fetch("https://example.com/").await.unwrap();
    assert_eq!(page, Page { title: "T".into(), content: "C".into() });
}

const ARTICLE: &str = r#"<html><head><title>Direct page</title></head><body>
<nav><a href="/home">Home</a> | <a href="/about">About</a></nav>
<article><h1>Direct page</h1>
<p>This is the first paragraph of a reasonably long article body, long enough for readability to keep it as content.
It talks about many things in detail so that the extractor scores it above the threshold.</p>
<p>A second paragraph with a <a href="docs/next.html">relative link</a> and more words to make the text longer and longer
and longer, because readability ignores tiny blocks of text that look like boilerplate.</p>
<p>A third paragraph closes the article with yet more sentences about the subject at hand, for good measure.</p>
</article><footer>Copyright footer</footer></body></html>"#;

#[tokio::test]
async fn fetch_falls_back_to_a_direct_get_when_the_service_cannot_reach_the_target() {
    let server = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path("/v1/tools/fetch"))
        .respond_with(contract_error(404, "failed to fetch URL"))
        .mount(&server)
        .await;
    Mock::given(method("GET"))
        .and(path("/a/page.html"))
        .and(header("user-agent", "Mozilla/5.0 (compatible; muliao web_fetch)"))
        .respond_with(ResponseTemplate::new(200).set_body_raw(ARTICLE, "text/html; charset=utf-8"))
        .mount(&server)
        .await;
    Mock::given(method("GET"))
        .and(path("/raw.py"))
        .respond_with(ResponseTemplate::new(200).set_body_raw("print('hi')\n", "text/x-python"))
        .mount(&server)
        .await;
    let p = provider(&server).await;

    let page = p.fetch(&format!("{}/a/page.html", server.uri())).await.unwrap();
    assert_eq!(page.title, "Direct page");
    assert!(page.content.starts_with("(Fetched directly from "), "{}", page.content);
    assert!(page.content.contains("first paragraph"), "{}", page.content);
    assert!(!page.content.contains("Copyright footer"), "boilerplate removed: {}", page.content);
    assert!(page.content.contains(&format!("({}/a/docs/next.html)", server.uri())), "{}", page.content);

    let page = p.fetch(&format!("{}/raw.py", server.uri())).await.unwrap();
    assert!(page.content.ends_with("\n\nprint('hi')\n"), "{}", page.content);
    assert_eq!(page.title, "");
}

#[tokio::test]
async fn fetch_falls_back_when_the_service_is_down_and_reports_both_reasons() {
    let server = MockServer::start().await;
    Mock::given(method("POST"))
        .and(path("/v1/tools/fetch"))
        .respond_with(ResponseTemplate::new(503).set_body_string("down"))
        .mount(&server)
        .await;
    Mock::given(method("GET")).and(path("/gone")).respond_with(ResponseTemplate::new(404)).mount(&server).await;
    Mock::given(method("GET"))
        .and(path("/pdf"))
        .respond_with(ResponseTemplate::new(200).set_body_raw("%PDF", "application/pdf"))
        .mount(&server)
        .await;
    Mock::given(method("GET"))
        .and(path("/js"))
        .respond_with(
            ResponseTemplate::new(200).set_body_raw("<html><body><div id=app></div></body></html>", "text/html"),
        )
        .mount(&server)
        .await;
    let p = provider(&server).await;
    for (route, direct_reason) in [
        ("/gone", "a direct GET returned HTTP 404"),
        ("/pdf", "content-type application/pdf"),
        ("/js", "without extractable text"),
    ] {
        let Err(Error::Domain(muliao_platform::errors::DomainError { message: reason, .. })) =
            p.fetch(&format!("{}{route}", server.uri())).await
        else {
            panic!("{route}: expected a rejection")
        };
        assert!(reason.contains("HTTP 503"), "{reason}");
        assert!(reason.contains(direct_reason), "{reason}");
    }
}

#[tokio::test]
async fn fetch_does_not_fall_back_on_bad_input_or_configuration() {
    for (status, want_rejected) in [(400, true), (401, false), (402, false)] {
        let server = MockServer::start().await;
        Mock::given(method("POST")).respond_with(contract_error(status, "no")).mount(&server).await;
        // 直连一旦发生会命中这里，测试就会因为 expect(0) 失败
        Mock::given(method("GET")).respond_with(ResponseTemplate::new(200)).expect(0).mount(&server).await;
        let err = provider(&server).await.fetch(&format!("{}/x", server.uri())).await.unwrap_err();
        assert_eq!(matches!(err, Error::Domain(_)), want_rejected, "{status}: {err}");
        assert_eq!(matches!(err, Error::Upstream(_)), !want_rejected, "{status}: {err}");
    }
}

#[tokio::test]
async fn direct_get_decodes_the_declared_charset() {
    let server = MockServer::start().await;
    Mock::given(method("POST")).respond_with(contract_error(404, "failed to fetch URL")).mount(&server).await;
    let (gbk, _, _) = encoding_rs::GBK.encode("你好，世界");
    Mock::given(method("GET"))
        .and(path("/gbk.txt"))
        .respond_with(ResponseTemplate::new(200).set_body_raw(gbk.into_owned(), "text/plain; charset=gbk"))
        .mount(&server)
        .await;
    let page = provider(&server).await.fetch(&format!("{}/gbk.txt", server.uri())).await.unwrap();
    assert!(page.content.ends_with("你好，世界"), "{}", page.content);
}
