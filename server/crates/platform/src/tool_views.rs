//! 工具调用在界面上的展示：每个工具可以给自己的参数与结果定一个展示形式（[`ToolRender`]），界面按展示块渲染；没定的
//! （包括全部 MCP 工具）显示参数与结果的原文。
//!
//! - 展示块是结构化的描述（[`ViewBlock`]），不是 HTML：界面每种块一个组件，样式与安全都在界面一处。
//! - render 挂在定义工具的地方（[`crate::tooling::ToolTraits::render`]），由运行端口的工具箱收集，会话领域投影条目时调用。
//!   历史与运行中的条目走同一份投影。
//! - 参数按工具自己的参数类型解析（与执行时同一个类型，含默认值）再交给 call：解析不了的调用（会被退回）按原文展示。
//!   结构化结果按 render 声明的结果类型还原，还原不了（旧历史的结果不合现在的类型）按原文展示并记 warning；render 自己
//!   认不出、不想画时返回 None，同样按原文展示。

use serde::Serialize;
use serde::de::DeserializeOwned;

use crate::text::truncate;

/// 一个展示块。输出时字段一律都在（没有就是 null），前端类型与实际输出一致。
#[derive(Debug, Clone, PartialEq, Eq, Serialize, utoipa::ToSchema)]
#[serde(tag = "kind", rename_all = "snake_case")]
pub enum ViewBlock {
    /// 终端：`$ command` 提示行、输出、状态行（如退出码、超时、后台任务）。界面把相邻的终端块（调用的命令与结果的输出）
    /// 画在同一个终端框里。failed：状态是失败（非零退出码、超时被杀），状态行醒目显示
    Terminal {
        command: Option<String>,
        output: Option<String>,
        status: Option<String>,
        failed: bool,
    },
    /// 一段代码或文件内容（等宽），title 多为文件路径
    Code {
        title: Option<String>,
        text: String,
    },
    /// 一处修改：统一 diff 格式的文本（以 `+`/`-`/空格开头的行），title 多为文件路径
    Diff {
        title: Option<String>,
        text: String,
    },
    Markdown {
        title: Option<String>,
        text: String,
    },
    /// 键值对（如 issue 的状态、指派人）
    Fields {
        rows: Vec<FieldRow>,
    },
    Table {
        columns: Vec<String>,
        rows: Vec<Vec<String>>,
    },
    Links {
        links: Vec<Link>,
    },
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, utoipa::ToSchema)]
pub struct FieldRow {
    pub label: String,
    pub value: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, utoipa::ToSchema)]
pub struct Link {
    pub title: String,
    pub url: String,
    pub summary: Option<String>,
}

pub type View = Vec<ViewBlock>;

/// 键值块的一行的值：没给的参数（None）不显示。
pub enum FieldValue {
    Text(String),
    List(Vec<String>),
    Missing,
}

impl From<&str> for FieldValue {
    fn from(s: &str) -> Self {
        FieldValue::Text(s.to_owned())
    }
}
impl From<String> for FieldValue {
    fn from(s: String) -> Self {
        FieldValue::Text(s)
    }
}
impl From<&String> for FieldValue {
    fn from(s: &String) -> Self {
        FieldValue::Text(s.clone())
    }
}
impl<T: Into<FieldValue>> From<Option<T>> for FieldValue {
    fn from(v: Option<T>) -> Self {
        v.map_or(FieldValue::Missing, Into::into)
    }
}
impl From<&[String]> for FieldValue {
    fn from(v: &[String]) -> Self {
        FieldValue::List(v.to_vec())
    }
}
impl From<&Vec<String>> for FieldValue {
    fn from(v: &Vec<String>) -> Self {
        FieldValue::List(v.clone())
    }
}

/// 键值块：值为 Missing 的行不显示，列表用逗号连起来（空列表显示 `(none)`）。
pub fn fields<const N: usize>(rows: [(&str, FieldValue); N]) -> ViewBlock {
    ViewBlock::Fields {
        rows: rows
            .into_iter()
            .filter_map(|(label, value)| {
                let value = match value {
                    FieldValue::Text(t) => t,
                    FieldValue::List(l) if l.is_empty() => "(none)".to_owned(),
                    FieldValue::List(l) => l.join(", "),
                    FieldValue::Missing => return None,
                };
                Some(FieldRow { label: label.to_owned(), value })
            })
            .collect(),
    }
}

/// 把一处替换（原文 → 新文）画成统一 diff：只有改动的行带 `-`/`+`，上下文是两边相同的行（前后各 3 行），多处改动之间
/// 留 `@@` 分段行。没有原文（只知道新文）时只有一段，行号没有意义，连 `@@` 也不要。
pub fn diff(title: Option<String>, old: &str, new: &str) -> ViewBlock {
    let d = similar::TextDiff::from_lines(old, new);
    let mut lines = Vec::new();
    for hunk in d.unified_diff().context_radius(3).iter_hunks() {
        if !old.is_empty() {
            lines.push(hunk.header().to_string());
        }
        for change in hunk.iter_changes() {
            let sign = match change.tag() {
                similar::ChangeTag::Delete => '-',
                similar::ChangeTag::Insert => '+',
                similar::ChangeTag::Equal => ' ',
            };
            let value = change.value();
            let value = value.strip_suffix('\n').unwrap_or(value);
            lines.push(format!("{sign}{}", value.strip_suffix('\r').unwrap_or(value)));
        }
    }
    ViewBlock::Diff { title, text: lines.join("\n") }
}

/// 展示块里的长文字截断（实时条目用）：只截块本身的文字字段，键值、表格、链接的项本来就短。
pub fn truncated(view: View, max_chars: usize) -> View {
    let cut = |s: String| truncate(&s, max_chars);
    let cut_opt = |s: Option<String>| s.map(|s| truncate(&s, max_chars));
    view.into_iter()
        .map(|b| match b {
            ViewBlock::Terminal { command, output, status, failed } => ViewBlock::Terminal {
                command: cut_opt(command),
                output: cut_opt(output),
                status: cut_opt(status),
                failed,
            },
            ViewBlock::Code { title, text } => ViewBlock::Code { title: cut_opt(title), text: cut(text) },
            ViewBlock::Diff { title, text } => ViewBlock::Diff { title: cut_opt(title), text: cut(text) },
            ViewBlock::Markdown { title, text } => ViewBlock::Markdown { title: cut_opt(title), text: cut(text) },
            other => other,
        })
        .collect()
}

type CallView = Box<dyn Fn(&str) -> Option<View> + Send + Sync>;
type ResultView = Box<dyn Fn(&str, &str) -> Option<View> + Send + Sync>;

/// 一个工具的展示规则。哪一边没给、或返回 None（认不出、不该画），那一边显示原文；返回空列表表示这一边不需要显示。
#[derive(Default)]
pub struct ToolRender {
    call: Option<CallView>,
    result: Option<ResultView>,
}

impl ToolRender {
    pub fn new() -> Self {
        Self::default()
    }

    /// 参数的展示：A 是工具自己的参数类型（与执行时同一个类型，含默认值）。
    pub fn call<A: DeserializeOwned>(mut self, f: impl Fn(&A) -> Option<View> + Send + Sync + 'static) -> Self {
        self.call = Some(Box::new(move |args| {
            // 没有参数的调用有的模型给空串（同执行时）
            let args = if args.trim().is_empty() { "{}" } else { args };
            serde_json::from_str::<A>(args).ok().map(|a| f(&a)).unwrap_or(None)
        }));
        self
    }

    /// 结果的展示：结果交给模型的文字原样交给 f。
    pub fn result_text(mut self, f: impl Fn(&str) -> Option<View> + Send + Sync + 'static) -> Self {
        self.result = Some(Box::new(move |_tool, text| f(text)));
        self
    }

    /// 结果的展示：结果是一段 JSON（工具用 `agenter::tool::json` 交回的），按 R 还原后交给 f。
    pub fn result_json<R: DeserializeOwned>(mut self, f: impl Fn(&R) -> Option<View> + Send + Sync + 'static) -> Self {
        self.result = Some(Box::new(move |tool, text| match serde_json::from_str::<R>(text) {
            Ok(r) => f(&r),
            Err(e) => {
                tracing::warn!(tool, error = %e, result = %truncate(text, 200), "tool result does not fit its render type, shown raw");
                None
            }
        }));
        self
    }

    /// 参数（模型给的 JSON 原文）的展示；None 显示原文。
    pub fn call_view(&self, args: &str) -> Option<View> {
        self.call.as_ref().and_then(|f| f(args))
    }

    /// 工具 `tool` 的结果（交给模型的文字）的展示；None 显示原文。
    pub fn result_view(&self, tool: &str, text: &str) -> Option<View> {
        self.result.as_ref().and_then(|f| f(tool, text))
    }
}

#[cfg(test)]
mod tests {
    use serde::Deserialize;

    use super::*;

    #[derive(Deserialize)]
    struct Args {
        query: String,
        #[serde(default)]
        limit: Option<u32>,
    }

    #[test]
    fn render_parses_args_and_results_or_falls_back_to_raw() {
        let r = ToolRender::new()
            .call(|a: &Args| {
                Some(vec![fields([
                    ("query", a.query.as_str().into()),
                    ("limit", a.limit.map(|l| l.to_string()).into()),
                ])])
            })
            .result_json(|hits: &Vec<String>| {
                Some(vec![ViewBlock::Table { columns: vec!["hit".into()], rows: vec![hits.clone()] }])
            });
        assert_eq!(
            r.call_view(r#"{"query":"q"}"#),
            Some(vec![ViewBlock::Fields { rows: vec![FieldRow { label: "query".into(), value: "q".into() }] }]),
            "missing optional values are not shown"
        );
        assert_eq!(r.call_view(r#"{"limit":1}"#), None, "a call that does not parse is shown raw");
        assert!(r.result_view("t", r#"["a"]"#).is_some());
        assert_eq!(r.result_view("t", "not json"), None);
        assert_eq!(ToolRender::new().call_view("{}"), None, "no render for that side");
    }

    #[test]
    fn view_blocks_serialize_with_every_field() {
        let v = serde_json::to_value(ViewBlock::Code { title: None, text: "x".into() }).unwrap();
        assert_eq!(v, serde_json::json!({"kind": "code", "title": null, "text": "x"}));
        let f = fields([("roles", (&vec![]).into()), ("allow", (&vec!["a".to_string(), "b".into()]).into())]);
        assert_eq!(
            f,
            ViewBlock::Fields {
                rows: vec![
                    FieldRow { label: "roles".into(), value: "(none)".into() },
                    FieldRow { label: "allow".into(), value: "a, b".into() }
                ]
            }
        );
    }

    #[test]
    fn diff_shows_changed_lines_with_context() {
        let ViewBlock::Diff { text, .. } = diff(None, "a\nb\nc\n", "a\nB\nc\n") else { unreachable!() };
        assert_eq!(text, "@@ -1,3 +1,3 @@\n a\n-b\n+B\n c");
        let ViewBlock::Diff { text, .. } = diff(None, "", "x\ny") else { unreachable!() };
        assert_eq!(text, "+x\n+y", "without the old text there is no hunk header");
    }

    #[test]
    fn truncated_cuts_only_text_fields() {
        let v = truncated(
            vec![
                ViewBlock::Code { title: None, text: "abcdef".into() },
                ViewBlock::Table { columns: vec!["abcdef".into()], rows: vec![] },
            ],
            3,
        );
        assert_eq!(v[0], ViewBlock::Code { title: None, text: "abc…[truncated: 3 more chars]".into() });
        assert_eq!(v[1], ViewBlock::Table { columns: vec!["abcdef".into()], rows: vec![] });
    }
}
