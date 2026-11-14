//! 触发器脚本出错：前置检查失败、流式命令反复退出（docs/architecture.md 第 14 节「脚本出错」）。
//!
//! 这两种都是触发器所属 agent 写的脚本坏了，记录、清除、告警的规则只在这里写一次：
//! - 记录（[`Faults::report_in`]）：存在触发器的 faults 上，同一来源只留一条，原因取最新；只有新出现的才告警，所以每分钟
//!   检查一次的触发器坏了不会每分钟告警。
//! - 清除：来源自己的恢复信号清本来源（[`recovered`]：检查成功、流式命令稳定运行）；人工干预（换前置检查、改配置、重新启用）
//!   清全部（[`reset`]）。
//! - 告警去向（[`Faults::alert_in`]）：交给触发器所属的 agent 开一个新会话，由它修脚本；投不进去（agent 已归档、正在删除）
//!   时退给用户推送，不静默吞掉。
//!
//! `Trigger::faults` 只在这里改。

use chrono::Utc;
use chrono_tz::Tz;
use muliao_notifications::{Notifications, Notify};
use muliao_platform::db::Tx;
use muliao_platform::text::truncate;
use muliao_sessions::intake::inbound::{SenderInbox, SenderMail, SenderTarget};
use muliao_sessions::intake::origins::Sender;
use serde::{Deserialize, Serialize};

use crate::store::Trigger;
use muliao_platform::errors::Result;

/// 告警推给用户时正文最多这么多字符
const SUMMARY_MAX: usize = 500;

/// 哪个脚本出的错。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize, schemars::JsonSchema, utoipa::ToSchema)]
#[serde(rename_all = "snake_case")]
pub enum FaultSource {
    Precheck,
    Stream,
}

impl FaultSource {
    /// 告警会话的标题（跟在触发器名后面）
    fn title(self) -> &'static str {
        match self {
            FaultSource::Precheck => "前置检查出错",
            FaultSource::Stream => "流式命令反复退出",
        }
    }
}

/// 一条出错：来源、原因。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize, schemars::JsonSchema, utoipa::ToSchema)]
#[serde(deny_unknown_fields)]
pub struct TriggerFault {
    pub source: FaultSource,
    pub error: String,
}

/// 记下这条出错：同一来源只留一条、原因取最新；返回它是不是新出现的（要告警）。
fn record(t: &mut Trigger, fault: TriggerFault) -> bool {
    match t.faults.iter_mut().find(|f| f.source == fault.source) {
        Some(known) => {
            *known = fault;
            false
        }
        None => {
            t.faults.push(fault);
            true
        }
    }
}

/// 这个来源恢复了（检查成功、流式命令稳定运行）：清掉它的出错，再坏时重新告警。
pub fn recovered(t: &mut Trigger, source: FaultSource) {
    let before = t.faults.len();
    t.faults.retain(|f| f.source != source);
    if t.faults.len() != before {
        tracing::info!(trigger = %t.name, ?source, "trigger script recovered");
    }
}

/// 人工干预（换前置检查、改配置、重新启用）：旧的错不再作数，全部清掉，再出错重新告警。
pub fn reset(t: &mut Trigger) {
    t.faults.clear();
}

/// 告警的投递：交给所属 agent，投不进退给用户。
#[derive(Clone)]
pub struct Faults {
    inbox: SenderInbox,
    notifications: Notifications,
    tz: Tz,
}

impl Faults {
    pub fn new(inbox: SenderInbox, notifications: Notifications, tz: Tz) -> Self {
        Self { inbox, notifications, tz }
    }

    /// 在调用方事务里记下这条出错（调用方写回这一行）；新出现时用 letter(触发器) 写信告警一次。
    pub fn report_in(
        &self,
        tx: &mut Tx<'_>,
        t: &mut Trigger,
        fault: TriggerFault,
        letter: impl FnOnce(&Trigger) -> String,
    ) -> Result<()> {
        tracing::warn!(trigger = %t.name, source = ?fault.source, error = %fault.error, "trigger script failed");
        let (source, error) = (fault.source, fault.error.clone());
        if record(t, fault) {
            let letter = letter(t);
            self.alert_in(tx, t, source.title(), letter, &error)?;
        }
        Ok(())
    }

    /// 告诉触发器所属的 agent（新会话，letter 说明出了什么错、怎么处理）；投不进去（它已归档或正在删除）时退给用户推送
    /// （title 与 summary）：不论哪种原因，告警都不能静默丢掉。
    pub fn alert_in(&self, tx: &mut Tx<'_>, t: &Trigger, title: &str, letter: String, summary: &str) -> Result<()> {
        let mail = SenderMail {
            sender: Sender::Trigger { trigger_id: t.id },
            content: letter,
            session: SenderTarget::New {
                agent_id: t.agent_id.clone(),
                title: format!("{} {title} · {}", t.name, local_minute(Utc::now(), self.tz)),
            },
        };
        if let Err(refused) = self.inbox.receive_in(tx, mail)? {
            tracing::warn!(trigger = %t.name, reason = ?refused.reason, detail = %refused.detail,
                "trigger alert could not reach the agent, notifying the user");
            self.notifications.notify_in(
                tx,
                &Notify {
                    title: format!("触发器 {} {title}", t.name),
                    body: truncate(summary, SUMMARY_MAX),
                    session_id: None,
                    link: None,
                },
            )?;
        }
        Ok(())
    }
}

/// 给人看的本地时间（精确到分钟）：触发器开的会话标题。
pub fn local_minute(at: chrono::DateTime<Utc>, tz: Tz) -> String {
    at.with_timezone(&tz).format("%Y-%m-%d %H:%M").to_string()
}
