//! 组装一次运行的指令（system prompt），以及每次运行开头那段运行上下文。纯函数。
//!
//! 两者的分工只有一条规则：**指令只放同一个会话的不同运行之间不会变的内容，随运行变化的放进运行上下文**。原因是缓存前缀：
//! 指令是请求的 system，缓存断点就放在它末尾，DeepSeek 等自动前缀缓存也从这里开始匹配。指令里只要有一个每次运行都不同的值
//! （最早是「当前时间」），整段历史每次运行都要重新写入缓存（写缓存比普通输入还贵）。运行上下文由会话领域放进这次运行新增
//! 的第一条请求，随消息入库、之后原样重放，旧消息保持字节不变。

use std::path::Path;

use chrono::{DateTime, Utc};
use chrono_tz::Tz;
use muliao_platform::tooling::Audience;

fn audience_note(audience: Audience) -> &'static str {
    match audience {
        Audience::User => "The user is notified with your final answer and may reply here.",
        Audience::Agent => {
            "Another agent is waiting for your final answer, which is returned to it; answer it directly and \
             completely. The user is not notified."
        }
        Audience::None => {
            "The user is NOT notified automatically: send them a notification (`notify_user`, if you have it) only \
             when there is something worth their attention."
        }
    }
}

/// 一次运行开头交给模型的运行上下文：必须随运行变化、因而不能放进指令的那几行。
/// - 当前时间：每次运行都不同。
/// - 回答交给谁：由这次运行处理的信件推导，同一个会话的不同运行会不同。
///
/// 会话来源的说明不在这里：它是会话创建时定下、整个会话不变的，留在指令里（origin_note）。
pub fn run_context(now: DateTime<Utc>, tz: Tz, audience: Audience) -> String {
    let local = now.with_timezone(&tz);
    format!(
        "[Current local time: {} ({})]\n[{}]",
        local.format("%Y-%m-%dT%H:%M%:z"),
        local.format("%Z"),
        audience_note(audience)
    )
}

/// 运行上下文接上这次运行开始时发现的问题（如连不上的 MCP server、没加载的资源文件），一条一行。
pub fn with_notices(run_context: &str, notices: &[String]) -> String {
    let mut lines: Vec<String> = Vec::new();
    if !run_context.is_empty() {
        lines.push(run_context.to_owned());
    }
    lines.extend(notices.iter().map(|n| format!("[{n}]")));
    lines.join("\n")
}

/// 指令的输入：只能是会话级、agent 级的稳定内容。
pub struct InstructionParts<'a> {
    pub agent_name: &'a str,
    pub agent_description: &'a str,
    pub workspace: &'a Path,
    /// 会话来源的说明（会话领域给出）
    pub origin_note: &'a str,
    /// 资源领域写的几段（记忆、skill 索引、Self-improvement，[`muliao_assets::run_assets::RunAssets::instruction_sections`]）
    pub assets: &'a [String],
    /// 压缩后的早期上下文摘要
    pub summary: Option<&'a str>,
}

pub fn build_instructions(p: &InstructionParts<'_>) -> String {
    let context = [
        format!("- You are agent {:?}: {}", p.agent_name, p.agent_description),
        format!("- Workspace (base for relative paths and shell cwd): {}", p.workspace.display()),
        format!("- {}", p.origin_note),
    ];
    let mut sections = vec![format!("## Runtime context\n{}", context.join("\n"))];
    sections.extend(p.assets.iter().cloned());
    if let Some(summary) = p.summary {
        sections.push(format!(
            "## Earlier in this session\nOlder messages of this session were compacted into this summary; the \
             messages after it follow in full.\n<summary>\n{summary}\n</summary>"
        ));
    }
    sections.join("\n\n")
}

#[cfg(test)]
mod tests {
    use std::path::PathBuf;

    use chrono::TimeZone;

    use super::*;

    #[test]
    fn run_context_has_local_time_and_audience() {
        let now = Utc.with_ymd_and_hms(2026, 11, 12, 11, 0, 30).unwrap();
        let text = run_context(now, chrono_tz::Asia::Shanghai, Audience::None);
        assert_eq!(
            text,
            "[Current local time: 2026-11-12T19:00+08:00 (CST)]\n[The user is NOT notified automatically: send them \
             a notification (`notify_user`, if you have it) only when there is something worth their attention.]"
        );
        assert_eq!(with_notices("ctx", &["a".into(), "b".into()]), "ctx\n[a]\n[b]");
        assert_eq!(with_notices("", &["a".into()]), "[a]");
    }

    #[test]
    fn instructions_put_context_assets_and_summary_in_order() {
        let ws = PathBuf::from("/w/id1");
        let text = build_instructions(&InstructionParts {
            agent_name: "me",
            agent_description: "does things",
            workspace: &ws,
            origin_note: "Started by the user.",
            assets: &["## Memory\nm".into(), "## Skill index\ns".into()],
            summary: Some("earlier"),
        });
        assert!(text.starts_with(
            "## Runtime context\n- You are agent \"me\": does things\n- Workspace (base for relative paths and shell \
             cwd): /w/id1\n- Started by the user.\n\n## Memory\nm\n\n## Skill index\ns\n\n## Earlier"
        ));
        assert!(text.ends_with("<summary>\nearlier\n</summary>"));
    }
}
