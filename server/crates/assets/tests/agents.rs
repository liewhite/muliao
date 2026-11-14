//! agent 的建、改、改名、默认、归档、删除。

mod common;

use std::sync::Arc;

use common::{world, write};
use muliao_assets::access::Actor;
use muliao_assets::agents;
use muliao_assets::formats::agent_file::{AgentConfig, Thinking};
use muliao_assets::kinds::StoredKind;
use muliao_assets::lifecycle::{AgentLifecycle, ArchiveBlocker, DeletionHook};
use muliao_assets::rules::AgentRules;
use muliao_assets::service::{AgentPatch, AgentSpec};
use muliao_assets::tree::Owner;
use muliao_platform::errors::Error;

fn read(path: std::path::PathBuf) -> String {
    std::fs::read_to_string(path).unwrap_or_default()
}

#[tokio::test(flavor = "multi_thread")]
async fn create_writes_files_rules_and_row() {
    let w = world().await;
    let spec = AgentSpec {
        name: " 交易 ".into(),
        description: "做交易".into(),
        group: Some("  组 ".into()),
        thinking: Thinking::High,
        rules: AgentRules { allow: vec!["use:tool:create_agent".parse().unwrap()], ..Default::default() },
        ..Default::default()
    };
    let info = w.service.create(spec, &Actor::User).await.unwrap();
    assert_eq!(info.name.as_str(), "交易");
    let view = w.service.view(&info.id).await.unwrap();
    let config = view.config.unwrap();
    assert_eq!(config.file.model().to_string(), "p:default");
    assert_eq!(config.file.group(), Some("组"));
    assert_eq!(config.file.thinking(), Thinking::High);
    assert_eq!(muliao_assets::rules::texts(&view.rules.allow), ["use:tool:create_agent"]);
    assert!(w.root().join("交易/memory.md").is_file());
    assert!(w.workspaces().join(info.id.as_str()).is_dir());
    assert!(read(w.root().join("platform/access.toml")).contains(&format!("[agents.{}] # 交易\n", info.id)));

    // 名字被占（库里、目录只差大小写）、规则写错、模型不能用：什么都不写
    let again = |name: &str, rules: AgentRules, model: Option<&str>| {
        let s = w.service.clone();
        let spec = AgentSpec {
            name: name.into(),
            description: "d".into(),
            model: model.map(|m| m.parse().unwrap()),
            rules,
            ..Default::default()
        };
        async move { s.create(spec, &Actor::User).await }
    };
    assert!(again("交易", AgentRules::default(), None).await.unwrap_err().to_string().contains("already taken"));
    // 只差大小写的目录：macOS 的文件系统不分大小写，建目录时就失败
    std::fs::create_dir(w.root().join("Bob")).unwrap();
    assert!(again("bob", AgentRules::default(), None).await.is_err());
    // 只差大小写的名字算同一个
    again("alice", AgentRules::default(), None).await.unwrap();
    assert!(again("Alice", AgentRules::default(), None).await.unwrap_err().to_string().contains("already taken"));
    let bad = AgentRules { allow: vec!["use:skill:nope".parse().unwrap()], ..Default::default() };
    assert!(again("carol", bad, None).await.is_err());
    assert!(again("carol", AgentRules::default(), Some("q:x")).await.is_err());
    assert!(!w.root().join("carol").exists());
    assert_eq!(w.service.list().await.unwrap().len(), 2);

    // 写库失败时撤回目录、工作目录与 access.toml
    let before = read(w.root().join("platform/access.toml"));
    w.db.write(|tx| {
        tx.execute_batch("CREATE TRIGGER no_insert BEFORE INSERT ON agents BEGIN SELECT RAISE(ABORT, 'nope'); END")
    })
    .await
    .unwrap();
    let rules = AgentRules { roles: vec!["admin".into()], ..Default::default() };
    assert!(again("dave", rules, None).await.is_err());
    assert!(!w.root().join("dave").exists());
    assert_eq!(read(w.root().join("platform/access.toml")), before);
    assert_eq!(std::fs::read_dir(w.workspaces()).unwrap().count(), 2);
}

#[tokio::test(flavor = "multi_thread")]
async fn update_renames_and_rolls_back() {
    let w = world().await;
    let id = w.create("alice").await;
    let access = w.root().join("platform/access.toml");
    write(&access, &format!("# 人写的注释\n[agents.{id}]\nroles = [\"admin\"]\n"));
    let toml = w.root().join("alice/agent.toml");
    write(&toml, &format!("# alice 的注释\n{}", read(toml.clone())));

    let patch = AgentPatch {
        name: Some("alice2".into()),
        config: AgentConfig { description: Some("新描述".into()), ..Default::default() },
        ..Default::default()
    };
    let info = w.service.update(&id, patch).await.unwrap();
    assert_eq!(info.name.as_str(), "alice2");
    assert!(!w.root().join("alice").exists());
    assert!(read(w.root().join("alice2/agent.toml")).starts_with("# alice 的注释\n"));
    assert!(read(w.root().join("alice2/agent.toml")).contains("新描述"));
    let text = read(access.clone());
    assert!(text.starts_with("# 人写的注释\n") && text.contains(&format!("[agents.{id}] # alice2\n")), "{text}");
    let view = w.service.view(&id).await.unwrap();
    assert_eq!(view.rules.roles, vec!["admin".to_owned()]);
    assert_eq!(view.config.unwrap().file.description(), "新描述");

    // 只改大小写也行（同一个目录）
    w.service.update(&id, AgentPatch { name: Some("Alice2".into()), ..Default::default() }).await.unwrap();
    assert_eq!(w.service.view(&id).await.unwrap().config.unwrap().name.as_str(), "Alice2");

    // 写库失败：目录挪回、agent.toml 与 access.toml 改回
    w.db.write(|tx| {
        tx.execute_batch(
            "CREATE TRIGGER no_rename BEFORE UPDATE OF name ON agents BEGIN SELECT RAISE(ABORT, 'nope'); END",
        )
    })
    .await
    .unwrap();
    let access_before = read(access.clone());
    let toml_before = read(w.root().join("Alice2/agent.toml"));
    let patch = AgentPatch {
        name: Some("bob".into()),
        config: AgentConfig { model: Some("p:other".parse().unwrap()), ..Default::default() },
        allow: Some(vec!["use:tool:read_file".parse().unwrap()]),
        ..Default::default()
    };
    assert!(w.service.update(&id, patch).await.is_err());
    assert!(!w.root().join("bob").exists());
    assert_eq!(read(w.root().join("Alice2/agent.toml")), toml_before);
    assert_eq!(read(access), access_before);
}

#[tokio::test(flavor = "multi_thread")]
async fn default_and_archive() {
    let w = world().await;
    let a = w.create("alice").await;
    let b = w.create("bob").await;
    let busy = Arc::new(std::sync::atomic::AtomicBool::new(true));
    let flag = busy.clone();
    let blocker: ArchiveBlocker = Arc::new(move |_c, _id| Ok(flag.load(std::sync::atomic::Ordering::SeqCst)));
    let life = AgentLifecycle::new(w.source.clone(), w.workspaces(), vec![], vec![("sessions".into(), blocker)]);

    w.service.set_default(&a, true).await.unwrap();
    w.service.set_default(&b, true).await.unwrap();
    assert!(!w.service.get(&a).await.unwrap().is_default, "at most one default");
    assert!(life.set_archived(&b, true).await.unwrap_err().to_string().contains("default agent"));
    assert!(life.set_archived(&a, true).await.unwrap_err().to_string().contains("working right now"));
    busy.store(false, std::sync::atomic::Ordering::SeqCst);
    assert!(life.set_archived(&a, true).await.unwrap().archived());
    let availability = w.db.read(move |c| agents::availability_in(c, &a)).await.unwrap();
    assert_eq!(availability, agents::Availability::Archived);
    let id = w.service.list().await.unwrap()[0].id.clone();
    assert!(w.service.set_default(&id, true).await.unwrap_err().to_string().contains("archived"));
    assert!(!life.set_archived(&id, false).await.unwrap().archived());
}

#[tokio::test(flavor = "multi_thread")]
async fn delete_cleans_up_and_keeps_shared_assets() {
    let w = world().await;
    let a = w.create("alice").await;
    let b = w.create("bob").await;
    let root = w.root();
    write(&root.join("alice/skills/used/SKILL.md"), "---\ndescription: 别人在用\n---\n");
    write(&root.join("alice/skills/mine/SKILL.md"), "---\ndescription: 只有自己用\n---\n");
    write(
        &access(&root),
        &format!("[agents.{a}]\nallow = [\"use:tool:read_file\"]\n[agents.{b}]\nallow = [\"use:skill:used\"]\n"),
    );
    std::fs::write(w.workspaces().join(a.as_str()).join("f"), "x").unwrap();

    // 一个领域的清理报错：整个回滚，agent 照常可用
    let refuse: DeletionHook = Arc::new(|_tx, _id| Err(muliao_platform::errors::DomainError::conflict("busy").into()));
    let life = AgentLifecycle::new(w.source.clone(), w.workspaces(), vec![refuse], vec![]);
    assert!(matches!(life.delete(&a, &Actor::User).await, Err(Error::Domain(_))));
    assert!(w.service.get_active(&a).await.is_ok());

    let calls = Arc::new(std::sync::Mutex::new(Vec::new()));
    let seen = calls.clone();
    let hook: DeletionHook = Arc::new(move |tx, id| {
        // 在标记的同一事务里：已看不到它
        assert!(agents::active_agent_in(tx, id).is_err());
        seen.lock().unwrap().push(id.to_owned());
        Ok(())
    });
    let life = AgentLifecycle::new(w.source.clone(), w.workspaces(), vec![hook], vec![]);
    life.delete(&a, &Actor::Agent(b.clone())).await.unwrap();
    assert_eq!(*calls.lock().unwrap(), vec![a.clone()]);
    assert!(w.service.get(&a).await.is_err(), "the row is gone");
    assert!(!root.join("alice").exists());
    assert!(!w.workspaces().join(a.as_str()).exists());
    let s = w.source.snapshot().await.unwrap();
    assert_eq!(s.get(StoredKind::Skill, "used").unwrap().owner, Owner::Platform);
    assert!(s.get(StoredKind::Skill, "mine").is_none());
    assert!(!read(access(&root)).contains(a.as_str()));
    assert!(s.problems.is_empty(), "{:?}", s.problems);
}

#[tokio::test(flavor = "multi_thread")]
async fn interrupted_deletion_resumes() {
    let w = world().await;
    let a = w.create("alice").await;
    // 标记了、文件还没删（上次删文件失败或服务重启）
    let id = a.clone();
    w.db.write(move |tx| {
        tx.execute("UPDATE agents SET deleting = 1 WHERE id = ?1", [&id])?;
        Ok::<_, rusqlite::Error>(())
    })
    .await
    .unwrap();
    // 正在删除的：不在列表里，资源快照不让往它目录里写
    assert!(w.service.list().await.unwrap().is_empty());
    let r = w.source.write(&[(w.root().join("alice/memory.md"), "x".into(), muliao_assets::tree::Expect::Any)]).await;
    assert!(r.unwrap_err().to_string().contains("being deleted"));
    let life = AgentLifecycle::new(w.source.clone(), w.workspaces(), vec![], vec![]);
    assert!(life.delete(&a, &Actor::User).await.unwrap_err().to_string().contains("already being deleted"));
    life.resume_deletions().await.unwrap();
    assert!(w.service.get(&a).await.is_err());
    assert!(!w.root().join("alice").exists());
}

#[tokio::test(flavor = "multi_thread")]
async fn failed_file_removal_is_reported_and_resumed() {
    use std::os::unix::fs::PermissionsExt;
    let w = world().await;
    let a = w.create("alice").await;
    let life = AgentLifecycle::new(w.source.clone(), w.workspaces(), vec![], vec![]);
    // 资源目录根删不了子目录：删文件失败
    let root = w.root();
    std::fs::set_permissions(&root, std::fs::Permissions::from_mode(0o555)).unwrap();
    let err = life.delete(&a, &Actor::User).await;
    std::fs::set_permissions(&root, std::fs::Permissions::from_mode(0o755)).unwrap();
    assert!(err.as_ref().is_err_and(|e| e.internal_as::<std::io::Error>().is_some()), "{err:?}");
    let info = w.service.get(&a).await.unwrap();
    assert_eq!(info.state, muliao_assets::agents::AgentState::Deleting, "it stays marked as being deleted");
    assert!(w.service.list().await.unwrap().is_empty());
    life.resume_deletions().await.unwrap();
    assert!(w.service.get(&a).await.is_err());
    assert!(!root.join("alice").exists());
}

fn access(root: &std::path::Path) -> std::path::PathBuf {
    root.join("platform/access.toml")
}
