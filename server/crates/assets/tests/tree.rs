//! 读树、快照缓存、写入口、分发与写沙箱的领域输入。

mod common;

use common::{World, world, write};
use muliao_assets::access::{Actor, grants_for, write_scope};
use muliao_assets::kinds::StoredKind;
use muliao_assets::tree::Owner;
use muliao_assets::{AssetRejected, Error};

fn skill(dir: &std::path::Path, name: &str, description: &str) {
    write(
        &dir.join(format!("skills/{name}/SKILL.md")),
        &format!("---\nname: {name}\ndescription: {description}\n---\nbody\n"),
    );
}

fn reasons_for<'a>(snapshot: &'a muliao_assets::tree::Snapshot, needle: &str) -> Vec<&'a str> {
    snapshot.problems.iter().filter(|p| p.path.to_string_lossy().contains(needle)).map(|p| p.reason.as_str()).collect()
}

async fn two_agents() -> (World, String, String) {
    let w = world().await;
    let a = w.create("alice").await;
    let b = w.create("bob").await;
    (w, a, b)
}

#[tokio::test(flavor = "multi_thread")]
async fn conflicts_broken_files_and_platform_priority() {
    let (w, a, b) = two_agents().await;
    let root = w.root();
    skill(&root.join("platform"), "shared", "平台的");
    skill(&root.join("alice"), "shared", "alice 复制的");
    skill(&root.join("alice"), "dup", "a");
    skill(&root.join("bob"), "dup", "b");
    skill(&root.join("bob"), "solo", "bob 的");
    write(&root.join("bob/skills/broken/SKILL.md"), "no frontmatter");
    write(&root.join("bob/mcp/Bad_Name.json"), "{}");
    write(&root.join("bob/notes.txt"), "x");
    write(&root.join("stray.txt"), "x");
    write(&root.join(".hidden/x"), "x");
    write(&root.join("platform/memory/big.md"), &"x".repeat(150));

    let s = w.source.snapshot().await.unwrap();
    assert_eq!(s.get(StoredKind::Skill, "shared").unwrap().owner, Owner::Platform);
    assert!(reasons_for(&s, "alice/skills/shared")[0].contains("already used by the platform's"));
    assert_eq!(s.shadowed(StoredKind::Skill, "shared").len(), 1);
    assert!(s.get(StoredKind::Skill, "dup").is_none());
    assert_eq!(reasons_for(&s, "skills/dup").len(), 2);
    assert!(s.check_free(StoredKind::Skill, "dup").is_err(), "conflicting names stay taken");
    assert!(s.check_free(StoredKind::Skill, "broken").is_err(), "broken ones stay taken");
    assert_eq!(s.get(StoredKind::Skill, "solo").unwrap().owner, Owner::Agent(b.clone()));
    assert!(reasons_for(&s, "Bad_Name.json")[0].contains("invalid MCP server name"));
    assert!(reasons_for(&s, "bob/notes.txt")[0].contains("not a place for an asset file"));
    assert!(reasons_for(&s, "stray.txt")[0].contains("only platform/ and agent directories"));
    assert!(reasons_for(&s, ".hidden").is_empty());
    // 超长的记忆照常加载，并报出
    assert!(s.get(StoredKind::Memory, "big").is_some());
    assert!(reasons_for(&s, "big.md")[0].contains("over the limit"));
    // 问题报给相关的 agent；平台的只报给 admin
    assert!(s.problems_for(&a).iter().all(|p| p.agent.as_deref() == Some(a.as_str())));
    assert!(s.problems_for(&b).iter().any(|p| p.path.ends_with("skills/broken")));
    assert!(!s.problems_for(&b).iter().any(|p| p.path.ends_with("stray.txt")));
    // 私有记忆：显示名是 agent 名、规则不按名字指它
    let private = s.private_memory(&a).unwrap();
    assert_eq!(private.name, "alice");
    assert_eq!(private.rule_name(), None);
}

#[tokio::test(flavor = "multi_thread")]
async fn agent_directories_must_match_the_database() {
    let (w, a, _b) = two_agents().await;
    let root = w.root();
    // 手动改名的目录对不上任何 agent：不加载，agent 自己没有目录
    std::fs::rename(root.join("alice"), root.join("alice2")).unwrap();
    let s = w.source.snapshot().await.unwrap();
    assert!(!s.agents.contains_key(&a));
    assert!(reasons_for(&s, "alice2")[0].contains("no agent is named"));
    assert!(s.loaded_agent(&a).unwrap_err().to_string().contains("has no directory named"));
    assert!(s.owner_dir(&Owner::Agent(a.clone())).is_err());
    std::fs::rename(root.join("alice2"), root.join("alice")).unwrap();

    // agent.toml 坏了：报给它
    let toml = std::fs::read_to_string(root.join("alice/agent.toml")).unwrap();
    write(&root.join("alice/agent.toml"), &format!("id = \"{a}\"\n{toml}"));
    let s = w.source.snapshot().await.unwrap();
    assert!(!s.agents.contains_key(&a));
    assert!(s.problems_for(&a).iter().any(|p| p.reason.contains("unknown keys")));

    // 模型在当前配置下不能用：照常加载，记成问题
    write(&root.join("alice/agent.toml"), &toml.replace("p:default", "q:x"));
    let s = w.source.snapshot().await.unwrap();
    assert!(s.agents.contains_key(&a));
    assert!(s.problems_for(&a).iter().any(|p| p.reason.contains("cannot run until this is fixed")));
}

#[tokio::test(flavor = "multi_thread")]
async fn snapshot_is_cached_and_access_toml_survives_breakage() {
    let (w, a, _b) = two_agents().await;
    let access = w.root().join("platform/access.toml");
    write(&access, &format!("[agents.{a}]\nroles = [\"admin\"]\n"));
    let s1 = w.source.snapshot().await.unwrap();
    assert!(std::sync::Arc::ptr_eq(&s1, &w.source.snapshot().await.unwrap()), "unchanged tree is not re-read");
    assert!(s1.administers(&a));
    // 写坏整份：沿用上一份，并报给 admin
    write(&access, &format!("[agents.{a}]\nroles = [\"admin\"]\ndeny = [\"nonsense\"]\n"));
    let s2 = w.source.snapshot().await.unwrap();
    assert!(!std::sync::Arc::ptr_eq(&s1, &s2));
    assert!(s2.administers(&a), "the last good rules stay");
    assert!(s2.problems_for(&a).iter().any(|p| p.reason.contains("last read successfully")));
    // 平台这时拒绝改它
    let err = w.service.set_default(&a, true).await;
    assert!(err.is_ok());
    let err = muliao_assets::distribution::Distribution::new(w.source.clone())
        .create(&muliao_assets::distribution::RoleSpec {
            name: "r".into(),
            description: "d".into(),
            rules: Default::default(),
        })
        .await
        .unwrap_err();
    assert!(err.to_string().contains("fix it first"), "{err}");
}

#[tokio::test(flavor = "multi_thread")]
async fn write_entry_vets_like_the_reader() {
    let (w, a, _b) = two_agents().await;
    let root = w.root();
    let rejected = |e: Error| match e {
        Error::Rejected(r) => r,
        other => panic!("not rejected: {other:?}"),
    };
    let write = |path: std::path::PathBuf, text: &str| {
        let source = w.source.clone();
        let text = text.to_owned();
        async move { source.write(&[(path, text, muliao_assets::tree::Expect::Any)]).await }
    };
    // 记忆规整空白
    let out = write(root.join("alice/memory.md"), "a  \n\n\n\nb\n\n").await.unwrap();
    assert_eq!(out[0].1.text, "a\n\nb\n");
    // 新 secret 不能手写
    let r: AssetRejected = rejected(write(root.join("alice/secrets/K.toml"), "ciphertext = \"x\"").await.unwrap_err());
    assert!(r.reason.contains("vault_set"));
    assert!(r.for_agent().contains("section 'Secrets'"), "{}", r.for_agent());
    assert!(r.whose.contains("alice"));
    // agent.toml 按格式校验
    let toml = std::fs::read_to_string(root.join("alice/agent.toml")).unwrap();
    let r = rejected(write(root.join("alice/agent.toml"), &format!("{toml}extra = 1\n")).await.unwrap_err());
    assert!(r.reason.contains("unknown keys"));
    // 模型不能用的不写
    assert!(write(root.join("alice/agent.toml"), &toml.replace("p:default", "q:x")).await.is_err());
    // skill 附件要先有 SKILL.md；SKILL.md 按 frontmatter 校验；approval 要指向存在的脚本
    assert!(write(root.join("alice/skills/s/run.sh"), "echo").await.is_err());
    assert!(
        write(root.join("alice/skills/s/SKILL.md"), "---\ndescription: d\napproval: [run.sh]\n---\n").await.is_err()
    );
    write(root.join("alice/skills/s/SKILL.md"), "---\ndescription: d\n---\n").await.unwrap();
    write(root.join("alice/skills/s/run.sh"), "echo").await.unwrap();
    write(root.join("alice/skills/s/SKILL.md"), "---\ndescription: d\napproval: [run.sh]\n---\n").await.unwrap();
    // 同名的不让新建
    assert!(write(root.join("bob/skills/s/SKILL.md"), "---\ndescription: d\n---\n").await.is_err());
    // 不在任何 agent 目录、根目录、目录本身
    assert!(write(root.join("nobody/memory.md"), "x").await.is_err());
    assert!(write(root.join("x.md"), "x").await.is_err());
    assert!(write(root.join("alice/skills"), "x").await.is_err());
    // access.toml：新加的规则要匹配得上
    let access = root.join("platform/access.toml");
    assert!(write(access.clone(), &format!("[agents.{a}]\nallow = [\"use:skill:typo\"]\n")).await.is_err());
    // 节名是 id，写成名字的拒绝
    assert!(write(access.clone(), "[agents.alice]\nallow = [\"use:skill:s\"]\n").await.is_err());
    // 新加的节 id 要是现有的 agent
    assert!(write(access.clone(), "[agents.0123456789ab]\nallow = [\"use:skill:s\"]\n").await.is_err());
    write(access, &format!("[agents.{a}]\nallow = [\"use:skill:s\"]\n")).await.unwrap();
    // 隐藏文件不校验
    write(root.join("alice/.cache"), "x").await.unwrap();
    // 有一份不合预期就一份都不写
    use muliao_assets::tree::Expect;
    let r = w
        .source
        .write(&[
            (root.join("alice/memory.md"), "changed".into(), Expect::Any),
            (root.join("alice/skills/s/SKILL.md"), "---\ndescription: d\n---\n".into(), Expect::Absent),
        ])
        .await;
    assert!(r.unwrap_err().to_string().contains("already exists"));
    let stale = Expect::Version(muliao_assets::tree::version_of("old"));
    assert!(w.source.write(&[(root.join("alice/memory.md"), "changed".into(), stale)]).await.is_err());
    assert_eq!(std::fs::read_to_string(root.join("alice/memory.md")).unwrap(), "a\n\nb\n");
}

#[tokio::test(flavor = "multi_thread")]
async fn grants_and_write_scope() {
    let (w, a, b) = two_agents().await;
    let root = w.root();
    skill(&root.join("bob"), "bobs", "bob 的");
    write(&root.join("platform/memory/team.md"), "共享\n");
    write(
        &root.join("platform/access.toml"),
        &format!(
            "[roles.team]\ndescription = \"团队\"\nallow = [\"*:memory:team\", \"use:skill:bobs\"]\n\
             [agents.{a}]\nroles = [\"team\"]\nallow = [\"write:path:~/projects/x\"]\ndeny = [\"use:tool:run_shell\"]\n\
             [agents.{b}]\nroles = [\"admin\"]\ndeny = [\"write:memory:global\"]\n"
        ),
    );
    let s = w.source.snapshot().await.unwrap();
    let alice = grants_for(&s, &Actor::Agent(a.clone()));
    let team = s.get(StoredKind::Memory, "team").unwrap();
    assert!(alice.uses(team) && alice.writes(team));
    let bobs = s.get(StoredKind::Skill, "bobs").unwrap();
    assert!(alice.uses(bobs) && !alice.writes(bobs));
    assert!(!alice.rules().includes(muliao_assets::kinds::Kind::Tool, Some("run_shell")));
    assert!(!alice.rules().includes(muliao_assets::kinds::Kind::Tool, Some("create_agent")));
    // 私有记忆只有自己与 admin
    assert!(!alice.uses(s.private_memory(&b).unwrap()));

    let scope = write_scope(&s, &a);
    assert!(!scope.admin);
    assert!(scope.assets.contains(&root.join("alice")));
    assert!(scope.assets.contains(&team.path));
    assert!(!scope.assets.contains(&bobs.path));
    assert_eq!(scope.granted, vec!["~/projects/x".to_owned()]);
    let refusal = scope.refusal(&bobs.path.join("SKILL.md")).unwrap();
    assert!(refusal.contains("write:skill:bobs"), "{refusal}");
    assert!(scope.refusal(&root.join("bob/x")).unwrap().contains("agent \"bob\"'s directory"));
    assert!(scope.refusal(&root.join("platform/access.toml")).unwrap().contains("only by an admin"));
    assert!(scope.refusal(&w.dir.path().join("elsewhere")).is_none());

    let scope = write_scope(&s, &b);
    assert!(scope.admin);
    assert_eq!(scope.assets, vec![root.clone()]);
    assert_eq!(scope.withheld, vec![root.join("platform/memory/global.md")]);
}
