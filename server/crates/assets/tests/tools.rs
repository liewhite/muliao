//! 本领域的工具：经 agenter 的工具接口真调一次（参数解析、结果 JSON、错误交还模型）。

mod common;

use std::sync::Arc;

use agenter::{Content, ToolCall, ToolCtx, ToolError};
use muliao_assets::access::Actor;
use muliao_assets::distribution::Distribution;
use muliao_assets::lifecycle::AgentLifecycle;
use muliao_assets::secrets::Secrets;
use muliao_assets::service::AgentSpec;
use muliao_assets::tools::tools;
use muliao_platform::tooling::{Audience, BuiltinTool, RunScope, ToolAccess};
use muliao_platform::vault::Vault;
use serde_json::Value;

struct Kit {
    w: common::World,
    tools: Vec<BuiltinTool>,
}

impl Kit {
    async fn call(&self, me: &str, name: &str, args: &str) -> Result<String, ToolError> {
        let tool = self.tools.iter().find(|t| t.name() == name).unwrap();
        let scope = RunScope::new(me.into(), "/w".into(), "s".into(), Audience::User);
        let call = ToolCall { id: "c".into(), name: name.into(), args: args.into(), meta: None };
        let out = tool.tool.call(ToolCtx { deps: &scope, call: &call, retries: 0, approval: None }, args).await?;
        match out.as_slice() {
            [Content::Text { text }] => Ok(text.clone()),
            other => panic!("{other:?}"),
        }
    }
}

async fn kit() -> Kit {
    let w = common::world().await;
    let vault = Arc::new(Vault::new("3QOHdQSfwdIhMog5wkCMiIBXFANh4YdV-XA9xY3RmyY=").unwrap());
    let tools = tools(
        w.service.clone(),
        Arc::new(Distribution::new(w.source.clone())),
        Arc::new(AgentLifecycle::new(w.source.clone(), w.workspaces(), vec![], vec![])),
        Arc::new(Secrets::new(w.source.clone(), vault)),
    );
    Kit { w, tools }
}

#[tokio::test]
async fn declarations() {
    let k = kit().await;
    let by = |n: &str| k.tools.iter().find(|t| t.name() == n).unwrap().traits.clone();
    assert_eq!(by("list_agents").access, ToolAccess::Read);
    assert!(by("create_agent").admin && by("delete_agent").admin);
    assert!(!by("vault_set").admin);
    assert_eq!(by("vault_set").secret_args, ["value"]);
}

#[tokio::test]
async fn list_get_create_and_delete_agents_by_name() {
    let k = kit().await;
    let me = k.w.create("me").await;
    k.w.service
        .create(
            AgentSpec {
                name: "grouped".into(),
                description: "g".into(),
                group: Some("ops".into()),
                ..Default::default()
            },
            &Actor::User,
        )
        .await
        .unwrap();

    let listed: Value = serde_json::from_str(&k.call(&me, "list_agents", "{}").await.unwrap()).unwrap();
    assert_eq!(listed[0]["group"], Value::Null, "ungrouped agents come first");
    assert_eq!(listed[0]["agents"][0]["name"], "me");
    assert_eq!(listed[1]["group"], "ops");
    assert!(!listed.to_string().contains(&me), "the model never sees ids");

    let detail: Value = serde_json::from_str(&k.call(&me, "get_agent", r#"{"agent":"me"}"#).await.unwrap()).unwrap();
    assert_eq!(detail["model"], "p:default");
    assert_eq!(detail["thinking"], "default");
    assert_eq!(detail["tools"], serde_json::json!(["read_file", "run_shell"]), "base has no admin tools");
    assert_eq!(detail["all_tools"].as_array().unwrap().len(), 3);
    assert_eq!(detail["available_roles"][0]["name"], "base");

    let e = k.call(&me, "get_agent", r#"{"agent":"nobody"}"#).await.unwrap_err();
    assert!(matches!(&e, ToolError::Retry(m) if m.contains("nobody")), "{e:?}");

    let made = k
        .call(&me, "create_agent", r#"{"name":"helper","description":"helps","roles":["admin"],"thinking":"high"}"#)
        .await
        .unwrap();
    assert_eq!(made, "created agent helper");
    let helper: Value =
        serde_json::from_str(&k.call(&me, "get_agent", r#"{"agent":"helper"}"#).await.unwrap()).unwrap();
    assert_eq!(helper["roles"], serde_json::json!(["admin"]));
    assert_eq!(helper["thinking"], "high");
    let e = k.call(&me, "create_agent", r#"{"name":"x","description":"d","thinking":"max"}"#).await.unwrap_err();
    assert!(matches!(&e, ToolError::Retry(m) if m.contains("thinking")), "bad enum values go back to the model: {e:?}");

    let e = k.call(&me, "delete_agent", r#"{"agent":"me"}"#).await.unwrap_err();
    assert!(matches!(&e, ToolError::Retry(m) if m.contains("cannot delete yourself")));
    assert_eq!(k.call(&me, "delete_agent", r#"{"agent":"helper"}"#).await.unwrap(), "deleted agent helper");
    assert!(k.call(&me, "get_agent", r#"{"agent":"helper"}"#).await.is_err());
}

#[tokio::test]
async fn vault_set_stores_under_the_caller() {
    let k = kit().await;
    let me = k.w.create("me").await;
    let out = k.call(&me, "vault_set", r#"{"key":"API_KEY","value":"v1","allow_shell":false}"#).await.unwrap();
    assert_eq!(out, "stored secret API_KEY (allow_shell: false)");
    assert!(k.w.root().join("me/secrets/API_KEY.toml").is_file());
    let e = k.call(&me, "vault_set", r#"{"key":"bad key","value":"v"}"#).await.unwrap_err();
    assert!(matches!(e, ToolError::Retry(_)));
}
