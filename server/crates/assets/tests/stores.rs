//! 各种资源的读写：记忆、skill、MCP、secret、资源池、角色，以及文件变动监听。

mod common;

use std::collections::BTreeMap;
use std::sync::Arc;
use std::time::Duration;

use common::{world, write};
use muliao_assets::access::Actor;
use muliao_assets::catalog::{AssetCatalog, Detail};
use muliao_assets::distribution::{Distribution, RoleSpec};
use muliao_assets::formats::mcp::{McpConfig, McpTransport};
use muliao_assets::kinds::{Kind, StoredKind};
use muliao_assets::mcp::McpRegistry;
use muliao_assets::memory::{MemoryAccess, MemoryStore};
use muliao_assets::rules::{AgentRules, Rules};
use muliao_assets::run_assets::RunAssets;
use muliao_assets::secrets::{SecretUse, Secrets};
use muliao_assets::skills::SkillStore;
use muliao_assets::tree::Owner;
use muliao_platform::vault::Vault;

#[tokio::test(flavor = "multi_thread")]
async fn memory_versions_and_blocks() {
    let w = world().await;
    let a = w.create("alice").await;
    let memory = MemoryStore::new(w.source.clone());
    let global = memory.read("global").await.unwrap();
    assert_eq!(global.content, "全局");
    let saved = memory.put("global", "新的  \n\n\n内容", Some(&global.version)).await.unwrap();
    assert_eq!(saved.content, "新的\n\n内容");
    // 拿旧版本再改：冲突，不盖掉
    assert!(
        memory.put("global", "x", Some(&global.version)).await.unwrap_err().to_string().contains("changed elsewhere")
    );
    assert!(memory.put("global", "x", None).await.unwrap_err().to_string().contains("already exists"));
    memory.put("team", "共享", None).await.unwrap();
    let private = memory.read_private(&a).await.unwrap();
    memory.put_private(&a, "我的", Some(&private.version)).await.unwrap();
    let blocks = memory.for_agent(&a).await.unwrap();
    let names: Vec<(&str, MemoryAccess)> = blocks.iter().map(|b| (b.name.as_str(), b.access)).collect();
    // team 没分给它；全局记忆 base 收录但不能改
    assert_eq!(names, vec![("alice", MemoryAccess::Own), ("global", MemoryAccess::ReadOnly)]);
}

#[tokio::test(flavor = "multi_thread")]
async fn skills_index_scripts_and_edit() {
    let w = world().await;
    let a = w.create("alice").await;
    let skills = SkillStore::new(w.source.clone());
    let meta = skills.write("tool", "做事", "正文", &Owner::Agent(a.clone()), None).await.unwrap();
    assert_eq!(meta.owner, Owner::Agent(a.clone()));
    write(&w.root().join("alice/skills/tool/run.sh"), "echo 1");
    let skill = skills.read("tool").await.unwrap();
    assert_eq!(skill.body, "正文");
    let md = std::fs::read_to_string(&meta.path).unwrap();
    std::fs::write(&meta.path, md.replace("description: \"做事\"", "description: \"做事\"\napproval: [run.sh]"))
        .unwrap();
    let scripts = skills.scripts("tool", &a).await.unwrap();
    assert_eq!(scripts.approval.len(), 1);
    assert!(scripts.needs_approval(&muliao_platform::fs::real(w.root().join("alice/skills/tool/run.sh")).unwrap()));
    let before = scripts.fingerprint().unwrap();
    std::fs::write(&meta.path, std::fs::read_to_string(&meta.path).unwrap() + "\n改说明\n").unwrap();
    assert_eq!(scripts.fingerprint().unwrap(), before, "editing SKILL.md keeps the fingerprint");
    write(&w.root().join("alice/skills/tool/.cache/x"), "y");
    assert_eq!(scripts.fingerprint().unwrap(), before, "hidden files do not count");
    write(&w.root().join("alice/skills/tool/run.sh"), "echo 2");
    assert_ne!(scripts.fingerprint().unwrap(), before);
    assert_eq!(RunAssets::of(&w.source.snapshot().await.unwrap(), &a).unwrap().skills.len(), 1);
    let b = w.create("bob").await;
    assert!(skills.scripts("tool", &b).await.is_err(), "not shared with bob");
    // 编辑时保留 approval，旧版本冲突
    let skill = skills.read("tool").await.unwrap();
    skills.write("tool", "改了", "新正文", &Owner::Platform, Some(&skill.version)).await.unwrap();
    assert_eq!(skills.scripts("tool", &a).await.unwrap().approval.len(), 1);
    assert!(skills.write("tool", "x", "y", &Owner::Platform, Some(&skill.version)).await.is_err());
}

/// Python 版算出的指纹（同一棵小目录）：已有的「始终允许」照样认。
#[test]
fn fingerprint_matches_python() {
    let dir = tempfile::tempdir().unwrap();
    let root = dir.path().join("s");
    write(&root.join("SKILL.md"), "---\ndescription: d\n---\n");
    write(&root.join("run.sh"), "echo hi\n");
    write(&root.join("lib/a.py"), "print(1)\n");
    write(&root.join("lib/__pycache__/a.pyc"), "x");
    write(&root.join(".venv/x"), "x");
    let scripts = muliao_assets::skills::SkillScripts { directory: root, approval: vec![] };
    assert_eq!(scripts.fingerprint().unwrap(), "1ef6ee7ba3fd63c6e4ef308ec3874456476102080846e291fb683e2cdc2576ba");
}

#[tokio::test(flavor = "multi_thread")]
async fn mcp_add_probe_and_toggle() {
    let w = world().await;
    let a = w.create("alice").await;
    let registry = McpRegistry::new(w.source.clone());
    let config =
        McpConfig::new(McpTransport::Stdio, Some("npx".into()), vec![], None, BTreeMap::new(), BTreeMap::new())
            .unwrap();
    let failing = async |_: &muliao_assets::mcp::McpServer| -> muliao_platform::errors::Result<Vec<String>> {
        Err(muliao_platform::errors::DomainError::invalid("cannot connect").into())
    };
    assert!(registry.add("srv", config.clone(), Owner::Agent(a.clone()), Some(failing)).await.is_err());
    assert!(!w.root().join("alice/mcp/srv.json").exists(), "a failed probe writes nothing");
    let probe = async |_: &muliao_assets::mcp::McpServer| Ok(vec!["t1".to_owned()]);
    let (_, tools) = registry.add("srv", config.clone(), Owner::Agent(a.clone()), Some(probe)).await.unwrap();
    assert_eq!(tools, vec!["t1"]);
    assert_eq!(RunAssets::of(&w.source.snapshot().await.unwrap(), &a).unwrap().mcp.len(), 1);
    registry.set_enabled("srv", false).await.unwrap();
    assert!(RunAssets::of(&w.source.snapshot().await.unwrap(), &a).unwrap().mcp.is_empty());
}

#[tokio::test(flavor = "multi_thread")]
async fn secrets_set_and_resolve() {
    let w = world().await;
    let a = w.create("alice").await;
    let b = w.create("bob").await;
    let vault = Arc::new(Vault::new("3QOHdQSfwdIhMog5wkCMiIBXFANh4YdV-XA9xY3RmyY=").unwrap());
    let secrets = Secrets::new(w.source.clone(), vault);
    let alice = Actor::Agent(a.clone());
    assert!(!secrets.set("K", "v1", "d", &alice, &Owner::Agent(a.clone()), Some(false)).await.unwrap());
    // 只改值：allow_shell 不被悄悄放开
    assert!(!secrets.set("K", "v2", "d", &alice, &Owner::Platform, None).await.unwrap());
    assert_eq!(secrets.get("K").await.unwrap().owner, Owner::Agent(a.clone()), "the owner stays");
    let keys = vec!["K".to_owned()];
    assert_eq!(secrets.resolve(&alice, &keys, SecretUse::Wrapped).await.unwrap()["K"], "v2");
    assert!(
        secrets.resolve(&alice, &keys, SecretUse::AgentCommand).await.unwrap_err().to_string().contains("allow_shell")
    );
    let bob = Actor::Agent(b.clone());
    assert!(secrets.resolve(&bob, &keys, SecretUse::Wrapped).await.is_err());
    assert!(
        secrets.set("K", "x", "d", &bob, &Owner::Agent(b), None).await.unwrap_err().to_string().contains("belongs to")
    );
    assert_eq!(secrets.resolve(&Actor::User, &keys, SecretUse::Wrapped).await.unwrap()["K"], "v2");
    let text = std::fs::read_to_string(w.root().join("alice/secrets/K.toml")).unwrap();
    assert!(!text.contains("v2"));
}

#[tokio::test(flavor = "multi_thread")]
async fn catalog_transfer_and_roles() {
    let w = world().await;
    let a = w.create("alice").await;
    write(&w.root().join("alice/skills/s/SKILL.md"), "---\ndescription: d\n---\n");
    let catalog = AssetCatalog::new(w.source.clone());
    let roles = Distribution::new(w.source.clone());
    roles
        .create(&RoleSpec {
            name: "team".into(),
            description: "团队".into(),
            rules: Rules::new(vec!["use:skill:s".parse().unwrap()], vec![]),
        })
        .await
        .unwrap();
    let all = catalog.catalog().await.unwrap();
    let s = all.iter().find(|c| c.kind() == Kind::Skill && c.name == "s").unwrap();
    assert_eq!(s.roles, vec!["team".to_owned()]);
    assert!(!s.shared, "a role held by nobody does not share it");
    assert!(matches!(all[0].detail, Detail::Tool(_)));
    // 能不能删和删除入口是同一条规则：skill 能删；内置工具、全局记忆、私有记忆不能
    assert!(s.deletable && !all[0].deletable);
    let memories: Vec<_> = all.iter().filter(|c| c.kind() == Kind::Memory).collect();
    assert!(memories.iter().any(|m| m.name == "global") && memories.iter().all(|m| !m.deletable));
    assert!(roles.delete("base").await.is_err());
    let w2 = w.service.clone();
    w2.update(&a, muliao_assets::service::AgentPatch { roles: Some(vec!["team".into()]), ..Default::default() })
        .await
        .unwrap();
    assert!(roles.delete("team").await.unwrap_err().to_string().contains("used by agents"));
    let view = roles.view("team").await.unwrap();
    assert_eq!(view.holders, vec![a.clone()]);
    assert_eq!(view.reach.asset_patterns(), vec!["skill:s".to_owned()]);
    let used = roles.assets(&a).await.unwrap();
    let s_used = used.iter().find(|u| u.name == "s").unwrap();
    assert!(s_used.source.own && s_used.source.roles == vec!["team".to_owned()]);
    let preview =
        roles.preview_agent(None, &AgentRules { roles: vec!["admin".into()], ..Default::default() }).await.unwrap();
    assert!(preview.tool_names().contains(&"create_agent"));
    assert!(roles.preview_role(None, &Rules::new(vec!["use:skill:typo".parse().unwrap()], vec![])).await.is_err());

    catalog.transfer(StoredKind::Skill, "s", &Owner::Platform).await.unwrap();
    assert!(w.root().join("platform/skills/s/SKILL.md").is_file());
    assert!(catalog.transfer(StoredKind::Memory, "global", &Owner::Platform).await.is_err());
    assert!(catalog.delete(StoredKind::Memory, "global").await.is_err());
    catalog.delete(StoredKind::Skill, "s").await.unwrap();
    assert!(!w.root().join("platform/skills/s").exists());
}

#[tokio::test(flavor = "multi_thread")]
async fn watch_reports_visible_changes() {
    use tokio::sync::mpsc::UnboundedReceiver;
    use tokio::time::timeout;
    // 收齐一阵通知：直到安静 1 秒，返回收到几次。只靠「等到事件」推进，不赌固定 sleep 够不够（#840：
    // 机器忙时 FSEvents 起监听和投递都可能晚一两秒）
    async fn drain(rx: &mut UnboundedReceiver<()>) -> usize {
        let mut n = 0;
        while timeout(Duration::from_secs(1), rx.recv()).await.is_ok_and(|m| m.is_some()) {
            n += 1;
        }
        n
    }
    let w = world().await;
    let (tx, mut rx) = tokio::sync::mpsc::unbounded_channel();
    let source = w.source.clone();
    let task = tokio::spawn(async move {
        source
            .watch(move || {
                let _ = tx.send(());
            })
            .await
    });
    // 监听何时生效没有信号：反复写一个可见探针，收到第一次通知即就绪，再等它的余波散尽
    let probe = w.root().join("platform/memory/probe.md");
    timeout(Duration::from_secs(20), async {
        loop {
            write(&probe, "x");
            if timeout(Duration::from_millis(500), rx.recv()).await.is_ok() {
                break;
            }
        }
    })
    .await
    .expect("watcher never reported the probe");
    drain(&mut rx).await;

    write(&w.root().join(".hidden"), "x");
    assert_eq!(drain(&mut rx).await, 0, "hidden files do not count");
    for i in 0..5 {
        write(&w.root().join(format!("platform/memory/m{i}.md")), "x");
    }
    timeout(Duration::from_secs(20), rx.recv()).await.expect("burst never reported");
    let n = 1 + drain(&mut rx).await;
    assert!((1..=2).contains(&n), "a burst is reported once or twice, got {n}");
    task.abort();
}
