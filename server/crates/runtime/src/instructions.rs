//! 组装一次运行的指令（system prompt），以及每次运行开头那段运行上下文。纯函数。
//!
//! 两者的分工只有一条规则：**指令只放同一个会话的不同运行之间不会变的内容，随运行变化的放进运行上下文**。原因是缓存前缀：
//! 指令是请求的 system，缓存断点就放在它末尾，DeepSeek 等自动前缀缓存也从这里开始匹配。指令里只要有一个每次运行都不同的值
//! （最早是「当前时间」），整段历史每次运行都要重新写入缓存（写缓存比普通输入还贵）。运行上下文由会话领域放进这次运行新增
//! 的第一条请求，随消息入库、之后原样重放，旧消息保持字节不变。

use muliao_platform::ids::AgentId;
use std::path::Path;

use chrono::{DateTime, Utc};
use chrono_tz::Tz;
use muliao_assets::kinds::ASSETS_SKILL;
use muliao_assets::memory::MemoryBlock;
use muliao_assets::skills::SkillMeta;
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
    pub agent_id: &'a AgentId,
    pub agent_name: &'a str,
    pub agent_description: &'a str,
    pub workspace: &'a Path,
    /// agent 自己的资源目录
    pub asset_dir: &'a Path,
    /// 会话来源的说明（会话领域给出）
    pub origin_note: &'a str,
    pub memories: &'a [MemoryBlock],
    pub skills: &'a [SkillMeta],
    pub memory_limit: usize,
    /// 压缩后的早期上下文摘要
    pub summary: Option<&'a str>,
}

pub fn build_instructions(p: &InstructionParts<'_>) -> String {
    let context = [
        format!("- You are agent {:?}: {}", p.agent_name, p.agent_description),
        format!("- Workspace (base for relative paths and shell cwd): {}", p.workspace.display()),
        format!("- {}", p.origin_note),
    ];
    let skill_lines: Vec<String> = if p.skills.is_empty() {
        vec!["(no skills yet)".into()]
    } else {
        p.skills
            .iter()
            .map(|s| {
                let yours = if s.owner.is(p.agent_id) { " (yours)" } else { "" };
                format!("- `{}`{yours}: {} ({})", s.name, s.description, s.path.display())
            })
            .collect()
    };
    let assets_root = p.asset_dir.parent().unwrap_or(p.asset_dir);
    let mut sections = vec![
        format!("## Runtime context\n{}", context.join("\n")),
        memory_section(p.memories, p.memory_limit),
        format!("## Skill index\n{}", skill_lines.join("\n")),
        self_improvement(p.memory_limit, p.asset_dir, assets_root),
    ];
    if let Some(summary) = p.summary {
        sections.push(format!(
            "## Earlier in this session\nOlder messages of this session were compacted into this summary; the \
             messages after it follow in full.\n<summary>\n{summary}\n</summary>"
        ));
    }
    sections.join("\n\n")
}

fn self_improvement(memory_limit: usize, asset_dir: &Path, assets_root: &Path) -> String {
    format!(
        "## Self-improvement\n\
Everything you learn persists only as memory or skills. Both are plain files: yours are in {asset_dir} \
(shared ones elsewhere under {assets_root}). Change them with the file tools like any other file; changes \
take effect from your next run. The layout, file formats, permissions and what to do when a write is \
refused are in the `{ASSETS_SKILL}` skill.\n\
- Memory: the memory blocks above, each limited to {memory_limit} characters and shown with its file: your \
private one, the global one every agent shares, and any shared by your roles. Edit a block marked editable \
(its description says what belongs in memory and what goes into a skill); fix or condense entries the same \
way. Put a fact in the narrowest block it holds for: your private block unless it holds for everyone who \
shares the other block.\n\
- Skills are reusable procedures: a directory with a SKILL.md (frontmatter `description`: when to use it) \
plus any attachments. Before a task that matches a skill in the index, read its SKILL.md. After working out \
a non-trivial procedure likely to recur, create `skills/<name>/SKILL.md` in your directory (a new skill is \
yours; other agents get it only through a role); when a skill proves wrong or incomplete, fix that file.\n\
- Secrets belong in the vault (`vault_set`), referenced by key; never write secret values into files, memory \
or skills.\n\
- Keep state you need across runs (cursors, caches) in files in your workspace.",
        asset_dir = asset_dir.display(),
        assets_root = assets_root.display(),
    )
}

/// 这个 agent 能用的每一块记忆，标出名字与能不能改。界面的 agent 详情用它预览注入的样子，与运行时同一个函数。
pub fn memory_section(memories: &[MemoryBlock], limit: usize) -> String {
    let blocks: Vec<String> = if memories.is_empty() {
        vec!["(no memory blocks)".into()]
    } else {
        memories
            .iter()
            .map(|m| {
                let content = if m.content.is_empty() { "(empty)" } else { m.content.as_str() };
                format!(
                    "<memory name=\"{}\" access=\"{}\" file=\"{}\">\n{content}\n</memory>",
                    escape(&m.name),
                    access_of(m),
                    escape(&m.path.display().to_string())
                )
            })
            .collect()
    };
    format!(
        "## Memory\nThe memory blocks you use, by name (each limited to {limit} characters); change an editable one \
         by editing its file.\n{}",
        blocks.join("\n")
    )
}

fn access_of(m: &MemoryBlock) -> &'static str {
    if m.yours {
        "editable, your private memory"
    } else if m.editable {
        "editable, shared"
    } else {
        "read-only, shared"
    }
}

/// 属性值的转义（同 Python 的 html.escape）。
fn escape(s: &str) -> String {
    s.replace('&', "&amp;").replace('<', "&lt;").replace('>', "&gt;").replace('"', "&quot;").replace('\'', "&#x27;")
}

#[cfg(test)]
mod tests {
    use std::path::PathBuf;

    use chrono::TimeZone;
    use muliao_assets::tree::Owner;

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
    fn instructions_list_memory_skills_and_summary() {
        let memories = vec![
            MemoryBlock {
                name: "global".into(),
                content: "be <brief>".into(),
                editable: true,
                yours: false,
                path: "/a/platform/memory/global.md".into(),
            },
            MemoryBlock {
                name: "me".into(),
                content: String::new(),
                editable: true,
                yours: true,
                path: "/a/me/memory.md".into(),
            },
        ];
        let skills = vec![
            SkillMeta {
                name: "s1".into(),
                description: "d1".into(),
                owner: Owner::Agent(AgentId::new("id1")),
                path: "/a/me/skills/s1/SKILL.md".into(),
            },
            SkillMeta {
                name: "s2".into(),
                description: "d2".into(),
                owner: Owner::Platform,
                path: "/a/platform/skills/s2/SKILL.md".into(),
            },
        ];
        let ws = PathBuf::from("/w/id1");
        let dir = PathBuf::from("/a/me");
        let text = build_instructions(&InstructionParts {
            agent_id: &AgentId::new("id1"),
            agent_name: "me",
            agent_description: "does things",
            workspace: &ws,
            asset_dir: &dir,
            origin_note: "Started by the user.",
            memories: &memories,
            skills: &skills,
            memory_limit: 4000,
            summary: Some("earlier"),
        });
        assert!(text.starts_with(
            "## Runtime context\n- You are agent \"me\": does things\n- Workspace (base for relative paths and shell \
             cwd): /w/id1\n- Started by the user.\n\n## Memory\n"
        ));
        // 只转义属性，正文原样
        assert!(text.contains(
            "<memory name=\"global\" access=\"editable, shared\" file=\"/a/platform/memory/global.md\">\nbe <brief>\n</memory>"
        ));
        assert!(
            text.contains("access=\"editable, your private memory\" file=\"/a/me/memory.md\">\n(empty)\n</memory>")
        );
        assert!(
            text.contains("- `s1` (yours): d1 (/a/me/skills/s1/SKILL.md)\n- `s2`: d2 (/a/platform/skills/s2/SKILL.md)")
        );
        assert!(text.contains("yours are in /a/me (shared ones elsewhere under /a)"));
        assert!(text.ends_with("<summary>\nearlier\n</summary>"));
    }

    #[test]
    fn attribute_values_are_escaped() {
        assert_eq!(escape("<a&'\">"), "&lt;a&amp;&#x27;&quot;&gt;");
    }
}
