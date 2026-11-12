//! skill 的 SKILL.md 与目录：frontmatter 的规则（description、name、approval），读树与写入口按它读、校验一个 skill 目录，
//! 以及界面整体改写 SKILL.md 时的拼法（[`compose`]）。frontmatter 的切法同 python-frontmatter：去掉首尾空白后第一行是
//! `---`（三个以上的 `-`），到下一行 `---` 为止是 YAML，之后是正文。

use std::path::Path;

use muliao_platform::fs::{RealPath, resolve_within};
use yaml_rust2::yaml::Hash;
use yaml_rust2::{Yaml, YamlEmitter, YamlLoader};

use super::{Draft, Loaded, modified, read};
use crate::kinds::Content;

pub const SKILL_FILE: &str = "SKILL.md";

/// skill 的内容：描述与执行前要批准的脚本（frontmatter 里列的，读树时已换成目录里存在的文件的身份，
/// [`approval_scripts`]）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SkillDir {
    pub description: String,
    pub approval: Vec<RealPath>,
}

/// SKILL.md 的 frontmatter（读树与界面的编辑都按它）。
#[derive(Debug, Clone)]
pub struct Frontmatter {
    pub description: String,
    /// 执行前要用户批准的脚本（相对 skill 目录的路径，#530）
    pub approval: Vec<String>,
}

fn is_boundary(line: &str) -> bool {
    let t = line.trim_end();
    t.len() >= 3 && t.bytes().all(|b| b == b'-')
}

/// 把 SKILL.md 切成 (frontmatter 的 YAML 文本, 正文)；没有 frontmatter 时 None（正文是去掉首尾空白的全文）。
fn split(text: &str) -> Option<(&str, &str)> {
    let text = text.trim();
    let first_end = text.find('\n').unwrap_or(text.len());
    if !is_boundary(&text[..first_end]) {
        return None;
    }
    let rest = text.get(first_end + 1..)?;
    let mut offset = 0;
    for line in rest.split_inclusive('\n') {
        if is_boundary(line.trim_end_matches(['\n', '\r'])) {
            return Some((&rest[..offset], rest[offset + line.len()..].trim()));
        }
        offset += line.len();
    }
    None
}

/// frontmatter 读成 YAML 映射；没有 frontmatter、或它不是映射时为空映射。YAML 写坏了报错。
fn metadata(text: &str, where_: &str) -> Result<Hash, String> {
    let Some((fm, _)) = split(text) else { return Ok(Hash::new()) };
    let docs = YamlLoader::load_from_str(fm).map_err(|e| format!("{where_}: cannot parse frontmatter: {e}"))?;
    Ok(match docs.into_iter().next() {
        Some(Yaml::Hash(h)) => h,
        _ => Hash::new(),
    })
}

/// 正文（frontmatter 之后，去掉首尾空白）。
pub fn body_of(text: &str) -> String {
    split(text).map(|(_, body)| body.to_owned()).unwrap_or_else(|| text.trim().to_owned())
}

fn key(name: &str) -> Yaml {
    Yaml::String(name.to_owned())
}

/// SKILL.md 的 frontmatter 规则：description 是非空字符串；有 name 时必须与 skill（目录名）同名；有 approval 时是相对
/// skill 目录的脚本路径列表。
pub fn parse_frontmatter(text: &str, name: &str) -> Result<Frontmatter, String> {
    let where_ = format!("{name}/{SKILL_FILE}");
    let meta = metadata(text, &where_)?;
    match meta.get(&key("name")) {
        None | Some(Yaml::Null) => {}
        Some(Yaml::String(n)) if n == name => {}
        Some(other) => {
            return Err(format!("{where_}: frontmatter name {} does not match {name:?}", show(other)));
        }
    }
    let Some(Yaml::String(description)) = meta.get(&key("description")) else {
        return Err(format!("{where_}: frontmatter must contain a string 'description'"));
    };
    let approval = match meta.get(&key("approval")) {
        None => Vec::new(),
        Some(value) => approval(value, &where_)?,
    };
    Ok(Frontmatter { description: valid_description(description)?, approval })
}

fn show(value: &Yaml) -> String {
    match value {
        Yaml::String(s) => format!("{s:?}"),
        Yaml::Integer(i) => i.to_string(),
        Yaml::Real(r) => r.clone(),
        Yaml::Boolean(b) => b.to_string(),
        other => format!("{other:?}"),
    }
}

fn approval(value: &Yaml, where_: &str) -> Result<Vec<String>, String> {
    let bad = || {
        format!(
            "{where_}: 'approval' must be a list of script paths relative to the skill directory, e.g. \
             [scripts/deploy.sh], got {}",
            show(value)
        )
    };
    let Yaml::Array(items) = value else { return Err(bad()) };
    let mut out = Vec::new();
    for item in items {
        let Yaml::String(p) = item else { return Err(bad()) };
        if p.trim().is_empty() {
            return Err(bad());
        }
        if p.starts_with('/') {
            return Err(format!("{where_}: approval path {p:?} must be relative to the skill directory"));
        }
        out.push(p.trim().to_owned());
    }
    Ok(out)
}

pub fn valid_description(description: &str) -> Result<String, String> {
    let d = description.trim();
    if d.is_empty() {
        return Err("skill description must not be empty".into());
    }
    Ok(d.to_owned())
}

/// frontmatter 的 approval 换成目录里的脚本文件（身份，skill_run 按它认要批准的脚本）。哪一项不是目录里存在的文件就
/// 报错：写错的一项若照样加载，那个脚本就静默地不用批准。
pub fn approval_scripts(directory: &Path, approval: &[String]) -> Result<Vec<RealPath>, String> {
    let dir_name = directory.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default();
    let where_ = format!("{dir_name}/{SKILL_FILE}");
    approval
        .iter()
        .map(|p| {
            let script = resolve_within(directory, p).map_err(|e| format!("{where_}: approval: {e}"))?;
            if !script.is_file() {
                return Err(format!(
                    "{where_}: approval path {p:?} is not a file in the skill directory (looked for {}); create the \
                     script first, or fix the path",
                    script.display()
                ));
            }
            Ok(script)
        })
        .collect()
}

/// 读树：path 是 skill 目录，修改时间取 SKILL.md 的。
pub fn load(path: &Path, name: &str, _memory_max_chars: usize) -> Result<Loaded, String> {
    let md = path.join(SKILL_FILE);
    if !md.is_file() {
        return Err(format!("skill directory has no {SKILL_FILE}"));
    }
    let fm = parse_frontmatter(&read(&md)?, name)?;
    let approval = approval_scripts(path, &fm.approval)?;
    Ok(Loaded {
        content: Content::Skill(SkillDir { description: fm.description, approval }),
        updated_at: modified(&md).map_err(|e| e.to_string())?,
        warning: None,
    })
}

/// 写入口：SKILL.md 按读树的规则校验；附件要 skill 已有 SKILL.md，否则写出来的是一个读树时加载不了的目录。
pub fn vet(draft: &Draft<'_>) -> Result<String, String> {
    if draft.inner == [SKILL_FILE] {
        let fm = parse_frontmatter(draft.text, draft.name)?;
        approval_scripts(&draft.item, &fm.approval)?;
        return Ok(draft.text.to_owned());
    }
    if !draft.item.join(SKILL_FILE).is_file() {
        return Err(format!(
            "{name}/ has no {SKILL_FILE}; write {name}/{SKILL_FILE} first, then its other files",
            name = draft.name
        ));
    }
    Ok(draft.text.to_owned())
}

/// 界面整体改写 SKILL.md：在 current（已有的全文，新建为 None）的 frontmatter 上设 name 与 description（别的键保留），
/// 正文换成 body。
pub fn compose(current: Option<&str>, name: &str, description: &str, body: &str) -> Result<String, String> {
    let mut meta = match current {
        Some(text) => metadata(text, &format!("{name}/{SKILL_FILE}"))?,
        None => Hash::new(),
    };
    meta.insert(key("name"), key(name));
    meta.insert(key("description"), key(&valid_description(description)?));
    let mut yaml = String::new();
    YamlEmitter::new(&mut yaml).dump(&Yaml::Hash(meta)).map_err(|e| format!("cannot write frontmatter: {e:?}"))?;
    let yaml = yaml.strip_prefix("---\n").unwrap_or(&yaml);
    Ok(format!("---\n{yaml}\n---\n\n{}\n", body.trim()))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn frontmatter_rules() {
        let ok = "\n---\nname: s\ndescription: \"  do x \"\napproval: [run.sh]\n---\n\nbody\n";
        let fm = parse_frontmatter(ok, "s").unwrap();
        assert_eq!(fm.description, "do x");
        assert_eq!(fm.approval, vec!["run.sh"]);
        assert_eq!(body_of(ok), "body");
        assert!(parse_frontmatter("---\nname: t\ndescription: d\n---\n", "s").unwrap_err().contains("does not match"));
        assert!(parse_frontmatter("no frontmatter", "s").unwrap_err().contains("'description'"));
        assert!(parse_frontmatter("---\ndescription: [a\n---\n", "s").unwrap_err().contains("cannot parse"));
        assert!(parse_frontmatter("---\ndescription: d\napproval: /x\n---\n", "s").is_err());
        assert!(parse_frontmatter("---\ndescription: d\napproval: [/x]\n---\n", "s").unwrap_err().contains("relative"));
        assert!(parse_frontmatter("---\nname:\ndescription: d\n---\n", "s").is_ok());
    }

    #[test]
    fn compose_keeps_other_keys() {
        let text = compose(Some("---\ndescription: old\nlicense: MIT\n---\nold body"), "s", "new", " body \n").unwrap();
        let fm = parse_frontmatter(&text, "s").unwrap();
        assert_eq!(fm.description, "new");
        assert!(text.contains("license: MIT"), "{text}");
        assert_eq!(body_of(&text), "body");
        assert!(text.ends_with("body\n"));
    }
}
