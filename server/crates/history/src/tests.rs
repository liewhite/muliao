//! 历史领域的测试：真库（agents、sessions、issues、history 的表）；会话、信件、运行、消息经会话领域的入口写，工具经
//! agenter 的工具接口真调。

use muliao_platform::ids::{AgentId, Importance, SessionId};
use std::sync::Arc;

use agenter::{
    Content, Message, Request, RequestPart, Response, ResponsePart, ToolCall, ToolCtx, ToolError, ToolResult,
};
use chrono::{Duration, NaiveDate, Utc};
use muliao_platform::db::{Database, migrate};
use muliao_platform::errors::{Error, ErrorKind};
use muliao_platform::tooling::{Audience, RunScope};
use muliao_sessions::execution::actor::Waker;
use muliao_sessions::execution::runs::Outcome;
use muliao_sessions::intake::origins::Origin;
use muliao_sessions::testing::{record_run_in, user_request};
use serde_json::{Value, json};

use crate::*;

const A: &str = "aaaaaaaaaaaa";
const B: &str = "bbbbbbbbbbbb";
const TZ: chrono_tz::Tz = chrono_tz::Asia::Shanghai;

struct World {
    _dir: tempfile::TempDir,
    db: Arc<Database>,
    history: History,
    waker: Waker,
    _wakes: tokio::sync::mpsc::UnboundedReceiver<SessionId>,
}

async fn world() -> World {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("t.db");
    migrate(
        &path,
        &[
            muliao_assets::agents::MIGRATION,
            muliao_files::MIGRATION,
            muliao_sessions::MIGRATION,
            muliao_issues::MIGRATION,
            MIGRATION,
        ],
    )
    .unwrap();
    let db = Arc::new(Database::open(&path, Arc::new(|| {})).unwrap());
    db.write(|tx| {
        for (id, name) in [(A, "alpha"), (B, "beta")] {
            tx.execute(
                "INSERT INTO agents (id, name, name_key, is_default, created_at, updated_at) VALUES (?1, ?2, ?2, 0, ?3, ?3)",
                rusqlite::params![id, name, Utc::now()],
            )?;
        }
        Ok::<_, rusqlite::Error>(())
    })
    .await
    .unwrap();
    let (waker, wakes) = Waker::channel();
    World { _dir: dir, history: History::new(db.clone(), TZ), db, waker, _wakes: wakes }
}

fn entry(day: NaiveDate, title: &str, summary: &str, refs: &[&str], importance: i64) -> NewEntry {
    NewEntry {
        day,
        title: title.into(),
        summary: summary.into(),
        refs: refs.iter().map(|r| r.to_string()).collect(),
        importance: Importance::try_from(importance).unwrap(),
    }
}

fn kind_of(e: Error) -> ErrorKind {
    match e {
        Error::Domain(d) => d.kind,
        other => panic!("not a domain error: {other}"),
    }
}

fn user_text(text: &str) -> Message {
    user_texts(&[(Origin::User, text)])
}

/// 一条请求：每段文字记下它所在信件的发送方（交出信件时就这样记）。
fn user_texts(texts: &[(Origin, &str)]) -> Message {
    user_request(texts, "Current local time: now")
}

fn reply(text: &str, calls: &[(&str, &str, &str)]) -> Message {
    let mut parts = vec![ResponsePart::Text { text: text.into(), meta: None }];
    parts.extend(calls.iter().map(|(id, name, args)| {
        ResponsePart::ToolCall(ToolCall { id: (*id).into(), name: (*name).into(), args: (*args).into(), meta: None })
    }));
    Message::Assistant(Response {
        parts,
        protocol: "anthropic".into(),
        model: "m".into(),
        response_id: None,
        finish_reason: None,
        usage: Default::default(),
        timestamp: Utc::now(),
        metadata: None,
    })
}

fn result(id: &str, name: &str, text: &str) -> Message {
    Message::User(Request {
        parts: vec![RequestPart::ToolResult(ToolResult {
            call_id: id.into(),
            tool_name: name.into(),
            content: vec![Content::text(text)],
            is_error: false,
            metadata: None,
        })],
        timestamp: Utc::now(),
        metadata: None,
    })
}

impl World {
    fn today(&self) -> NaiveDate {
        self.history.today()
    }

    /// 建一个会话（origin 与 origin_ref 直接写库）。
    async fn session(&self, id: &str, agent: &str, title: &str, origin: &str, reference: Option<&str>) {
        let (id, agent, title, origin, reference) =
            (id.to_owned(), agent.to_owned(), title.to_owned(), origin.to_owned(), reference.map(str::to_owned));
        self.db
            .write(move |tx| {
                tx.execute(
                    "INSERT INTO sessions (id, agent_id, title, origin, origin_ref, created_at, updated_at) \
                     VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?6)",
                    rusqlite::params![id, agent, title, origin, reference, Utc::now()],
                )
            })
            .await
            .unwrap();
    }

    /// 一次运行：投 mail（发送方 from 的消息），交给运行并保存 messages，按 outcome 结束（None 为还在跑）。
    async fn run(&self, session: &str, from: Option<(Origin, &str)>, messages: Vec<Message>, outcome: Option<Outcome>) {
        let (session, waker) = (SessionId::new(session), self.waker.clone());
        let live = muliao_sessions::execution::live::Live::new(std::sync::Arc::new(muliao_platform::bus::Bus::new(16)));
        let from = from.map(|(o, t)| (o, t.to_owned()));
        self.db.write(move |tx| record_run_in(tx, &waker, &live, &session, from, &messages, outcome)).await.unwrap();
    }

    async fn call(&self, me: &str, name: &str, args: Value, max_chars: usize) -> Result<String, String> {
        let tool = tools(self.history.clone(), max_chars).into_iter().find(|t| t.name() == name).unwrap();
        let scope = RunScope::new(AgentId::new(me), "/tmp".into(), SessionId::new("s"), Audience::None);
        let args = args.to_string();
        let call = ToolCall { id: "c".into(), name: name.into(), args: args.clone(), meta: None };
        match tool.tool.call(ToolCtx { deps: &scope, call: &call, retries: 0, approval: None }, &args).await {
            Ok(output) => Ok(output
                .content
                .iter()
                .filter_map(|c| if let Content::Text { text } = c { Some(text.clone()) } else { None })
                .collect()),
            Err(ToolError::Retry(m)) => Err(m),
            Err(e) => panic!("{e:?}"),
        }
    }
}

// ---- 冷历史 ----

#[tokio::test]
async fn append_checks_every_entry_and_writes_all_or_nothing() {
    let w = world().await;
    let today = w.today();
    let ok = entry(today, " 修好了触发器 ", " 摘要 \n", &["#1"], 2);
    let written =
        w.history.append(&AgentId::new(A), vec![ok.clone(), entry(today, "第二件", "s", &[], 1)]).await.unwrap();
    assert_eq!(written.len(), 2);
    assert_eq!((written[0].title.as_str(), written[0].summary.as_str()), ("修好了触发器", "摘要"));
    assert_eq!(written[1].id, written[0].id + 1);

    let bad = [
        entry(today, "  ", "s", &[], 1),
        entry(today, &"长".repeat(201), "s", &[], 1),
        entry(today, "t", " ", &[], 1),
        entry(today + Duration::days(1), "t", "s", &[], 1),
    ];
    for b in bad {
        let e = w.history.append(&AgentId::new(A), vec![ok.clone(), b.clone()]).await.unwrap_err();
        assert_eq!(kind_of(e), ErrorKind::InvalidInput, "{b:?}");
    }
    assert_eq!(kind_of(w.history.append(&AgentId::new(A), vec![]).await.unwrap_err()), ErrorKind::InvalidInput);
    // 200 个字符正好可以
    w.history.append(&AgentId::new(A), vec![entry(today, &"长".repeat(200), "s", &[], 1)]).await.unwrap();
    assert_eq!(w.history.search(Search::of(None)).await.unwrap().len(), 3);
}

#[tokio::test]
async fn rows_cannot_be_changed_or_deleted() {
    let w = world().await;
    w.history.append(&AgentId::new(A), vec![entry(w.today(), "t", "s", &[], 1)]).await.unwrap();
    for sql in ["UPDATE agent_history SET title = 'x'", "DELETE FROM agent_history"] {
        let e = w.db.write(move |tx| tx.execute(sql, [])).await.unwrap_err();
        assert!(e.to_string().contains("append-only"), "{e}");
    }
}

#[tokio::test]
async fn search_lists_by_date_or_matches_every_word() {
    let w = world().await;
    let d = |n: i64| w.today() - Duration::days(n);
    w.history
        .append(
            &AgentId::new(A),
            vec![
                entry(d(3), "触发器前置检查改成脚本", "precheck 每分钟跑一次", &["#155", "工单"], 1),
                entry(d(1), "部署 muliao", "改了 SSE", &["#200"], 3),
                entry(d(1), "前置检查的超时", "precheck timeout 60s", &[], 2),
            ],
        )
        .await
        .unwrap();
    w.history.append(&AgentId::new(B), vec![entry(d(2), "beta 的前置检查", "无关", &[], 1)]).await.unwrap();
    let titles = |rs: Vec<Record>| rs.into_iter().map(|r| r.title).collect::<Vec<_>>();
    let search = |agents: Option<&[&str]>| Search::of(agents.map(|a| a.iter().copied().map(AgentId::new).collect()));

    // 没有 query：日期倒序，同一天按写入倒序
    let all = w.history.search(search(Some(&[A]))).await.unwrap();
    assert_eq!(titles(all), ["前置检查的超时", "部署 muliao", "触发器前置检查改成脚本"]);
    // FTS（每个词都不短于 3 个字符，中文可用）：每个词都要出现
    let q = |s: &str| Search { query: Some(s.into()), ..search(Some(&[A])) };
    assert_eq!(titles(w.history.search(q("前置检查 precheck")).await.unwrap()).len(), 2);
    assert_eq!(titles(w.history.search(q("前置检查 timeout")).await.unwrap()), ["前置检查的超时"]);
    // 引用也能搜到；短词走 LIKE（引用按解码后的值，中文不被转义挡住）
    assert_eq!(titles(w.history.search(q("#155")).await.unwrap()), ["触发器前置检查改成脚本"]);
    assert_eq!(titles(w.history.search(q("工单")).await.unwrap()), ["触发器前置检查改成脚本"]);
    assert_eq!(titles(w.history.search(q("SSE 部署")).await.unwrap()), ["部署 muliao"]);
    // LIKE 的 % 按字面
    assert!(w.history.search(q("%")).await.unwrap().is_empty());
    // 全部 agent、日期与重要程度
    assert_eq!(w.history.search(Search { query: Some("前置检查".into()), ..search(None) }).await.unwrap().len(), 3);
    let ranged = Search { since: Some(d(2)), until: Some(d(1)), ..search(None) };
    assert_eq!(titles(w.history.search(ranged).await.unwrap()), ["前置检查的超时", "部署 muliao", "beta 的前置检查"]);
    let important = Search { min_importance: Importance::try_from(2).unwrap(), ..search(None) };
    assert_eq!(w.history.search(important).await.unwrap().len(), 2);
    let limited = Search { limit: 1, ..search(None) };
    assert_eq!(titles(w.history.search(limited).await.unwrap()), ["前置检查的超时"]);

    for bad in [
        Search { limit: 0, ..search(None) },
        Search { limit: 201, ..search(None) },
        Search { since: Some(d(1)), until: Some(d(2)), ..search(None) },
        Search { query: Some("  ".into()), ..search(None) },
    ] {
        assert_eq!(kind_of(w.history.search(bad).await.unwrap_err()), ErrorKind::InvalidInput);
    }
}

#[tokio::test]
async fn the_tools_append_your_own_and_search_by_agent_name() {
    let w = world().await;
    let today = w.today().to_string();
    let out = w
        .call(
            A,
            "history_append",
            json!({"entries": [{"day": today, "title": "t", "summary": "s", "refs": ["#1"]}]}),
            20_000,
        )
        .await
        .unwrap();
    assert_eq!(out, format!("appended 1 entries (ids 1-1) for {today}"));
    w.history.append(&AgentId::new(B), vec![entry(w.today(), "b", "s", &[], 1)]).await.unwrap();

    let found: Value = serde_json::from_str(&w.call(A, "history_search", json!({}), 20_000).await.unwrap()).unwrap();
    assert_eq!(
        found,
        json!([{"id": 1, "day": today, "agent": "alpha", "title": "t", "summary": "s", "refs": ["#1"], "importance": 1}])
    );
    let found: Value =
        serde_json::from_str(&w.call(A, "history_search", json!({"agent": "all"}), 20_000).await.unwrap()).unwrap();
    assert_eq!(found.as_array().unwrap().len(), 2);
    let found: Value =
        serde_json::from_str(&w.call(A, "history_search", json!({"agent": "beta"}), 20_000).await.unwrap()).unwrap();
    assert_eq!(found[0]["agent"], "beta");
    // 重要程度在读入工具参数时就只能是 1-3
    let e = w.call(A, "history_search", json!({"min_importance": 4}), 20_000).await.unwrap_err();
    assert!(e.contains("`min_importance`"), "{e}");
    let e = w.call(A, "history_search", json!({"agent": "nobody"}), 20_000).await.unwrap_err();
    assert!(e.contains("alpha"), "{e}");
    let e = w.call(A, "history_search", json!({"limit": 500}), 20_000).await.unwrap_err();
    assert!(e.contains("limit"), "{e}");
}

// ---- 热历史 ----

/// alpha 今天的三个会话：用户发起的（有一次失败的运行）、issue #7 的、用户发起的 thread；beta 的一个会话。
async fn day_of_work(w: &World) {
    w.session("s-user-0001", A, "看看 SSE", "user", None).await;
    w.run(
        "s-user-0001",
        Some((Origin::User, "帮我看看 SSE 为什么断")),
        vec![
            user_text("帮我看看 SSE 为什么断"),
            reply("我去查日志", &[("c1", "run_shell", r#"{"command":"tail -n 200 ~/.muliao/muliao.log"}"#)]),
            result("c1", "run_shell", &"x".repeat(5000)),
            reply("日志里是重启导致的。", &[]),
        ],
        Some(Outcome::Done),
    )
    .await;
    w.run(
        "s-user-0001",
        Some((Origin::User, "再试一次")),
        vec![user_text("再试一次")],
        Some(Outcome::Failed("model said no\n  twice".into())),
    )
    .await;

    w.session("s-issue-001", A, "#7 修 bug", "issue", Some("7")).await;
    w.run(
        "s-issue-001",
        Some((Origin::Issue, "New activity on issue #7")),
        vec![
            user_texts(&[(Origin::Issue, "New activity on issue #7"), (Origin::User, "顺便看下 #8")]),
            reply("好的，开工", &[]),
        ],
        Some(Outcome::Done),
    )
    .await;
    w.session("s-beta-0001", B, "beta 的会话", "user", None).await;
    w.run("s-beta-0001", Some((Origin::User, "beta 你好")), vec![user_text("beta 你好")], None).await;
    w.history.append(&AgentId::new(A), vec![entry(w.today(), "今天的事", "s", &["#7"], 2)]).await.unwrap();
}

#[tokio::test]
async fn activity_overview_lists_sessions_words_failures_and_history() {
    let w = world().await;
    day_of_work(&w).await;
    // 用户对 alpha 负责的 issue #7 留的评论
    w.db.write(|tx| {
        tx.execute(
            "INSERT INTO issues (id, title, status, priority, owner_agent_id, creator, created_at, updated_at) \
             VALUES (7, '修 bug', 'in_progress', 'none', ?1, 'user', ?2, ?2)",
            rusqlite::params![A, Utc::now()],
        )?;
        tx.execute(
            "INSERT INTO issue_activities (issue_id, created_at, actor, kind, body) VALUES (7, ?1, 'user', 'comment', '先看日志\n再改')",
            [Utc::now()],
        )
    })
    .await
    .unwrap();

    let out = w.call(A, "activity_overview", json!({}), 20_000).await.unwrap();
    let today = w.today();
    assert!(out.starts_with(&format!("# activity of alpha on {today} (local day, times local)")), "{out}");
    assert!(out.contains("## sessions (2, issue 1, user 1)"), "{out}");
    // 只有一个 agent 时不写 agent 名
    assert!(out.contains("- s-user-0 [user] \"看看 SSE\": idle, 5 msgs"), "{out}");
    assert!(out.contains(", 2 runs, 1 FAILED"), "{out}");
    assert!(out.contains("- s-issue- [issue #7] \"#7 修 bug\": idle, 2 msgs"), "{out}");
    // 用户说的话只按信件的发送方认：issue 投进来的信不算
    assert!(out.contains("## what the user typed (2, full text)"), "{out}");
    assert!(out.contains("  帮我看看 SSE 为什么断") && out.contains("  再试一次"), "{out}");
    assert!(!out.contains("  New activity on issue #7"), "{out}");
    assert!(out.contains("## what the user did on issues (1 issues, comments in full)"), "{out}");
    assert!(out.contains("- #7 \"修 bug\" (now in_progress):"), "{out}");
    assert!(out.contains("先看日志") && out.contains("再改"), "{out}");
    assert!(out.contains("## failed runs (1)"), "{out}");
    assert!(out.contains("model said no twice"), "{out}");
    assert!(out.contains("## work history for"), "{out}");
    assert!(out.contains("- [2] 今天的事 [\"#7\"]"), "{out}");
    assert!(!out.contains("beta"), "{out}");

    // 全部 agent：每行带 agent 名
    let all = w.call(A, "activity_overview", json!({"agent": "all"}), 20_000).await.unwrap();
    assert!(all.starts_with("# activity of every agent"), "{all}");
    assert!(all.contains("- beta s-beta-0 [user] \"beta 的会话\": running"), "{all}");
    assert!(all.contains("- alpha [2] 今天的事"), "{all}");

    // 别的日子没有活动
    let yesterday = (today - Duration::days(1)).to_string();
    let empty = w.call(A, "activity_overview", json!({"day": yesterday}), 20_000).await.unwrap();
    assert!(empty.contains("## sessions (0)\n(none)") && empty.contains("(none yet)"), "{empty}");

    // 超出上限时在整行处停下，给出接着读的行号
    let first = w.call(A, "activity_overview", json!({}), 200).await.unwrap();
    let note = first.lines().last().unwrap();
    assert!(note.starts_with("[output limit reached at line "), "{first}");
    let next: usize = note.rsplit("offset=").next().unwrap().trim_end_matches(']').parse().unwrap();
    let rest = w.call(A, "activity_overview", json!({"offset": next}), 20_000).await.unwrap();
    assert!(out.ends_with(&rest), "{rest}");
    let e = w.call(A, "activity_overview", json!({"offset": 10_000}), 20_000).await.unwrap_err();
    assert!(e.contains("past the last line"), "{e}");
}

#[tokio::test]
async fn session_read_shows_the_timeline_with_labels_grep_clip_and_offset() {
    let w = world().await;
    day_of_work(&w).await;
    let out = w.call(A, "session_read", json!({"session": "s-user"}), 20_000).await.unwrap();
    assert!(out.starts_with("# session s-user-0001 [user] \"看看 SSE\", agent alpha: 6 entries"), "{out}");
    assert!(out.contains("] USER\n  帮我看看 SSE 为什么断"), "{out}");
    assert!(out.contains("] ASSISTANT\n  我去查日志"), "{out}");
    assert!(out.contains("] call run_shell\n  {\"command\""), "{out}");
    // 工具结果按 clip 截断，运行上下文不出现
    assert!(out.contains("] result run_shell\n  xxx") && out.contains("…[truncated: 3000 more chars]"), "{out}");
    assert!(!out.contains("Current local time"), "{out}");

    // 每段按信件记下的发送方标：issue 会话里用户插的话标 USER
    let issue = w.call(A, "session_read", json!({"session": "s-issue", "labels": ["ISSUE"]}), 20_000).await.unwrap();
    assert!(
        issue.contains("1 entries labelled ISSUE") && issue.contains("] ISSUE\n  New activity on issue #7"),
        "{issue}"
    );
    let users = w.call(A, "session_read", json!({"session": "s-issue", "labels": ["USER"]}), 20_000).await.unwrap();
    assert!(users.contains("1 entries labelled USER") && users.contains("] USER\n  顺便看下 #8"), "{users}");

    // labels 按前缀词匹配，grep 不分大小写、标签和文字都看
    let calls = w.call(A, "session_read", json!({"session": "s-user", "labels": ["call"]}), 20_000).await.unwrap();
    assert!(calls.contains("1 entries labelled call"), "{calls}");
    let grep = w.call(A, "session_read", json!({"session": "s-user", "grep": "sse"}), 20_000).await.unwrap();
    assert!(grep.contains(": 1 entries matching /sse/") && grep.contains("] USER\n"), "{grep}");
    let full = w.call(A, "session_read", json!({"session": "s-user", "clip": 0}), 20_000).await.unwrap();
    assert!(full.contains(&"x".repeat(5000)), "full");
    let since = w.call(A, "session_read", json!({"session": "s-user", "since": w.today().to_string()}), 20_000).await;
    assert!(since.unwrap().contains("6 entries since"));

    // 超出上限时在整条处停下
    let first = w.call(A, "session_read", json!({"session": "s-user"}), 300).await.unwrap();
    let note = first.lines().last().unwrap();
    assert!(note.starts_with("[output limit reached at entry "), "{first}");
    let next: usize = note.rsplit("offset=").next().unwrap().trim_end_matches(']').parse().unwrap();
    let rest = w.call(A, "session_read", json!({"session": "s-user", "offset": next}), 20_000).await.unwrap();
    assert!(rest.contains("] ASSISTANT\n  日志里是重启导致的。"), "{rest}");

    for (args, says) in [
        (json!({"session": "s-"}), "matches several sessions"),
        (json!({"session": "nope"}), "no session id starts with"),
        (json!({"session": "s-user", "labels": ["user"]}), "is not a label"),
        (json!({"session": "s-user", "labels": [" USER"]}), "is not a label"),
        (json!({"session": "s-user", "grep": "("}), "not a valid regular expression"),
        (json!({"session": "s-user", "since": "yesterday"}), "ISO 8601"),
        (json!({"session": "s-user", "offset": 99}), "past the last entry"),
        (json!({"session": "s-user", "offset": 0}), "1 or more"),
    ] {
        let e = w.call(A, "session_read", args.clone(), 20_000).await.unwrap_err();
        assert!(e.contains(says), "{args}: {e}");
    }
}
