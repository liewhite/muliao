//! `<agent 名>/agent.toml` 的格式：读（[`parse`]）与平台自己写（[`render`]）在这一处。名字不在文件里：目录名就是名字；
//! 目录与数据库里 agent 的对应在读树（[`crate::tree`]）。
//!
//! ```toml
//! id = "143cd08d29b0"        # 把目录与 agent 绑在一起，不改
//! description = "…"
//! model = "claude:claude-x"  # <provider>:<模型名>
//! thinking = "high"          # 可省略，缺省 default
//! group = "交易"             # 可省略：不分组
//! ```

use std::fmt;

use serde::{Deserialize, Serialize};

pub const AGENT_FILE: &str = "agent.toml";
pub const GROUP_MAX: usize = 64;

/// 思考档位：default 不指定档位，按模型与服务端的默认；off 关闭；其余是 effort 档位。怎么换算由模型层决定。
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default, Serialize, Deserialize, schemars::JsonSchema)]
#[serde(rename_all = "lowercase")]
pub enum Thinking {
    #[default]
    Default,
    Off,
    Low,
    Medium,
    High,
    Xhigh,
}

impl Thinking {
    pub const ALL: [Thinking; 6] =
        [Thinking::Default, Thinking::Off, Thinking::Low, Thinking::Medium, Thinking::High, Thinking::Xhigh];

    pub fn as_str(self) -> &'static str {
        match self {
            Thinking::Default => "default",
            Thinking::Off => "off",
            Thinking::Low => "low",
            Thinking::Medium => "medium",
            Thinking::High => "high",
            Thinking::Xhigh => "xhigh",
        }
    }

    pub fn parse(value: &str) -> Option<Thinking> {
        Self::ALL.into_iter().find(|t| t.as_str() == value)
    }
}

impl fmt::Display for Thinking {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(self.as_str())
    }
}

/// agent.toml 的内容（校验过：只能经 [`parse`] 或 [`AgentFile::new`] 得到）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AgentFile {
    id: String,
    description: String,
    model: String,
    thinking: Thinking,
    group: Option<String>,
}

/// 能改的配置字段（id 不改）：新建时没给的取默认，修改时 None 为不变。
#[derive(Debug, Clone, Default)]
pub struct AgentConfig {
    pub description: Option<String>,
    pub model: Option<String>,
    pub thinking: Option<Thinking>,
    /// 分组名：Some("") 是不分组（界面与工具给的会去掉首尾空白）
    pub group: Option<String>,
}

fn invalid(reason: impl fmt::Display) -> String {
    format!("invalid {AGENT_FILE}: {reason}")
}

impl AgentFile {
    pub fn new(
        id: String,
        description: String,
        model: String,
        thinking: Thinking,
        group: Option<String>,
    ) -> Result<Self, String> {
        if description.trim().is_empty() {
            return Err(invalid("description must not be empty"));
        }
        if let Some(g) = &group
            && (g.is_empty() || g != g.trim() || g.chars().count() > GROUP_MAX)
        {
            return Err(invalid(format!(
                "group must be 1-{GROUP_MAX} characters without leading or trailing whitespace, got {g:?}"
            )));
        }
        Ok(Self { id, description, model, thinking, group })
    }

    /// 这份配置改上 config 里给出的字段之后的样子（id 不变）。
    pub fn with(&self, config: &AgentConfig) -> Result<Self, String> {
        let group = match &config.group {
            None => self.group.clone(),
            Some(g) => Some(g.trim().to_owned()).filter(|g| !g.is_empty()),
        };
        Self::new(
            self.id.clone(),
            config.description.clone().unwrap_or_else(|| self.description.clone()),
            config.model.clone().unwrap_or_else(|| self.model.clone()),
            config.thinking.unwrap_or(self.thinking),
            group,
        )
    }

    pub fn id(&self) -> &str {
        &self.id
    }
    pub fn description(&self) -> &str {
        &self.description
    }
    pub fn model(&self) -> &str {
        &self.model
    }
    pub fn thinking(&self) -> Thinking {
        self.thinking
    }
    pub fn group(&self) -> Option<&str> {
        self.group.as_deref()
    }
}

fn string(table: &toml::Table, key: &str) -> Result<Option<String>, String> {
    match table.get(key) {
        None => Ok(None),
        Some(toml::Value::String(s)) => Ok(Some(s.clone())),
        Some(_) => Err(invalid(format!("{key} must be a string"))),
    }
}

/// 文件里的键，按写出的顺序（读时只认这些，写时逐个写，[`AgentFile::fields`]）
const KEYS: [&str; 5] = ["id", "description", "model", "thinking", "group"];

impl AgentFile {
    /// 每个键要写成的值（None 为没有这个键：不分组）。顺序同 [`KEYS`]。
    fn fields(&self) -> [Option<&str>; 5] {
        [
            Some(&self.id),
            Some(&self.description),
            Some(&self.model),
            Some(self.thinking.as_str()),
            self.group.as_deref(),
        ]
    }
}

/// 不合格式时报错（原因）。
pub fn parse(text: &str) -> Result<AgentFile, String> {
    let table: toml::Table = text.parse().map_err(|e: toml::de::Error| invalid(e.message().trim()))?;
    let unknown: Vec<&String> = table.keys().filter(|k| !KEYS.contains(&k.as_str())).collect();
    if !unknown.is_empty() {
        return Err(invalid(format!("unknown keys {unknown:?}")));
    }
    let required = |key: &str| string(&table, key)?.ok_or_else(|| invalid(format!("{key} is required")));
    let thinking = match string(&table, "thinking")? {
        None => Thinking::Default,
        Some(t) => Thinking::parse(&t).ok_or_else(|| {
            invalid(format!("thinking must be one of {:?}, got {t:?}", Thinking::ALL.map(Thinking::as_str)))
        })?,
    };
    AgentFile::new(required("id")?, required("description")?, required("model")?, thinking, string(&table, "group")?)
}

/// 要写成 data 时的全文：current 是已有文件的原文（在它上面改，保留注释与别的排版），没有文件为 None。值没变的键原样
/// 不动；变了的换值，行尾注释留着。
pub fn render(current: Option<&str>, data: &AgentFile) -> Result<String, String> {
    let mut doc: toml_edit::DocumentMut = match current {
        Some(text) => text.parse().map_err(invalid)?,
        None => toml_edit::DocumentMut::new(),
    };
    for (key, value) in KEYS.into_iter().zip(data.fields()) {
        let Some(value) = value else {
            doc.remove(key);
            continue;
        };
        match doc.get_mut(key).and_then(|i| i.as_value_mut()) {
            Some(old) if old.as_str() == Some(value) => {}
            Some(old) => {
                let decor = old.decor().clone();
                *old = value.into();
                *old.decor_mut() = decor;
            }
            None => doc[key] = toml_edit::value(value),
        }
    }
    Ok(doc.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_and_render_keep_comments() {
        let text = "# 注释\nid = \"abc\"\ndescription = \"d\"  # 说明\nmodel = \"p:m\" # 模型\ngroup = \"g\"\n";
        let file = parse(text).unwrap();
        assert_eq!(file.thinking(), Thinking::Default);
        let config = AgentConfig { group: Some("  ".into()), description: Some("新".into()), ..Default::default() };
        let changed = file.with(&config).unwrap();
        let out = render(Some(text), &changed).unwrap();
        assert!(out.starts_with("# 注释\n"), "{out}");
        assert!(out.contains("description = \"新\"  # 说明\n"), "{out}");
        assert!(out.contains("model = \"p:m\" # 模型\n"), "{out}");
        assert!(!out.contains("group"));
        assert_eq!(parse(&out).unwrap(), changed);
        assert!(parse("id = \"a\"\ndescription = \" \"\nmodel = \"m\"").is_err());
        assert!(parse("id = \"a\"\ndescription = \"d\"\nmodel = \"m\"\nextra = 1").is_err());
        assert!(parse("id = \"a\"\ndescription = \"d\"\nmodel = \"m\"\nthinking = \"max\"").is_err());
        assert!(parse("id = \"a\"\ndescription = \"d\"\nmodel = \"m\"\ngroup = \" g\"").is_err());
        assert!(parse("id = 1\ndescription = \"d\"\nmodel = \"m\"").is_err());
    }
}
