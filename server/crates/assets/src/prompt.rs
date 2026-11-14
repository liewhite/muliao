//! 资源在给模型的文字里的写法：指令里的记忆、skill 索引、Self-improvement 三段，运行开头「资源文件没加载」的提示。资源
//! 目录的布局、记忆的上限、skill 的格式都是本领域的事，所以讲它们的文字也只在这里写；运行端口只把这几段拼进指令。

use muliao_platform::ids::AgentId;
use std::path::Path;

use crate::kinds::ASSETS_SKILL;
use crate::memory::{MemoryAccess, MemoryBlock};
use crate::skills::SkillMeta;
use crate::tree::Problem;

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

/// skill 索引：名字、什么时候用、SKILL.md 在哪；agent 自己的标 (yours)。
pub(crate) fn skill_index(skills: &[SkillMeta], agent_id: &AgentId) -> String {
    let lines: Vec<String> = if skills.is_empty() {
        vec!["(no skills yet)".into()]
    } else {
        skills
            .iter()
            .map(|s| {
                let yours = if s.owner.is(agent_id) { " (yours)" } else { "" };
                format!("- `{}`{yours}: {} ({})", s.name, s.description, s.path.display())
            })
            .collect()
    };
    format!("## Skill index\n{}", lines.join("\n"))
}

/// 教 agent 把学到的东西写成记忆与 skill：它自己的资源目录在哪、整棵资源树在哪、记忆的上限。
pub(crate) fn self_improvement(memory_limit: usize, asset_dir: &Path, assets_root: &Path) -> String {
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

/// 运行开头告诉 agent：与它相关的这个文件没加载，去修。
pub(crate) fn problem_notice(p: &Problem) -> String {
    format!(
        "Asset file not loaded: {}: {}. Fix the file (or tell an admin if you cannot write it); formats are in the \
         {ASSETS_SKILL} skill.",
        p.path.display(),
        p.reason
    )
}

fn access_of(m: &MemoryBlock) -> &'static str {
    match m.access {
        MemoryAccess::Own => "editable, your private memory",
        MemoryAccess::Editable => "editable, shared",
        MemoryAccess::ReadOnly => "read-only, shared",
    }
}

/// 属性值的转义（同 Python 的 html.escape）。
fn escape(s: &str) -> String {
    s.replace('&', "&amp;").replace('<', "&lt;").replace('>', "&gt;").replace('"', "&quot;").replace('\'', "&#x27;")
}

#[cfg(test)]
mod tests {
    use std::path::PathBuf;

    use crate::tree::Owner;

    use super::*;

    #[test]
    fn memory_skills_and_layout() {
        let memories = vec![
            MemoryBlock {
                name: "global".into(),
                content: "be <brief>".into(),
                access: MemoryAccess::Editable,
                path: "/a/platform/memory/global.md".into(),
            },
            MemoryBlock {
                name: "me".into(),
                content: String::new(),
                access: MemoryAccess::Own,
                path: "/a/me/memory.md".into(),
            },
        ];
        let memory = memory_section(&memories, 4000);
        assert!(memory.starts_with("## Memory\n"));
        // 只转义属性，正文原样
        assert!(memory.contains(
            "<memory name=\"global\" access=\"editable, shared\" file=\"/a/platform/memory/global.md\">\nbe <brief>\n</memory>"
        ));
        assert!(
            memory.contains("access=\"editable, your private memory\" file=\"/a/me/memory.md\">\n(empty)\n</memory>")
        );
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
        assert_eq!(
            skill_index(&skills, &AgentId::new("id1")),
            "## Skill index\n- `s1` (yours): d1 (/a/me/skills/s1/SKILL.md)\n- `s2`: d2 (/a/platform/skills/s2/SKILL.md)"
        );
        let text = self_improvement(4000, &PathBuf::from("/a/me"), &PathBuf::from("/a"));
        assert!(text.contains("yours are in /a/me (shared ones elsewhere under /a)"));
    }

    #[test]
    fn attribute_values_are_escaped() {
        assert_eq!(escape("<a&'\">"), "&lt;a&amp;&#x27;&quot;&gt;");
    }
}
