//! 起命令的底层：写沙箱的两种模式、secret 注入与脱敏、超时与取消杀进程组、输出读法。真起 bash 与 sandbox-exec。

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::{Duration, Instant};

use muliao_platform::fs::real;
use muliao_platform::sandbox::{Category, Confinement, Mode, WritablePaths, sandbox_for};
use muliao_platform::shell::{Command, ShellResult, run_bash};

struct Env {
    _dir: tempfile::TempDir,
    /// 可写的工作目录
    work: PathBuf,
    /// 可写目录之外的目录
    outside: PathBuf,
    /// 读不了内容的文件
    private: PathBuf,
}

fn env() -> Env {
    let dir = tempfile::tempdir().unwrap();
    let base = real(dir.path()).unwrap();
    let work = base.join("work");
    let outside = base.join("outside");
    std::fs::create_dir_all(&work).unwrap();
    std::fs::create_dir_all(&outside).unwrap();
    let private = base.join("private.toml");
    std::fs::write(&private, "key = 1\n").unwrap();
    Env { _dir: dir, work, outside, private }
}

fn confinement(mode: Mode, e: &Env) -> Confinement {
    let home = std::env::home_dir().unwrap();
    let sandbox = sandbox_for(mode, &home).unwrap();
    let mut rules: Vec<(bool, PathBuf)> = Vec::new();
    rules.extend(sandbox.paths(Category::Devices).into_iter().map(|p| (true, p)));
    rules.push((true, e.work.clone()));
    // 可写目录的上级（临时目录）不放开：outside 必须写不了
    Confinement {
        sandbox,
        writable: WritablePaths::of(rules, "test-agent"),
        home,
        explain: Arc::new(|p: &Path| format!("{} is not writable", p.display())),
        unreadable: vec![e.private.clone()],
    }
}

async fn run(
    c: &Confinement,
    cwd: &Path,
    script: &str,
    secrets: &BTreeMap<String, String>,
    timeout: Duration,
) -> ShellResult {
    let cmd = Command { script, cwd: cwd.to_path_buf(), secrets, confinement: c, merge_stderr: false, pipefail: false };
    run_bash(&cmd, timeout, None).await.unwrap()
}

fn none() -> BTreeMap<String, String> {
    BTreeMap::new()
}

#[tokio::test]
async fn allowed_work_gives_the_same_output_in_both_modes() {
    let e = env();
    let script = "mkdir -p sub && echo hello > sub/f && cat sub/f && ls sub && echo err >&2; exit 3";
    let enforce = run(&confinement(Mode::Enforce, &e), &e.work, script, &none(), Duration::from_secs(30)).await;
    std::fs::remove_dir_all(e.work.join("sub")).unwrap();
    let report = run(&confinement(Mode::Report, &e), &e.work, script, &none(), Duration::from_secs(30)).await;
    assert_eq!(enforce, report);
    assert_eq!(enforce.exit_code, Some(3));
    assert_eq!(enforce.stdout, "hello\nf\n");
    assert_eq!(enforce.stderr, "err\n");
    assert!(!enforce.left_running);
}

#[tokio::test]
async fn writes_outside_are_refused_only_when_enforcing() {
    let e = env();
    let target = e.outside.join("x");
    let script = format!("echo x > '{}' && cat '{}'", target.display(), e.private.display());
    let enforce = run(&confinement(Mode::Enforce, &e), &e.work, &script, &none(), Duration::from_secs(30)).await;
    assert_ne!(enforce.exit_code, Some(0));
    assert!(enforce.stderr.contains("Operation not permitted"), "{enforce:?}");
    assert!(!target.exists());

    let report = run(&confinement(Mode::Report, &e), &e.work, &script, &none(), Duration::from_secs(30)).await;
    assert_eq!(report.exit_code, Some(0), "{report:?}");
    assert_eq!(report.stdout, "key = 1\n");
    assert!(target.exists());
}

#[tokio::test]
async fn private_files_are_unreadable_but_visible() {
    let e = env();
    let c = confinement(Mode::Enforce, &e);
    let script = format!("test -f '{p}' && echo exists; cat '{p}'", p = e.private.display());
    let r = run(&c, &e.work, &script, &none(), Duration::from_secs(30)).await;
    assert_eq!(r.stdout, "exists\n");
    assert!(r.stderr.contains("Operation not permitted"), "{r:?}");
    assert!(!c.readable(&e.private));
    assert!(c.readable(&e.work.join("x")));
}

#[tokio::test]
async fn secrets_are_injected_redacted_and_env_is_minimal() {
    let e = env();
    let c = confinement(Mode::Enforce, &e);
    let secrets = BTreeMap::from([("API_TOKEN".to_owned(), "tok-123456".to_owned())]);
    // SAFETY: 测试进程里设一个变量，看它不被继承（这个测试文件里别的测试不读它）
    unsafe { std::env::set_var("MULIAO_TEST_NOT_INHERITED", "1") };
    let r = run(
        &c,
        &e.work,
        "echo $API_TOKEN; echo \"[${MULIAO_TEST_NOT_INHERITED:-unset}]\"; echo $API_TOKEN >&2",
        &secrets,
        Duration::from_secs(30),
    )
    .await;
    assert_eq!(r.stdout, "[secret:API_TOKEN]\n[unset]\n");
    assert_eq!(r.stderr, "[secret:API_TOKEN]\n");
}

#[tokio::test]
async fn timeout_kills_the_whole_group() {
    let e = env();
    let c = confinement(Mode::Enforce, &e);
    let marker = e.work.join("survived");
    let script = format!("echo started; (sleep 2; touch '{}') & sleep 60", marker.display());
    let started = Instant::now();
    let r = run(&c, &e.work, &script, &none(), Duration::from_millis(500)).await;
    assert!(started.elapsed() < Duration::from_secs(4));
    assert_eq!(r.exit_code, None);
    assert_eq!(r.stdout, "started\n", "output before the kill is kept");
    tokio::time::sleep(Duration::from_secs(3)).await;
    assert!(!marker.exists(), "the background child was killed with the group");
}

#[tokio::test]
async fn cancelling_kills_the_group() {
    let e = env();
    let c = confinement(Mode::Enforce, &e);
    let marker = e.work.join("survived");
    let script = format!("sleep 2; touch '{}'", marker.display());
    let cmd = Command {
        script: &script,
        cwd: e.work.clone(),
        secrets: &none(),
        confinement: &c,
        merge_stderr: false,
        pipefail: false,
    };
    let r = tokio::time::timeout(Duration::from_millis(300), run_bash(&cmd, Duration::from_secs(60), None)).await;
    assert!(r.is_err());
    tokio::time::sleep(Duration::from_secs(3)).await;
    assert!(!marker.exists());
}

#[tokio::test]
async fn exit_does_not_wait_for_descendants_holding_the_pipe() {
    let e = env();
    let c = confinement(Mode::Enforce, &e);
    let started = Instant::now();
    let r = run(&c, &e.work, "echo before; sleep 20 &", &none(), Duration::from_secs(60)).await;
    let took = started.elapsed();
    assert_eq!(r.exit_code, Some(0));
    assert_eq!(r.stdout, "before\n");
    assert!(r.left_running);
    assert!(took < Duration::from_secs(10), "{took:?}");
}

#[tokio::test]
async fn merged_output_stdin_and_pipefail() {
    let e = env();
    let c = confinement(Mode::Enforce, &e);
    let secrets = none();
    let merged = Command {
        script: "echo a; echo b >&2; echo c",
        cwd: e.work.clone(),
        secrets: &secrets,
        confinement: &c,
        merge_stderr: true,
        pipefail: false,
    };
    let r = run_bash(&merged, Duration::from_secs(30), None).await.unwrap();
    assert_eq!((r.stdout.as_str(), r.stderr.as_str()), ("a\nb\nc\n", ""));

    let cat = Command {
        script: "wc -c",
        cwd: e.work.clone(),
        secrets: &secrets,
        confinement: &c,
        merge_stderr: false,
        pipefail: false,
    };
    let big = vec![b'x'; 1 << 20];
    let r = run_bash(&cat, Duration::from_secs(30), Some(big)).await.unwrap();
    assert_eq!(r.stdout.trim(), "1048576");
    // 命令不读 stdin 就退出：不算错
    let ignore = Command { script: "true", ..cat };
    assert_eq!(run_bash(&ignore, Duration::from_secs(30), Some(vec![b'x'; 1 << 20])).await.unwrap().exit_code, Some(0));

    let pipe = Command {
        script: "false | true",
        cwd: e.work.clone(),
        secrets: &secrets,
        confinement: &c,
        merge_stderr: false,
        pipefail: true,
    };
    assert_eq!(run_bash(&pipe, Duration::from_secs(30), None).await.unwrap().exit_code, Some(1));
    let nopipe = Command { pipefail: false, ..pipe };
    assert_eq!(run_bash(&nopipe, Duration::from_secs(30), None).await.unwrap().exit_code, Some(0));
}

#[tokio::test]
async fn signal_exit_is_negative() {
    let e = env();
    let c = confinement(Mode::Enforce, &e);
    let r = run(&c, &e.work, "kill -TERM $$", &none(), Duration::from_secs(30)).await;
    assert_eq!(r.exit_code, Some(-15));
}
