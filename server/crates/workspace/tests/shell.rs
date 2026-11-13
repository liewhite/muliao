//! run_shell、后台任务、skill_run 与「始终允许」（照 Python 版 tests/test_jobs.py、test_skill_run.py 的主要用例）。

mod common;

use std::time::Duration;

use agenter::Approval;
use common::{Out, World, world};
use muliao_sessions::events::SessionStopped;
use muliao_sessions::mailbox::MailBody;
use muliao_workspace::grants::GRANTED_NOTE;
use serde_json::json;

const SCRIPT: &str = "#!/bin/sh\necho \"args: $*\"\necho \"key is $DEPLOY_KEY\"\n";

fn make_skill(w: &World, name: &str, script: &str, executable: bool, approval: bool) -> std::path::PathBuf {
    let base = w.asset_dir("me").join("skills").join(name);
    std::fs::create_dir_all(base.join("scripts")).unwrap();
    let approval = if approval { "approval: [scripts/deploy.sh]\n" } else { "" };
    std::fs::write(
        base.join("SKILL.md"),
        format!("---\ndescription: deploy things\nname: {name}\n{approval}---\n\nRun scripts/deploy.sh\n"),
    )
    .unwrap();
    let path = base.join("scripts/deploy.sh");
    std::fs::write(&path, script).unwrap();
    if executable {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o755)).unwrap();
    }
    base
}

fn job_id(started: &str) -> i64 {
    started.split_whitespace().nth(3).unwrap().trim_end_matches(';').parse().unwrap()
}

async fn wait_done(w: &World, id: i64) -> muliao_workspace::jobs::JobInfo {
    for _ in 0..200 {
        let info = w.workspace.jobs.info(id, &w.me).await.unwrap();
        if info.status != "running" {
            return info;
        }
        tokio::time::sleep(Duration::from_millis(50)).await;
    }
    panic!("job {id} did not finish");
}

#[tokio::test]
async fn foreground_commands_report_exit_code_and_output() {
    let w = world(20_000).await;
    let out = w
        .run(vec![
            ("run_shell", json!({"command": "echo hi; echo err >&2; exit 3", "timeout": 10})),
            ("run_shell", json!({"command": "sleep 5", "timeout": 0.3})),
            ("run_shell", json!({"command": "pwd", "timeout": 10})),
            ("run_shell", json!({"command": "true"})),
            ("run_shell", json!({"command": "true", "timeout": 10, "heartbeat_seconds": 5})),
            ("run_shell", json!({"command": "true", "timeout": 0})),
            ("run_shell", json!({"command": "true", "background": true, "timeout": 5})),
            ("run_shell", json!({"command": "true", "background": true, "heartbeat_seconds": 0})),
            ("run_shell", json!({"command": "echo x > /etc/muliao-test", "timeout": 10})),
            ("run_shell", json!({"command": "yes | head -c 30000", "timeout": 10})),
        ])
        .await;
    assert_eq!(out[0], "exit_code: 3\nhi\nerr\n");
    assert_eq!(out[1], "killed after 0.3s timeout\n");
    assert!(out[2].starts_with("exit_code: 0\n/") && out[2].ends_with(&format!("/workspaces/{}\n", w.me)));
    assert!(out[3].starts_with("timeout is required for a foreground command"));
    assert_eq!(out[4], "heartbeat_seconds is only for background jobs; a foreground command uses `timeout`");
    assert_eq!(out[5], "timeout must be greater than 0 seconds");
    assert!(out[6].starts_with("background jobs have no timeout"));
    assert_eq!(out[7], "heartbeat_seconds must be greater than 0");
    assert!(
        out[8].starts_with("exit_code: 1\n") && out[8].contains("/etc/muliao-test is outside the paths"),
        "{}",
        out[8]
    );
    assert!(out[9].ends_with("…[truncated: 10000 more chars]"));
}

#[tokio::test]
async fn the_description_carries_the_configured_output_limit() {
    let w = world(12_345).await;
    let tool = w.tools.iter().find(|t| t.name() == "run_shell").unwrap();
    assert!(tool.tool.def().description.contains("Only the first 12345 characters of output are kept"));
}

#[tokio::test]
async fn secrets_are_injected_and_redacted() {
    let w = world(20_000).await;
    w.set_secret("TOKEN", "tok-123456", true).await;
    w.set_secret("DEPLOY_KEY", "s3cr3t-deploy", false).await;
    make_skill(&w, "ops", SCRIPT, true, false);
    let out = w
        .run(vec![
            ("run_shell", json!({"command": "echo $TOKEN", "timeout": 10, "secrets": ["TOKEN"]})),
            ("run_shell", json!({"command": "echo $DEPLOY_KEY", "timeout": 10, "secrets": ["DEPLOY_KEY"]})),
            (
                "skill_run",
                json!({"skill": "ops", "script": "scripts/deploy.sh", "args": ["prod", "a b"], "timeout": 10, "secrets": ["DEPLOY_KEY"]}),
            ),
        ])
        .await;
    assert_eq!(out[0], "exit_code: 0\n[secret:TOKEN]\n");
    assert!(out[1].contains("not allowed in shell commands") && !out[1].contains("s3cr3t"), "{}", out[1]);
    assert_eq!(out[2], "exit_code: 0\nargs: prod a b\nkey is [secret:DEPLOY_KEY]\n");
}

#[tokio::test]
async fn skill_run_checks_the_skill_and_the_script() {
    let w = world(20_000).await;
    let base = make_skill(&w, "plain", SCRIPT, false, false);
    std::os::unix::fs::symlink("/bin/echo", base.join("scripts/out.sh")).unwrap();
    let out = w
        .run(vec![
            ("skill_run", json!({"skill": "theirs", "script": "scripts/deploy.sh", "timeout": 10})),
            ("skill_run", json!({"skill": "plain", "script": "../../SKILL.md", "timeout": 10})),
            ("skill_run", json!({"skill": "plain", "script": "scripts/out.sh", "timeout": 10})),
            ("skill_run", json!({"skill": "plain", "script": "scripts/missing.sh", "timeout": 10})),
            ("skill_run", json!({"skill": "plain", "script": "scripts/deploy.sh", "timeout": 10})),
        ])
        .await;
    assert!(out[0].contains("skill \"theirs\" not found") || out[0].contains("skill 'theirs' not found"), "{}", out[0]);
    assert!(out[1].contains("inside the skill's directory"));
    assert!(out[2].contains("inside the skill's directory"));
    assert!(out[3].contains("has no script 'scripts/missing.sh'"));
    assert!(out[4].contains("is not executable"));
}

#[tokio::test]
async fn approved_scripts_wait_for_the_user_and_always_allow_is_remembered() {
    let w = world(20_000).await;
    let base = make_skill(&w, "ops", SCRIPT, true, true);
    let args = json!({"skill": "ops", "script": "scripts/deploy.sh", "args": ["x"], "timeout": 10});
    let scope = w.scope();
    // 参数不对的不去问
    let bad = w.call_in(&scope, "skill_run", json!({"skill": "ops", "script": "scripts/deploy.sh"}), None).await.0;
    assert!(bad.retry().starts_with("timeout is required"));
    let Out::NeedsApproval(pin) = w.call_in(&scope, "skill_run", args.clone(), None).await.0 else { panic!() };
    // 批准的不是现在这一份：不执行
    let stale = Approval { always: false, pin: "old".into() };
    let out = w.call_in(&scope, "skill_run", args.clone(), Some(&stale)).await.0;
    assert!(out.retry().contains("are not the version the user approved"));
    // 批准一次（不始终允许）：执行，下次照样要问
    let once = Approval { always: false, pin: pin.clone() };
    assert_eq!(
        w.call_in(&scope, "skill_run", args.clone(), Some(&once)).await.0.ok(),
        "exit_code: 0\nargs: x\nkey is \n"
    );
    assert!(matches!(w.call_in(&scope, "skill_run", args.clone(), None).await.0, Out::NeedsApproval(_)));
    // 始终允许：记下，之后同一调用直接执行并注明
    let always = Approval { always: true, pin: pin.clone() };
    w.call_in(&scope, "skill_run", args.clone(), Some(&always)).await.0.ok();
    let again = w.call_in(&scope, "skill_run", args.clone(), None).await.0;
    assert_eq!(again.ok(), format!("{GRANTED_NOTE}\nexit_code: 0\nargs: x\nkey is \n"));
    let grants = w.workspace.grants.list().await.unwrap();
    assert_eq!((grants.len(), grants[0].hits, grants[0].script.as_str()), (1, 1, "scripts/deploy.sh"));
    // 参数不同、脚本改了：都要重新问；改 SKILL.md 不算
    let other = json!({"skill": "ops", "script": "scripts/deploy.sh", "args": ["y"], "timeout": 10});
    assert!(matches!(w.call_in(&scope, "skill_run", other, None).await.0, Out::NeedsApproval(_)));
    std::fs::write(base.join("SKILL.md"), "---\ndescription: changed\nname: ops\napproval: [scripts/deploy.sh]\n---\n")
        .unwrap();
    w.call_in(&scope, "skill_run", args.clone(), None).await.0.ok();
    std::fs::write(base.join("scripts/deploy.sh"), "#!/bin/sh\necho changed\n").unwrap();
    assert!(matches!(w.call_in(&scope, "skill_run", args.clone(), None).await.0, Out::NeedsApproval(_)));
    // 撤销后重新要批准
    w.workspace.grants.revoke(grants[0].id).await.unwrap();
    assert!(w.workspace.grants.list().await.unwrap().is_empty());
}

#[tokio::test]
async fn a_finished_background_job_reports_back_with_its_output() {
    let w = world(20_000).await;
    w.set_secret("TOKEN", "tok-123456", true).await;
    let started = w
        .call(
            "run_shell",
            json!({"command": "echo $TOKEN; echo done; exit 2", "background": true, "secrets": ["TOKEN"]}),
        )
        .await;
    let started = started.ok();
    let id = job_id(started);
    assert_eq!(
        started,
        format!(
            "started background job {id}; output goes to jobs/{id}.log.\nYou will get a check-in event every 60 seconds \
             while it runs and an event when it finishes; keep working or end your reply."
        )
    );
    let info = wait_done(&w, id).await;
    assert_eq!((info.status, info.exit_code), ("exited", Some(2)));
    assert_eq!(std::fs::read_to_string(w.ws().join(&info.log_path)).unwrap(), "[secret:TOKEN]\ndone\n");
    let mail = w.queued().await;
    assert_eq!(mail.len(), 1);
    assert!(matches!(&mail[0].body, MailBody::Event { source, .. } if *source == format!("job:{id}")));
    let text = mail[0].body.content();
    assert!(
        text.starts_with(&format!("Background job {id} finished with exit code 2 after 0m00s.\nCommand: echo $TOKEN")),
        "{text}"
    );
    assert!(text.ends_with(&format!(
        "Last lines of output:\n[secret:TOKEN]\ndone\nFull output: jobs/{id}.log (in your workspace)"
    )));
    let status = w.call("job_status", json!({"job_id": id})).await;
    assert!(status.ok().starts_with(&format!("job {id}: exited with code 2 (0m00s)\ncommand: ")));
    assert_eq!(
        w.call("job_kill", json!({"job_id": id})).await.retry(),
        format!("job {id} is not running (status: exited)")
    );
    assert_eq!(w.call("job_status", json!({"job_id": 999})).await.retry(), "job 999 not found");
}

#[tokio::test]
async fn running_jobs_get_check_events_without_piling_up_and_can_be_killed() {
    let w = world(20_000).await;
    let started = w
        .call("run_shell", json!({"command": "echo working; sleep 30", "background": true, "heartbeat_seconds": 0.1}))
        .await;
    let id = job_id(started.ok());
    tokio::time::sleep(Duration::from_millis(400)).await;
    assert_eq!(w.workspace.jobs.check().await.unwrap(), 1);
    tokio::time::sleep(Duration::from_millis(200)).await;
    // 上一封还排着：不再投
    w.workspace.jobs.check().await.unwrap();
    let mail = w.queued().await;
    assert_eq!(mail.len(), 1);
    assert!(matches!(mail[0].body, MailBody::Event { check_in: true, .. }));
    assert!(mail[0].body.content().starts_with(&format!("Background job {id} is still running (0m00s so far).")));
    assert!(mail[0].body.content().contains("Last lines of output:\nworking\n"));
    assert!(
        mail[0].body.content().ends_with("you will get another check-in in 0.1 seconds and an event when it finishes.")
    );
    assert_eq!(
        w.call("job_kill", json!({"job_id": id})).await.ok(),
        format!("sent kill signal to job {id}; its final status will arrive as an event")
    );
    let info = wait_done(&w, id).await;
    assert_eq!(info.status, "killed");
    let mail = w.queued().await;
    assert!(
        mail.last()
            .unwrap()
            .body
            .content()
            .starts_with(&format!("Background job {id} was killed (job_kill) after 0m00s."))
    );
}

#[tokio::test]
async fn a_job_ends_when_its_process_exits_even_if_a_grandchild_keeps_the_output_open() {
    let w = world(20_000).await;
    let started = w.call("run_shell", json!({"command": "(sleep 30 &); echo parent", "background": true})).await;
    let id = job_id(started.ok());
    let info = wait_done(&w, id).await;
    assert_eq!(info.status, "exited");
    let mail = w.queued().await;
    assert!(
        mail[0].body.content().contains(&format!("Note: {}", muliao_platform::shell::LEFT_RUNNING_NOTE)),
        "{}",
        mail[0].body.content()
    );
}

#[tokio::test]
async fn stopping_the_session_kills_its_jobs() {
    let w = world(20_000).await;
    let started = w.call("run_shell", json!({"command": "sleep 30", "background": true})).await;
    let id = job_id(started.ok());
    // 会话领域在停止的事务里发布 SessionStopped（这里直接交给订阅者）
    let jobs = w.workspace.jobs.clone();
    let session = w.session.clone();
    w.db.write(move |tx| {
        jobs.on_stopped(tx, &SessionStopped { session_id: session, cleared: vec![muliao_sessions::holds::HoldOf::Job { job_id: id }] });
        Ok::<_, rusqlite::Error>(())
    })
    .await
    .unwrap();
    assert_eq!(wait_done(&w, id).await.status, "killed");
}

#[tokio::test]
async fn jobs_left_running_by_a_previous_process_are_reported_interrupted() {
    let w = world(20_000).await;
    let started = w.call("run_shell", json!({"command": "sleep 30", "background": true})).await;
    let id = job_id(started.ok());
    // 另一个服务进程（没有照看者）启动时收掉它
    let fresh = muliao_workspace::JobRunner::new(
        w.db.clone(),
        w.workspace.commands.clone(),
        muliao_sessions::work::SessionWork::new(muliao_sessions::actor::Waker::channel().0),
    );
    fresh.run().await.unwrap().abort();
    let info = fresh.info(id, &w.me).await.unwrap();
    assert_eq!(info.status, "interrupted");
    let mail = w.queued().await;
    let text = mail[0].body.content();
    assert!(
        text.contains("was interrupted after 0m00s: the service stopped unexpectedly. Its process group"),
        "{text}"
    );
    assert!(text.contains("may still be running: check with `pgrep -g"));
    // 原来的照看者结束时不重复记结局（记一条 error）；进程这里自己杀掉
    w.call("job_kill", json!({"job_id": id})).await;
}

#[tokio::test]
async fn shutting_down_reports_running_jobs_as_interrupted() {
    let w = world(20_000).await;
    let started = w.call("run_shell", json!({"command": "sleep 30", "background": true})).await;
    let id = job_id(started.ok());
    w.workspace.jobs.shutdown().await;
    let info = w.workspace.jobs.info(id, &w.me).await.unwrap();
    assert_eq!(info.status, "interrupted");
    let mail = w.queued().await;
    assert!(mail[0].body.content().starts_with(&format!(
        "Background job {id} was interrupted after 0m00s: the service was stopped. It may have partly run."
    )));
}

#[tokio::test]
async fn a_background_skill_run_redacts_its_log() {
    let w = world(20_000).await;
    w.set_secret("DEPLOY_KEY", "s3cr3t-deploy", false).await;
    make_skill(&w, "ops", SCRIPT, true, false);
    let started = w
        .call(
            "skill_run",
            json!({"skill": "ops", "script": "scripts/deploy.sh", "secrets": ["DEPLOY_KEY"], "background": true}),
        )
        .await;
    let info = wait_done(&w, job_id(started.ok())).await;
    assert_eq!(std::fs::read_to_string(w.ws().join(&info.log_path)).unwrap(), "args: \nkey is [secret:DEPLOY_KEY]\n");
}

#[tokio::test]
async fn an_unreadable_log_does_not_keep_the_job_from_ending() {
    let w = world(20_000).await;
    let started = w.call("run_shell", json!({"command": "sleep 0.5; echo x", "background": true})).await;
    let id = job_id(started.ok());
    tokio::time::sleep(Duration::from_millis(200)).await;
    let log = w.ws().join(format!("jobs/{id}.log"));
    use std::os::unix::fs::PermissionsExt;
    std::fs::set_permissions(&log, std::fs::Permissions::from_mode(0o000)).unwrap();
    let info = wait_done(&w, id).await;
    std::fs::set_permissions(&log, std::fs::Permissions::from_mode(0o644)).unwrap();
    assert_eq!(info.status, "exited");
    assert!(w.queued().await[0].body.content().contains("Last lines of output:\n(could not read the log: "));
}
