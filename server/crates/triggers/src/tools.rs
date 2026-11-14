//! 触发器的管理工具：一组 trigger_* 覆盖所有触发方式，方式的差异只在 kind 与 config。agent 按名称指定，不出现 id。

use agenter::{Tool, ToolCtx, ToolError, ToolOutput};
use chrono::{DateTime, Utc};
use muliao_assets::agents::{AgentNames, agent_names_in, named_in};
use muliao_platform::ids::SessionId;
use muliao_platform::tooling::{BuiltinTool, RunScope, ToolTraits, builtin};
use schemars::JsonSchema;
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};

use crate::faults::TriggerFault;
use crate::kinds::{Kind, KindView, NewConfig};
use crate::precheck::Precheck;
use crate::service::{NewSpec, PrecheckChange, Prompt, TriggerName, TriggerService, TriggerView, Update};
use crate::store::Listing;
use muliao_platform::errors::{Error, invalid};

#[derive(Serialize)]
struct KindInfo {
    kind: Kind,
    description: &'static str,
}

/// 给模型看的一个触发器。
#[derive(Serialize)]
struct TriggerInfo {
    name: TriggerName,
    /// kind、config，以及 status：这种方式的运行状态，如下次触发时间
    #[serde(flatten)]
    kind: KindView,
    agent: String,
    prompt: String,
    enabled: bool,
    precheck: Option<Precheck>,
    faults: Vec<TriggerFault>,
    /// 最近一次触发开出的会话；已结束的（跑完的一次性触发器）在这里能看到它的结果去了哪
    last_session_id: Option<SessionId>,
    /// 不为空表示它已经跑完、不会再触发了
    finished_at: Option<DateTime<Utc>>,
}

fn info(view: TriggerView, names: &AgentNames) -> TriggerInfo {
    let t = view.trigger;
    TriggerInfo {
        agent: names.name(&t.agent_id),
        name: t.name,
        kind: view.kind,
        prompt: t.prompt.into(),
        enabled: t.enabled,
        precheck: t.precheck,
        faults: t.faults,
        last_session_id: view.live_session_id,
        finished_at: t.finished_at,
    }
}

async fn names(service: &TriggerService) -> Result<AgentNames, Error> {
    service.db().read(agent_names_in).await
}

#[derive(Deserialize, JsonSchema)]
pub struct NoArgs {}

pub struct TriggerKinds;

impl Tool<RunScope> for TriggerKinds {
    type Args = NoArgs;

    fn name(&self) -> &str {
        "trigger_kinds"
    }

    fn description(&self) -> &str {
        "List the kinds of triggers and the `config` each one takes. Returns a JSON array, each item with `kind` and \
         `description`."
    }

    async fn call(&self, _ctx: ToolCtx<'_, RunScope>, _a: NoArgs) -> Result<ToolOutput, ToolError> {
        let kinds: Vec<KindInfo> =
            Kind::ALL.iter().map(|k| KindInfo { kind: *k, description: k.description() }).collect();
        Ok(agenter::tool::json(&kinds))
    }
}

#[derive(Deserialize, JsonSchema)]
pub struct ListArgs {
    /// List the finished ones instead — `once` triggers that already ran and are gone from the list above. Their
    /// `last_session_id` is the conversation they started, so this is how you look up what came of a one-off task.
    #[serde(default)]
    finished: bool,
}

pub struct TriggerList(TriggerService);

impl Tool<RunScope> for TriggerList {
    type Args = ListArgs;

    fn name(&self) -> &str {
        "trigger_list"
    }

    fn description(&self) -> &str {
        "List triggers: things that wake an agent (schedules, one-offs, streams). Returns a JSON array of triggers, \
         each with `name`, `kind`, `config`, `status` (kind-specific state such as the next firing time), `agent`, \
         `prompt`, `enabled`, `precheck`, `faults` (`source`, `error`), `last_session_id` and `finished_at` (ISO \
         8601; set once it will not fire again)."
    }

    async fn call(&self, _ctx: ToolCtx<'_, RunScope>, a: ListArgs) -> Result<ToolOutput, ToolError> {
        let triggers = self.0.list(Listing { finished: a.finished, ..Listing::default() }).await?;
        let names = names(&self.0).await?;
        let infos: Vec<TriggerInfo> = self.0.views(triggers).await?.into_iter().map(|v| info(v, &names)).collect();
        Ok(agenter::tool::json(&infos))
    }
}

#[derive(Deserialize, JsonSchema)]
pub struct CreateArgs {
    /// Lowercase letters, digits and `-`, e.g. `daily-report`.
    name: TriggerName,
    /// Trigger kind from `trigger_kinds`.
    kind: Kind,
    /// What to do on each firing.
    prompt: Prompt,
    /// Kind-specific settings, e.g. {"cron": "0 9 * * *"} for a schedule, {"at": "2026-10-22T10:30"} for a one-off
    /// `once` trigger (never a cron expression with a date: that fires again every year and stays in the list).
    config: Map<String, Value>,
    /// Name of the agent to run; defaults to you.
    #[serde(default)]
    agent: Option<String>,
    /// Optional command deciding whether to fire; see above.
    #[serde(default)]
    precheck: Option<Precheck>,
}

pub struct TriggerCreate {
    service: TriggerService,
    description: String,
}

impl Tool<RunScope> for TriggerCreate {
    type Args = CreateArgs;

    fn name(&self) -> &str {
        "trigger_create"
    }

    fn description(&self) -> &str {
        &self.description
    }

    async fn call(&self, ctx: ToolCtx<'_, RunScope>, a: CreateArgs) -> Result<ToolOutput, ToolError> {
        let agent_id = match a.agent {
            None => ctx.deps.agent_id.clone(),
            Some(name) => self.service.db().read(move |c| Ok::<_, Error>(named_in(c, &name)?.id)).await?,
        };
        let spec = NewSpec {
            name: a.name,
            agent_id,
            prompt: a.prompt,
            config: NewConfig::parse(a.kind, Value::Object(a.config))?,
            precheck: a.precheck,
        };
        let created = self.service.create(spec).await?;
        let view = self.service.view(created).await?;
        Ok(agenter::tool::text(format!(
            "created {} trigger {}; status: {}",
            view.trigger.mode.kind(),
            view.trigger.name,
            view.kind.status_json()
        )))
    }
}

#[derive(Deserialize, JsonSchema)]
pub struct UpdateArgs {
    /// Trigger name.
    name: TriggerName,
    /// New instructions for each firing.
    #[serde(default)]
    prompt: Option<Prompt>,
    /// New kind-specific settings (replaces the old config).
    #[serde(default)]
    config: Option<Map<String, Value>>,
    /// Pause (false) or resume (true).
    #[serde(default)]
    enabled: Option<bool>,
    /// New precheck (replaces the old one and resets its baseline); see `trigger_create`.
    #[serde(default)]
    precheck: Option<Precheck>,
    /// Remove the precheck so every firing runs the agent.
    #[serde(default)]
    remove_precheck: bool,
}

pub struct TriggerUpdate(TriggerService);

impl Tool<RunScope> for TriggerUpdate {
    type Args = UpdateArgs;

    fn name(&self) -> &str {
        "trigger_update"
    }

    fn description(&self) -> &str {
        "Change a trigger; only the given fields change. Disable it to pause without losing it."
    }

    async fn call(&self, _ctx: ToolCtx<'_, RunScope>, a: UpdateArgs) -> Result<ToolOutput, ToolError> {
        let precheck = match (a.precheck, a.remove_precheck) {
            (Some(_), true) => return Err(invalid("give either precheck or remove_precheck, not both").into()),
            (Some(p), false) => PrecheckChange::Set(p),
            (None, true) => PrecheckChange::Remove,
            (None, false) => PrecheckChange::Keep,
        };
        let t = self.0.get_by_name(&a.name).await?;
        let update = Update { prompt: a.prompt, config: a.config.map(Value::Object), enabled: a.enabled, precheck };
        let updated = self.0.update(t.id, update).await?;
        let state = if updated.enabled { "enabled" } else { "paused" };
        let view = self.0.view(updated).await?;
        Ok(agenter::tool::text(format!("updated trigger {} ({state}); status: {}", a.name, view.kind.status_json())))
    }
}

#[derive(Deserialize, JsonSchema)]
pub struct NameArgs {
    /// Trigger name.
    name: TriggerName,
}

pub struct TriggerRun(TriggerService);

impl Tool<RunScope> for TriggerRun {
    type Args = NameArgs;

    fn name(&self) -> &str {
        "trigger_run"
    }

    fn description(&self) -> &str {
        "Fire a trigger now (without an event body), starting a new conversation like any firing. Its precheck, if \
         any, runs first, so this also tests the precheck. This does not use up a `once` trigger: it still fires at \
         its own time."
    }

    async fn call(&self, _ctx: ToolCtx<'_, RunScope>, a: NameArgs) -> Result<ToolOutput, ToolError> {
        let t = self.0.get_by_name(&a.name).await?;
        Ok(agenter::tool::text(match self.0.run(t.id).await? {
            None => format!("precheck of {} found nothing to do (or recorded its baseline): not fired", a.name),
            Some(fired) => format!("fired trigger {}: session {}", a.name, fired.session_id),
        }))
    }
}

pub struct TriggerDelete(TriggerService);

impl Tool<RunScope> for TriggerDelete {
    type Args = NameArgs;

    fn name(&self) -> &str {
        "trigger_delete"
    }

    fn description(&self) -> &str {
        "Delete a trigger."
    }

    async fn call(&self, _ctx: ToolCtx<'_, RunScope>, a: NameArgs) -> Result<ToolOutput, ToolError> {
        let t = self.0.get_by_name(&a.name).await?;
        self.0.delete(t.id).await?;
        Ok(agenter::tool::text(format!("deleted trigger {}", a.name)))
    }
}

/// trigger_create 的说明：带着前置检查的时限（runtime.precheck_timeout_seconds），agent 写检查脚本时要知道。
fn create_description(precheck_timeout_seconds: f64) -> String {
    format!(
        "Create a trigger: each firing starts a new conversation of the agent with `prompt` (plus the event, if the \
         kind carries one). See `trigger_kinds` for the kinds and their `config`. Triggered runs do not notify the \
         user automatically: the prompt should say when to notify the user (`notify_user`, so that agent needs the \
         notify tools). To act elsewhere (comment on an issue, hand work to another agent), the prompt tells the \
         firing conversation to do it with its own tools.\n\n\
         For frequent checks (e.g. every minute) add a `precheck`, so the agent only runs when something happened: a \
         bash command run in the agent's workspace before each firing, with the event (a stream's lines) on stdin. \
         With fire_when \"output\", exit 0 with output fires and the output becomes the event the agent sees, exit 0 \
         with no output skips; with \"changed\", it fires only when the output differs from the last run and the \
         agent gets the diff plus the output (the first run only records a baseline). A non-zero exit, or running \
         longer than {} seconds (the command is then killed), is recorded in the trigger's `faults`; when the check \
         goes from healthy to failing, the trigger's agent gets one new conversation with the error to fix it \
         (`trigger_run` only reports a failure back to its caller). Keep the precheck cheap and well under that \
         limit, retries included; slow work (full scans, long retry loops) belongs in the agent's run, not in the \
         precheck. `faults` (a failing precheck, a stream command that keeps exiting) are cleared when the precheck \
         or config changes or the trigger is re-enabled, and also once that script works again.",
        format_seconds(precheck_timeout_seconds)
    )
}

/// 同 Python 的 `{:g}`：整数不带小数点
fn format_seconds(s: f64) -> String {
    if s.fract() == 0.0 { format!("{}", s as i64) } else { format!("{s}") }
}

/// 触发器领域的工具组。
pub fn tools(service: TriggerService, precheck_timeout_seconds: f64) -> Vec<BuiltinTool> {
    let create = TriggerCreate { service: service.clone(), description: create_description(precheck_timeout_seconds) };
    vec![
        builtin(TriggerKinds, ToolTraits::read()),
        builtin(TriggerList(service.clone()), ToolTraits::read()),
        builtin(create, ToolTraits::write()),
        builtin(TriggerUpdate(service.clone()), ToolTraits::write()),
        builtin(TriggerRun(service.clone()), ToolTraits::write()),
        builtin(TriggerDelete(service), ToolTraits::write()),
    ]
}
