//! issue 看板的工具：agent 按名称指代 agent（`user` 指用户），不出现 agent id。说明文字沿用 Python 版原文。

use agenter::{Tool, ToolCtx, ToolError, ToolOutput};
use chrono::{DateTime, Utc};
use muliao_assets::agents::{AgentNames, agent_names_in, named_in};
use muliao_assets::naming::USER;
use muliao_platform::tool_views::{ToolRender, View, ViewBlock, fields};
use muliao_platform::tooling::{BuiltinTool, RunScope, ToolTraits, builtin};
use muliao_sessions::activity::WorkState;
use schemars::JsonSchema;
use serde::{Deserialize, Serialize};

use crate::board::{Board, BoardIssue, Filter, NewIssue, in_batch};
use crate::change::Patch;
use crate::error::{Result, invalid};
use crate::graph::Upstream;
use crate::model::{Assignee, By, Priority, Status};
use crate::render::assignee_name;

/// issue 看板的工具组。
pub fn tools(board: Board) -> Vec<BuiltinTool> {
    vec![
        builtin(IssueList(board.clone()), ToolTraits::read().render(list_render())),
        builtin(IssueShow(board.clone()), ToolTraits::read().render(show_render())),
        builtin(IssueCreate(board.clone()), ToolTraits::write().render(create_render())),
        builtin(IssueUpdate(board.clone()), ToolTraits::write().render(update_render())),
        builtin(IssueStop(board.clone()), ToolTraits::write().render(stop_render())),
        builtin(IssueComment(board), ToolTraits::write().render(comment_render())),
    ]
}

fn by(ctx: &ToolCtx<'_, RunScope>) -> By {
    By::agent(&ctx.deps.agent_id, &ctx.deps.session_id)
}

/// 按名称找 agent 的 id（找不到时把可用的名称交还模型）。
async fn agent_id(board: &Board, name: String) -> Result<String> {
    Ok(board.db().read(move |c| named_in(c, &name)).await?.id)
}

/// 指派人：`user` 是用户，其余按 agent 名称找。
async fn assignee(board: &Board, name: String) -> Result<Assignee> {
    if name == USER { Ok(Assignee::User) } else { Ok(Assignee::Agent(agent_id(board, name).await?)) }
}

async fn names(board: &Board) -> Result<AgentNames> {
    Ok(board.db().read(agent_names_in).await?)
}

fn markdown(text: &str) -> Option<ViewBlock> {
    (!text.is_empty()).then(|| ViewBlock::Markdown { title: None, text: text.to_owned() })
}

fn issue_field(id: i64) -> (&'static str, muliao_platform::tool_views::FieldValue) {
    ("issue", format!("#{id}").into())
}

// ---- issue_list ----

/// issue_list 的一项（给模型看：agent 用名称）。
#[derive(Debug, Serialize)]
pub struct IssueSummary {
    pub id: i64,
    pub title: String,
    /// 负责的项目（agent 名）；负责的 agent 被删除后为 null
    pub owner: Option<String>,
    pub status: Status,
    pub priority: Priority,
    /// agent 名、`user`，或 null（未指派）
    pub assignee: Option<String>,
    pub assignee_state: Option<WorkState>,
    pub stalled: bool,
    pub depends_on: Vec<i64>,
    pub waiting_on: Vec<i64>,
    pub updated_at: DateTime<Utc>,
}

impl IssueSummary {
    fn of(b: BoardIssue, names: &AgentNames) -> Self {
        let i = b.issue;
        IssueSummary {
            id: i.id,
            title: i.title,
            owner: i.owner.map(|o| names.name(&o)),
            status: i.status,
            priority: i.priority,
            assignee: i.assignee.as_ref().map(|a| assignee_name(Some(a), names)),
            assignee_state: b.work.assignee_state,
            stalled: b.work.stalled,
            depends_on: b.work.depends_on,
            waiting_on: b.work.waiting_on,
            updated_at: i.updated_at,
        }
    }
}

#[derive(Deserialize, JsonSchema)]
pub struct ListArgs {
    /// Only these statuses; all when omitted.
    #[serde(default)]
    statuses: Vec<Status>,
    /// Only issues assigned to this agent (by name) or `user`.
    #[serde(default)]
    assignee: Option<String>,
    /// Only issues owned by this agent (by name): the whole history of its project.
    #[serde(default)]
    owner: Option<String>,
}

pub struct IssueList(Board);

impl Tool<RunScope> for IssueList {
    type Args = ListArgs;

    fn name(&self) -> &str {
        "issue_list"
    }

    fn description(&self) -> &str {
        "List issues on the muliao issue board. Returns a JSON array of issues, most recently updated first, each \
         with `id`, `title`, `owner`, `status`, `priority`, `assignee`, `assignee_state`, `stalled`, `depends_on`, \
         `waiting_on` and `updated_at` (ISO 8601). `assignee_state` tells whether the assigned agent is working on it \
         in the issue's conversation: running, waiting (on a background job, delegated work, a `wake_me` wake-up or \
         the user's answer), idle, or null (no agent assignee or no conversation yet). `stalled` marks an \
         `in_progress` issue assigned to an agent whose issue conversation is idle or missing.\n`waiting_on` lists \
         dependencies not yet `done`/`cancelled`; such an issue is not delivered or stalled."
    }

    async fn call(&self, _ctx: ToolCtx<'_, RunScope>, a: ListArgs) -> std::result::Result<ToolOutput, ToolError> {
        let filter = Filter {
            statuses: a.statuses,
            assignee: match a.assignee {
                Some(n) => Some(assignee(&self.0, n).await?),
                None => None,
            },
            owner: match a.owner {
                Some(n) => Some(agent_id(&self.0, n).await?),
                None => None,
            },
        };
        let found = self.0.list(filter).await?;
        let names = names(&self.0).await?;
        let out: Vec<IssueSummary> = found.into_iter().map(|b| IssueSummary::of(b, &names)).collect();
        Ok(agenter::tool::json(&out))
    }
}

/// 界面上 issue_list 的结果表只用到的列（从 [`IssueSummary`] 的 JSON 还原）。
#[derive(Deserialize)]
struct ListedRow {
    id: i64,
    title: String,
    status: String,
    assignee: Option<String>,
    owner: Option<String>,
}

fn list_render() -> ToolRender {
    ToolRender::new()
        .call(|a: &ListArgs| {
            let statuses: Vec<String> = a.statuses.iter().map(|s| s.as_str().to_owned()).collect();
            Some(vec![fields([
                ("statuses", (!statuses.is_empty()).then_some(&statuses).into()),
                ("assignee", a.assignee.as_ref().into()),
                ("owner", a.owner.as_ref().into()),
            ])])
        })
        .result_json(|found: &Vec<ListedRow>| {
            Some(vec![ViewBlock::Table {
                columns: ["#", "title", "status", "assignee", "owner"].map(String::from).to_vec(),
                rows: found
                    .iter()
                    .map(|i| {
                        vec![
                            format!("#{}", i.id),
                            i.title.clone(),
                            i.status.clone(),
                            i.assignee.clone().unwrap_or_default(),
                            i.owner.clone().unwrap_or_default(),
                        ]
                    })
                    .collect(),
            }])
        })
}

// ---- issue_show ----

#[derive(Deserialize, JsonSchema)]
pub struct ShowArgs {
    /// Issue number.
    issue_id: i64,
}

pub struct IssueShow(Board);

impl Tool<RunScope> for IssueShow {
    type Args = ShowArgs;

    fn name(&self) -> &str {
        "issue_show"
    }

    fn description(&self) -> &str {
        "Show an issue with its description, dependencies, comments and history."
    }

    async fn call(&self, _ctx: ToolCtx<'_, RunScope>, a: ShowArgs) -> std::result::Result<ToolOutput, ToolError> {
        Ok(agenter::tool::text(self.0.render(a.issue_id).await?))
    }
}

fn show_render() -> ToolRender {
    ToolRender::new()
        .call(|a: &ShowArgs| Some(vec![fields([issue_field(a.issue_id)])]))
        .result_text(|text| Some(vec![ViewBlock::Markdown { title: None, text: text.to_owned() }]))
}

// ---- issue_create ----

// issue_create 的一项：字段与 issue_create 的单个参数一致（说明只写在那里），另加 key。类型上只写 `//` 注释，免得进 schema
#[derive(Debug, Clone, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct NewIssueItem {
    /// Name for this item that other items list in their depends_on, e.g. `api`.
    #[serde(default)]
    key: Option<String>,
    title: String,
    owner: String,
    #[serde(default)]
    description: String,
    #[serde(default)]
    status: Status,
    #[serde(default)]
    priority: Priority,
    #[serde(default)]
    assignee: Option<String>,
    /// What this one waits for: keys of other items of this batch and/or existing issue numbers.
    #[serde(default)]
    depends_on: Vec<Upstream>,
}

#[derive(Deserialize, JsonSchema)]
pub struct CreateArgs {
    /// One-line summary (omit when using `issues`).
    #[serde(default)]
    title: Option<String>,
    /// The agent (by name) whose project this is; it stays the owner when the issue is reassigned, and gets the
    /// user's comments while no agent is working on the issue. Usually yourself for your own project's work (omit
    /// when using `issues`).
    #[serde(default)]
    owner: Option<String>,
    /// Markdown details: context, what done looks like.
    #[serde(default)]
    description: String,
    /// Initial status.
    #[serde(default)]
    status: Status,
    /// Priority.
    #[serde(default)]
    priority: Priority,
    /// Agent name, `user`, or omit for unassigned.
    #[serde(default)]
    assignee: Option<String>,
    /// Issue numbers this one waits for; dependencies must not form a cycle.
    #[serde(default)]
    depends_on: Vec<i64>,
    /// Several issues to create at once, each with the fields above plus an optional `key`; in their `depends_on`, a
    /// string is the key of another item of this batch, a number an existing issue.
    #[serde(default)]
    issues: Vec<NewIssueItem>,
}

impl CreateArgs {
    /// 单个参数给成的一项（没有 key）。
    fn single(&self) -> NewIssueItem {
        NewIssueItem {
            key: None,
            title: self.title.clone().unwrap_or_default(),
            owner: self.owner.clone().unwrap_or_default(),
            description: self.description.clone(),
            status: self.status,
            priority: self.priority,
            assignee: self.assignee.clone(),
            depends_on: self.depends_on.iter().map(|&i| Upstream::Issue(i)).collect(),
        }
    }

    /// 除 issues 之外给了别的参数（缺省值与不给等价）。
    fn gives_single(&self) -> bool {
        self.title.is_some()
            || self.owner.is_some()
            || !self.description.is_empty()
            || self.status != Status::default()
            || self.priority != Priority::default()
            || self.assignee.is_some()
            || !self.depends_on.is_empty()
    }
}

pub struct IssueCreate(Board);

impl IssueCreate {
    async fn new_issue(&self, item: NewIssueItem, size: usize, n: usize) -> Result<NewIssue> {
        let key = item.key.clone();
        let resolved = async {
            let owner = agent_id(&self.0, item.owner).await?;
            let assignee = match item.assignee {
                Some(a) => Some(assignee(&self.0, a).await?),
                None => None,
            };
            Ok((owner, assignee))
        }
        .await;
        let (owner, assignee) = in_batch(size, n, key.as_deref(), resolved)?;
        Ok(NewIssue {
            owner,
            title: item.title,
            description: item.description,
            status: item.status,
            priority: item.priority,
            assignee,
            depends_on: item.depends_on,
            key,
        })
    }
}

impl Tool<RunScope> for IssueCreate {
    type Args = CreateArgs;

    fn name(&self) -> &str {
        "issue_create"
    }

    fn description(&self) -> &str {
        "Create an issue. Each agent works on an issue in one conversation dedicated to it; while the issue is \
         `in_progress`, its creation and every later change are delivered there to the assigned agent (also when the \
         assignee is you: create it here, it is worked on there), except changes made from that very conversation. \
         Use `in_progress` for work to start now, `backlog` for later work.\nTo run issues in order, give \
         `depends_on`: the issue is delivered only once all of them are `done` or `cancelled` (changes before that \
         are only recorded), so a chain can be created up front, all `in_progress`: each one starts when its last \
         dependency finishes.\nTo create several issues that depend on each other, pass `issues` instead (one call, \
         not several parallel calls: those cannot refer to each other's numbers): give each item a `key` and list \
         other items' keys in its `depends_on`. The whole batch is created in one go (nothing is created if any item \
         is invalid) and delivered only after all of it exists with its dependencies.\nTo break down a larger piece \
         of work: in one `issues` call, create the steps (`in_progress`, ordered with `depends_on` where needed) and \
         an `in_progress` parent issue with the overall goal that depends on all of them."
    }

    async fn call(&self, ctx: ToolCtx<'_, RunScope>, a: CreateArgs) -> std::result::Result<ToolOutput, ToolError> {
        let items = if a.issues.is_empty() {
            if a.title.is_none() || a.owner.is_none() {
                return Err(invalid("title and owner are required (or pass issues)").into());
            }
            vec![a.single()]
        } else {
            if a.gives_single() {
                return Err(invalid("pass either one issue (title/owner/...) or issues, not both").into());
            }
            a.issues.clone()
        };
        let size = items.len();
        let mut new = Vec::new();
        for (n, item) in items.into_iter().enumerate() {
            new.push(self.new_issue(item, size, n).await?);
        }
        let keys: Vec<Option<String>> = new.iter().map(|i| i.key.clone()).collect();
        let created = self.0.create_many(by(&ctx), new).await?;
        if a.issues.is_empty() {
            return Ok(agenter::tool::text(format!("created issue #{}", created[0].id)));
        }
        let lines: Vec<String> = created
            .iter()
            .zip(keys)
            .map(|(i, key)| match key {
                Some(k) => format!("#{} (key {k}): {}", i.id, i.title),
                None => format!("#{}: {}", i.id, i.title),
            })
            .collect();
        Ok(agenter::tool::text(format!("created issues:\n{}", lines.join("\n"))))
    }
}

fn create_render() -> ToolRender {
    ToolRender::new().call(|a: &CreateArgs| {
        // 一次建多个时每项的字段与单个参数同名，单个的没有 key
        let items = if a.issues.is_empty() { vec![a.single()] } else { a.issues.clone() };
        let mut view: View = Vec::new();
        for i in items {
            let depends: Vec<String> = i
                .depends_on
                .iter()
                .map(|d| match d {
                    Upstream::Issue(n) => format!("#{n}"),
                    Upstream::Key(k) => k.clone(),
                })
                .collect();
            view.push(fields([
                ("title", i.title.as_str().into()),
                ("key", i.key.as_ref().into()),
                ("owner", i.owner.as_str().into()),
                ("status", i.status.as_str().into()),
                ("priority", i.priority.as_str().into()),
                ("assignee", i.assignee.as_ref().into()),
                ("depends on", (!depends.is_empty()).then_some(&depends).into()),
            ]));
            view.extend(markdown(&i.description));
        }
        Some(view)
    })
}

// ---- issue_update ----

#[derive(Deserialize, JsonSchema)]
pub struct UpdateArgs {
    /// Issue number.
    issue_id: i64,
    /// New owner agent (by name): the project this issue belongs to.
    #[serde(default)]
    owner: Option<String>,
    /// New title.
    #[serde(default)]
    title: Option<String>,
    /// New description (replaces the old one).
    #[serde(default)]
    description: Option<String>,
    /// New status; when you have finished, `in_review`, or `done` if other issues depend on it.
    #[serde(default)]
    status: Option<Status>,
    /// New priority.
    #[serde(default)]
    priority: Option<Priority>,
    /// Agent name, or `user` to hand it to the user (they get a notification).
    #[serde(default)]
    assignee: Option<String>,
    /// Remove the assignee.
    #[serde(default)]
    unassign: bool,
    /// The full new list of issue numbers this one waits for (replaces the old list; `[]` removes all); dependencies
    /// must not form a cycle.
    #[serde(default)]
    depends_on: Option<Vec<i64>>,
}

pub struct IssueUpdate(Board);

impl Tool<RunScope> for IssueUpdate {
    type Args = UpdateArgs;

    fn name(&self) -> &str {
        "issue_update"
    }

    fn description(&self) -> &str {
        "Change an issue; only the given fields change. When the issue is (or now becomes) `in_progress` and assigned \
         to an agent, the change is delivered to that agent in its conversation for the issue, except when the agent \
         makes it from that very conversation. Set `blocked` with assignee `user` when only the user can unblock it \
         (to wait for anything else, keep it `in_progress` and let the conversation wait: a background job, \
         delegated work, or `wake_me`). Finished work goes to `in_review` with assignee `user`, and the user moves it \
         to `done` after reviewing; but when other issues depend on it (`issue_show` lists them under `blocks`), set \
         it `done` yourself when finished, so the issues waiting for it can start. Changing the status does not stop \
         a conversation working on the issue: to call off work under way, use `issue_stop`."
    }

    async fn call(&self, ctx: ToolCtx<'_, RunScope>, a: UpdateArgs) -> std::result::Result<ToolOutput, ToolError> {
        if a.assignee.is_some() && a.unassign {
            return Err(invalid("give either assignee or unassign, not both").into());
        }
        let assignee = match (a.unassign, a.assignee) {
            (true, _) => Some(None),
            (false, Some(n)) => Some(Some(assignee(&self.0, n).await?)),
            (false, None) => None,
        };
        let owner = match a.owner {
            Some(n) => Some(agent_id(&self.0, n).await?),
            None => None,
        };
        let patch = Patch {
            owner,
            title: a.title,
            description: a.description,
            status: a.status,
            priority: a.priority,
            assignee,
            depends_on: a.depends_on,
        };
        let issue = self.0.update(by(&ctx), a.issue_id, patch).await?;
        Ok(agenter::tool::text(format!("updated issue #{} (status: {})", issue.id, issue.status)))
    }
}

fn update_render() -> ToolRender {
    ToolRender::new().call(|a: &UpdateArgs| {
        let depends: Option<Vec<String>> = a.depends_on.as_ref().map(|d| d.iter().map(|i| format!("#{i}")).collect());
        let assignee = if a.unassign { Some("(unassign)".to_owned()) } else { a.assignee.clone() };
        let mut view = vec![fields([
            issue_field(a.issue_id),
            ("title", a.title.as_ref().into()),
            ("status", a.status.map(Status::as_str).into()),
            ("priority", a.priority.map(Priority::as_str).into()),
            ("assignee", assignee.into()),
            ("owner", a.owner.as_ref().into()),
            ("depends on", depends.as_ref().into()),
        ])];
        view.extend(a.description.as_deref().and_then(markdown));
        Some(view)
    })
}

// ---- issue_stop ----

#[derive(Deserialize, JsonSchema)]
pub struct StopArgs {
    /// Issue number.
    issue_id: i64,
    /// New status: `backlog`, `blocked`, `cancelled`, or `in_progress` to redirect the work (then `message` is
    /// required); not `in_review` or `done`.
    status: Status,
    /// Recorded as your comment and delivered to the stopped conversation, which then runs once more to act on it
    /// (wrap up, or what to do instead), whatever the new status.
    #[serde(default)]
    message: Option<String>,
}

pub struct IssueStop(Board);

impl Tool<RunScope> for IssueStop {
    type Args = StopArgs;

    fn name(&self) -> &str {
        "issue_stop"
    }

    fn description(&self) -> &str {
        "Stop the assigned agent's conversation on an issue, like the user's stop button: its current run ends, its \
         queued messages are dropped and the threads it opened are stopped too. Then the issue is set to `status`. \
         Use it to call off work that is under way (e.g. sub-issues no longer needed): changing the status alone \
         does not stop a conversation that is running. Not from the issue's own conversation (use `issue_update` \
         there). When the assigned agent has no conversation on the issue yet, only the status changes."
    }

    async fn call(&self, ctx: ToolCtx<'_, RunScope>, a: StopArgs) -> std::result::Result<ToolOutput, ToolError> {
        let with_message = a.message.is_some();
        let result = self.0.stop(by(&ctx), a.issue_id, a.status, a.message).await?;
        let name = names(&self.0).await?.name(&result.agent);
        let id = a.issue_id;
        if !result.stopped {
            return Ok(agenter::tool::text(format!(
                "{name} has no conversation on issue #{id} yet, nothing to stop; issue #{id} is {}",
                result.issue.status
            )));
        }
        let delivered = if with_message { "; your message was delivered there" } else { "" };
        Ok(agenter::tool::text(format!(
            "stopped {name}'s conversation on issue #{id}; it is {}{delivered}",
            result.issue.status
        )))
    }
}

fn stop_render() -> ToolRender {
    ToolRender::new().call(|a: &StopArgs| {
        let mut view = vec![fields([issue_field(a.issue_id), ("status", a.status.as_str().into())])];
        view.extend(a.message.as_deref().and_then(markdown));
        Some(view)
    })
}

// ---- issue_comment ----

#[derive(Deserialize, JsonSchema)]
pub struct CommentArgs {
    /// Issue number.
    issue_id: i64,
    /// Comment text.
    body: String,
}

pub struct IssueComment(Board);

impl Tool<RunScope> for IssueComment {
    type Args = CommentArgs;

    fn name(&self) -> &str {
        "issue_comment"
    }

    fn description(&self) -> &str {
        "Comment on an issue (Markdown): progress, findings, results, questions for the user. Delivered like any \
         other change (see `issue_update`)."
    }

    async fn call(&self, ctx: ToolCtx<'_, RunScope>, a: CommentArgs) -> std::result::Result<ToolOutput, ToolError> {
        self.0.comment(by(&ctx), a.issue_id, a.body).await?;
        Ok(agenter::tool::text(format!("commented on issue #{}", a.issue_id)))
    }
}

fn comment_render() -> ToolRender {
    ToolRender::new().call(|a: &CommentArgs| {
        let mut view = vec![fields([issue_field(a.issue_id)])];
        view.extend(markdown(&a.body));
        Some(view)
    })
}
