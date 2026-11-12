//! 运行的内置工具：各领域带着声明交来的工具（[`BuiltinTool`]），收集成分发用的工具表（[`Toolbox::specs`]，组合根交给
//! 资源目录的 `ToolTable::bind`）、界面的展示规则（[`Toolbox::renders`]）、消息落库前的脱敏（[`Toolbox::redaction`]），
//! 以及一次运行按 agent 的规则开放的那部分（[`Toolbox::allowed`]）。MCP 工具不在这里（[`crate::mcp`]，每次运行连接）。

use std::collections::HashMap;
use std::sync::Arc;

use agenter::{DynTool, Message, ResponsePart, ToolCall, ToolCtx, ToolDef, ToolError, ToolOutput};
use futures::future::BoxFuture;
use muliao_assets::rules::{Rules, ToolSpec};
use muliao_platform::tool_views::ToolRender;
use muliao_platform::tooling::{BuiltinTool, RunScope};
use serde_json::{Map, Value};

pub struct Toolbox {
    tools: Vec<BuiltinTool>,
}

impl Toolbox {
    /// 全部内置工具，按注册顺序。重名是组合根的 bug，当场 panic。
    pub fn new(tools: Vec<BuiltinTool>) -> Self {
        let mut seen = std::collections::HashSet::new();
        for t in &tools {
            assert!(seen.insert(t.name().to_owned()), "built-in tool {:?} is registered more than once", t.name());
        }
        Self { tools }
    }

    /// 分发用的工具表（组合根交给 `ToolTable::bind`）。
    pub fn specs(&self) -> Vec<ToolSpec> {
        self.tools
            .iter()
            .map(|t| ToolSpec { name: t.name().to_owned(), access: t.traits.access, admin: t.traits.admin })
            .collect()
    }

    /// 内置工具在界面上的展示规则：声明了 render 的那些；MCP 工具没有，显示原文。
    pub fn renders(&self) -> HashMap<String, Arc<ToolRender>> {
        self.tools.iter().filter_map(|t| Some((t.name().to_owned(), t.traits.render.clone()?))).collect()
    }

    /// 消息落库、推送前的脱敏：声明了 secret_args 的那些工具。
    pub fn redaction(&self) -> Redaction {
        Redaction {
            secret_args: self
                .tools
                .iter()
                .filter(|t| !t.traits.secret_args.is_empty())
                .map(|t| (t.name().to_owned(), t.traits.secret_args))
                .collect(),
        }
    }

    /// 一次运行开放的内置工具：rules（agent 现在的最终规则）放行的那些，按注册顺序。声明了 sequential 的工具共用这次
    /// 运行的一把锁：同一步里它们一个一个执行。
    pub fn allowed(&self, rules: &Rules) -> Vec<Arc<dyn DynTool<RunScope>>> {
        let names: Vec<&str> = self.tools.iter().map(BuiltinTool::name).collect();
        let allowed = rules.tools(names);
        let lock = Arc::new(tokio::sync::Mutex::new(()));
        self.tools
            .iter()
            .filter(|t| allowed.contains(&t.name()))
            .map(|t| {
                if t.traits.sequential {
                    Arc::new(Sequential { inner: t.tool.clone(), lock: lock.clone() }) as Arc<dyn DynTool<RunScope>>
                } else {
                    t.tool.clone()
                }
            })
            .collect()
    }
}

/// 拿到这次运行的锁才执行。
struct Sequential {
    inner: Arc<dyn DynTool<RunScope>>,
    lock: Arc<tokio::sync::Mutex<()>>,
}

impl DynTool<RunScope> for Sequential {
    fn def(&self) -> &ToolDef {
        self.inner.def()
    }

    fn call<'a>(&'a self, ctx: ToolCtx<'a, RunScope>, args: &'a str) -> BoxFuture<'a, Result<ToolOutput, ToolError>> {
        Box::pin(async move {
            let _turn = self.lock.lock().await;
            self.inner.call(ctx, args).await
        })
    }
}

pub const REDACTED: &str = "[redacted]";

/// 工具调用参数里的 secret 脱敏：在消息落库、推送到界面之前执行（会话领域）。只能防止明文进入保存的对话记录；同一次运行
/// 中模型已经看到过明文。参数解析失败时交还模型的「请重试」不带参数的值（agenter 保证），不用处理。
#[derive(Debug, Clone, Default)]
pub struct Redaction {
    secret_args: HashMap<String, &'static [&'static str]>,
}

impl Redaction {
    /// 脱敏后的调用。参数不是合法的 JSON 对象时（模型写错，或停止时参数还没生成完被截断）无法定位字段，整体替换：宁可丢掉
    /// 参数也不留下明文。
    pub fn call(&self, call: &ToolCall) -> ToolCall {
        let Some(secret) = self.secret_args.get(&call.name) else { return call.clone() };
        let args = match serde_json::from_str::<Value>(if call.args.trim().is_empty() { "{}" } else { &call.args }) {
            Ok(Value::Object(m)) => {
                let redacted: Map<String, Value> = m
                    .into_iter()
                    .map(|(k, v)| {
                        let v = if secret.contains(&k.as_str()) { Value::from(REDACTED) } else { v };
                        (k, v)
                    })
                    .collect();
                Value::Object(redacted).to_string()
            }
            _ => serde_json::json!({ "_": REDACTED }).to_string(),
        };
        ToolCall { args, ..call.clone() }
    }

    /// 消息里所有工具调用就地脱敏。
    pub fn messages(&self, messages: &mut [Message]) {
        for m in messages {
            let Message::Assistant(r) = m else { continue };
            for p in &mut r.parts {
                if let ResponsePart::ToolCall(c) = p {
                    *c = self.call(c);
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use agenter::Tool;
    use muliao_platform::tooling::{Audience, ToolTraits, builtin};
    use schemars::JsonSchema;
    use serde::Deserialize;

    use super::*;

    #[derive(Deserialize, JsonSchema)]
    struct Args {}

    struct Named(&'static str, Arc<std::sync::Mutex<Vec<String>>>);

    impl Tool<RunScope> for Named {
        type Args = Args;
        fn name(&self) -> &str {
            self.0
        }
        fn description(&self) -> &str {
            "t"
        }
        async fn call(&self, _ctx: ToolCtx<'_, RunScope>, _a: Args) -> Result<ToolOutput, ToolError> {
            self.1.lock().unwrap().push(format!("{} start", self.0));
            tokio::time::sleep(std::time::Duration::from_millis(20)).await;
            self.1.lock().unwrap().push(format!("{} end", self.0));
            Ok(agenter::tool::text("ok"))
        }
    }

    fn log() -> Arc<std::sync::Mutex<Vec<String>>> {
        Arc::default()
    }

    #[test]
    fn specs_renders_and_redaction_come_from_the_declarations() {
        let l = log();
        let tb = Toolbox::new(vec![
            builtin(Named("read_x", l.clone()), ToolTraits::read().render(ToolRender::new())),
            builtin(Named("make_y", l.clone()), ToolTraits::write().admin().secret_args(&["value"])),
        ]);
        let specs = tb.specs();
        assert_eq!(
            specs[0],
            ToolSpec { name: "read_x".into(), access: muliao_assets::rules::ToolAccess::Read, admin: false }
        );
        assert!(specs[1].admin);
        assert_eq!(tb.renders().keys().collect::<Vec<_>>(), ["read_x"]);
        let r = tb.redaction();
        let call =
            |name: &str, args: &str| ToolCall { id: "1".into(), name: name.into(), args: args.into(), meta: None };
        assert_eq!(
            r.call(&call("make_y", r#"{"key":"K","value":"s3cret"}"#)).args,
            r#"{"key":"K","value":"[redacted]"}"#
        );
        assert_eq!(r.call(&call("make_y", r#"{"value":"s3cr"#)).args, r#"{"_":"[redacted]"}"#, "truncated args");
        assert_eq!(r.call(&call("read_x", r#"{"value":"v"}"#)).args, r#"{"value":"v"}"#, "other tools untouched");
    }

    #[test]
    #[should_panic(expected = "registered more than once")]
    fn duplicate_names_are_a_bug() {
        let l = log();
        Toolbox::new(vec![
            builtin(Named("a", l.clone()), ToolTraits::read()),
            builtin(Named("a", l), ToolTraits::read()),
        ]);
    }

    #[tokio::test]
    async fn allowed_follows_rules_and_sequential_tools_take_turns() {
        let l = log();
        let tb = Toolbox::new(vec![
            builtin(Named("f1", l.clone()), ToolTraits::read().sequential()),
            builtin(Named("f2", l.clone()), ToolTraits::write().sequential()),
            builtin(Named("other", l.clone()), ToolTraits::read()),
        ]);
        let rules = Rules::new(vec!["use:tool:f*".into()], vec!["use:tool:other".into()]);
        let tools = tb.allowed(&rules);
        assert_eq!(tools.iter().map(|t| t.def().name.as_str()).collect::<Vec<_>>(), ["f1", "f2"]);
        let scope = RunScope::new("a".into(), "/w".into(), "s".into(), Audience::None);
        let c1 = ToolCall { id: "1".into(), name: "f1".into(), args: "{}".into(), meta: None };
        let c2 = ToolCall { id: "2".into(), name: "f2".into(), args: "{}".into(), meta: None };
        let ctx = |c| ToolCtx { deps: &scope, call: c, retries: 0, approval: None };
        let (a, b) = tokio::join!(tools[0].call(ctx(&c1), "{}"), tools[1].call(ctx(&c2), "{}"));
        a.unwrap();
        b.unwrap();
        assert_eq!(*l.lock().unwrap(), ["f1 start", "f1 end", "f2 start", "f2 end"]);
    }
}
