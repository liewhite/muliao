//! 整个服务的端到端（离线）：组合根装配出的真服务（真库、资源目录、写沙箱、会话 actor），在本机随便一个端口上跑 HTTP，用
//! HTTP 客户端走一遍各组接口。模型是 wiremock 扮的 Anthropic 接口（按顺序交回排好的回复）。

use std::collections::VecDeque;
use std::net::SocketAddr;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use futures::StreamExt;
use reqwest::{Method, StatusCode, header};
use serde_json::{Value, json};
use wiremock::matchers::{method, path};
use wiremock::{Mock, MockServer, Request, Respond, ResponseTemplate};

const PASSWORD: &str = "correct horse";

// ---- 扮演的模型：按顺序交回排好的回复 ----

#[derive(Clone, Default)]
struct Script {
    replies: Arc<Mutex<VecDeque<ResponseTemplate>>>,
}

impl Script {
    fn text(&self, text: &str) {
        let events = [
            json!({"type": "message_start", "message": {"model": "m", "id": "msg", "type": "message", "role": "assistant", "content": [], "stop_reason": null, "usage": {"input_tokens": 10, "output_tokens": 1}}}),
            json!({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
            json!({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}}),
            json!({"type": "content_block_stop", "index": 0}),
            json!({"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"input_tokens": 10, "output_tokens": 2}}),
            json!({"type": "message_stop"}),
        ];
        let body: String =
            events.iter().map(|e| format!("event: {}\ndata: {e}\n\n", e["type"].as_str().unwrap())).collect();
        self.replies.lock().unwrap().push_back(ResponseTemplate::new(200).set_body_string(body));
    }
}

impl Respond for Script {
    fn respond(&self, _r: &Request) -> ResponseTemplate {
        self.replies
            .lock()
            .unwrap()
            .pop_front()
            .unwrap_or_else(|| panic!("the model was asked more times than scripted"))
    }
}

struct World {
    dir: tempfile::TempDir,
    base: String,
    http: reqwest::Client,
    cookie: String,
    script: Script,
    _model: MockServer,
    _app: Arc<muliao_server::Serving>,
}

async fn world() -> World {
    let _ = tracing_subscriber::fmt().with_test_writer().with_env_filter("warn").try_init();
    let model = MockServer::start().await;
    let script = Script::default();
    Mock::given(method("POST")).and(path("/v1/messages")).respond_with(script.clone()).mount(&model).await;
    let dir = tempfile::tempdir().unwrap();
    let assets = dir.path().join("assets");
    std::fs::create_dir_all(assets.join("platform/memory")).unwrap();
    std::fs::write(assets.join("platform/memory/global.md"), "全局记忆\n").unwrap();
    let web = dir.path().join("web");
    std::fs::create_dir_all(web.join("assets")).unwrap();
    std::fs::write(web.join("index.html"), "<!doctype html><title>muliao</title>").unwrap();
    std::fs::write(web.join("assets/app.js"), "console.log(1)").unwrap();
    let config = format!(
        "timezone = \"Asia/Shanghai\"\n[server]\npublic_url = \"http://127.0.0.1\"\n[auth]\npassword = \"{PASSWORD}\"\n\
         secret_key = \"0123456789abcdef0123456789abcdef\"\n[vault]\nkey = \"3QOHdQSfwdIhMog5wkCMiIBXFANh4YdV-XA9xY3RmyY=\"\n\
         [push]\nvapid_subject = \"mailto:a@b.c\"\n[llm]\ndefault_model = \"claude:m\"\n[llm.providers.claude]\n\
         type = \"anthropic\"\napi_key = \"k\"\nbase_url = \"{}\"\n",
        model.uri()
    );
    let config = muliao_server::Config::parse(&config, dir.path().join("config.toml")).unwrap();
    let app = Arc::new(muliao_server::assemble(config, web).await.unwrap().start().await.unwrap());
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let base = format!("http://{}", listener.local_addr().unwrap());
    let router = app.router();
    tokio::spawn(async move {
        axum::serve(listener, router.into_make_service_with_connect_info::<SocketAddr>()).await.unwrap();
    });
    let http = reqwest::Client::builder().no_proxy().redirect(reqwest::redirect::Policy::none()).build().unwrap();
    let mut w = World { dir, base, http, cookie: String::new(), script, _model: model, _app: app };
    let r = w.send(Method::POST, "/api/auth/login", Some(json!({"password": PASSWORD}))).await;
    assert_eq!(r.status(), StatusCode::NO_CONTENT);
    let set = r.headers()[header::SET_COOKIE].to_str().unwrap().to_owned();
    assert!(set.contains("HttpOnly") && set.contains("SameSite=lax") && !set.contains("Secure"), "{set}");
    w.cookie = set.split(';').next().unwrap().to_owned();
    w
}

impl World {
    async fn send(&self, m: Method, path: &str, body: Option<Value>) -> reqwest::Response {
        let mut req = self.http.request(m, format!("{}{path}", self.base));
        if !self.cookie.is_empty() {
            req = req.header(header::COOKIE, &self.cookie);
        }
        if let Some(b) = body {
            req = req.json(&b);
        }
        req.send().await.unwrap()
    }

    /// 发请求，断言状态码，返回 JSON（没有响应体时为 Null）
    async fn call(&self, m: Method, path: &str, body: Option<Value>, status: u16) -> Value {
        let r = self.send(m.clone(), path, body).await;
        let got = r.status().as_u16();
        let text = r.text().await.unwrap();
        assert_eq!(got, status, "{m} {path}: {text}");
        if text.is_empty() { Value::Null } else { serde_json::from_str(&text).unwrap() }
    }

    async fn get(&self, path: &str) -> Value {
        self.call(Method::GET, path, None, 200).await
    }

    /// 等会话停下（不在运行）
    async fn idle(&self, session_id: &str) -> Value {
        for _ in 0..400 {
            let d = self.get(&format!("/api/sessions/{session_id}")).await;
            if d["state"] == "idle" {
                return d;
            }
            tokio::time::sleep(Duration::from_millis(25)).await;
        }
        panic!("session {session_id} did not settle");
    }

    async fn agent(&self, name: &str) -> String {
        let a = self.call(Method::POST, "/api/agents", Some(json!({"name": name, "description": "测试"})), 201).await;
        a["id"].as_str().unwrap().to_owned()
    }
}

#[tokio::test]
async fn login_guards_the_api_and_backs_off() {
    let w = world().await;
    let as_cookie = |cookie: &'static str, path: &'static str| {
        let req = w.http.get(format!("{}{path}", w.base)).header(header::COOKIE, cookie);
        async move {
            let r = req.send().await.unwrap();
            (r.status(), r.json::<Value>().await.unwrap())
        }
    };
    let unauthorized = (StatusCode::UNAUTHORIZED, json!({"detail": "not authenticated"}));
    assert_eq!(as_cookie("", "/api/agents").await, unauthorized);
    assert_eq!(as_cookie("", "/api/auth/me").await, unauthorized);
    assert_eq!(as_cookie("muliao_session=v1.1.abc", "/api/agents").await, unauthorized);
    w.call(Method::GET, "/api/auth/me", None, 204).await;

    // 同一来源连续输错：第 5 次起要等，等待期内连正确密码也拒绝；别的来源不受影响
    let login = |ip: &'static str, pw: &'static str| {
        let req = w.http.post(format!("{}/api/auth/login", w.base)).header("x-forwarded-for", ip);
        async move { req.json(&json!({"password": pw})).send().await.unwrap() }
    };
    for _ in 0..5 {
        assert_eq!(login("9.9.9.9", "wrong").await.status(), StatusCode::UNAUTHORIZED);
    }
    let blocked = login("9.9.9.9", PASSWORD).await;
    assert_eq!(blocked.status(), StatusCode::TOO_MANY_REQUESTS);
    assert_eq!(blocked.headers()[header::RETRY_AFTER], "30");
    assert_eq!(login("8.8.8.8", PASSWORD).await.status(), StatusCode::NO_CONTENT);

    let out = w.send(Method::POST, "/api/auth/logout", None).await;
    assert_eq!(out.status(), StatusCode::NO_CONTENT);
    assert!(out.headers()[header::SET_COOKIE].to_str().unwrap().starts_with("muliao_session=; Max-Age=0"));
}

#[tokio::test]
async fn a_conversation_over_http_with_live_events() {
    let w = world().await;
    let agent = w.agent("helper").await;

    // SSE：连上先收到心跳（带间隔与前端版本）
    // 浏览器会带 Accept-Encoding：事件流不压缩（压缩会缓冲、事件延迟），下面照样边发边收
    let events = w
        .http
        .get(format!("{}/api/events", w.base))
        .header(header::COOKIE, &w.cookie)
        .header(header::ACCEPT_ENCODING, "gzip")
        .send()
        .await
        .unwrap();
    assert_eq!(events.headers()[header::CONTENT_TYPE], "text/event-stream");
    assert_eq!(events.headers().get(header::CONTENT_ENCODING), None);
    let mut stream = events.bytes_stream();
    let mut buffer = String::new();
    let mut seen: Vec<Value> = Vec::new();
    let mut next_event = async || -> Value {
        loop {
            if let Some(i) = buffer.find("\n\n") {
                let block: String = buffer.drain(..i + 2).collect();
                let data: String = block.lines().filter_map(|l| l.strip_prefix("data: ")).collect();
                if !data.is_empty() {
                    return serde_json::from_str(&data).unwrap();
                }
                continue;
            }
            let chunk = tokio::time::timeout(Duration::from_secs(10), stream.next()).await.unwrap().unwrap().unwrap();
            buffer.push_str(std::str::from_utf8(&chunk).unwrap());
        }
    };
    let beat = next_event().await;
    assert_eq!(beat["type"], "heartbeat");
    assert_eq!(beat["interval_seconds"], 15.0);
    assert_eq!(beat["ui_version"].as_str().unwrap().len(), 12);

    w.script.text("你好，我是 helper");
    let session = w.call(Method::POST, "/api/sessions", Some(json!({"agent_id": agent, "prompt": "你好"})), 201).await;
    let id = session["id"].as_str().unwrap().to_owned();
    assert_eq!(session["origin"], json!({"kind": "user"}));
    assert_eq!(session["title"], "你好");
    let detail = w.idle(&id).await;
    let texts: Vec<(&str, &str)> = detail["items"]
        .as_array()
        .unwrap()
        .iter()
        .map(|i| (i["item"]["type"].as_str().unwrap(), i["item"]["text"].as_str().unwrap()))
        .collect();
    assert_eq!(texts, [("user", "你好"), ("assistant", "你好，我是 helper")]);
    assert_eq!(detail["run"], Value::Null);
    assert_eq!(detail["waits"], json!([]));
    assert_eq!(detail["usage"]["requests"], 1);
    assert_eq!(detail["context"]["threshold_tokens"], 100_000);

    // 这次运行推过的事件：实时的文字、保存了一步、数据变了
    for _ in 0..50 {
        let e = next_event().await;
        let kind = e["type"].as_str().unwrap().to_owned();
        seen.push(e);
        if kind == "changed" && seen.iter().any(|e| e["type"] == "run") {
            break;
        }
    }
    let run = seen.iter().find(|e| e["type"] == "run").unwrap();
    assert_eq!(run["session_id"], json!(id));
    assert_eq!(run["event"], "delta");
    assert_eq!(run["seq"], 0);

    // 列表：带工作状态与未读
    let list = w.get(&format!("/api/sessions?agent_id={agent}&origin=user&origin=trigger")).await;
    assert_eq!(list[0]["id"], json!(id));
    assert_eq!(list[0]["state"], "idle");
    assert!(list[0]["unread"].is_boolean());
    w.call(Method::GET, "/api/sessions?trigger_id=1&origin=user", None, 422).await;
    w.call(Method::GET, "/api/sessions?q=helper", None, 200).await;

    // 续写、重试（没失败的不能重试）、标已读、压缩（后台做）
    w.script.text("第二句");
    let r = w
        .call(
            Method::POST,
            &format!("/api/sessions/{id}/messages"),
            Some(json!({"kind": "message", "prompt": "再说一句"})),
            202,
        )
        .await;
    assert_eq!(r["id"], json!(id));
    let detail = w.idle(&id).await;
    assert_eq!(detail["items"].as_array().unwrap().len(), 4);
    w.call(Method::POST, &format!("/api/sessions/{id}/retry"), None, 409).await;
    w.call(Method::POST, &format!("/api/sessions/{id}/read"), None, 204).await;
    let compacted = w.call(Method::POST, &format!("/api/sessions/{id}/compact"), None, 200).await;
    assert_eq!(compacted, json!({"outcome": "compacted", "steps": 0}), "nothing beyond the kept steps");
    w.call(Method::POST, &format!("/api/sessions/{id}/stop"), None, 202).await;
    w.call(Method::GET, "/api/sessions/nope", None, 404).await;
    w.call(Method::POST, "/api/sessions", Some(json!({"agent_id": agent})), 422).await;
    w.call(Method::DELETE, &format!("/api/sessions/{id}"), None, 204).await;
    w.call(Method::GET, &format!("/api/sessions/{id}"), None, 404).await;
}

#[tokio::test]
async fn agents_roles_assets_and_memory() {
    let w = world().await;
    let defaults = w.get("/api/agent-defaults").await;
    assert_eq!(defaults["model"], json!({"provider": "claude", "model": "m"}));
    assert_eq!(defaults["thinking"], "default");
    let candidates = w.get("/api/assets/rule-candidates").await;
    assert_eq!(candidates[0], "use:tool:*");
    assert!(candidates.as_array().unwrap().iter().any(|c| c == "*:memory:global"), "{candidates}");
    let id = w.agent("writer").await;
    let a = w.get(&format!("/api/agents/{id}")).await;
    assert_eq!(a["config"]["model"], json!({"provider": "claude", "model": "m"}));
    // 模型按 provider 与模型名两部分给，空的不收
    let bad = json!({"model": {"provider": "claude", "model": ""}});
    w.call(Method::PATCH, &format!("/api/agents/{id}"), Some(bad), 422).await;
    assert_eq!(a["is_default"], false);
    w.call(Method::POST, "/api/agents", Some(json!({"name": "writer", "description": "x"})), 409).await;
    let patched = w
        .call(Method::PATCH, &format!("/api/agents/{id}"), Some(json!({"group": "写作", "thinking": "high"})), 200)
        .await;
    assert_eq!(
        (patched["config"]["group"].as_str(), patched["config"]["thinking"].as_str()),
        (Some("写作"), Some("high"))
    );
    assert_eq!(w.call(Method::PUT, &format!("/api/agents/{id}/default"), None, 200).await["is_default"], true);
    w.call(Method::PUT, &format!("/api/agents/{id}/archive"), None, 409).await;
    w.call(Method::DELETE, &format!("/api/agents/{id}/default"), None, 200).await;
    assert!(w.call(Method::PUT, &format!("/api/agents/{id}/archive"), None, 200).await["archived_at"].is_string());
    w.call(Method::DELETE, &format!("/api/agents/{id}/archive"), None, 200).await;

    let roles = w.get("/api/roles").await;
    assert_eq!(roles[0]["name"], "base");
    assert!(roles[0]["reach"]["tools"].as_array().unwrap().iter().any(|t| t["name"] == "read_file"));
    let role = w
        .call(
            Method::POST,
            "/api/roles/ops",
            Some(json!({"description": "运维", "allow": ["use:tool:*"], "deny": []})),
            201,
        )
        .await;
    assert_eq!(role["assignable"], true);
    w.call(Method::PATCH, "/api/roles/ops", Some(json!({"description": "运维二"})), 200).await;
    let reach = w
        .call(
            Method::POST,
            "/api/agent-preview",
            Some(json!({"agent_id": null, "roles": ["ops"], "allow": [], "deny": []})),
            200,
        )
        .await;
    assert!(reach["tools"].as_array().unwrap().iter().any(|t| t["name"] == "create_agent"));
    w.call(Method::POST, "/api/role-preview", Some(json!({"name": null, "allow": ["bad"], "deny": []})), 422).await;
    w.call(Method::DELETE, "/api/roles/ops", None, 204).await;

    // 资源池：内置工具在前，带种类专有的详情
    let catalog = w.get("/api/assets").await;
    assert_eq!(catalog[0]["kind"], "tool");
    assert!(catalog[0]["access"].is_string() && catalog[0]["updated_at"].is_null());
    assert!(catalog.as_array().unwrap().iter().any(|a| a["kind"] == "memory" && a["name"] == "global"));

    // 记忆：版本防覆盖
    let m = w.get("/api/assets/memory/global").await;
    assert_eq!(m["content"], "全局记忆");
    let put = |v: Value| w.call(Method::PUT, "/api/assets/memory/global", Some(v.clone()), 200);
    let saved = put(json!({"content": "新的", "version": m["version"]})).await;
    w.call(Method::PUT, "/api/assets/memory/global", Some(json!({"content": "旧版本", "version": m["version"]})), 409)
        .await;
    assert_eq!(saved["content"], "新的");
    let private = w.get(&format!("/api/agents/{id}/memory")).await;
    w.call(
        Method::PUT,
        &format!("/api/agents/{id}/memory"),
        Some(json!({"content": "我的", "version": private["version"]})),
        200,
    )
    .await;
    let assets = w.get(&format!("/api/agents/{id}/assets")).await;
    assert!(assets["memory_preview"].as_str().unwrap().contains("我的"));

    // skill、secret、MCP
    let skill = w
        .call(
            Method::PUT,
            "/api/assets/skill/demo",
            Some(json!({"owner": id, "description": "演示", "body": "# 步骤\n", "version": null})),
            200,
        )
        .await;
    assert_eq!(skill["owner"], json!(id));
    assert_eq!(w.get("/api/assets/skill/demo").await["body"].as_str().map(str::trim), Some("# 步骤"));
    let files = w.get("/api/assets/skill/demo/files").await;
    assert_eq!(files[0]["name"], "SKILL.md");
    let f = w.send(Method::GET, "/api/assets/skill/demo/file?path=SKILL.md", None).await;
    assert_eq!(f.headers()[header::CONTENT_DISPOSITION], "attachment; filename=\"SKILL.md\"");
    assert_eq!(f.headers()["x-content-type-options"], "nosniff");
    w.call(Method::GET, "/api/assets/skill/demo/file?path=../../x", None, 422).await;
    w.call(Method::PUT, "/api/assets/secret/TOKEN", Some(json!({"value": "s3cret", "description": "测试"})), 204).await;
    let secret = w.get("/api/assets/secret/TOKEN").await;
    assert_eq!((secret["allow_shell"].as_bool(), secret.get("value")), (Some(true), None));
    let mcp = w
        .call(
            Method::PUT,
            "/api/assets/mcp/docs",
            Some(json!({"owner": null, "transport": "http", "url": "https://example.invalid/mcp", "probe": false})),
            201,
        )
        .await;
    assert_eq!((mcp["enabled"].as_bool(), mcp["tools"].clone()), (Some(true), json!([])));
    w.call(Method::PUT, "/api/assets/mcp/bad", Some(json!({"owner": null, "transport": "stdio", "probe": false})), 422)
        .await;
    assert_eq!(
        w.call(Method::PUT, "/api/assets/mcp/docs/enabled", Some(json!({"enabled": false})), 200).await["enabled"],
        false
    );
    w.call(Method::PUT, "/api/assets/skill/demo/owner", Some(json!({"owner": null})), 204).await;
    w.call(Method::DELETE, "/api/assets/memory/global", None, 422).await;
    w.call(Method::DELETE, "/api/assets/secret/TOKEN", None, 204).await;
    w.call(Method::DELETE, "/api/assets/mcp/docs", None, 204).await;
    w.call(Method::DELETE, "/api/assets/skill/demo", None, 204).await;

    assert_eq!(w.get("/api/providers").await, json!(["claude"]));
    assert_eq!(w.get("/api/features").await, json!({"transcription": false}));
    w.call(Method::DELETE, &format!("/api/agents/{id}"), None, 204).await;
    w.call(Method::GET, &format!("/api/agents/{id}"), None, 404).await;
}

#[tokio::test]
async fn files_workspace_pages_and_the_web_ui() {
    let w = world().await;
    let id = w.agent("pages").await;

    // 上传与下载：内容按不可信处理
    let up = w
        .http
        .post(format!("{}/api/files?name=报告.html", w.base))
        .header(header::COOKIE, &w.cookie)
        .header(header::CONTENT_TYPE, "text/html")
        .body("<script>alert(1)</script>")
        .send()
        .await
        .unwrap();
    assert_eq!(up.status(), StatusCode::CREATED);
    let file: Value = up.json().await.unwrap();
    assert_eq!(file["inline"], false);
    let down = w.send(Method::GET, file["url"].as_str().unwrap(), None).await;
    assert_eq!(down.status(), StatusCode::OK);
    let h = down.headers();
    assert_eq!(h[header::CONTENT_DISPOSITION], "attachment; filename*=utf-8''%E6%8A%A5%E5%91%8A.html");
    assert!(h["content-security-policy"].to_str().unwrap().contains("sandbox"));
    assert!(h[header::CACHE_CONTROL].to_str().unwrap().contains("immutable"));
    assert_eq!(down.text().await.unwrap(), "<script>alert(1)</script>");
    w.call(Method::GET, &format!("/api/files/{}/other.html", file["id"].as_str().unwrap()), None, 404).await;
    let no_type = w
        .http
        .post(format!("{}/api/files?name=x", w.base))
        .header(header::COOKIE, &w.cookie)
        .body("x")
        .send()
        .await
        .unwrap();
    assert_eq!(no_type.status(), StatusCode::UNPROCESSABLE_ENTITY);

    // 工作目录浏览与 agent 页面
    let site = w.dir.path().join(format!("data/workspaces/{id}/site/dash"));
    std::fs::create_dir_all(&site).unwrap();
    std::fs::write(site.join("index.html"), "<title>看板</title><p>hi</p>").unwrap();
    std::fs::write(site.join("data.json"), "{}").unwrap();
    let listing = w.get(&format!("/api/agents/{id}/workspace")).await;
    assert!(listing.as_array().unwrap().iter().any(|e| e["name"] == "site" && e["kind"] == "dir"));
    let read = w.send(Method::GET, &format!("/api/agents/{id}/workspace/file?path=site/dash/data.json"), None).await;
    assert_eq!(read.headers()[header::CACHE_CONTROL], "no-cache");
    let pages = w.get("/api/pages").await;
    assert_eq!(pages[0]["title"], "看板");
    let url = pages[0]["url"].as_str().unwrap().to_owned();
    assert!(url.ends_with('/'));
    let anonymous = reqwest::Client::builder().no_proxy().redirect(reqwest::redirect::Policy::none()).build().unwrap();
    let page = anonymous.get(format!("{}{url}", w.base)).send().await.unwrap();
    assert_eq!(page.status(), StatusCode::OK);
    assert!(page.headers()["content-security-policy"].to_str().unwrap().starts_with("sandbox allow-scripts"));
    assert_eq!(page.headers()["access-control-allow-origin"], "*");
    let data = anonymous.get(format!("{}{url}data.json", w.base)).send().await.unwrap();
    assert_eq!(data.text().await.unwrap(), "{}");
    let root = anonymous.get(format!("{}{}", w.base, url.trim_end_matches('/'))).send().await.unwrap();
    assert_eq!(root.status(), StatusCode::TEMPORARY_REDIRECT);
    let forged = url.replace(&url[url.len() - 9..url.len() - 1], "00000000");
    assert_eq!(anonymous.get(format!("{}{forged}", w.base)).send().await.unwrap().status(), StatusCode::NOT_FOUND);

    // 始终允许、结束后台任务
    assert_eq!(w.get("/api/script-grants").await, json!([]));
    w.call(Method::POST, &format!("/api/agents/{id}/jobs/999/kill"), None, 404).await;

    // 前端：有的文件原样返回，其余路径给 index.html；/api 下没有的接口 404
    let index = anonymous.get(format!("{}/sessions/abc", w.base)).send().await.unwrap();
    assert_eq!(index.headers()[header::CACHE_CONTROL], "no-cache");
    assert!(index.text().await.unwrap().contains("<title>muliao</title>"));
    let js = anonymous.get(format!("{}/assets/app.js", w.base)).send().await.unwrap();
    assert_eq!(js.text().await.unwrap(), "console.log(1)");
    let missing = anonymous.get(format!("{}/api/nope", w.base)).send().await.unwrap();
    assert_eq!(missing.status(), StatusCode::NOT_FOUND);
    let escape = anonymous.get(format!("{}/..%2F..%2Fconfig.toml", w.base)).send().await.unwrap();
    assert!(escape.text().await.unwrap().contains("<title>muliao</title>"));
}

#[tokio::test]
async fn issues_triggers_notifications_usage_and_history() {
    let w = world().await;
    let id = w.agent("worker").await;

    let issue = w.call(Method::POST, "/api/issues", Some(json!({"title": "整理", "owner": id})), 201).await;
    let user = json!({"kind": "user"});
    assert_eq!((issue["status"].as_str(), &issue["creator"]), (Some("backlog"), &user));
    assert_eq!(issue["startable"], true);
    let n = issue["id"].as_i64().unwrap();
    let patched = w
        .call(Method::PATCH, &format!("/api/issues/{n}"), Some(json!({"assignee": user, "priority": "high"})), 200)
        .await;
    assert_eq!((&patched["assignee"], patched["reviewable"].as_bool()), (&user, Some(true)));
    let cleared = w.call(Method::PATCH, &format!("/api/issues/{n}"), Some(json!({"assignee": null})), 200).await;
    assert_eq!(cleared["assignee"], Value::Null);
    let unchanged = w.call(Method::PATCH, &format!("/api/issues/{n}"), Some(json!({"title": "整理一下"})), 200).await;
    assert_eq!((unchanged["priority"].as_str(), unchanged["title"].as_str()), (Some("high"), Some("整理一下")));
    let c = w.call(Method::POST, &format!("/api/issues/{n}/comments"), Some(json!({"body": "看一下"})), 201).await;
    assert_eq!((c["kind"].as_str(), c["body"].as_str(), &c["actor"]), (Some("comment"), Some("看一下"), &user));
    let detail = w.get(&format!("/api/issues/{n}")).await;
    let kinds: Vec<&str> =
        detail["activities"].as_array().unwrap().iter().map(|a| a["kind"].as_str().unwrap()).collect();
    assert_eq!(kinds, ["created", "priority", "assignee", "assignee", "title", "comment"]);
    assert_eq!(detail["activities"][2]["change"], json!({"from": null, "to": user}));
    w.call(Method::POST, &format!("/api/issues/{n}/approve"), None, 409).await;
    let listed = w.get("/api/issues?status=backlog&status=in_progress").await;
    assert_eq!(listed.as_array().unwrap().len(), 1);
    // 列表不带描述；浏览器说能解 gzip 就压缩（#843）
    assert_eq!(listed[0].get("description"), None);
    let zipped = w
        .http
        .get(format!("{}/api/issues", w.base))
        .header(header::COOKIE, &w.cookie)
        .header(header::ACCEPT_ENCODING, "gzip")
        .send()
        .await
        .unwrap();
    assert_eq!(zipped.headers()[header::CONTENT_ENCODING], "gzip");
    w.call(Method::GET, "/api/issues?status=nope", None, 422).await;
    // 指派人的种类写在值里：写错的（如大写的 User、裸的 agent id）不当成 agent id
    assert!(w.get("/api/issues?assignee=user").await.as_array().unwrap().is_empty(), "the assignee was cleared above");
    assert!(w.get(&format!("/api/issues?assignee=agent:{id}")).await.as_array().unwrap().is_empty());
    w.call(Method::GET, "/api/issues?assignee=User", None, 422).await;
    w.call(Method::GET, &format!("/api/issues?assignee={id}"), None, 422).await;
    w.call(Method::DELETE, &format!("/api/issues/{n}"), None, 204).await;

    // 触发器：按方式校验配置
    let kinds = w.get("/api/trigger-kinds").await;
    let names: Vec<&str> = kinds.as_array().unwrap().iter().map(|k| k["name"].as_str().unwrap()).collect();
    assert_eq!(names, ["schedule", "once", "stream"]);
    let t = w
        .call(Method::POST, "/api/triggers", Some(json!({"name": "daily", "agent_id": id, "kind": "schedule", "prompt": "日报", "config": {"cron": "0 9 * * *"}})), 201)
        .await;
    assert!(t["status"]["next_run_at"].is_string());
    assert_eq!(t["precheck"], Value::Null);
    let tid = t["id"].as_i64().unwrap();
    for bad in [
        json!({"name": "bad", "agent_id": id, "kind": "schedule", "prompt": "x", "config": {"cron": "nope"}}),
        json!({"name": "bad", "agent_id": id, "kind": "schedule", "prompt": " ", "config": {"cron": "0 9 * * *"}}),
        json!({"name": "bad", "agent_id": id, "kind": "nope", "prompt": "x", "config": {}}),
    ] {
        w.call(Method::POST, "/api/triggers", Some(bad), 422).await;
    }
    let with_check = w
        .call(Method::PATCH, &format!("/api/triggers/{tid}"), Some(json!({"precheck": {"command": "echo hi"}})), 200)
        .await;
    assert_eq!(with_check["precheck"]["fire_when"], "output");
    let kept = w.call(Method::PATCH, &format!("/api/triggers/{tid}"), Some(json!({"enabled": false})), 200).await;
    assert_eq!((kept["enabled"].as_bool(), kept["precheck"]["command"].as_str()), (Some(false), Some("echo hi")));
    let removed = w.call(Method::PATCH, &format!("/api/triggers/{tid}"), Some(json!({"precheck": null})), 200).await;
    assert_eq!(removed["precheck"], Value::Null);
    assert_eq!(w.get("/api/triggers").await.as_array().unwrap().len(), 1);
    assert_eq!(w.get("/api/triggers?finished=true").await, json!([]));
    w.call(Method::POST, &format!("/api/triggers/{tid}/run"), None, 409).await;
    w.call(Method::DELETE, &format!("/api/triggers/{tid}"), None, 204).await;

    // 通知与推送
    assert_eq!(w.get("/api/notifications/unread").await, json!({"count": 0}));
    w.call(Method::POST, "/api/push/test", None, 202).await;
    let list = w.get("/api/notifications").await;
    assert_eq!(list[0]["url"], Value::Null);
    w.call(Method::POST, "/api/notifications/read", Some(json!({"through_id": list[0]["id"]})), 204).await;
    assert_eq!(w.get("/api/notifications/unread").await, json!({"count": 0}));
    w.call(Method::POST, "/api/notifications/read-link", Some(json!({"link": "/issues/1"})), 204).await;
    assert!(w.get("/api/push/vapid").await["public_key"].as_str().unwrap().len() > 40);
    w.call(
        Method::POST,
        "/api/push/subscriptions",
        Some(json!({"endpoint": "http://x", "keys": {"p256dh": "a", "auth": "b"}})),
        422,
    )
    .await;

    // 用量（带时区的时刻）、工作历史、语音（没配置）
    w.get("/api/usage?since=2026-01-01T00:00:00Z&until=2026-12-31T00:00:00%2B08:00&group_by=agent&group_by=day").await;
    w.call(Method::GET, "/api/usage?since=2026-01-01T00:00:00&until=2026-12-31T00:00:00Z", None, 422).await;
    assert_eq!(w.get(&format!("/api/agents/{id}/history?min_importance=2")).await, json!([]));
    w.call(Method::POST, "/api/transcriptions", None, 404).await;
    w.call(Method::GET, "/api/providers/nope/models", None, 404).await;
}

#[test]
fn every_route_is_described() {
    let doc = muliao_http::openapi();
    let json = serde_json::to_value(&doc).unwrap();
    let ops: usize = json["paths"].as_object().unwrap().values().map(|p| p.as_object().unwrap().len()).sum();
    // Python 版 84 个，删了任务页 2 个、加了会话重试 1 个；资源的删除从一个通用路径拆到四种资源各自的路径（加 3 个）；
    // 规则补全候选（#847）
    assert_eq!(ops, 87);
    // 引用都能解析：参数里用到的类型 utoipa 不自动登记进 components（要 `#[param(inline)]`）
    fn refs<'a>(v: &'a Value, out: &mut Vec<&'a str>) {
        match v {
            Value::Object(m) => m.iter().for_each(|(k, v)| match (k.as_str(), v) {
                ("$ref", Value::String(r)) => out.push(r),
                _ => refs(v, out),
            }),
            Value::Array(a) => a.iter().for_each(|v| refs(v, out)),
            _ => {}
        }
    }
    let mut all = Vec::new();
    refs(&json, &mut all);
    for r in all {
        let name = r.strip_prefix("#/components/schemas/").unwrap_or(r);
        assert!(json["components"]["schemas"].get(name).is_some(), "dangling reference {r}");
    }
    // operationId 取自处理函数名，前端生成类型要求它全局唯一（不同模块里的同名函数会撞）
    let mut ids = std::collections::BTreeSet::new();
    for (path, item) in json["paths"].as_object().unwrap() {
        for (method, op) in item.as_object().unwrap() {
            let id = op["operationId"].as_str().unwrap();
            assert!(ids.insert(id), "{method} {path}: operationId {id} is used twice");
            let declared: Vec<&str> = op["parameters"]
                .as_array()
                .map(|ps| ps.iter().filter(|p| p["in"] == "path").map(|p| p["name"].as_str().unwrap()).collect())
                .unwrap_or_default();
            for segment in path.split('/').filter(|s| s.starts_with('{')) {
                let name = segment.trim_matches(|c| c == '{' || c == '}');
                assert!(declared.contains(&name), "{method} {path}: path parameter {name} is not described");
            }
        }
    }
    let schemas = json["components"]["schemas"].as_object().unwrap();
    // 响应里的字段都是必有的（可为 null）
    // 请求与响应共用的类型不改（新建触发器时有默认值的可以不填）
    let required: Vec<&str> =
        schemas["Precheck"]["required"].as_array().unwrap().iter().map(|v| v.as_str().unwrap()).collect();
    assert_eq!(required, ["command"]);
    let required: Vec<&str> =
        schemas["SessionOut"]["required"].as_array().unwrap().iter().map(|v| v.as_str().unwrap()).collect();
    assert!(required.contains(&"unread"));
    // 请求里可以不给的照样可以不给
    let required: Vec<&str> =
        schemas["AgentCreate"]["required"].as_array().unwrap().iter().map(|v| v.as_str().unwrap()).collect();
    assert_eq!(required, ["name", "description"]);
    assert!(schemas.contains_key("ServerEvent"));
}
