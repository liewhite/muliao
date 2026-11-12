//! 触发方式的多态：一种方式 = 一个 [`TriggerKind`]（纯逻辑：校验配置、推导状态、给界面看的状态）+ 它的激活机制
//! （按到期时间的实现 [`DueRule`]，由 [`crate::due::DueRunner`] 统一扫描；流式有自己的托管 [`crate::stream::StreamRunner`]）。
//! 加一种方式只加代码：一个实现，在 [`crate::TriggerDomain::build`] 里登记；表、工具、入口不用改。

use std::sync::Arc;

use chrono::{DateTime, Utc};
use muliao_platform::db::Tx;
use serde_json::Value;

use crate::error::{Result, invalid};
use crate::store::Trigger;

/// 由配置推导出的东西：规范化后的配置与这种方式的初始运行状态。
#[derive(Debug, Clone, PartialEq)]
pub struct Configured {
    pub config: Value,
    pub state: Value,
}

/// 一种触发方式的纯逻辑部分。
pub trait TriggerKind: Send + Sync {
    /// 种类名（存在 `triggers.kind`）
    fn name(&self) -> &'static str;

    /// 给模型与界面看的说明：什么时候触发、config 的格式
    fn description(&self) -> &str;

    /// 校验并规范化 config，推导初始 state。创建、改配置（规范化后与原配置不同才算）、重新启用时调用；配置不合法是
    /// 领域错误 InvalidInput。
    fn configure(&self, config: &Value, now: DateTime<Utc>) -> Result<Configured>;

    /// 规范化后的 config 要交给命令的 vault key（没有就是空）；创建、改配置、重新启用时据此核对 secret 可用。
    fn secrets(&self, _config: &Value) -> Vec<String> {
        Vec::new()
    }

    /// 给界面与模型看的运行状态（如下次触发时间），不含凭证。存下的数据坏了是 [`crate::error::Error::Corrupt`]。
    fn status(&self, t: &Trigger) -> Result<Value>;
}

/// 按到期时间触发的一种方式的激活规则（定时、一次性）：到期时间从哪来、这次到期被用掉之后这一行怎么变。
pub trait DueRule: Send + Sync {
    fn kind(&self) -> &'static str;

    /// 下次到期的时间。存下的数据坏了是 Corrupt（扫描只停用这一个）。
    fn due_at(&self, t: &Trigger) -> Result<DateTime<Utc>>;

    /// 在到期的事务里消耗这次到期：改这一行（调用方写回），使它不再在 now 到期（定时推进到下次，一次性的结束）。返回
    /// Some(原因) 表示这次不触发（记日志）；这次到期无论如何都已被消耗。
    fn consume_in(&self, tx: &mut Tx<'_>, t: &mut Trigger, now: DateTime<Utc>) -> Result<Option<String>>;
}

/// 登记的全部方式。
#[derive(Clone)]
pub struct Kinds(Vec<Arc<dyn TriggerKind>>);

impl Kinds {
    /// 同名登记两次是装配错误（panic）。
    pub fn new(kinds: Vec<Arc<dyn TriggerKind>>) -> Self {
        for (i, k) in kinds.iter().enumerate() {
            assert!(kinds[..i].iter().all(|o| o.name() != k.name()), "trigger kind {:?} registered twice", k.name());
        }
        Self(kinds)
    }

    pub fn all(&self) -> &[Arc<dyn TriggerKind>] {
        &self.0
    }

    /// 按名字找；不认识是调用方的输入错误。
    pub fn get(&self, name: &str) -> Result<&Arc<dyn TriggerKind>> {
        self.0.iter().find(|k| k.name() == name).ok_or_else(|| {
            let names: Vec<&str> = self.0.iter().map(|k| k.name()).collect();
            invalid(format!("unknown trigger kind {name:?}; available: {names:?}"))
        })
    }
}

/// 按这种方式的配置类型校验 config（未知字段报错），不合法时报 InvalidInput。
pub fn parse_config<T: serde::de::DeserializeOwned>(kind: &str, config: &Value) -> Result<T> {
    serde_json::from_value(config.clone()).map_err(|e| invalid(format!("invalid {kind} trigger config {config}: {e}")))
}

/// 存下的时间（配置时总是带时区写入）。
pub fn stored_time(t: &Trigger, value: &str, what: &str) -> Result<DateTime<Utc>> {
    DateTime::parse_from_rfc3339(value).map(|d| d.with_timezone(&Utc)).map_err(|e| {
        crate::error::Error::Corrupt(format!("trigger {} ({}) has no valid {what}: {value:?}: {e}", t.id, t.name))
    })
}
