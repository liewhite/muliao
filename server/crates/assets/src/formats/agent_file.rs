//! `<agent 名>/agent.toml` 的格式：读（[`parse`]）与平台自己写（[`render`]）在这一处。名字与 id 都不在文件里：目录名
//! 就是名字，读树（[`crate::tree`]）按目录名对上数据库里的 agent。
//!
//! ```toml
//! description = "…"
//! model = "claude:claude-x"  # <provider>:<模型名>
//! thinking = "high"          # 可省略，缺省 default
//! group = "交易"             # 可省略：不分组
//! ```

use std::fmt;

use muliao_platform::config::ModelRef;
use serde::{Deserialize, Serialize};

pub const AGENT_FILE: &str = "agent.toml";
pub const GROUP_MAX: usize = 64;

/// 思考档位：default 不指定档位，按模型与服务端的默认；off 关闭；其余是 effort 档位。怎么换算由模型层决定。
#[derive(
    Debug, Clone, Copy, PartialEq, Eq, Default, Serialize, Deserialize, schemars::JsonSchema, utoipa::ToSchema,
)]
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

muliao_platform::serde_names!(Thinking);

/// agent.toml 的内容（校验过：只能经 [`parse`] 或 [`AgentFile::new`] 得到）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AgentFile {
    description: String,
    model: ModelRef,
    thinking: Thinking,
    group: Option<String>,
}

/// 能改的配置字段：新建时没给的取默认，修改时 None 为不变。
#[derive(Debug, Clone, Default)]
pub struct AgentConfig {
    pub description: Option<String>,
    pub model: Option<ModelRef>,
    pub thinking: Option<Thinking>,
    pub group: Option<Grouping>,
}

fn invalid(reason: impl fmt::Display) -> String {
    format!("invalid {AGENT_FILE}: {reason}")
}

/// 分在哪个组：不分组，或某个组（名字去掉了首尾空白、非空）。
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub enum Grouping {
    #[default]
    Ungrouped,
    In(String),
}

impl Grouping {
    /// 界面与工具给的分组名的唯一规整：去掉首尾空白，空的是不分组。
    pub fn parse(input: &str) -> Self {
        match input.trim() {
            "" => Grouping::Ungrouped,
            g => Grouping::In(g.to_owned()),
        }
    }

    pub fn name(&self) -> Option<&str> {
        match self {
            Grouping::Ungrouped => None,
            Grouping::In(g) => Some(g),
        }
    }
}

impl AgentFile {
    pub fn new(description: String, model: ModelRef, thinking: Thinking, group: Grouping) -> Result<Self, String> {
        Self::checked(description, model, thinking, group.name().map(str::to_owned))
    }

    fn checked(
        description: String,
        model: ModelRef,
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
        Ok(Self { description, model, thinking, group })
    }

    /// 这份配置改上 config 里给出的字段之后的样子。
    pub fn with(&self, config: &AgentConfig) -> Result<Self, String> {
        let group = match &config.group {
            None => self.group.clone(),
            Some(g) => g.name().map(str::to_owned),
        };
        Self::checked(
            config.description.clone().unwrap_or_else(|| self.description.clone()),
            config.model.clone().unwrap_or_else(|| self.model.clone()),
            config.thinking.unwrap_or(self.thinking),
            group,
        )
    }

    pub fn description(&self) -> &str {
        &self.description
    }
    pub fn model(&self) -> &ModelRef {
        &self.model
    }
    pub fn thinking(&self) -> Thinking {
        self.thinking
    }
    pub fn group(&self) -> Option<&str> {
        self.group.as_deref()
    }
}

/// 文件的样子：读入只经它（未知的键、类型不对都在这里报出），再经 [`AgentFile::checked`] 查取值。
#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Stored {
    description: String,
    model: ModelRef,
    #[serde(default)]
    thinking: Thinking,
    group: Option<String>,
}

/// 文件里的键，按写出的顺序（同 [`Stored`] 的字段；写时逐个写，[`AgentFile::fields`]）
const KEYS: [&str; 4] = ["description", "model", "thinking", "group"];

impl AgentFile {
    /// 每个键要写成的值（None 为没有这个键：不分组）。顺序同 [`KEYS`]。
    fn fields(&self) -> [Option<String>; 4] {
        [
            Some(self.description.clone()),
            Some(self.model.to_string()),
            Some(self.thinking.to_string()),
            self.group.clone(),
        ]
    }
}

/// 不合格式时报错（原因）。
pub fn parse(text: &str) -> Result<AgentFile, String> {
    let s: Stored = toml::from_str(text).map_err(|e: toml::de::Error| invalid(e.message().trim()))?;
    AgentFile::checked(s.description, s.model, s.thinking, s.group)
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
            Some(old) if old.as_str() == Some(value.as_str()) => {}
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
        let text = "# 注释\ndescription = \"d\"  # 说明\nmodel = \"p:m\" # 模型\ngroup = \"g\"\n";
        let file = parse(text).unwrap();
        assert_eq!(file.thinking(), Thinking::Default);
        let config =
            AgentConfig { group: Some(Grouping::parse("  ")), description: Some("新".into()), ..Default::default() };
        let changed = file.with(&config).unwrap();
        let out = render(Some(text), &changed).unwrap();
        assert!(out.starts_with("# 注释\n"), "{out}");
        assert!(out.contains("description = \"新\"  # 说明\n"), "{out}");
        assert!(out.contains("model = \"p:m\" # 模型\n"), "{out}");
        assert!(!out.contains("group"));
        assert_eq!(parse(&out).unwrap(), changed);
        assert!(parse("description = \" \"\nmodel = \"p:m\"").is_err());
        assert!(parse("description = \"d\"\nmodel = \"m\"").is_err());
        assert!(parse("description = \"d\"\nmodel = \"p:m\"\nextra = 1").is_err());
        assert!(parse("description = \"d\"\nmodel = \"p:m\"\nthinking = \"max\"").is_err());
        assert!(parse("description = \"d\"\nmodel = \"p:m\"\ngroup = \" g\"").is_err());
        assert!(parse("id = \"a\"\ndescription = \"d\"\nmodel = \"p:m\"").is_err());
    }
}
