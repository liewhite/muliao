//! 迁完之后的检查：在新库的一份副本上启动真正的新版服务（组合根装配 + 启动），经 HTTP 打开每个会话页、每个 issue、
//! 各 agent 的工作历史，以及 agent、触发器、通知、用量、始终允许等列表页，任何一个不是 200 都记成问题。
//!
//! 副本与真实环境隔开，启动不产生副作用：
//! - 数据目录是临时目录（工作目录为空、新的推送密钥），附件目录链接到真的（只读用）；
//! - 副本里停用全部触发器、删掉推送订阅；没结束的运行记为失败（不续跑：续跑会调模型、起 MCP 进程）；正在删除的 agent
//!   从副本里去掉（否则启动时会接着删它的资源文件，资源目录是真的）；
//! - 模型地址换成本机一个不通的端口，不配网页搜索和语音。
//!
//! 续跑本身（迁成「没结束」的运行在新版里接着跑）不在这里验，留给并行试跑（#689）。

use std::net::SocketAddr;
use std::path::Path;

use anyhow::{Context, bail};
use chrono::Utc;
use muliao_platform::config::Settings;
use muliao_workspace::grants::ScriptCall;
use reqwest::{StatusCode, header};
use rusqlite::Connection;
use serde_json::{Value, json};

use crate::report::Report;

/// 不通的模型地址（discard 端口，本机没有服务）
const DEAD_MODEL_URL: &str = "http://127.0.0.1:9";

/// 准备副本，返回副本里为了隔离改掉的东西的说明。
fn isolate(db: &Path) -> anyhow::Result<Vec<String>> {
    let conn = Connection::open(db)?;
    let now = Utc::now();
    let mut done = Vec::new();
    let n = conn.execute("UPDATE triggers SET enabled = 0 WHERE enabled", [])?;
    done.push(format!("停用触发器 {n} 个"));
    let n = conn.execute("DELETE FROM push_subscriptions", [])?;
    done.push(format!("删推送订阅 {n} 个"));
    let n = conn.execute(
        "UPDATE runs SET ended_at = ?1, outcome = 'failed', error = 'closed by the migration check' WHERE ended_at IS NULL",
        [now],
    )?;
    done.push(format!("没结束的运行 {n} 个记为失败（不续跑）"));
    let n = conn.execute("DELETE FROM agents WHERE deleting_by IS NOT NULL", [])?;
    done.push(format!("去掉正在删除的 agent {n} 个"));
    Ok(done)
}

/// 「始终允许」的查找键是按 Python 版同样的算法算的：迁过来的每一条按 Rust 版重算一遍，对不上就不会再被认出来。
fn check_grant_keys(db: &Path, report: &mut Report) -> anyhow::Result<()> {
    let conn = Connection::open(db)?;
    let mut stmt =
        conn.prepare("SELECT id, agent_id, skill, script, args, secrets, fingerprint, key FROM script_grants")?;
    let rows = stmt.query_map([], |r| {
        Ok((
            r.get::<_, i64>(0)?,
            r.get::<_, String>(1)?,
            r.get::<_, String>(2)?,
            r.get::<_, String>(3)?,
            r.get::<_, String>(4)?,
            r.get::<_, String>(5)?,
            r.get::<_, String>(6)?,
            r.get::<_, String>(7)?,
        ))
    })?;
    let mut n = 0;
    for row in rows {
        let (id, agent, skill, script, args, secrets, fingerprint, key) = row?;
        let args: Vec<String> = serde_json::from_str(&args).with_context(|| format!("script grant {id} args"))?;
        let secrets: Vec<String> =
            serde_json::from_str(&secrets).with_context(|| format!("script grant {id} secrets"))?;
        if ScriptCall::new(&agent, &skill, &script, &args, &secrets, &fingerprint).key() != key {
            report.problems.push(format!("始终允许 #{id}：按新版算出的查找键与旧的不同，迁过来也不会被认出"));
        }
        n += 1;
    }
    report.checks.push(format!("始终允许：{n} 条的查找键按新版算法重算"));
    Ok(())
}

fn ids(db: &Path, sql: &str) -> anyhow::Result<Vec<String>> {
    let conn = Connection::open(db)?;
    let mut stmt = conn.prepare(sql)?;
    let out = stmt.query_map([], |r| r.get::<_, rusqlite::types::Value>(0))?.collect::<Result<Vec<_>, _>>()?;
    Ok(out
        .into_iter()
        .map(|v| match v {
            rusqlite::types::Value::Integer(i) => i.to_string(),
            rusqlite::types::Value::Text(t) => t,
            other => format!("{other:?}"),
        })
        .collect())
}

struct Client {
    base: String,
    http: reqwest::Client,
    cookie: String,
}

impl Client {
    /// GET，不是 200 时返回说明。
    async fn get(&self, path: &str) -> Result<Value, String> {
        let r = self
            .http
            .get(format!("{}{path}", self.base))
            .header(header::COOKIE, &self.cookie)
            .send()
            .await
            .map_err(|e| format!("GET {path}: {e}"))?;
        let status = r.status();
        let text = r.text().await.map_err(|e| format!("GET {path}: reading the body: {e}"))?;
        if status != StatusCode::OK {
            let head: String = text.chars().take(300).collect();
            return Err(format!("GET {path} → {status}: {head}"));
        }
        serde_json::from_str(&text).map_err(|e| format!("GET {path}: not JSON: {e}"))
    }
}

/// 逐个打开，记下打开了多少、哪些失败。
async fn open_each(client: &Client, what: &str, paths: Vec<String>, report: &mut Report) {
    let mut failed = 0;
    for path in &paths {
        if let Err(e) = client.get(path).await {
            failed += 1;
            report.problems.push(e);
        }
    }
    report.checks.push(format!("{what}：打开 {} 个，失败 {failed} 个", paths.len()));
}

pub async fn verify(new_db: &Path, config: &Path, report: &mut Report) -> anyhow::Result<()> {
    let tmp = tempfile::tempdir()?;
    let data = tmp.path().join("data");
    std::fs::create_dir_all(&data)?;
    let db = data.join("muliao.db");
    std::fs::copy(new_db, &db).with_context(|| format!("copying {}", new_db.display()))?;

    let mut settings = Settings::load(config).with_context(|| format!("loading {}", config.display()))?;
    let real_files = settings.files_dir();
    if real_files.is_dir() {
        std::os::unix::fs::symlink(&real_files, data.join("files"))?;
    }
    for done in isolate(&db)? {
        report.checks.push(format!("检查用的副本：{done}"));
    }
    check_grant_keys(&db, report)?;
    settings.data_dir = data;
    settings.log_file = None;
    settings.web = None;
    settings.transcription = None;
    for provider in settings.llm.providers.values_mut() {
        provider.base_url = Some(DEAD_MODEL_URL.to_owned());
    }
    let password = settings.auth.password.expose().to_owned();

    let app = muliao_server::assemble(settings, tmp.path().join("web")).await.context("assembling the new version")?;
    app.start().await.context("starting the new version on the migrated database")?;
    report.checks.push("新版在迁移后的库上装配并启动成功".into());
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await?;
    let base = format!("http://{}", listener.local_addr()?);
    let router = app.router();
    let server =
        tokio::spawn(
            async move { axum::serve(listener, router.into_make_service_with_connect_info::<SocketAddr>()).await },
        );
    let http = reqwest::Client::builder().no_proxy().build()?;
    let login = http.post(format!("{base}/api/auth/login")).json(&json!({"password": password})).send().await?;
    if login.status() != StatusCode::NO_CONTENT {
        bail!("logging in to the new version failed: {}", login.status());
    }
    let cookie = login.headers()[header::SET_COOKIE].to_str()?.split(';').next().unwrap_or_default().to_owned();
    let client = Client { base, http, cookie };

    let lists = [
        "/api/agents",
        "/api/sessions?limit=200",
        "/api/sessions?q=muliao&limit=200",
        "/api/issues",
        "/api/triggers",
        "/api/triggers?finished=true",
        "/api/notifications?limit=500",
        "/api/notifications/unread",
        "/api/usage?since=2020-01-01T00:00:00Z&until=2100-01-01T00:00:00Z&group_by=agent&group_by=model&group_by=day",
        "/api/script-grants",
    ];
    open_each(
        &client,
        "列表页（agent、会话、搜索、issue、触发器、通知、用量、始终允许）",
        lists.map(String::from).to_vec(),
        report,
    )
    .await;
    let sessions = ids(&db, "SELECT id FROM sessions ORDER BY updated_at DESC")?;
    open_each(&client, "会话页", sessions.iter().map(|id| format!("/api/sessions/{id}")).collect(), report).await;
    let issues = ids(&db, "SELECT id FROM issues ORDER BY id")?;
    open_each(&client, "issue 页", issues.iter().map(|id| format!("/api/issues/{id}")).collect(), report).await;
    let agents = ids(&db, "SELECT id FROM agents ORDER BY name")?;
    open_each(
        &client,
        "agent 详情与工作历史",
        agents
            .iter()
            .flat_map(|id| [format!("/api/agents/{id}"), format!("/api/agents/{id}/history?limit=200")])
            .collect(),
        report,
    )
    .await;

    server.abort();
    app.bus.close();
    app.shutdown().await;
    Ok(())
}
