//! 起一条 bash 命令、读它的输出、杀它的进程组的底层。以 agent 的身份起命令（哪些 secret、哪个目录、哪个沙箱）由工作区
//! 领域拼成 [`Command`] 交进来，这里只按给的做；agent 的命令都经这里起，没有别的入口。
//!
//! 只继承少量环境变量，按 key 注入 secret，输出里的 secret 明文替换为 `[secret:KEY]`；自成进程组，超时或被取消时杀掉
//! 整个进程组。命令关在写沙箱里（[`crate::sandbox`]）：只能写给它的可写路径；读、网络不限。

use std::collections::BTreeMap;
use std::io;
use std::os::fd::OwnedFd;
use std::os::unix::process::ExitStatusExt;
use std::path::PathBuf;
use std::pin::Pin;
use std::process::Stdio;
use std::time::Duration;

use tokio::io::{AsyncRead, AsyncReadExt, AsyncWriteExt};
use tokio::process::{Child, ChildStdin};

use crate::sandbox::Confinement;

/// 只继承这些环境变量，不把服务进程的其余环境带给命令
const INHERITED_ENV: &[&str] = &["PATH", "HOME", "LANG", "LC_ALL", "TERM", "TMPDIR", "USER", "SHELL"];
/// 进程结束（或被杀）后读完管道里剩余输出的时限：脱离进程组的后代可能一直持有管道
pub const DRAIN: Duration = Duration::from_secs(5);
/// 进程已退出、它留在后台的后代还占着输出管道时附在结果里的说明（前台与后台任务同一句）
pub const LEFT_RUNNING_NOTE: &str =
    "Processes it left running in the background still hold its output; their later output is not recorded.";

/// 要起的一条命令：bash -c `script`，在 cwd 里，注入 secrets，关在 confinement 里。
pub struct Command<'a> {
    pub script: &'a str,
    pub cwd: PathBuf,
    /// key → 明文：作为同名环境变量注入，输出里出现的明文替换为 `[secret:KEY]`
    pub secrets: &'a BTreeMap<String, String>,
    pub confinement: &'a Confinement,
    /// stderr 并进 stdout（同一个管道，先后顺序与终端里看到的一致）
    pub merge_stderr: bool,
    /// 管道里任何一步失败都算失败（bash 默认只看最后一步），给靠退出码判断成败的调用方
    pub pipefail: bool,
}

/// 一个输出管道
pub type Output = Pin<Box<dyn AsyncRead + Send>>;

/// 起了、还没等的命令。它的进程组归调用方处置（[`kill_group`]）：drop 它不会杀进程。
pub struct Spawned {
    pub child: Child,
    /// 进程组 id（等于 bash 的 pid）
    pub pgid: i32,
    pub stdin: Option<ChildStdin>,
    pub stdout: Output,
    /// merge_stderr 时为 None
    pub stderr: Option<Output>,
}

/// 起命令、不等它结束（后台任务、stream 触发器用）：只继承少量环境变量、注入 secret、关在写沙箱里、自成进程组
/// （[`kill_group`] 能杀掉它的整棵进程树）。stdin：给它一个可写的标准输入，否则接 /dev/null。
pub fn spawn_bash(cmd: &Command<'_>, stdin: bool) -> io::Result<Spawned> {
    std::fs::create_dir_all(&cmd.cwd)?;
    let mut bash = vec!["bash".to_owned()];
    if cmd.pipefail {
        bash.extend(["-o".to_owned(), "pipefail".to_owned()]);
    }
    bash.extend(["-c".to_owned(), cmd.script.to_owned()]);
    let argv = cmd.confinement.wrap(&bash);

    let mut command = tokio::process::Command::new(&argv[0]);
    command.args(&argv[1..]).current_dir(&cmd.cwd).env_clear().process_group(0);
    for key in INHERITED_ENV {
        if let Some(v) = std::env::var_os(key) {
            command.env(key, v);
        }
    }
    command.envs(cmd.secrets.iter());
    command.stdin(if stdin { Stdio::piped() } else { Stdio::null() });

    // 并进同一个管道：两个描述符指向同一个写端，读端交给调用方
    let merged = if cmd.merge_stderr {
        let (reader, writer) = io::pipe()?;
        command.stdout(writer.try_clone()?).stderr(writer);
        Some(reader)
    } else {
        command.stdout(Stdio::piped()).stderr(Stdio::piped());
        None
    };
    let mut child = command.spawn()?;
    // 父进程手里的写端随 command 一起关掉，否则读端永远等不到 EOF
    drop(command);

    let pgid = child.id().ok_or_else(|| io::Error::other("spawned process has no pid"))? as i32;
    let (stdout, stderr): (Output, Option<Output>) = match merged {
        Some(reader) => (Box::pin(receiver(reader.into())?), None),
        None => (
            Box::pin(child.stdout.take().expect("stdout is piped")),
            Some(Box::pin(child.stderr.take().expect("stderr is piped")) as Output),
        ),
    };
    Ok(Spawned { pgid, stdin: child.stdin.take(), stdout, stderr, child })
}

fn receiver(fd: OwnedFd) -> io::Result<tokio::net::unix::pipe::Receiver> {
    tokio::net::unix::pipe::Receiver::from_owned_fd(fd)
}

/// 杀掉整个进程组。进程恰好已经退出、组里只剩没被回收的进程（macOS 返回 EPERM）都不算错：调用方只对自己还持有的
/// 进程组调用，pid 不会已被别人复用。
pub fn kill_group(pgid: i32) {
    // SAFETY: killpg 只发信号，不碰内存
    unsafe {
        libc::killpg(pgid, libc::SIGKILL);
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ShellResult {
    /// 超时被杀时为 None；被信号杀死的是负的信号值（同 Python 的 returncode）
    pub exit_code: Option<i32>,
    pub stdout: String,
    /// merge_stderr 时 stderr 已并入 stdout，这里为空
    pub stderr: String,
    /// 进程退出了，但它留在后台的后代还占着输出管道：之后的输出没有读
    pub left_running: bool,
}

/// 被取消（future 被 drop）时杀掉还在跑的命令的进程组。进程已被回收、不该再杀时把它置为 None。
pub struct KillOnDrop(pub Option<i32>);

impl Drop for KillOnDrop {
    fn drop(&mut self) {
        if let Some(pgid) = self.0 {
            kill_group(pgid);
        }
    }
}

async fn pump(reader: &mut Output, buf: &mut Vec<u8>) -> io::Result<()> {
    let mut chunk = vec![0u8; 65536];
    loop {
        let n = reader.read(&mut chunk).await?;
        if n == 0 {
            return Ok(());
        }
        buf.extend_from_slice(&chunk[..n]);
    }
}

async fn pump_all(
    stdout: &mut Output,
    out: &mut Vec<u8>,
    stderr: Option<&mut Output>,
    err: &mut Vec<u8>,
) -> io::Result<()> {
    match stderr {
        Some(stderr) => tokio::try_join!(pump(stdout, out), pump(stderr, err)).map(|_| ()),
        None => pump(stdout, out).await,
    }
}

async fn feed(stdin: Option<ChildStdin>, data: Option<Vec<u8>>) {
    let (Some(mut stdin), Some(data)) = (stdin, data) else { return };
    // 命令不读 stdin 就退出了（BrokenPipe）是正常的，结果看它的退出码与输出；写完关掉让它读到 EOF
    let _ = stdin.write_all(&data).await;
    let _ = stdin.shutdown().await;
}

/// 跑到结束（前台 run_shell、前置检查）。timeout 只限命令运行的时间：超时杀掉整个进程组，限时读完杀掉之前写进管道的
/// 输出；在时限前退出的，退出后最多再给 [`DRAIN`] 读完输出（它留在后台的后代可能一直占着管道），读不完的如实标出。
/// 命令退出后留在后台的后代不杀（同后台任务）：要脱离会话长跑的东西本来就该这样起。
/// 这个 future 被取消（停止会话、关停）时杀掉进程组。
pub async fn run_bash(cmd: &Command<'_>, timeout: Duration, stdin: Option<Vec<u8>>) -> io::Result<ShellResult> {
    let Spawned { mut child, pgid, stdin: child_stdin, mut stdout, mut stderr } = spawn_bash(cmd, stdin.is_some())?;
    let mut guard = KillOnDrop(Some(pgid));
    let (mut out, mut err) = (Vec::new(), Vec::new());

    let (exit, drained) = {
        let mut reads = std::pin::pin!(pump_all(&mut stdout, &mut out, stderr.as_mut(), &mut err));
        let mut fed = std::pin::pin!(feed(child_stdin, stdin));
        let mut read_done = false;
        let mut fed_done = false;
        let deadline = tokio::time::sleep(timeout);
        tokio::pin!(deadline);
        let status = loop {
            tokio::select! {
                r = &mut reads, if !read_done => { r?; read_done = true; }
                _ = &mut fed, if !fed_done => { fed_done = true; }
                s = child.wait() => break Some(s?),
                _ = &mut deadline => break None,
            }
        };
        match status {
            Some(status) => {
                // 已回收：进程组 id 可能被复用，之后不再杀它
                guard.0 = None;
                if !read_done {
                    // 读出错照常报出；只有到时没读完才算「后代占着管道」
                    if let Ok(r) = tokio::time::timeout(DRAIN, &mut reads).await {
                        r?;
                        read_done = true;
                    }
                }
                let code = status.code().unwrap_or_else(|| -status.signal().unwrap_or(0));
                (Some(code), read_done)
            }
            None => {
                kill_group(pgid);
                let _ = tokio::time::timeout(DRAIN, &mut reads).await;
                (None, true)
            }
        }
    };
    if exit.is_none() {
        // 杀掉之后回收，不留僵尸
        let _ = child.wait().await;
        guard.0 = None;
    }
    Ok(ShellResult {
        exit_code: exit,
        stdout: redact(&String::from_utf8_lossy(&out), cmd.secrets),
        stderr: redact(&String::from_utf8_lossy(&err), cmd.secrets),
        left_running: !drained,
    })
}

/// 一遍扫描替换所有 secret，同一位置优先匹配最长的：逐个替换时，A 是 B 的子串而 A 先替换，B 的其余部分就会漏出来；
/// 先替换出的 `[secret:KEY]` 也可能被后面的 secret 再次匹配。
pub fn redact(text: &str, secrets: &BTreeMap<String, String>) -> String {
    let mut values: Vec<(&str, &str)> =
        secrets.iter().filter(|(_, v)| !v.is_empty()).map(|(k, v)| (v.as_str(), k.as_str())).collect();
    if values.is_empty() {
        return text.to_owned();
    }
    values.sort_by_key(|(v, _)| std::cmp::Reverse(v.len()));
    let mut out = String::with_capacity(text.len());
    let mut i = 0;
    'scan: while i < text.len() {
        let rest = &text[i..];
        for (value, key) in &values {
            if rest.starts_with(value) {
                out.push_str("[secret:");
                out.push_str(key);
                out.push(']');
                i += value.len();
                continue 'scan;
            }
        }
        let c = rest.chars().next().expect("not at the end");
        out.push(c);
        i += c.len_utf8();
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn redact_prefers_longest_and_does_not_rescan() {
        let secrets = BTreeMap::from([
            ("A".to_owned(), "abc".to_owned()),
            ("B".to_owned(), "abcdef".to_owned()),
            ("C".to_owned(), "secret".to_owned()),
        ]);
        assert_eq!(redact("x abcdef abc y", &secrets), "x [secret:B] [secret:A] y");
        // 替换出的文本不再被匹配
        let s = BTreeMap::from([("K".to_owned(), "secret:K".to_owned()), ("S".to_owned(), "[secret".to_owned())]);
        assert_eq!(redact("[secret:K]", &s), "[secret:S]:K]");
        assert_eq!(redact("中文 abc", &secrets), "中文 [secret:A]");
    }
}
