//! 触发方式。种类都在本 crate、由自己加，所以是 enum（docs/architecture.md 第 14 节）：
//! - [`Kind`]：种类本身（名字、给模型与界面的说明）。
//! - [`NewConfig`]：建、改时交进来的配置，按种类解析；[`NewConfig::configure`] 规范化并推导初始运行状态，得到 [`Mode`]。
//! - [`Mode`]：一个触发器存下的种类、配置与运行状态（`kind`、`config`、`state` 三列），读出时一次解析：读得出来就是合法的，
//!   读不出来是这一行坏了（整次查询报错），没有「坏了自动停用」。
//! - [`KindView`]：给界面与模型看的配置与运行状态。
//!
//! 加一种方式 = 每个 enum 加一个变体，编译器列出所有要跟着补的 match（激活机制、状态）；接口描述随类型生成，表与工具不用改。

use chrono::{DateTime, Utc};
use chrono_tz::Tz;
use schemars::JsonSchema;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

use crate::once::{self, OnceConfig, OnceInput, OnceStatus};
use crate::schedule::{self, ScheduleConfig, ScheduleState, ScheduleStatus};
use crate::store::Trigger;
use crate::stream::{self, Hosting, StreamConfig, StreamConfigOut, StreamState, StreamStatus};
use muliao_platform::errors::{Result, invalid};

/// 触发方式的种类。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, JsonSchema, utoipa::ToSchema)]
#[serde(rename_all = "snake_case")]
pub enum Kind {
    Schedule,
    Once,
    Stream,
}

muliao_platform::serde_names!(Kind);

impl Kind {
    pub const ALL: [Kind; 3] = [Kind::Schedule, Kind::Once, Kind::Stream];

    /// 给模型与界面看的说明：什么时候触发、config 的格式
    pub fn description(self) -> &'static str {
        match self {
            Kind::Schedule => schedule::DESCRIPTION,
            Kind::Once => once::DESCRIPTION,
            Kind::Stream => stream::DESCRIPTION,
        }
    }
}

/// 建、改时交进来的配置（未知字段报错）。
#[derive(Debug, Clone, Deserialize, utoipa::ToSchema)]
#[serde(tag = "kind", content = "config", rename_all = "snake_case")]
pub enum NewConfig {
    Schedule(ScheduleConfig),
    Once(OnceInput),
    Stream(StreamConfig),
}

impl NewConfig {
    /// 按种类解析 config；不合法是调用方的输入错误。
    pub fn parse(kind: Kind, config: Value) -> Result<Self> {
        serde_json::from_value(json!({ "kind": kind, "config": config }))
            .map_err(|e| invalid(format!("invalid {kind} trigger config: {e}")))
    }

    /// 规范化并推导初始运行状态。创建、改配置、重新启用时调用：定时的下次时间从 now 算起，流式换一个 generation（重起进程）。
    pub fn configure(self, tz: Tz, now: DateTime<Utc>) -> Result<Mode> {
        Ok(match self {
            NewConfig::Schedule(config) => Mode::Schedule { state: schedule::initial(&config, tz, now)?, config },
            NewConfig::Once(input) => Mode::Once { config: once::configure(&input, tz, now)? },
            NewConfig::Stream(config) => Mode::Stream { config, state: StreamState::fresh() },
        })
    }
}

/// 一个触发器存下的种类、规范化的配置与这种方式随运行变化的状态。
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum Mode {
    Schedule {
        config: ScheduleConfig,
        state: ScheduleState,
    },
    /// 到期时间就是配置本身，没有随运行变化的状态（state 列存 `{}`）。读出时 state 作为未知字段被忽略：本 enum
    /// 不能加 deny_unknown_fields（迁移测试守着）
    Once {
        config: OnceConfig,
    },
    Stream {
        config: StreamConfig,
        state: StreamState,
    },
}

impl Mode {
    pub fn kind(&self) -> Kind {
        match self {
            Mode::Schedule { .. } => Kind::Schedule,
            Mode::Once { .. } => Kind::Once,
            Mode::Stream { .. } => Kind::Stream,
        }
    }

    /// 从 kind、config、state 三列读出。
    pub(crate) fn from_columns(kind: Kind, config: &str, state: &str) -> serde_json::Result<Self> {
        let (config, state): (Value, Value) = (serde_json::from_str(config)?, serde_json::from_str(state)?);
        serde_json::from_value(json!({ "kind": kind, "config": config, "state": state }))
    }

    /// 写成 kind、config、state 三列。
    pub(crate) fn to_columns(&self) -> (Kind, String, String) {
        let mut v = serde_json::to_value(self).expect("trigger mode serializes");
        let state = v.as_object_mut().and_then(|o| o.remove("state")).unwrap_or_else(|| json!({}));
        (self.kind(), v["config"].to_string(), state.to_string())
    }

    /// 按存下的配置重新交进来（重新启用时从头推导）。
    pub fn input(&self) -> NewConfig {
        match self {
            Mode::Schedule { config, .. } => NewConfig::Schedule(config.clone()),
            Mode::Once { config } => NewConfig::Once(config.input()),
            Mode::Stream { config, .. } => NewConfig::Stream(config.clone()),
        }
    }

    /// 配置相同（不论状态）：规范化后没变的配置不算改动。
    pub fn same_config(&self, other: &Mode) -> bool {
        match self {
            Mode::Schedule { config, .. } => matches!(other, Mode::Schedule { config: o, .. } if o == config),
            Mode::Once { config } => matches!(other, Mode::Once { config: o } if o == config),
            Mode::Stream { config, .. } => matches!(other, Mode::Stream { config: o, .. } if o == config),
        }
    }

    /// 要交给命令的 vault key：创建、改配置、重新启用时据此核对 secret 可用。
    pub fn secrets(&self) -> &[String] {
        match self {
            Mode::Stream { config, .. } => &config.secrets,
            Mode::Schedule { .. } | Mode::Once { .. } => &[],
        }
    }

    /// 按到期时间触发的那部分（定时、一次性）；流式不按到期时间。
    pub fn due(&mut self) -> Option<Due<'_>> {
        match self {
            Mode::Schedule { config, state } => Some(Due::Schedule { config, state }),
            Mode::Once { config } => Some(Due::Once { config }),
            Mode::Stream { .. } => None,
        }
    }
}

/// 按到期时间触发的一种（[`crate::due`] 只对它 match）。
pub enum Due<'a> {
    Schedule { config: &'a ScheduleConfig, state: &'a mut ScheduleState },
    Once { config: &'a OnceConfig },
}

impl Due<'_> {
    pub fn at(&self) -> DateTime<Utc> {
        match self {
            Due::Schedule { state, .. } => state.next_run_at.to_utc(),
            Due::Once { config } => config.at.to_utc(),
        }
    }
}

/// 给界面与模型看的配置与运行状态（不含凭证）。
#[derive(Debug, Clone, Serialize, utoipa::ToSchema)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum KindView {
    Schedule { config: ScheduleConfig, status: ScheduleStatus },
    Once { config: OnceConfig, status: OnceStatus },
    Stream { config: StreamConfigOut, status: StreamStatus },
}

impl KindView {
    pub fn of(t: &Trigger, hosting: &Hosting) -> Self {
        match &t.mode {
            Mode::Schedule { config, state } => {
                KindView::Schedule { config: config.clone(), status: schedule::status(state) }
            }
            Mode::Once { config } => KindView::Once { config: config.clone(), status: once::status(config) },
            Mode::Stream { config, .. } => {
                KindView::Stream { config: config.into(), status: stream::status(&t.name, hosting.process(t.id)) }
            }
        }
    }

    /// 只有运行状态（JSON）。
    pub fn status_json(&self) -> String {
        let status = match self {
            KindView::Schedule { status, .. } => serde_json::to_value(status),
            KindView::Once { status, .. } => serde_json::to_value(status),
            KindView::Stream { status, .. } => serde_json::to_value(status),
        };
        status.expect("trigger status serializes").to_string()
    }
}
