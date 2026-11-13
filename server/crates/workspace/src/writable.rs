//! agent 能写哪里（#416）：命令（run_shell、后台任务、触发器的前置检查与 stream）与文件工具只能写可写路径，防 agent 误删、
//! 误写别处的东西。对 agent 无感：工具、说明、提示词都不提它；在范围内一切照旧，写到范围外时只在原本的系统报错后面补一句
//! 说明（[`denial_note`]）。
//!
//! 可写路径 = 它的工作目录 + 默认可写的几类路径（[`DEFAULT_WRITABLE`]）− 受保护的（[`PROTECTED`]）+ 它的
//! `write:path:<目录>` 授权 − 它的 deny 里的 `write:path:<目录>` − 整棵 AI 资源树 + 树里它能改的资源 − 它的 deny 收回的
//! 资源（assets 的 `write_scope`，#499）− 平台的私有文件（非 admin），后面的覆盖前面的。「能不能改某项资源」由此完全由
//! 沙箱回答。类别换成具体路径、命令怎么关进沙箱由平台层按当前系统做（`muliao_platform::sandbox`）。
//!
//! 读只拦一处（#539）：平台的私有文件（配置、备份、推送私钥）只有 admin 能读，命令由沙箱拒绝、文件工具由 [`reach`] 拒绝，
//! 补的说明同样走 `Confinement::explain`。防误操作（把密钥打进会话记录），不防故意。

use muliao_platform::ids::AgentId;
use std::collections::HashMap;
use std::path::{Component, Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use muliao_assets::distribution::Distribution;
use muliao_assets::service::workspace_of;
use muliao_platform::errors::{DomainError, ErrorKind};
use muliao_platform::fs::RealPath;
use muliao_platform::sandbox::{Category, Confinement, Sandbox, WritablePaths, covers, expand, real_forms};

use crate::text::split_lines;

/// 每个 agent 都能写的几类路径
pub const DEFAULT_WRITABLE: &[Category] = &[
    Category::Temp,
    Category::Devices,
    Category::UserCache,
    Category::Packages,
    Category::Toolchains,
    Category::ToolState,
];
/// 落在默认可写范围里、但不让写的（平台自身的安装）；要写须单独授权
pub const PROTECTED: &[Category] = &[Category::PlatformInstall];

fn note(path: &Path) -> String {
    // 读受系统保护的目录也报同样的错，分不出来：只说「如果这是一次写」
    format!(
        "[If this was a write: {} is outside the paths this agent may write (its workspace, temp, cache and toolchain \
         directories, plus directories granted to it), so the write sandbox refused it. If it really needs to write \
         there, ask the user or an agent with the admin role to grant it 'write:path:<directory>'.]",
        path.display()
    )
}

fn private_note(path: &Path) -> String {
    format!(
        "[{} is one of the platform's private files (its config with the login password and keys, its backups, its \
         push key): only an agent with the admin role may read or write them, so the sandbox refused it. If you need \
         something from them, ask the user or an admin agent.]",
        path.display()
    )
}

/// 按 agent 现在的分发规则与资源归属算出它的 Confinement：每次起命令、写文件时现算，改了授权下一条命令就生效。
/// 受保护的排在授权之前：`write:path` 授权它本身或它的上级目录都会放开它；资源树排在授权之后，只按归属。
pub struct Confiner {
    sandbox: Arc<dyn Sandbox>,
    workspaces_dir: PathBuf,
    distribution: Arc<Distribution>,
    /// 服务运行用户的家目录，与适配器的是同一个
    home: PathBuf,
    /// 平台的私有文件（配置的 private_files）：只有 admin 能读写
    private: Vec<PathBuf>,
    /// 每个 agent 一个：它的路径同一时间只有一处在认（卡住的只卡它自己）
    resolving: Mutex<HashMap<AgentId, Arc<Resolving>>>,
}

impl Confiner {
    pub fn new(
        sandbox: Arc<dyn Sandbox>,
        workspaces_dir: PathBuf,
        distribution: Arc<Distribution>,
        home: PathBuf,
        private: Vec<PathBuf>,
    ) -> Self {
        Self { sandbox, workspaces_dir, distribution, home, private, resolving: Mutex::default() }
    }

    fn resolving(&self, agent_id: &AgentId) -> Arc<Resolving> {
        self.resolving.lock().expect("resolving lock").entry(agent_id.to_owned()).or_default().clone()
    }

    pub async fn of(&self, agent_id: &AgentId) -> muliao_platform::errors::Result<Confinement> {
        let scope = Arc::new(self.distribution.write_scope(agent_id).await?);
        let private = if scope.admin { Vec::new() } else { self.private.clone() };
        // 规则的形式已在读入时校验：绝对路径或 ~/ 开头
        let mut rules: Vec<(bool, PathBuf)> = vec![(true, workspace_of(&self.workspaces_dir, agent_id))];
        rules.extend(DEFAULT_WRITABLE.iter().flat_map(|c| self.sandbox.paths(*c)).map(|p| (true, p)));
        rules.extend(PROTECTED.iter().flat_map(|c| self.sandbox.paths(*c)).map(|p| (false, p)));
        rules.extend(scope.granted.iter().map(|p| (true, expand(p, &self.home))));
        rules.extend(scope.revoked.iter().map(|p| (false, expand(p, &self.home))));
        // 资源树只按规则与归属管：排在 write:path 之后，祖先目录的授权（如 ~/.muliao）不放开 access.toml
        rules.push((false, scope.tree.clone()));
        rules.extend(scope.assets.iter().map(|p| (true, p.clone())));
        rules.extend(scope.withheld.iter().map(|p| (false, p.clone())));
        // 平台的私有文件排在最后：任何授权都放不开
        rules.extend(private.iter().map(|p| (false, p.clone())));
        // 换成真实路径要读文件系统，限时（见 [`Resolving`]）
        let owner = agent_id.clone();
        let (writable, unreadable) = self
            .resolving(agent_id)
            .run(RESOLVE_LIMIT, move |resolve| {
                let unreadable: Vec<_> = private.iter().flat_map(|p| resolve(p)).collect();
                let rules: Vec<_> =
                    rules.into_iter().flat_map(|(w, p)| resolve(&p).into_iter().map(move |f| (w, f))).collect();
                (WritablePaths::resolved(rules, owner), unreadable)
            })
            .await
            .map_err(|stuck| {
                tracing::warn!(agent = %agent_id, path = ?stuck, "resolving a writable path is stuck");
                DomainError::new(ErrorKind::Conflict, stuck_message(&stuck))
            })?;
        let private_forms = unreadable.clone();
        let explain = Arc::new(move |path: &Path| {
            if matches!(covers(&private_forms, path), Ok(true)) {
                return private_note(path);
            }
            scope.refusal(path).unwrap_or_else(|| note(path))
        });
        Ok(Confinement { sandbox: self.sandbox.clone(), writable, home: self.home.clone(), explain, unreadable })
    }
}

/// 认一个 agent 的可写路径最多等这么久。正常只要几毫秒；macOS 上碰到要授权才能访问的目录（如 iCloud 云盘），系统在等用户
/// 回答文件访问授权弹窗时，读文件系统的调用会一直不返回（#722：muliao 的前置检查与 run_shell 因此卡了两个小时，前置检查
/// 自己的时限管不到起命令之前的这一步）。
const RESOLVE_LIMIT: Duration = Duration::from_secs(10);

/// 一个 agent 认路径的过程：同一时间只有一处在认，记着正在认哪个路径，卡住时报出它。
///
/// 卡住的系统调用打断不了，只能不等它：调用方到时报错，那个线程留着等它自己返回。卡住期间后来的调用排队等到时限就报错、
/// 不再起新线程：否则每分钟一次的前置检查会一个个占满阻塞线程池，拖垮数据库等所有用它的地方。
#[derive(Default)]
struct Resolving {
    gate: Arc<tokio::sync::Mutex<()>>,
    current: Mutex<Option<PathBuf>>,
}

impl Resolving {
    /// 在阻塞线程里跑 work（它经传给它的 resolve 认路径），连排队在内最多等 limit；到时返回卡在哪个路径上（不知道时 None）。
    async fn run<T: Send + 'static>(
        self: Arc<Self>,
        limit: Duration,
        work: impl FnOnce(&dyn Fn(&Path) -> Vec<RealPath>) -> T + Send + 'static,
    ) -> Result<T, Option<PathBuf>> {
        let deadline = tokio::time::Instant::now() + limit;
        let Ok(turn) = tokio::time::timeout_at(deadline, self.gate.clone().lock_owned()).await else {
            return Err(self.stuck_at());
        };
        let this = self.clone();
        let job = tokio::task::spawn_blocking(move || {
            let _turn = turn;
            let resolve = |p: &Path| {
                *this.current.lock().expect("current lock") = Some(p.to_path_buf());
                real_forms(p)
            };
            let out = work(&resolve);
            *this.current.lock().expect("current lock") = None;
            out
        });
        match tokio::time::timeout_at(deadline, job).await {
            Ok(done) => Ok(done.expect("resolving writable paths panicked")),
            Err(_) => Err(self.stuck_at()),
        }
    }

    fn stuck_at(&self) -> Option<PathBuf> {
        self.current.lock().expect("current lock").clone()
    }
}

fn stuck_message(path: &Option<PathBuf>) -> String {
    let what = path.as_ref().map_or_else(|| "a path".to_owned(), |p| p.display().to_string());
    format!(
        "Could not work out where this agent may write, so nothing was run or written: checking {what} on disk has not \
         returned for over {}s. On macOS this usually means the system is waiting for someone to answer a file access \
         permission prompt (e.g. for iCloud Drive) on the Mac running muliao, or the muliao service lacks Full Disk \
         Access; tell the user. It works again by itself once that check returns.",
        RESOLVE_LIMIT.as_secs()
    )
}

/// 文件工具对一个路径要做的：只读（read_file、share_file），或写（write_file、edit_file；写的也要读得了）。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Access {
    Read,
    Write,
}

/// 模型给的 path 换成绝对路径：`~` 展开，相对路径以 workspace 为基准，`.` 段与多余的 `/` 去掉（同 pathlib，报错与回显里
/// 的写法与 Python 版一样）；`..` 原样保留。`~user/…`（别的用户的家目录）不展开、按相对路径算：agent 都以服务的用户
/// 运行，用不到它。
pub fn absolute(workspace: &Path, home: &Path, path: &str) -> PathBuf {
    let p = if path == "~" {
        home.to_path_buf()
    } else if let Some(rest) = path.strip_prefix("~/") {
        home.join(rest)
    } else {
        PathBuf::from(path)
    };
    let p = if p.is_absolute() { p } else { workspace.join(p) };
    p.components().filter(|c| *c != Component::CurDir).collect()
}

/// 文件工具拿路径的唯一入口：模型给的 path 换成绝对路径（[`absolute`]），并检查这个 agent 能不能这样访问它（按路径的
/// 身份：大小写不同、经符号链接的写法都按它指向的文件判断）。不能时 Forbidden，说法与命令里被沙箱拒绝时一样，后面补同一句
/// 说明。返回的是模型的写法（展示、报错里用），身份另按 real 取。
pub fn reach(confinement: &Confinement, workspace: &Path, path: &str, access: Access) -> Result<PathBuf, DomainError> {
    let target = absolute(workspace, &confinement.home, path);
    if confinement.readable(&target) && (access == Access::Read || confinement.writable.permits(&target)) {
        return Ok(target);
    }
    Err(DomainError::new(
        ErrorKind::Forbidden,
        format!("[Errno 1] Operation not permitted: '{}'\n{}", target.display(), (confinement.explain)(&target)),
    ))
}

/// 报错里的路径：绝对路径或 `~/` 开头，到空白、引号或冒号为止，前面不能紧挨着词字符、`.`、`~`、`/`、`-`（带空格的路径
/// 只取到空格前，可能拿错：只影响补不补说明）。同 Python 的 `(?<![\w.~/-])(~/[^\s'":]*|/[^\s'":]+)`。
fn paths_in(line: &str) -> Vec<&str> {
    let stops = |c: char| c.is_whitespace() || c == '\'' || c == '"' || c == ':';
    let joins = |c: char| c.is_alphanumeric() || c == '_' || matches!(c, '.' | '~' | '/' | '-');
    let body = |from: usize| from + line[from..].find(stops).unwrap_or(line.len() - from);
    let mut found = Vec::new();
    let mut prev: Option<char> = None;
    let mut i = 0;
    while let Some(c) = line[i..].chars().next() {
        if prev.is_none_or(|p| !joins(p)) {
            let end = if line[i..].starts_with("~/") {
                Some(body(i + 2))
            } else if c == '/' {
                Some(body(i + 1)).filter(|end| *end > i + 1)
            } else {
                None
            };
            if let Some(end) = end {
                found.push(&line[i..end]);
                // 接着从匹配之后找（不重叠）
                prev = line[..end].chars().next_back();
                i = end;
                continue;
            }
        }
        prev = Some(c);
        i += c.len_utf8();
    }
    found
}

/// 命令输出里有被拒绝的操作、且报错里的路径在可写范围外时补的说明；范围内的拒绝不是写沙箱拦的，不补。只认绝对路径与
/// `~/` 开头的路径：相对路径不知道命令当时在哪个目录。写沙箱拒绝时系统报的错一律是 EPERM：各语言的说法大小写不一（Node
/// `EPERM: operation not permitted`、Go `operation not permitted`）；EACCES（Permission denied）不是它拦的，不认。
pub fn denial_note(confinement: &Confinement, output: &str) -> Option<String> {
    for line in split_lines(output) {
        if !line.to_lowercase().contains("operation not permitted") {
            continue;
        }
        for found in paths_in(line) {
            let path = expand(found, &confinement.home);
            if !confinement.writable.permits(&path) {
                return Some((confinement.explain)(&path));
            }
        }
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn finds_paths_like_the_python_pattern() {
        assert_eq!(paths_in("touch: /etc/x: Operation not permitted"), ["/etc/x"]);
        assert_eq!(paths_in("cannot write '~/a/b' here"), ["~/a/b"]);
        // 紧挨着词字符、点、横线的不算（相对路径的一部分）
        assert_eq!(paths_in("a/b ./c x-/d"), Vec::<&str>::new());
        assert_eq!(paths_in("EPERM: open \"/tmp/q z\""), ["/tmp/q"]);
        assert_eq!(paths_in("/a /b"), ["/a", "/b"]);
        assert_eq!(paths_in("/ alone"), Vec::<&str>::new());
        assert_eq!(paths_in("x /中文/路径 y"), ["/中文/路径"]);
    }

    #[test]
    fn absolute_paths_drop_dot_segments() {
        let (ws, home) = (Path::new("/ws"), Path::new("/h"));
        assert_eq!(absolute(ws, home, "./a//b/"), PathBuf::from("/ws/a/b"));
        assert_eq!(absolute(ws, home, "~/x"), PathBuf::from("/h/x"));
        assert_eq!(absolute(ws, home, "/abs/../y"), PathBuf::from("/abs/../y"));
    }

    /// #722：认路径卡住（等系统授权弹窗）时到时报出卡在哪个路径；卡住期间后来的不再起新线程，也到时报错；卡住的返回后恢复
    #[tokio::test]
    async fn stuck_resolution_times_out_without_piling_up_threads() {
        use std::sync::atomic::{AtomicBool, Ordering};
        let limit = Duration::from_millis(200);
        let resolving = Arc::new(Resolving::default());
        let slow = Path::new("/slow");

        let started = std::time::Instant::now();
        let stuck = resolving.clone().run(limit, move |resolve| {
            resolve(slow);
            std::thread::sleep(Duration::from_millis(800));
        });
        assert_eq!(stuck.await.unwrap_err(), Some(slow.to_path_buf()));
        assert!(started.elapsed() < Duration::from_millis(600));

        let ran = Arc::new(AtomicBool::new(false));
        let flag = ran.clone();
        let queued = resolving.clone().run(limit, move |_| flag.store(true, Ordering::SeqCst)).await;
        assert_eq!(queued.unwrap_err(), Some(slow.to_path_buf()));
        assert!(!ran.load(Ordering::SeqCst), "must not start another resolution while one is stuck");

        tokio::time::sleep(Duration::from_millis(600)).await;
        let tmp = std::env::temp_dir();
        let forms = resolving.clone().run(limit, move |resolve| resolve(&tmp)).await.unwrap();
        assert!(!forms.is_empty());
        assert_eq!(resolving.stuck_at(), None);
    }
}
