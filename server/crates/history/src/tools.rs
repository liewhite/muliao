//! 历史的工具（每个 agent 都有）：追加自己做过的事、检索自己或别的 agent 的历史（冷历史），某一天的活动概览与一个会话的
//! 时间线（热历史：会话领域的读模型 `muliao_sessions::recall`、issue 看板的用户动作，加上当天的工作历史）。agent 按名称
//! 指代，不出现 id。说明文字沿用 Python 版原文。

use agenter::{Tool, ToolCtx, ToolError, ToolOutput};
use chrono::{DateTime, NaiveDate, Utc};
use chrono_tz::Tz;
use muliao_assets::agents::{AgentNames, agent_names_in, named_in};
use muliao_issues::{UserActions, user_actions_in};
use muliao_platform::text::truncate;
use muliao_platform::time::parse_local;
use muliao_platform::tooling::{BuiltinTool, RunScope, ToolTraits, builtin};
use muliao_sessions::directory::Session;
use muliao_sessions::origins::{Origin, SessionOrigin};
use muliao_sessions::recall::{Activity, Entry, between_in, timeline_in};
use muliao_sessions::transcript::ItemType;
use muliao_sessions::work::EventSource;
use schemars::JsonSchema;
use serde::{Deserialize, Serialize};

use crate::error::{Error, Result, invalid};
use crate::log::{DEFAULT_LIMIT, History, MAX_LIMIT, NewEntry, Record, Search, search_in};

/// history_search、activity_overview 的 agent 取这个值时是全部 agent
const ALL_AGENTS: &str = "all";
const SESSION_READ: &str = "session_read";
/// session_read：工具结果、事件、工具调用参数默认截到这么长；用户与助手的文字不截
const DEFAULT_CLIP: usize = 2000;

/// 历史的工具组。max_chars：一次交给模型的上限（runtime.tool_output_max_chars），activity_overview、session_read 超出时在
/// 整行、整条处停下，告诉模型从哪接着读。
pub fn tools(history: History, max_chars: usize) -> Vec<BuiltinTool> {
    vec![
        builtin(HistoryAppend(history.clone()), ToolTraits::write()),
        builtin(HistorySearch(history.clone()), ToolTraits::read()),
        builtin(ActivityOverview { history: history.clone(), max_chars }, ToolTraits::read()),
        builtin(SessionRead { history, max_chars }, ToolTraits::read()),
    ]
}

/// 工具参数 agent：不给是自己，`all` 是全部（None），否则按名字找。
async fn agent_ids_of(history: &History, me: &str, agent: Option<String>) -> Result<Option<Vec<String>>> {
    match agent.as_deref() {
        None => Ok(Some(vec![me.to_owned()])),
        Some(ALL_AGENTS) => Ok(None),
        Some(name) => {
            let name = name.to_owned();
            Ok(Some(vec![history.db().read(move |c| named_in(c, &name)).await.map_err(Error::from)?.id]))
        }
    }
}

async fn names(history: &History) -> Result<AgentNames> {
    history.db().read(|c| agent_names_in(c).map_err(Error::from)).await
}

// ---- history_append ----

#[derive(Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct HistoryItem {
    /// Local calendar date (YYYY-MM-DD) the work happened, not today's date.
    #[schemars(with = "String")]
    day: NaiveDate,
    /// One-line title of this piece of work.
    title: String,
    /// 2-5 lines: what was done, the outcome, what matters for later.
    summary: String,
    /// References such as `#155`, a session id or a commit hash.
    #[serde(default)]
    refs: Vec<String>,
    /// 1 routine, 2 notable, 3 major (decision, incident, milestone).
    #[serde(default = "routine")]
    importance: i64,
}

fn routine() -> i64 {
    1
}

#[derive(Deserialize, JsonSchema)]
pub struct AppendArgs {
    /// The pieces of work to record.
    entries: Vec<HistoryItem>,
}

pub struct HistoryAppend(History);

impl Tool<RunScope> for HistoryAppend {
    type Args = AppendArgs;

    fn name(&self) -> &str {
        "history_append"
    }

    fn description(&self) -> &str {
        "Append entries to your own work history: one entry per piece of work. Entries can never be changed or \
         deleted, so check first with `history_search` (since/until = that day) that the day is not recorded yet."
    }

    async fn call(&self, ctx: ToolCtx<'_, RunScope>, a: AppendArgs) -> std::result::Result<ToolOutput, ToolError> {
        let entries = a
            .entries
            .into_iter()
            .map(|e| NewEntry {
                day: e.day,
                title: e.title,
                summary: e.summary,
                refs: e.refs,
                importance: e.importance,
            })
            .collect();
        let records = self.0.append(&ctx.deps.agent_id, entries).await?;
        let days: std::collections::BTreeSet<String> = records.iter().map(|r| r.day.to_string()).collect();
        let (first, last) = (records[0].id, records[records.len() - 1].id);
        Ok(agenter::tool::text(format!(
            "appended {} entries (ids {first}-{last}) for {}",
            records.len(),
            days.into_iter().collect::<Vec<_>>().join(", ")
        )))
    }
}

// ---- history_search ----

#[derive(Deserialize, JsonSchema)]
pub struct SearchArgs {
    /// Keywords, e.g. `trigger precheck` or `#155`.
    #[serde(default)]
    query: Option<String>,
    /// First day to include (YYYY-MM-DD).
    #[serde(default)]
    #[schemars(with = "Option<String>")]
    since: Option<NaiveDate>,
    /// Last day to include (YYYY-MM-DD).
    #[serde(default)]
    #[schemars(with = "Option<String>")]
    until: Option<NaiveDate>,
    /// Agent name, or `all` for every agent; your own history when omitted.
    #[serde(default)]
    agent: Option<String>,
    /// Only entries at least this important (1-3).
    #[serde(default = "routine")]
    min_importance: i64,
    /// Maximum number of entries (1-200).
    #[serde(default = "default_limit")]
    limit: usize,
}

fn default_limit() -> usize {
    DEFAULT_LIMIT
}

/// history_search 的一项（给模型看：agent 用名称）。
#[derive(Serialize)]
struct Found {
    id: i64,
    day: NaiveDate,
    /// agent 名；已删除的 agent 显示为 (deleted agent <id>)
    agent: String,
    title: String,
    summary: String,
    refs: Vec<String>,
    importance: i64,
}

pub struct HistorySearch(History);

impl Tool<RunScope> for HistorySearch {
    type Args = SearchArgs;

    fn name(&self) -> &str {
        "history_search"
    }

    fn description(&self) -> &str {
        "Search the work history (what was done, when, with which issues/commits). Returns a JSON array of \
         entries, each with `id`, `day` (YYYY-MM-DD), `agent`, `title`, `summary`, `refs` and `importance`.\n\
         Without `query`, entries are listed newest day first; with `query`, every whitespace-separated word must \
         appear in the title, summary or refs, best matches first. For a vaguer question, list a date range and \
         judge the entries yourself."
    }

    async fn call(&self, ctx: ToolCtx<'_, RunScope>, a: SearchArgs) -> std::result::Result<ToolOutput, ToolError> {
        let search = Search {
            agent_ids: agent_ids_of(&self.0, &ctx.deps.agent_id, a.agent).await?,
            query: a.query,
            since: a.since,
            until: a.until,
            min_importance: a.min_importance,
            limit: a.limit,
        };
        let found = self.0.search(search).await?;
        let names = names(&self.0).await?;
        let out: Vec<Found> = found
            .into_iter()
            .map(|r| Found {
                id: r.id,
                day: r.day,
                agent: names.name(&r.agent_id),
                title: r.title,
                summary: r.summary,
                refs: r.refs,
                importance: r.importance,
            })
            .collect();
        Ok(agenter::tool::json(&out))
    }
}

// ---- 热历史的文字 ----

fn hm(at: DateTime<Utc>, tz: Tz) -> String {
    at.with_timezone(&tz).format("%H:%M").to_string()
}

/// 热历史里给模型看的本地时刻（月-日 时:分）。
fn short_time(at: DateTime<Utc>, tz: Tz) -> String {
    at.with_timezone(&tz).format("%m-%d %H:%M").to_string()
}

fn chars(text: &str, n: usize) -> String {
    text.chars().take(n).collect()
}

/// 热历史的文字里正文缩进两格，挂在标题行下面。
fn indented(text: &str) -> String {
    text.lines().map(|l| format!("  {l}")).collect::<Vec<_>>().join("\n")
}

/// 会话从哪来：来源、发送方身份（issue 号等），thread 再加上名字与父会话 id 前缀。
fn place(session: &Session) -> String {
    let mut place = match session.origin {
        SessionOrigin::Trigger { trigger_id: n } | SessionOrigin::Issue { issue_id: n } => {
            format!("{} #{n}", session.origin.origin().as_str())
        }
        SessionOrigin::User | SessionOrigin::Delegation => session.origin.origin().as_str().to_owned(),
    };
    if let Some(thread) = &session.thread {
        let parent = session.parent_session_id.as_deref().unwrap_or("(deleted)");
        place.push_str(&format!(" thread {thread:?} of {}", chars(parent, 8)));
    }
    place
}

/// 从第 offset 行起，到 max_chars 为止的整行；没完时最后一行写明从哪接着读（同 read_file）。
fn limited_lines(lines: &[String], offset: usize, max_chars: usize) -> Result<String> {
    if offset > lines.len().max(1) {
        return Err(invalid(format!("offset {offset} is past the last line ({})", lines.len())));
    }
    let mut out: Vec<&str> = Vec::new();
    let mut size = 0;
    let mut note = None;
    for number in offset..=lines.len() {
        let line = &lines[number - 1];
        let len = line.chars().count();
        if size + len + 1 > max_chars && !out.is_empty() {
            note = Some(format!(
                "[output limit reached at line {} of {}; continue with offset={number}]",
                number - 1,
                lines.len()
            ));
            break;
        }
        out.push(line);
        size += len + 1;
    }
    let mut text = out.join("\n");
    if let Some(note) = note {
        text.push('\n');
        text.push_str(&note);
    }
    Ok(text)
}

/// 某天的活动：会话、用户的话与 issue 动作、当天的工作历史（一次读出）。
struct Day {
    activity: Activity,
    actions: Vec<UserActions>,
    /// 按写入先后
    records: Vec<Record>,
    names: AgentNames,
}

/// activity_overview 交给模型的文字：会话、用户在会话里说的话与对 issue 做的事（全文）、失败的运行、当天的工作历史。
fn overview_lines(d: &Day, who: &str, day: NaiveDate, tz: Tz) -> Vec<String> {
    let agents: std::collections::BTreeSet<&str> = d
        .activity
        .sessions
        .iter()
        .map(|s| s.session.agent_id.as_str())
        .chain(d.records.iter().map(|r| r.agent_id.as_str()))
        .collect();
    let many = agents.len() > 1;
    let agent_of = |id: &str| if many { format!("{} ", d.names.name(id)) } else { String::new() };
    let sessions = &d.activity.sessions;
    let mut origins: std::collections::BTreeMap<&str, usize> = std::collections::BTreeMap::new();
    for s in sessions {
        *origins.entry(s.session.origin.origin().as_str()).or_default() += 1;
    }
    let mut out = vec![format!("# activity of {who} on {day} (local day, times local)"), String::new()];
    out.push(format!(
        "## sessions ({}{})",
        sessions.len(),
        origins.iter().map(|(k, v)| format!(", {k} {v}")).collect::<String>()
    ));
    for s in sessions {
        let span = match &s.span {
            None => "no messages yet".to_owned(),
            Some(span) => format!("{} msgs {}-{}", span.messages, hm(span.first_at, tz), hm(span.last_at, tz)),
        };
        let failed = if s.failures.is_empty() { String::new() } else { format!(", {} FAILED", s.failures.len()) };
        out.push(format!(
            "- {}{} [{}] {:?}: {}, {span}, {} runs{failed}",
            agent_of(&s.session.agent_id),
            chars(&s.session.id, 8),
            place(&s.session),
            chars(&s.session.title, 80),
            s.state.as_str(),
            s.runs
        ));
    }
    if sessions.is_empty() {
        out.push("(none)".into());
    }
    let words = &d.activity.words;
    out.extend([String::new(), format!("## what the user typed ({}, full text)", words.len())]);
    for w in words {
        let s = sessions.iter().find(|s| s.session.id == w.session_id).expect("words come from listed sessions");
        let files = if w.attachments > 0 { format!(" (+{} files)", w.attachments) } else { String::new() };
        out.push(format!(
            "- {} {}{} [{}] {:?}{files}:",
            hm(w.at, tz),
            agent_of(&s.session.agent_id),
            chars(&w.session_id, 8),
            place(&s.session),
            chars(&s.session.title, 40)
        ));
        out.extend(w.text.lines().map(|l| format!("  {l}")));
    }
    if words.is_empty() {
        out.push("(none)".into());
    }
    out.extend([
        String::new(),
        format!("## what the user did on issues ({} issues, comments in full)", d.actions.len()),
    ]);
    for a in &d.actions {
        out.push(format!("- #{} {:?} (now {}):", a.issue_id, chars(&a.title, 80), a.status.as_str()));
        for line in &a.lines {
            out.extend(line.lines().map(|l| format!("  {l}")));
        }
    }
    if d.actions.is_empty() {
        out.push("(none)".into());
    }
    let mut failures: Vec<_> = sessions.iter().flat_map(|s| s.failures.iter().map(move |f| (f, s))).collect();
    failures.sort_by_key(|(f, _)| f.at);
    out.extend([String::new(), format!("## failed runs ({})", failures.len())]);
    for (f, s) in &failures {
        let error = f.error.split_whitespace().collect::<Vec<_>>().join(" ");
        out.push(format!(
            "- {} {}{} run {}: {}",
            hm(f.at, tz),
            agent_of(&s.session.agent_id),
            chars(&s.session.id, 8),
            f.run_id,
            chars(&error, 300)
        ));
    }
    if failures.is_empty() {
        out.push("(none)".into());
    }
    out.extend([String::new(), format!("## work history for {day} ({})", d.records.len())]);
    for r in &d.records {
        out.push(format!("- {}[{}] {} {:?}", agent_of(&r.agent_id), r.importance, r.title, r.refs));
    }
    if d.records.is_empty() {
        out.push("(none yet)".into());
    }
    out.extend([
        String::new(),
        format!(
            "Next: `{SESSION_READ}(session=<id prefix>)` for a session worth a closer look (`since`/`grep` to narrow \
             it); `issue_show` for an issue's full thread."
        ),
    ]);
    out
}

// ---- activity_overview ----

#[derive(Deserialize, JsonSchema)]
pub struct OverviewArgs {
    /// Local calendar date (YYYY-MM-DD); today when omitted.
    #[serde(default)]
    #[schemars(with = "Option<String>")]
    day: Option<NaiveDate>,
    /// Agent name, or `all` for every agent; yourself when omitted.
    #[serde(default)]
    agent: Option<String>,
    /// Line number to start from (1-based), as given in the continue note.
    #[serde(default = "first")]
    offset: usize,
}

fn first() -> usize {
    1
}

pub struct ActivityOverview {
    history: History,
    max_chars: usize,
}

impl Tool<RunScope> for ActivityOverview {
    type Args = OverviewArgs;

    fn name(&self) -> &str {
        "activity_overview"
    }

    fn description(&self) -> &str {
        "What happened on one local calendar day (default today), as text: every conversation that was active \
         (id prefix, origin/issue, title, state running/waiting/idle, messages and time span, runs and failures), \
         everything the user typed that day and every comment or change the user made on the agent's issues (in \
         full), failed runs with their errors, and that day's work-history entries. Use it to recall recent work \
         (e.g. today, which `history_search` does not have yet), then `session_read` for a conversation worth a \
         closer look. Output is limited in size: it stops after a whole line and a last note in square brackets \
         says where to continue (`offset`)."
    }

    async fn call(&self, ctx: ToolCtx<'_, RunScope>, a: OverviewArgs) -> std::result::Result<ToolOutput, ToolError> {
        if a.offset < 1 {
            return Err(invalid(format!("offset must be 1 or more, got {}", a.offset)).into());
        }
        let h = &self.history;
        let agent_ids = agent_ids_of(h, &ctx.deps.agent_id, a.agent).await?;
        let day = a.day.unwrap_or_else(|| h.today());
        let (start, end) = h.day_bounds(day);
        let (tz, ids) = (h.tz(), agent_ids.clone());
        let d = h
            .db()
            .read(move |c| -> Result<Day> {
                let activity = between_in(c, start, end, ids.as_deref())?;
                let actions = user_actions_in(c, start, end, ids.as_deref(), tz)?;
                let search = Search { since: Some(day), until: Some(day), limit: MAX_LIMIT, ..Search::of(ids.clone()) };
                let mut records = search_in(c, &search)?;
                records.reverse();
                Ok(Day { activity, actions, records, names: agent_names_in(c)? })
            })
            .await?;
        let who = match &agent_ids {
            None => "every agent".to_owned(),
            Some(ids) => d.names.name(&ids[0]),
        };
        let lines = overview_lines(&d, &who, day, tz);
        Ok(agenter::tool::text(limited_lines(&lines, a.offset, self.max_chars)?))
    }
}

// ---- session_read ----

/// 非用户消息条目标签的首词（用户消息的首词是发送方来源的大写）。
const EVENT: &str = "EVENT";
const ASSISTANT: &str = "ASSISTANT";
const CALL: &str = "call";
const RESULT: &str = "result";
const ERROR: &str = "error";

/// labels 参数里每项的首词只能是这些（思考不展示，中途停下的标记不能筛）。
fn label_words() -> Vec<String> {
    let mut words: Vec<String> = Origin::ALL.iter().map(|o| o.as_str().to_uppercase()).collect();
    words.extend([EVENT, ASSISTANT, CALL, RESULT, ERROR].map(str::to_owned));
    words
}

/// 一条的标签；思考不展示（None）。
fn label(entry: &Entry) -> Result<Option<String>> {
    let item = &entry.item;
    let tool = || item.tool_name.clone().unwrap_or_default();
    Ok(Some(match item.kind {
        ItemType::User => {
            let sender =
                entry.sender.ok_or_else(|| Error::Broken("a user entry of a timeline has no sender".into()))?;
            sender.as_str().to_uppercase()
        }
        ItemType::Event => {
            let source = item.source.as_ref().ok_or_else(|| Error::Broken("an event item has no source".into()))?;
            let raw = match source {
                EventSource::Thread { raw, .. } | EventSource::Job { raw, .. } | EventSource::Other { raw } => raw,
            };
            format!("{EVENT} {raw}{}", if item.check_in { " (check-in)" } else { "" })
        }
        ItemType::Assistant => ASSISTANT.to_owned(),
        ItemType::ToolCall => format!("{CALL} {}", tool()),
        ItemType::ToolResult => format!("{RESULT} {}", tool()),
        ItemType::ToolError => format!("{ERROR} {}", tool()),
        ItemType::Thinking => return Ok(None),
        ItemType::Stopped => "-- run stopped by the user --".to_owned(),
    }))
}

/// 标签是 wanted 中某一项本身，或以它加空格开头（区分大小写）："call" 匹配所有工具调用，"call run_shell" 只匹配它。
fn labelled(label: &str, wanted: &[String]) -> bool {
    wanted.iter().any(|w| label == w || label.starts_with(&format!("{w} ")))
}

/// since 参数：ISO 8601 的日期或时刻，不带时区的按本地时间。
fn local_since(since: &str, tz: Tz) -> Result<DateTime<Utc>> {
    let midnight =
        NaiveDate::parse_from_str(since.trim(), "%Y-%m-%d").ok().and_then(|d| parse_local(&format!("{d}T00:00"), tz));
    midnight.or_else(|| parse_local(since, tz)).ok_or_else(|| {
        invalid(format!("`since` must be an ISO 8601 date or time such as \"2026-11-04T09:30\", not {since:?}"))
    })
}

#[derive(Deserialize, JsonSchema)]
pub struct ReadArgs {
    /// Session id or a unique prefix of it (8 characters are usually enough).
    session: String,
    /// Only messages saved from this local date or time on, ISO 8601, e.g. "2026-11-04" or "2026-11-04T09:30".
    #[serde(default)]
    since: Option<String>,
    /// Only entries whose label or text matches this regular expression (case-insensitive, so `USER` also matches
    /// `/Users/` paths: use `labels` to pick entries by who or what they are).
    #[serde(default)]
    grep: Option<String>,
    /// Only entries with one of these labels (case-sensitive; each item matches that label or any label starting
    /// with it and a space): USER (typed by the user), TRIGGER, ISSUE, DELEGATION, EVENT, ASSISTANT, `call`,
    /// `result`, `error`, or e.g. `call run_shell`. Combined with `grep`, both must match.
    #[serde(default)]
    labels: Vec<String>,
    /// Clip tool calls, tool results and events to this many characters; 0 shows them in full.
    #[serde(default = "default_clip")]
    clip: usize,
    /// Entry number to start from (1-based, counted after `labels` and `grep`), as given in the continue note.
    #[serde(default = "first")]
    offset: usize,
}

fn default_clip() -> usize {
    DEFAULT_CLIP
}

pub struct SessionRead {
    history: History,
    max_chars: usize,
}

impl Tool<RunScope> for SessionRead {
    type Args = ReadArgs;

    fn name(&self) -> &str {
        SESSION_READ
    }

    fn description(&self) -> &str {
        "Read one conversation's timeline (yours or another agent's), as text in time order: what came in (USER = \
         typed by the user in the app; TRIGGER / ISSUE / DELEGATION = sent by the session's starter), events, the \
         assistant's text, tool calls and their results. User and assistant text is shown in full; tool calls, \
         results and events are clipped. Use it to see exactly what was said and done, e.g. a session found with \
         `activity_overview`. Output is limited in size: it stops after a whole entry and a last note in square \
         brackets says where to continue (`offset`)."
    }

    async fn call(&self, _ctx: ToolCtx<'_, RunScope>, a: ReadArgs) -> std::result::Result<ToolOutput, ToolError> {
        Ok(agenter::tool::text(self.read(a).await?))
    }
}

impl SessionRead {
    async fn read(&self, a: ReadArgs) -> Result<String> {
        if a.offset < 1 {
            return Err(invalid(format!("offset must be 1 or more, got {}", a.offset)));
        }
        let pattern = a
            .grep
            .as_deref()
            .map(|g| regex::RegexBuilder::new(g).case_insensitive(true).build())
            .transpose()
            .map_err(|e| invalid(format!("grep is not a valid regular expression: {e}")))?;
        let words = label_words();
        for wanted in &a.labels {
            let head = wanted.split(' ').next().unwrap_or_default();
            if wanted != wanted.trim() || !words.iter().any(|w| w == head) {
                return Err(invalid(format!(
                    "labels: {wanted:?} is not a label; a label starts with one of {}",
                    words.join(", ")
                )));
            }
        }
        let tz = self.history.tz();
        let since = a.since.as_deref().map(|s| local_since(s, tz)).transpose()?;
        let session = a.session.clone();
        let (timeline, names) = self
            .history
            .db()
            .read(move |c| -> Result<_> { Ok((timeline_in(c, &session, since)?, agent_names_in(c)?)) })
            .await?;
        let mut shown = Vec::new();
        for entry in &timeline.entries {
            let Some(label) = label(entry)? else { continue };
            if !a.labels.is_empty() && !labelled(&label, &a.labels) {
                continue;
            }
            let item = &entry.item;
            if let Some(p) = &pattern
                && !(p.is_match(&label) || p.is_match(&item.text))
            {
                continue;
            }
            let full = matches!(item.kind, ItemType::User | ItemType::Assistant) || a.clip == 0;
            let mut text = if full { item.text.clone() } else { truncate(&item.text, a.clip) };
            for f in &item.attachments {
                text.push_str(&format!("\n[attachment {:?} ({})]", f.name, f.media_type));
            }
            shown.push(format!("[{}] {label}\n{}", short_time(entry.at, tz), indented(&text)));
        }
        let info = &timeline.session;
        let mut head = format!(
            "# session {} [{}] {:?}, agent {}: {} entries",
            info.id,
            place(info),
            info.title,
            names.name(&info.agent_id),
            shown.len()
        );
        if let Some(s) = &a.since {
            head.push_str(&format!(" since {s}"));
        }
        if !a.labels.is_empty() {
            head.push_str(&format!(" labelled {}", a.labels.join(", ")));
        }
        if let Some(g) = &a.grep {
            head.push_str(&format!(" matching /{g}/"));
        }
        if a.offset > shown.len().max(1) {
            return Err(invalid(format!("offset {} is past the last entry ({})", a.offset, shown.len())));
        }
        let mut size = head.chars().count();
        let mut out = vec![head];
        for number in a.offset..=shown.len() {
            let block = &shown[number - 1];
            let len = block.chars().count();
            if size + len + 2 > self.max_chars && number > a.offset {
                out.push(format!(
                    "[output limit reached at entry {} of {}; continue with offset={number}]",
                    number - 1,
                    shown.len()
                ));
                break;
            }
            out.push(block.clone());
            size += len + 2;
        }
        Ok(out.join("\n\n"))
    }
}
