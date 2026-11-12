//! 主动通知用户的工具（notify_user）。

use agenter::{Tool, ToolCtx, ToolError, ToolOutput};
use muliao_platform::tool_views::{ToolRender, ViewBlock};
use muliao_platform::tooling::{BuiltinTool, RunScope, ToolTraits, builtin};
use schemars::JsonSchema;
use serde::Deserialize;

use crate::inbox::{Notifications, Notify};

#[derive(Deserialize, JsonSchema)]
pub struct NotifyArgs {
    /// Short title.
    title: String,
    /// Notification text, a few sentences at most.
    body: String,
}

pub struct NotifyUser {
    notifications: Notifications,
}

impl Tool<RunScope> for NotifyUser {
    type Args = NotifyArgs;

    fn name(&self) -> &str {
        "notify_user"
    }

    fn description(&self) -> &str {
        "Send a push notification to the user's devices. Tapping it opens this session."
    }

    async fn call(&self, ctx: ToolCtx<'_, RunScope>, a: NotifyArgs) -> Result<ToolOutput, ToolError> {
        let n = Notify { title: a.title, body: a.body, session_id: Some(ctx.deps.session_id.clone()), issue_id: None };
        // 写库失败是系统自己的问题（模块说明里的统一规则）：整次运行失败，让人看见
        self.notifications.notify(n).await.map_err(|e| ToolError::Abort(e.into()))?;
        Ok(agenter::tool::text("queued notification to the user"))
    }
}

/// 通知领域的工具组。
pub fn tools(notifications: Notifications) -> Vec<BuiltinTool> {
    let render = ToolRender::new()
        .call(|a: &NotifyArgs| Some(vec![ViewBlock::Markdown { title: Some(a.title.clone()), text: a.body.clone() }]));
    vec![builtin(NotifyUser { notifications }, ToolTraits::write().render(render))]
}
