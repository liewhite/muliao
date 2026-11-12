//! 把旧库搬进新库：新库先按 Rust 版的迁移建好全部表，再把旧库只读挂上（`ATTACH ... mode=ro`），除消息以外的表都是一条
//! `INSERT ... SELECT`；消息要逐条转换（[`crate::messages`]）。全部在一个事务里，写到 `<新库>.partial`，成功才改名成新库，
//! 失败不留半个库。
//!
//! 取舍（#675 设计第六节）：
//! - 旧库的表结构 Rust 版大多照搬，行原样迁，时间统一改写成 Rust 版写库的格式（[`utc`]）。
//! - 任务队列没有了：旧的运行、压缩任务成为运行记录（`runs`），id 不变（消息、信件按它引用）；推送任务不迁。旧库清理过的
//!   任务，它保存的消息还在，按消息补一条运行记录。
//! - 切换那一刻还在进行中的东西也照迁（两版的表与状态含义相同）：在跑的运行迁成「没结束」、排着的信、还占着的占用（在跑的
//!   后台任务、等着的委派、到点唤醒）、还在等批准的调用。新版启动时照服务重启的规则接手：续跑停在半路的会话、叫醒有信的
//!   会话、把上次没收尾的后台任务记为被中断并告诉 agent、到点照常唤醒。只有排着还没开始的运行任务不迁：新版凭排着的信叫醒
//!   会话，用不到它。

use std::path::Path;

use anyhow::{Context, bail};
use chrono::{DateTime, NaiveDateTime, Utc};
use muliao_sessions::conventions::{is_stopped, searchable_text};
use rusqlite::functions::FunctionFlags;
use rusqlite::{Connection, OpenFlags, Transaction, params};

use crate::messages::{self, Notes};
use crate::report::{Report, TableReport};

/// 一张表（或一张表的一部分）怎么搬。SQL 里旧库是 `old`、新库是 `main`。
struct Step {
    table: &'static str,
    source: &'static str,
    /// 旧库里与这一步对应的行数
    old_count: &'static str,
    insert: String,
    /// 没迁的行：原因与计数 SQL（各原因互斥，加起来等于旧行数减迁入数）
    skipped: Vec<(&'static str, &'static str)>,
    /// 迁了但改了内容的：说明与计数 SQL（在插入之后执行，可以查新库）
    changed: Vec<(&'static str, &'static str)>,
}

const LIVE_SESSIONS: &str = "(SELECT id FROM main.sessions)";

/// 旧库的时间文本 → Rust 版写库的格式（rusqlite 写 `DateTime<Utc>` 的样子：`2026-11-12 10:00:00.123456+00:00`）。
/// 旧库存的是不带时区的 UTC（`2026-11-12 10:00:00.123456`）；带时区的也认。读不懂就报错，整个迁移回滚。
pub fn utc(text: &str) -> Result<String, String> {
    let fmt = if text.as_bytes().get(10) == Some(&b'T') { "%FT%T%.f" } else { "%F %T%.f" };
    let parsed = DateTime::parse_from_str(text, &format!("{fmt}%#z"))
        .map(|d| d.with_timezone(&Utc))
        .or_else(|_| NaiveDateTime::parse_from_str(text, fmt).map(|n| n.and_utc()))
        .map_err(|e| format!("bad time {text:?}: {e}"))?;
    Ok(format_utc(parsed))
}

fn format_utc(at: DateTime<Utc>) -> String {
    at.format("%F %T%.f%:z").to_string()
}

fn register_utc(conn: &Connection) -> rusqlite::Result<()> {
    conn.create_scalar_function("utc", 1, FunctionFlags::SQLITE_UTF8 | FunctionFlags::SQLITE_DETERMINISTIC, |ctx| {
        let v: Option<String> = ctx.get(0)?;
        v.map(|t| utc(&t).map_err(|e| rusqlite::Error::UserFunctionError(e.into()))).transpose()
    })
}

/// 消息之前的各步（消息引用会话与运行记录）。
fn before_messages() -> Vec<Step> {
    let s = LIVE_SESSIONS;
    vec![
        Step {
            table: "agents",
            source: "agents",
            old_count: "SELECT count(*) FROM old.agents",
            insert: "INSERT INTO main.agents (id, name, deleting_by, is_default, archived_at, created_at, updated_at)
                SELECT id, name, deleting_by, is_default, utc(archived_at), utc(created_at), utc(updated_at) FROM old.agents"
                .into(),
            skipped: vec![],
            changed: vec![],
        },
        Step {
            table: "files",
            source: "files",
            old_count: "SELECT count(*) FROM old.files",
            insert: "INSERT INTO main.files (id, sha256, name, media_type, size, created_at)
                SELECT id, sha256, name, media_type, size, utc(created_at) FROM old.files"
                .into(),
            skipped: vec![],
            changed: vec![],
        },
        Step {
            table: "model_usage",
            source: "model_usage",
            old_count: "SELECT count(*) FROM old.model_usage",
            insert: "INSERT INTO main.model_usage (id, created_at, agent_id, session_id, kind, model, requests,
                    input_tokens, output_tokens, cache_read_tokens, cache_write_tokens)
                SELECT id, utc(created_at), agent_id, session_id, kind, model, requests,
                    input_tokens, output_tokens, cache_read_tokens, cache_write_tokens FROM old.model_usage"
                .into(),
            skipped: vec![],
            changed: vec![],
        },
        Step {
            table: "sessions",
            source: "sessions",
            old_count: "SELECT count(*) FROM old.sessions",
            insert: "INSERT INTO main.sessions (id, agent_id, title, origin, origin_ref, parent_session_id, thread,
                    created_at, updated_at)
                SELECT id, agent_id, title, origin, origin_ref,
                    CASE WHEN parent_session_id IN (SELECT id FROM old.sessions WHERE agent_id IN (SELECT id FROM old.agents))
                        THEN parent_session_id END,
                    thread, utc(created_at), utc(updated_at)
                FROM old.sessions WHERE agent_id IN (SELECT id FROM old.agents)"
                .into(),
            skipped: vec![(
                "agent 已不存在",
                "SELECT count(*) FROM old.sessions WHERE agent_id NOT IN (SELECT id FROM old.agents)",
            )],
            changed: vec![],
        },
        Step {
            table: "runs",
            source: "tasks（运行与压缩）",
            old_count: "SELECT count(*) FROM old.tasks",
            insert: format!(
                "INSERT INTO main.runs (id, session_id, kind, started_at, ended_at, outcome, error)
                SELECT id, session_id, kind, utc(coalesce(started_at, created_at)),
                    CASE WHEN status IN ('done', 'failed') THEN utc(coalesce(finished_at, started_at, created_at)) END,
                    CASE status WHEN 'done' THEN 'done' WHEN 'failed' THEN 'failed' END,
                    CASE WHEN status = 'failed' THEN coalesce(error, 'failed') END
                FROM old.tasks
                WHERE kind IN ('run', 'compaction') AND status IN ('done', 'failed', 'running') AND session_id IN {s}"
            ),
            skipped: vec![
                (
                    "推送任务（新版推送不经任务队列）",
                    "SELECT count(*) FROM old.tasks WHERE kind NOT IN ('run', 'compaction')",
                ),
                (
                    "会话已不存在",
                    "SELECT count(*) FROM old.tasks WHERE kind IN ('run', 'compaction')
                        AND session_id NOT IN (SELECT id FROM main.sessions)",
                ),
                (
                    "排着还没开始的（新版凭排着的信叫醒会话，用不到它）",
                    "SELECT count(*) FROM old.tasks WHERE kind IN ('run', 'compaction')
                        AND session_id IN (SELECT id FROM main.sessions) AND status NOT IN ('done', 'failed', 'running')",
                ),
            ],
            changed: vec![(
                "正在跑的：迁成没结束的运行，新版启动后从上一步续跑",
                "SELECT count(*) FROM main.runs WHERE ended_at IS NULL",
            )],
        },
        Step {
            table: "runs",
            source: "按消息补的运行记录（旧库已清理掉的任务）",
            old_count: "SELECT count(DISTINCT task_id) FROM old.messages
                WHERE session_id IN (SELECT id FROM main.sessions) AND task_id NOT IN (SELECT id FROM main.runs)",
            insert: format!(
                "INSERT INTO main.runs (id, session_id, kind, started_at, ended_at, outcome, error)
                SELECT task_id, min(session_id), 'run', utc(min(created_at)), utc(max(created_at)), 'done', NULL
                FROM old.messages WHERE session_id IN {s} AND task_id NOT IN (SELECT id FROM main.runs)
                GROUP BY task_id"
            ),
            skipped: vec![],
            changed: vec![],
        },
    ]
}

/// 消息之后的各步（信件、委派等引用运行与会话）。
fn after_messages() -> Vec<Step> {
    let s = LIVE_SESSIONS;
    vec![
        Step {
            table: "compactions",
            source: "compactions",
            old_count: "SELECT count(*) FROM old.compactions",
            insert: format!(
                "INSERT INTO main.compactions (id, session_id, through_message_id, at_message_id, summary, created_at)
                SELECT id, session_id, through_message_id, at_message_id, summary, utc(created_at)
                FROM old.compactions WHERE session_id IN {s}"
            ),
            skipped: vec![(
                "会话已不存在",
                "SELECT count(*) FROM old.compactions WHERE session_id NOT IN (SELECT id FROM main.sessions)",
            )],
            changed: vec![],
        },
        Step {
            table: "thread_calls",
            source: "thread_calls",
            old_count: "SELECT count(*) FROM old.thread_calls",
            insert: format!(
                "INSERT INTO main.thread_calls (id, parent_session_id, call_id, thread_session_id, check_seconds)
                SELECT id, parent_session_id, call_id, thread_session_id, check_seconds FROM old.thread_calls
                WHERE parent_session_id IN {s} AND thread_session_id IN {s}"
            ),
            skipped: vec![(
                "会话已不存在",
                "SELECT count(*) FROM old.thread_calls WHERE parent_session_id NOT IN (SELECT id FROM main.sessions)
                    OR thread_session_id NOT IN (SELECT id FROM main.sessions)",
            )],
            changed: vec![],
        },
        Step {
            table: "mail",
            source: "mail",
            old_count: "SELECT count(*) FROM old.mail",
            insert: format!(
                "INSERT INTO main.mail (id, session_id, kind, source, origin, content, check_in, attachments, answers,
                    awaited, delegation_id, state, taken_by, created_at)
                SELECT id, session_id, kind, source, origin, content, check_in, attachments, answers, awaited,
                    CASE WHEN delegation_id IN (SELECT id FROM main.thread_calls) THEN delegation_id END,
                    state,
                    CASE WHEN taken_by IN (SELECT id FROM main.runs) THEN taken_by END,
                    utc(created_at)
                FROM old.mail WHERE session_id IN {s}"
            ),
            skipped: vec![(
                "会话已不存在",
                "SELECT count(*) FROM old.mail WHERE session_id NOT IN (SELECT id FROM main.sessions)",
            )],
            changed: vec![
                (
                    "交给的运行已不存在（置空）",
                    "SELECT count(*) FROM old.mail o JOIN main.mail n ON n.id = o.id
                        WHERE o.taken_by IS NOT NULL AND n.taken_by IS NULL",
                ),
                (
                    "所属委派已不存在（置空）",
                    "SELECT count(*) FROM old.mail o JOIN main.mail n ON n.id = o.id
                        WHERE o.delegation_id IS NOT NULL AND n.delegation_id IS NULL",
                ),
            ],
        },
        Step {
            table: "session_holds",
            source: "session_holds",
            old_count: "SELECT count(*) FROM old.session_holds",
            insert: format!(
                "INSERT INTO main.session_holds (id, session_id, holder, thread_call_id, call_id, awaited, status,
                    created_at, ended_at)
                SELECT id, session_id, holder,
                    CASE WHEN thread_call_id IN (SELECT id FROM main.thread_calls) THEN thread_call_id END,
                    call_id, awaited, status, utc(created_at), utc(ended_at)
                FROM old.session_holds WHERE session_id IN {s}"
            ),
            skipped: vec![(
                "会话已不存在",
                "SELECT count(*) FROM old.session_holds WHERE session_id NOT IN (SELECT id FROM main.sessions)",
            )],
            changed: vec![],
        },
        Step {
            table: "wakeups",
            source: "wakeups",
            old_count: "SELECT count(*) FROM old.wakeups",
            insert: format!(
                "INSERT INTO main.wakeups (id, holder, session_id, due_at, note, created_at)
                SELECT id, holder, session_id, utc(due_at), note, utc(created_at) FROM old.wakeups
                WHERE session_id IN {s}"
            ),
            skipped: vec![(
                "会话已不存在",
                "SELECT count(*) FROM old.wakeups WHERE session_id NOT IN (SELECT id FROM main.sessions)",
            )],
            changed: vec![],
        },
        Step {
            table: "approval_requests",
            source: "approval_requests",
            old_count: "SELECT count(*) FROM old.approval_requests",
            insert: format!(
                "INSERT INTO main.approval_requests (id, session_id, call_id, pin, created_at, taken_at)
                SELECT id, session_id, call_id, pin, utc(created_at), utc(taken_at) FROM old.approval_requests
                WHERE session_id IN {s}"
            ),
            skipped: vec![(
                "会话已不存在",
                "SELECT count(*) FROM old.approval_requests WHERE session_id NOT IN (SELECT id FROM main.sessions)",
            )],
            changed: vec![],
        },
        Step {
            table: "jobs",
            source: "jobs",
            old_count: "SELECT count(*) FROM old.jobs",
            insert:
                "INSERT INTO main.jobs (id, session_id, agent_id, command, secrets, status, exit_code, log_path, pid,
                    check_seconds, started_at, finished_at)
                SELECT id, session_id, agent_id, command, secrets, status, exit_code, log_path, pid, check_seconds,
                    utc(started_at), utc(finished_at)
                FROM old.jobs"
                    .into(),
            skipped: vec![],
            changed: vec![],
        },
        Step {
            table: "script_grants",
            source: "script_grants（始终允许）",
            old_count: "SELECT count(*) FROM old.script_grants",
            insert: "INSERT INTO main.script_grants (id, agent_id, skill, script, args, secrets, fingerprint, key,
                    session_id, call_id, created_at, hits, last_used_at)
                SELECT id, agent_id, skill, script, args, secrets, fingerprint, key, session_id, call_id,
                    utc(created_at), hits, utc(last_used_at) FROM old.script_grants"
                .into(),
            skipped: vec![],
            changed: vec![],
        },
        Step {
            table: "notifications",
            source: "notifications",
            old_count: "SELECT count(*) FROM old.notifications",
            insert: "INSERT INTO main.notifications (id, title, body, session_id, issue_id, unread, created_at)
                SELECT id, title, body, session_id, issue_id, unread, utc(created_at) FROM old.notifications"
                .into(),
            skipped: vec![],
            changed: vec![],
        },
        Step {
            table: "push_subscriptions",
            source: "push_subscriptions",
            old_count: "SELECT count(*) FROM old.push_subscriptions",
            insert: "INSERT INTO main.push_subscriptions (endpoint, p256dh, auth, created_at)
                SELECT endpoint, p256dh, auth, utc(created_at) FROM old.push_subscriptions"
                .into(),
            skipped: vec![],
            changed: vec![],
        },
        Step {
            table: "issues",
            source: "issues",
            old_count: "SELECT count(*) FROM old.issues",
            insert:
                "INSERT INTO main.issues (id, title, description, status, priority, owner_agent_id, assignee, creator,
                    created_at, updated_at)
                SELECT id, title, description, status, priority, owner_agent_id, assignee, creator,
                    utc(created_at), utc(updated_at) FROM old.issues"
                    .into(),
            skipped: vec![],
            changed: vec![],
        },
        Step {
            table: "issue_dependencies",
            source: "issue_dependencies",
            old_count: "SELECT count(*) FROM old.issue_dependencies",
            insert: "INSERT INTO main.issue_dependencies (issue_id, depends_on_id)
                SELECT issue_id, depends_on_id FROM old.issue_dependencies"
                .into(),
            skipped: vec![],
            changed: vec![],
        },
        Step {
            table: "issue_activities",
            source: "issue_activities",
            old_count: "SELECT count(*) FROM old.issue_activities",
            insert: format!(
                "INSERT INTO main.issue_activities (id, issue_id, created_at, actor, kind, body, change, session_id)
                SELECT id, issue_id, utc(created_at), actor, kind, body, nullif(change, 'null'),
                    CASE WHEN session_id IN {s} THEN session_id END
                FROM old.issue_activities"
            ),
            skipped: vec![],
            changed: vec![
                (
                    "没有变化内容的动态：旧库存的 JSON 文字 null 改成空值（同新版）",
                    "SELECT count(*) FROM old.issue_activities WHERE change = 'null'",
                ),
                (
                    "发生时所在的会话已不存在（置空，同新版删除会话时的做法）",
                    "SELECT count(*) FROM old.issue_activities
                    WHERE session_id IS NOT NULL AND session_id NOT IN (SELECT id FROM main.sessions)",
                ),
            ],
        },
        Step {
            table: "agent_history",
            source: "agent_history（全文索引随插入重建）",
            old_count: "SELECT count(*) FROM old.agent_history",
            insert: "INSERT INTO main.agent_history (id, agent_id, day, title, summary, refs, importance, created_at)
                SELECT id, agent_id, day, title, summary, refs, importance, utc(created_at) FROM old.agent_history"
                .into(),
            skipped: vec![],
            changed: vec![],
        },
        Step {
            table: "triggers",
            source: "triggers",
            old_count: "SELECT count(*) FROM old.triggers",
            insert:
                "INSERT INTO main.triggers (id, name, agent_id, prompt, kind, config, state, enabled, last_fired_at,
                    last_session_id, precheck, last_checked_at, precheck_output, disabled_reason, faults, finished_at,
                    created_at)
                SELECT id, name, agent_id, prompt, kind, config, state, enabled, utc(last_fired_at), last_session_id,
                    nullif(precheck, 'null'), utc(last_checked_at), precheck_output, disabled_reason, faults,
                    utc(finished_at), utc(created_at) FROM old.triggers"
                    .into(),
            skipped: vec![],
            changed: vec![(
                "没有前置检查的：旧库存的 JSON 文字 null 改成空值（新版以空值表示没有）",
                "SELECT count(*) FROM old.triggers WHERE precheck = 'null'",
            )],
        },
    ]
}

/// 旧库里整张不迁的表（其余的表都在各步里；FTS 的影子表随 agent_history 重建，不单列）。
const DROPPED: &[(&str, &str)] = &[("alembic_version", "Python 版的迁移版本号；新库按领域记在 schema_versions")];

fn count(tx: &Transaction, sql: &str) -> anyhow::Result<i64> {
    tx.query_row(sql, [], |r| r.get(0)).with_context(|| format!("counting: {sql}"))
}

fn run_step(tx: &Transaction, step: &Step) -> anyhow::Result<TableReport> {
    let old_rows = count(tx, step.old_count)?;
    let new_rows = tx.execute(&step.insert, []).with_context(|| format!("copying into {}", step.table))? as i64;
    let mut skipped = Vec::new();
    for (why, sql) in &step.skipped {
        skipped.push((why.to_string(), count(tx, sql)?));
    }
    let mut changed = Vec::new();
    for (what, sql) in &step.changed {
        changed.push((what.to_string(), count(tx, sql)?));
    }
    let accounted: i64 = skipped.iter().map(|(_, n)| n).sum();
    if old_rows - new_rows != accounted {
        bail!(
            "{} ({}): {old_rows} old rows, {new_rows} copied, but the skip reasons account for {accounted}",
            step.table,
            step.source
        );
    }
    Ok(TableReport { table: step.table.into(), source: step.source.into(), old_rows, new_rows, skipped, changed })
}

/// 消息：逐条转换，运行记录按旧的任务 id（[`before_messages`] 已为每条消息的任务建好运行记录），同一事务写可搜索文字。
/// 被停止的运行（消息标着已停止）的结局改成 stopped。
fn copy_messages(tx: &Transaction, report: &mut Report) -> anyhow::Result<TableReport> {
    let old_rows = count(tx, "SELECT count(*) FROM old.messages")?;
    let gone = count(tx, "SELECT count(*) FROM old.messages WHERE session_id NOT IN (SELECT id FROM main.sessions)")?;
    let mut notes = Notes::new();
    let mut new_rows = 0;
    let mut stopped_runs = std::collections::BTreeSet::new();
    {
        let mut read = tx.prepare(
            "SELECT id, session_id, task_id, payload, created_at FROM old.messages
             WHERE session_id IN (SELECT id FROM main.sessions) ORDER BY id",
        )?;
        let mut insert = tx.prepare(
            "INSERT INTO main.messages (id, session_id, run_id, payload, created_at) VALUES (?1, ?2, ?3, ?4, ?5)",
        )?;
        let mut text = tx.prepare("INSERT INTO main.message_texts (message_id, text) VALUES (?1, ?2)")?;
        let mut rows = read.query([])?;
        while let Some(row) = rows.next()? {
            let id: i64 = row.get(0)?;
            let session_id: String = row.get(1)?;
            let run_id: i64 = row.get(2)?;
            let payload: String = row.get(3)?;
            let created_at: DateTime<Utc> = row.get(4)?;
            let message = match messages::convert(&payload, created_at, &mut notes) {
                Ok(m) => m,
                Err(e) => {
                    report.message_failures.entry(e).or_default().push(id);
                    continue;
                }
            };
            if is_stopped(&message) {
                stopped_runs.insert(run_id);
            }
            let json = serde_json::to_string(&message).expect("messages serialize");
            insert.execute(params![id, session_id, run_id, json, created_at])?;
            let searchable = searchable_text(&message);
            if !searchable.is_empty() {
                text.execute(params![id, searchable])?;
            }
            new_rows += 1;
        }
    }
    let mut stopped = 0;
    for run in &stopped_runs {
        stopped += tx.execute("UPDATE main.runs SET outcome = 'stopped' WHERE id = ?1 AND outcome = 'done'", [run])?;
    }
    report.message_notes = notes;
    // 历史中间少一条可能让工具调用与结果对不上，那个会话就再也跑不了：有转不了的就整个不迁，先把转换补上
    if !report.message_failures.is_empty() {
        let lines: Vec<String> = report
            .message_failures
            .iter()
            .map(|(why, ids)| format!("{why}: {} (e.g. ids {:?})", ids.len(), &ids[..ids.len().min(5)]))
            .collect();
        bail!("some messages could not be converted, nothing was written:\n{}", lines.join("\n"));
    }
    if old_rows - new_rows != gone {
        bail!("messages: {old_rows} old rows, {new_rows} copied, but only {gone} belong to sessions that are gone");
    }
    Ok(TableReport {
        table: "messages".into(),
        source: "messages（逐条转换，可搜索文字重新生成）".into(),
        old_rows,
        new_rows,
        skipped: vec![("会话已不存在".into(), gone)],
        changed: vec![("被停止的运行：结局记为 stopped".into(), stopped as i64)],
    })
}

/// SQLite URI 里的文件路径（`%`、`?`、`#` 要转义）。
fn uri_path(path: &Path) -> String {
    path.to_string_lossy().replace('%', "%25").replace('?', "%3f").replace('#', "%23")
}

/// 迁移：读 old（只读），写 new（不能已存在）。
pub fn migrate(old: &Path, new: &Path, report: &mut Report) -> anyhow::Result<()> {
    if new.exists() {
        bail!("{} already exists: the migration only writes a new database", new.display());
    }
    if !old.is_file() {
        bail!("{} is not a file", old.display());
    }
    let partial = new.with_file_name(format!(
        "{}.partial",
        new.file_name().context("the new database path has no file name")?.to_string_lossy()
    ));
    for leftover in [partial.clone(), partial.with_extension("partial-journal")] {
        if leftover.exists() {
            std::fs::remove_file(&leftover).with_context(|| format!("removing {}", leftover.display()))?;
        }
    }
    muliao_platform::db::migrate(&partial, &muliao_server::MIGRATIONS).context("creating the new schema")?;
    let mut conn = Connection::open_with_flags(
        &partial,
        OpenFlags::SQLITE_OPEN_READ_WRITE | OpenFlags::SQLITE_OPEN_URI | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    )?;
    register_utc(&conn)?;
    conn.pragma_update(None, "foreign_keys", true)?;
    conn.execute("ATTACH DATABASE ?1 AS old", [format!("file:{}?mode=ro", uri_path(old))])
        .with_context(|| format!("opening {} read-only", old.display()))?;
    {
        let tx = conn.transaction()?;
        // 外键在提交时一起查（会话的父会话可能排在后面）
        tx.pragma_update(None, "defer_foreign_keys", true)?;
        for step in &before_messages() {
            report.tables.push(run_step(&tx, step)?);
        }
        let messages = copy_messages(&tx, report)?;
        report.tables.push(messages);
        for step in &after_messages() {
            report.tables.push(run_step(&tx, step)?);
        }
        for (table, why) in DROPPED {
            let rows = count(&tx, &format!("SELECT count(*) FROM old.{table}"))?;
            report.dropped_tables.push((table.to_string(), rows, why.to_string()));
        }
        let violations: Vec<(String, i64, String)> = {
            let mut stmt = tx.prepare("PRAGMA main.foreign_key_check")?;
            stmt.query_map([], |r| Ok((r.get(0)?, r.get::<_, Option<i64>>(1)?.unwrap_or(0), r.get(2)?)))?
                .take(20)
                .collect::<Result<_, _>>()?
        };
        if !violations.is_empty() {
            bail!("the copied data breaks foreign keys (table, rowid, parent): {violations:?}");
        }
        tx.commit()?;
    }
    conn.execute("DETACH DATABASE old", [])?;
    conn.close().map_err(|(_, e)| e)?;
    std::fs::rename(&partial, new).with_context(|| format!("renaming {} to {}", partial.display(), new.display()))?;
    report.old_db = old.display().to_string();
    report.new_db = new.display().to_string();
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Python 版库的表结构（取自线上库的 sqlite_master）
    const PYTHON_SCHEMA: &str = include_str!("../tests/fixtures/python_schema.sql");

    fn request(text: &str, stopped: bool) -> String {
        let metadata = if stopped { serde_json::json!({"muliao_stopped": 1}) } else { serde_json::Value::Null };
        serde_json::json!({
            "kind": "request", "metadata": metadata,
            "parts": [{"part_kind": "user-prompt", "content": text, "timestamp": "2026-11-01T00:00:00Z"}]
        })
        .to_string()
    }

    /// 一个小的旧库：每种取舍各一行。
    fn old_db(path: &Path) {
        let c = Connection::open(path).unwrap();
        c.execute_batch(PYTHON_SCHEMA).unwrap();
        let t = "2026-11-01 00:00:00.123456";
        c.execute_batch(&format!(
            "INSERT INTO agents (id, name, created_at, updated_at) VALUES ('a', 'alice', '{t}', '{t}');
             INSERT INTO sessions (id, agent_id, title, origin, created_at, updated_at)
                 VALUES ('s', 'a', 'x', 'user', '{t}', '{t}'), ('orphan', 'gone', 'y', 'user', '{t}', '{t}');
             INSERT INTO tasks (id, kind, lane, session_id, payload, status, attempts, error, not_before, created_at,
                     started_at, finished_at) VALUES
                 (1, 'run', 's', 's', '{{}}', 'done', 1, NULL, '{t}', '{t}', '{t}', '{t}'),
                 (2, 'run', 's', 's', '{{}}', 'running', 1, NULL, '{t}', '{t}', '{t}', NULL),
                 (3, 'deliver', 'push', NULL, '{{}}', 'done', 1, NULL, '{t}', '{t}', '{t}', '{t}'),
                 (4, 'run', 's', 's', '{{}}', 'queued', 0, NULL, '{t}', '{t}', NULL, NULL),
                 (5, 'run', 'o', 'orphan', '{{}}', 'failed', 3, 'boom', '{t}', '{t}', '{t}', '{t}');
             INSERT INTO mail (id, session_id, kind, content, attachments, answers, awaited, state, taken_by, created_at)
                 VALUES (1, 's', 'message', 'hi', '[]', '{{}}', 1, 'consumed', 1, '{t}'),
                        (2, 's', 'message', 'later', '[]', '{{}}', 1, 'queued', NULL, '{t}');
             INSERT INTO session_holds (id, session_id, holder, awaited, status, created_at, ended_at) VALUES
                 (1, 's', 'job:1', 0, 'released', '{t}', '{t}'), (2, 's', 'job:2', 0, 'active', '{t}', NULL);
             INSERT INTO approval_requests (id, session_id, call_id, created_at, taken_at) VALUES
                 (1, 's', 'c1', '{t}', '{t}'), (2, 's', 'c2', '{t}', NULL);
             INSERT INTO jobs (id, session_id, agent_id, command, secrets, status, log_path, started_at, finished_at)
                 VALUES (1, 's', 'a', 'ls', '[]', 'exited', 'l', '{t}', '{t}'),
                        (2, 's', 'a', 'sleep', '[]', 'running', 'l', '{t}', NULL);
             INSERT INTO triggers (id, name, agent_id, prompt, kind, config, state, enabled, precheck, faults, created_at)
                 VALUES (1, 'daily', 'a', 'p', 'schedule', '{{\"cron\": \"0 9 * * *\"}}', '{{}}', 1, 'null', '[]', '{t}');
             INSERT INTO issues (id, title, description, status, priority, creator, created_at, updated_at,
                     owner_agent_id, assignee) VALUES (1, 'i', '', 'done', 'none', 'user', '{t}', '{t}', 'a', NULL);
             INSERT INTO issue_activities (id, issue_id, created_at, actor, kind, body, change, session_id)
                 VALUES (1, 1, '{t}', 'user', 'created', NULL, 'null', 'deleted-session');
             INSERT INTO alembic_version VALUES ('x');"
        ))
        .unwrap();
        let mut insert = c
            .prepare("INSERT INTO messages (id, session_id, task_id, payload, created_at) VALUES (?1, ?2, ?3, ?4, ?5)")
            .unwrap();
        insert.execute(params![1, "s", 1, request("hello", true), t]).unwrap();
        // 任务 99 已被旧库清理掉
        insert.execute(params![2, "s", 99, request("pruned", false), "2026-11-02 00:00:00"]).unwrap();
        insert.execute(params![3, "orphan", 5, request("gone", false), t]).unwrap();
    }

    #[test]
    fn migrates_with_the_documented_choices() {
        let dir = tempfile::tempdir().unwrap();
        let (old, new) = (dir.path().join("old.db"), dir.path().join("new.db"));
        old_db(&old);
        let mut report = Report::default();
        migrate(&old, &new, &mut report).unwrap();
        assert!(!dir.path().join("new.db.partial").exists());
        let c = Connection::open(&new).unwrap();
        let q = |sql: &str| -> String {
            c.query_row(sql, [], |r| r.get::<_, Option<String>>(0)).unwrap().unwrap_or_else(|| "NULL".into())
        };
        // 运行：done 被停止过（消息标着已停止）→ stopped；running → 没结束；排队的、推送的、孤儿会话的不迁；清理掉的任务补上
        assert_eq!(
            q("SELECT group_concat(id || ':' || coalesce(outcome, '-'), ',') FROM runs ORDER BY id"),
            "1:stopped,2:-,99:done"
        );
        assert_eq!(
            q("SELECT started_at || '|' || ended_at FROM runs WHERE id = 99"),
            "2026-11-02 00:00:00+00:00|2026-11-02 00:00:00+00:00"
        );
        assert_eq!(q("SELECT group_concat(id || ':' || run_id, ',') FROM messages"), "1:1,2:99");
        assert_eq!(q("SELECT CAST(count(*) AS TEXT) FROM message_texts"), "2");
        assert_eq!(q("SELECT created_at FROM sessions"), "2026-11-01 00:00:00.123456+00:00");
        // 进行中的照迁：排着的信、还占着的占用、等批准的调用、在跑的后台任务（新版启动时接手）
        assert_eq!(
            q("SELECT group_concat(id || ':' || state || ':' || coalesce(taken_by, '-'), ',') FROM mail"),
            "1:consumed:1,2:queued:-"
        );
        assert_eq!(q("SELECT group_concat(id || ':' || status, ',') FROM session_holds"), "1:released,2:active");
        assert_eq!(q("SELECT group_concat(id) FROM approval_requests"), "1,2");
        assert_eq!(q("SELECT status || '|' || (finished_at IS NULL) FROM jobs WHERE id = 2"), "running|1");
        // JSON 文字 null → 空值；不存在的会话置空
        assert_eq!(q("SELECT precheck FROM triggers"), "NULL");
        assert_eq!(
            q("SELECT coalesce(change, 'NULL') || '|' || coalesce(session_id, 'NULL') FROM issue_activities"),
            "NULL|NULL"
        );
        let runs: Vec<&TableReport> = report.tables.iter().filter(|t| t.table == "runs").collect();
        assert_eq!(runs[0].skipped.iter().map(|(_, n)| *n).collect::<Vec<_>>(), [1, 1, 1]);
        let messages = report.tables.iter().find(|t| t.table == "messages").unwrap();
        assert_eq!((messages.old_rows, messages.new_rows), (3, 2));
        assert!(report.to_markdown().contains("alembic_version"));
        // 新库已存在时拒绝
        assert!(migrate(&old, &new, &mut Report::default()).is_err());
    }

    #[test]
    fn times_take_the_format_rust_writes() {
        assert_eq!(utc("2026-11-12 14:35:10.708480").unwrap(), "2026-11-12 14:35:10.708480+00:00");
        assert_eq!(utc("2026-11-12 14:35:10").unwrap(), "2026-11-12 14:35:10+00:00");
        assert_eq!(utc("2026-11-12T22:35:10.5+08:00").unwrap(), "2026-11-12 14:35:10.500+00:00");
        assert!(utc("yesterday").is_err());
        // 与 rusqlite 写 DateTime<Utc> 的格式一致，读回来是同一时刻
        let at: DateTime<Utc> = "2026-11-12T14:35:10.708480Z".parse().unwrap();
        let conn = Connection::open_in_memory().unwrap();
        let written: String = conn.query_row("SELECT ?1", [at], |r| r.get(0)).unwrap();
        assert_eq!(utc("2026-11-12 14:35:10.708480").unwrap(), written);
    }
}
