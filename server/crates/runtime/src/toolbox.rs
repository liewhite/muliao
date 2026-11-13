//! 运行的内置工具：各领域带着声明交来的工具（[`BuiltinTool`]），收集成分发用的工具表（[`Toolbox::specs`]，组合根交给
//! 资源目录的 `TableBinder::bind`）、界面的展示规则（[`Toolbox::renders`]）、消息落库前的脱敏（[`Toolbox::redaction`]），
//! 以及一次运行按 agent 的规则开放的那部分（[`Toolbox::allowed`]）。MCP 工具不在这里（[`crate::mcp`]，每次运行连接）。

use std::collections::{HashMap, HashSet};
use std::sync::Arc;

use agenter::{DynTool, Event, Message, ResponsePart, RunEvent, ToolCall};
use muliao_assets::rules::{Rules, ToolSpec};
use muliao_platform::tool_views::ToolRender;
use muliao_platform::tooling::{BuiltinTool, RunScope};
use serde_json::{Map, Value};

pub struct Toolbox {
    tools: Vec<BuiltinTool>,
}

impl Toolbox {
    /// 全部内置工具，按注册顺序。重名、声明要脱敏的参数不在工具的参数里（字段改了名而声明没跟着改，脱敏就会静默失效），
    /// 都是代码的 bug，启动时当场 panic。
    pub fn new(tools: Vec<BuiltinTool>) -> Self {
        let mut seen = std::collections::HashSet::new();
        for t in &tools {
            assert!(seen.insert(t.name().to_owned()), "built-in tool {:?} is registered more than once", t.name());
            let params = &t.tool.def().parameters["properties"];
            for arg in t.traits.secret_args {
                assert!(
                    params.get(arg).is_some(),
                    "tool {:?} declares secret argument {arg:?} it does not have",
                    t.name()
                );
            }
        }
        Self { tools }
    }

    /// 分发用的工具表（组合根交给 `TableBinder::bind`）。
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

    /// 一次运行开放的内置工具：rules（agent 现在的最终规则）放行的那些，按注册顺序。
    pub fn allowed(&self, rules: &Rules) -> Vec<Arc<dyn DynTool<RunScope>>> {
        let names: Vec<&str> = self.tools.iter().map(BuiltinTool::name).collect();
        let allowed = rules.tools(names);
        self.tools.iter().filter(|t| allowed.contains(&t.name())).map(|t| t.tool.clone()).collect()
    }
}

pub const REDACTED: &str = "[redacted]";

/// 工具调用参数里的 secret 脱敏。两处用：会话领域保存消息前（[`Redaction::messages`]）；运行中的实时预览在运行端口
/// 交出事件前就已脱敏（[`Redaction::preview`]，[`crate::Run::step`] 套上），消费方不用记得做。只能防止明文进入保存的
/// 对话记录与界面；同一次运行中模型已经看到过明文。参数解析失败时交还模型的「请重试」不带参数的值（agenter 保证），不用处理。
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

    fn covers(&self, tool: &str) -> bool {
        self.secret_args.contains_key(tool)
    }

    /// 一步的实时事件先脱敏再交给 sink：声明了脱敏参数的工具，它的调用段开始、整段替换时交出脱敏后的调用，参数的增量
    /// 不交出（跟着事件攒半截回复的一方攒出的也是脱敏的）；工具开始执行时同样交出脱敏后的调用。一步用一个（段号只在一次
    /// 模型请求里有意义）。
    pub fn preview<'f>(
        &'f self,
        sink: &'f mut (dyn FnMut(RunEvent<'_>) + Send),
    ) -> impl FnMut(RunEvent<'_>) + Send + 'f {
        let mut secret_parts: HashSet<usize> = HashSet::new();
        move |event| match event {
            RunEvent::Model(Event::PartStart { index, part: ResponsePart::ToolCall(c) }) if self.covers(&c.name) => {
                secret_parts.insert(*index);
                sink(RunEvent::Model(&Event::PartStart { index: *index, part: ResponsePart::ToolCall(self.call(c)) }));
            }
            RunEvent::Model(Event::Replace { index, part: ResponsePart::ToolCall(c) }) if self.covers(&c.name) => {
                sink(RunEvent::Model(&Event::Replace { index: *index, part: ResponsePart::ToolCall(self.call(c)) }));
            }
            RunEvent::Model(Event::Delta { index, .. }) if secret_parts.contains(index) => {}
            RunEvent::ToolStarted(c) if self.covers(&c.name) => sink(RunEvent::ToolStarted(&self.call(c))),
            other => sink(other),
        }
    }
}

#[cfg(test)]
mod tests {
    use agenter::{DeltaKind, Tool, ToolCtx, ToolError, ToolOutput};
    use muliao_platform::tooling::{ToolTraits, builtin};
    use schemars::JsonSchema;
    use serde::Deserialize;

    use super::*;

    #[derive(Deserialize, JsonSchema)]
    struct Args {
        #[allow(dead_code)]
        value: Option<String>,
    }

    struct Named(&'static str);

    impl Tool<RunScope> for Named {
        type Args = Args;
        fn name(&self) -> &str {
            self.0
        }
        fn description(&self) -> &str {
            "t"
        }
        async fn call(&self, _ctx: ToolCtx<'_, RunScope>, _a: Args) -> Result<ToolOutput, ToolError> {
            Ok(agenter::tool::text("ok"))
        }
    }

    fn call(name: &str, args: &str) -> ToolCall {
        ToolCall { id: "1".into(), name: name.into(), args: args.into(), meta: None }
    }

    #[test]
    fn specs_renders_and_redaction_come_from_the_declarations() {
        let tb = Toolbox::new(vec![
            builtin(Named("read_x"), ToolTraits::read().render(ToolRender::new())),
            builtin(Named("make_y"), ToolTraits::write().admin().secret_args(&["value"])),
        ]);
        let specs = tb.specs();
        assert_eq!(
            specs[0],
            ToolSpec { name: "read_x".into(), access: muliao_assets::rules::ToolAccess::Read, admin: false }
        );
        assert!(specs[1].admin);
        assert_eq!(tb.renders().keys().collect::<Vec<_>>(), ["read_x"]);
        let r = tb.redaction();
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
        Toolbox::new(vec![builtin(Named("a"), ToolTraits::read()), builtin(Named("a"), ToolTraits::read())]);
    }

    #[test]
    #[should_panic(expected = "declares secret argument \"token\" it does not have")]
    fn secret_args_must_be_real_arguments() {
        Toolbox::new(vec![builtin(Named("a"), ToolTraits::write().secret_args(&["token"]))]);
    }

    #[test]
    fn allowed_follows_rules_and_keeps_the_barrier_declaration() {
        let tb = Toolbox::new(vec![
            builtin(Named("f1"), ToolTraits::read().sequential()),
            builtin(Named("f2"), ToolTraits::write()),
            builtin(Named("other"), ToolTraits::read()),
        ]);
        let rules = Rules::new(vec!["use:tool:f*".into()], vec!["use:tool:other".into()]);
        let tools = tb.allowed(&rules);
        assert_eq!(tools.iter().map(|t| t.def().name.as_str()).collect::<Vec<_>>(), ["f1", "f2"]);
        assert!(tools[0].sequential() && !tools[1].sequential(), "agenter runs f1 as a barrier");
    }

    #[test]
    fn preview_events_are_redacted() {
        let tb = Toolbox::new(vec![builtin(Named("vault_set"), ToolTraits::write().secret_args(&["value"]))]);
        let r = tb.redaction();
        let mut seen: Vec<String> = Vec::new();
        let mut sink = |e: RunEvent<'_>| seen.push(format!("{e:?}"));
        {
            let mut preview = r.preview(&mut sink);
            let part = |args: &str| ResponsePart::ToolCall(call("vault_set", args));
            preview(RunEvent::Model(&Event::PartStart { index: 0, part: part("") }));
            preview(RunEvent::Model(&Event::Delta {
                index: 0,
                kind: DeltaKind::ToolArgs,
                text: "{\"value\":\"hunter2".into(),
            }));
            preview(RunEvent::Model(&Event::Replace { index: 0, part: part(r#"{"value":"hunter2"}"#) }));
            preview(RunEvent::Model(&Event::Delta { index: 1, kind: DeltaKind::Text, text: "ok".into() }));
            preview(RunEvent::ToolStarted(&call("vault_set", r#"{"value":"hunter2"}"#)));
        }
        let all = seen.join("\n");
        assert!(!all.contains("hunter2"), "{all}");
        assert_eq!(seen.len(), 4, "the argument delta is dropped, the text delta passes: {all}");
        assert!(all.contains("[redacted]") && all.contains("ok"));
    }
}
