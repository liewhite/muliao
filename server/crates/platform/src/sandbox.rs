//! 写沙箱（#416）：agent 的命令只能写给它的可写路径，别处的写被操作系统拒绝；读不了指定的私有文件的内容。防 agent
//! 误删、误写别处的东西，不防故意绕过（见架构文档「安全边界」）。
//!
//! 与操作系统无关的部分在这里：可写路径（[`WritablePaths`]）、路径的类别（[`Category`]）、适配器的接口（[`Sandbox`]）与
//! 按当前系统挑适配器（[`sandbox_for`]）。每个系统一个适配器（现在只有 macOS 的 [`Seatbelt`]），把类别换成本系统的具体
//! 路径、把命令包进本系统的沙箱；当前系统没有适配器时报错，不退回到不隔离。哪些类别默认可写、agent 的授权怎么并进来，
//! 是工作区领域的事。

use crate::ids::AgentId;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use crate::fs::{RealPath, is_within, real, real_nofollow};

/// 一类路径：领域按类别声明默认可写与受保护的范围，适配器换成本系统的具体路径。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Category {
    /// 临时目录（/tmp 与每个用户的临时目录）
    Temp,
    /// 设备文件（/dev/null、终端）
    Devices,
    /// 用户缓存（XDG 缓存、系统的用户缓存目录）
    UserCache,
    /// 包管理器的安装目录与包缓存（Homebrew、~/.local、npm、Maven 等）
    Packages,
    /// 语言工具链（pyenv、nvm、cargo、rustup、go 等）
    Toolchains,
    /// 工具运行时写的状态：写不了会报错或卡住、丢了也无害（known_hosts、docker / kube 配置缓存、REPL 历史）
    ToolState,
    /// 平台自身的安装（运行中的服务的可执行文件）
    PlatformInstall,
}

/// `~/x` 换成 home 下的路径，其余原样（授权规则与报错里的路径共用）。
pub fn expand(path: &str, home: &Path) -> PathBuf {
    match path.strip_prefix("~/") {
        Some(rest) => home.join(rest),
        None => PathBuf::from(path),
    }
}

/// 路径在文件系统里的身份：链接本身的，以及它指向的。系统按真实路径判断，而 /tmp 这类上级是链接的路径、本身是链接
/// 的路径两种都会被写到。
///
/// 认不出（系统拒绝打开路径上的某一级，如 iCloud 目录间歇报 EPERM）时记 warning、退回字面的绝对路径：一条规则的路径
/// 出问题只影响这一条，不让整组规则、每个 agent 的每次运行跟着失败（#624）。
pub fn real_forms(path: &Path) -> Vec<RealPath> {
    match (real_nofollow(path), real(path)) {
        (Ok(link), Ok(target)) if link == target => vec![link],
        (Ok(link), Ok(target)) => vec![link, target],
        (Err(e), _) | (_, Err(e)) => {
            let literal = std::path::absolute(path).unwrap_or_else(|_| path.to_path_buf());
            tracing::warn!(path = %literal.display(), error = %e, "cannot resolve path on disk, using it as written");
            vec![RealPath::assumed(literal)]
        }
    }
}

/// path（绝对路径）按它的真实路径落在 paths 的某一个里：就是它，或在它下面。认不出 path 的真实路径时报错，由调用方
/// 按「不行」处理（不退回字面路径：换个大小写就能绕过）。
pub fn covers(paths: &[RealPath], path: &Path) -> std::io::Result<bool> {
    let target = real(path)?;
    Ok(paths.iter().any(|p| is_within(p, &target)))
}

/// 一组可写路径：按顺序的 (可写, 路径)，路径都是真实路径（[`real_forms`]）。一个路径能不能写看最后一条覆盖它的规则
/// （规则的路径是它本身或它的上级目录），没有覆盖它的就不能写。后面的规则覆盖前面的：先放开一类目录，再收回其中一部分。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct WritablePaths {
    rules: Vec<(bool, RealPath)>,
    /// 这组路径属于谁（agent 的 id）：只用于系统日志里认出是谁的命令
    owner: AgentId,
}

impl WritablePaths {
    /// rules 的路径可以带符号链接：在这里换成真实路径（读文件系统）。
    pub fn of(rules: impl IntoIterator<Item = (bool, PathBuf)>, owner: AgentId) -> Self {
        let rules = rules.into_iter().flat_map(|(w, p)| real_forms(&p).into_iter().map(move |form| (w, form)));
        Self::resolved(rules, owner)
    }

    /// 规则的路径已由调用方换成真实路径（[`real_forms`]）时用：调用方要自己掌握逐条读文件系统的过程（如限时）。
    pub fn resolved(rules: impl IntoIterator<Item = (bool, RealPath)>, owner: AgentId) -> Self {
        Self { rules: rules.into_iter().collect(), owner }
    }

    pub fn rules(&self) -> &[(bool, RealPath)] {
        &self.rules
    }

    pub fn owner(&self) -> &AgentId {
        &self.owner
    }

    /// path（绝对路径）能不能写：按它最终写到的真实路径判断；认不出真实路径的不能写。
    pub fn permits(&self, path: &Path) -> bool {
        let Ok(target) = real(path) else { return false };
        let mut verdict = false;
        for (writable, rule) in &self.rules {
            if is_within(rule, &target) {
                verdict = *writable;
            }
        }
        verdict
    }
}

/// 一个系统的适配器。
pub trait Sandbox: Send + Sync {
    /// 这一类在本系统的具体路径（不存在的也列出：以后建出来就能写）。
    fn paths(&self, category: Category) -> Vec<PathBuf>;

    /// 把命令包进沙箱：返回实际要执行的 argv，它与它起的所有子进程只能写 writable 允许的路径，读不了 unreadable 里的
    /// 文件内容（真实路径，文件或目录；存不存在、大小这些元数据照样看得到）。
    fn wrap(&self, argv: &[String], writable: &WritablePaths, unreadable: &[RealPath]) -> Vec<String>;
}

/// 当前系统的适配器（home：服务运行用户的家目录，家目录下的类别按它算）；没有时报错（启动时报出，不退回到不隔离）。
pub fn sandbox_for(home: &Path) -> Result<Arc<dyn Sandbox>, String> {
    if cfg!(target_os = "macos") && Seatbelt::supported() {
        return Ok(Arc::new(Seatbelt::new(home.to_path_buf())));
    }
    Err(format!(
        "no write sandbox is available on this system ({}); agents' shell commands cannot run unconfined",
        std::env::consts::OS
    ))
}

/// 被拒的路径补在系统报错后面的说明（领域给的：为什么不让、找谁）
pub type Explain = Arc<dyn Fn(&Path) -> String + Send + Sync>;

/// 一个 agent 的命令在什么限制下跑：本系统的适配器 + 它的可写路径。shell 用它包命令，文件工具用它检查写。
#[derive(Clone)]
pub struct Confinement {
    pub sandbox: Arc<dyn Sandbox>,
    pub writable: WritablePaths,
    /// 家目录：规则与报错里的 `~/` 按它展开
    pub home: PathBuf,
    pub explain: Explain,
    /// 读不了的文件或目录（真实路径，[`real_forms`]）
    pub unreadable: Vec<RealPath>,
}

impl Confinement {
    pub fn wrap(&self, argv: &[String]) -> Vec<String> {
        self.sandbox.wrap(argv, &self.writable, &self.unreadable)
    }

    /// path（绝对路径）的内容能不能读：按它真正读到的真实路径判断；认不出真实路径的不能读。
    pub fn readable(&self, path: &Path) -> bool {
        matches!(covers(&self.unreadable, path), Ok(false))
    }
}

// ---- 类 Unix 系统上相同的部分：家目录下各工具约定的目录 ----

const HOME_PACKAGES: &[&str] = &[".local", ".npm", ".yarn", ".m2", ".gradle", ".ivy2", ".sbt"];
const HOME_TOOLCHAINS: &[&str] = &[".pyenv", ".nvm", ".cargo", ".rustup", "go", ".foundry", ".svm", ".sdkman"];
const HOME_TOOL_STATE: &[&str] = &[
    ".ssh/known_hosts",
    ".docker",
    ".kube",
    ".aws/sso/cache",
    ".aws/cli/cache",
    ".ipython",
    ".matplotlib",
    // gcloud 刷新访问令牌时写它的凭据库（#416 试跑：sto-bard-deposit-monitor）
    ".config/gcloud",
    ".python_history",
    ".psql_history",
    ".node_repl_history",
    ".lesshst",
    ".viminfo",
];
const HOME_CACHE: &str = ".cache";

fn under(home: &Path, relative: &[&str]) -> Vec<PathBuf> {
    relative.iter().map(|r| home.join(r)).collect()
}

/// 运行中的服务的可执行文件（升级时由 deploy 脚本替换；agent 不能改它）。
fn platform_install() -> Vec<PathBuf> {
    std::env::current_exe().into_iter().collect()
}

// ---- macOS：sandbox-exec 加一份按可写路径生成的 Seatbelt 配置（SBPL） ----

const SANDBOX_EXEC: &str = "/usr/bin/sandbox-exec";
/// 在沙箱里跑不起来、只好放到沙箱外执行的程序（它们与它们的子进程写文件不受限，已知缺口）：
/// - setuid 程序：macOS 不让沙箱里的进程执行（与写规则无关），ps、top 常用；sudo / su / login 要密码
/// - 自己建沙箱的：沙箱里不能再套一层带规则的沙箱（sandbox_apply 报 EPERM），sandbox-exec 本身与 Chromium 系浏览器
const UNSANDBOXED_LITERALS: &[&str] = &[
    "/bin/ps",
    "/usr/bin/top",
    "/usr/bin/atq",
    "/usr/bin/crontab",
    "/usr/bin/atrm",
    "/usr/bin/newgrp",
    "/usr/bin/su",
    "/usr/bin/batch",
    "/usr/bin/at",
    "/usr/bin/quota",
    "/usr/bin/sudo",
    "/usr/bin/login",
    "/usr/sbin/traceroute",
    "/usr/sbin/traceroute6",
    SANDBOX_EXEC,
];
const UNSANDBOXED_REGEX: &str = r"/(chrome-headless-shell|Chromium|Google Chrome[^/]*)$";
/// 拒绝记录附带的说明，后面接 agent=<id>
pub const LOG_MESSAGE: &str = "muliao write sandbox";

/// macOS 的适配器。配置：先放开一切（只限制写，读、网络、进程照旧；跑不起来的几种程序放到沙箱外执行），再禁止一切
/// 文件写，然后按 [`WritablePaths`] 的顺序逐条放开或收回（SBPL 里后写的规则优先，与「最后一条覆盖它的规则」同义），最后
/// 禁止读不可读路径的内容。沙箱由子进程继承，nohup、python 起的后代同样受限。
pub struct Seatbelt {
    home: PathBuf,
}

impl Seatbelt {
    pub fn new(home: PathBuf) -> Self {
        Self { home }
    }

    pub fn supported() -> bool {
        Path::new(SANDBOX_EXEC).is_file()
    }

    pub fn profile(&self, writable: &WritablePaths, unreadable: &[RealPath]) -> String {
        let message = format!("(with message \"{LOG_MESSAGE} agent={}\")", writable.owner());
        let refuse = format!("(deny file-write* {message}");
        // 只禁读内容（file-read-data：文件内容与列目录），stat 照常：`ls -l` 上级目录、`test -f` 不报错
        let hide = format!("(deny file-read-data {message}");
        let programs: Vec<String> =
            UNSANDBOXED_LITERALS.iter().map(|p| format!("(literal {})", quote(Path::new(p)))).collect();
        let mut lines = vec![
            "(version 1)".to_owned(),
            "(allow default)".to_owned(),
            format!("(allow process-exec {} (regex #\"{UNSANDBOXED_REGEX}\") (with no-sandbox))", programs.join(" ")),
            format!("{refuse})"),
        ];
        for (allowed, path) in writable.rules() {
            let matcher = format!("(subpath {})", quote(path));
            lines.push(if *allowed {
                format!("(allow file-write* {matcher})")
            } else {
                format!("{refuse} {matcher})")
            });
        }
        lines.extend(unreadable.iter().map(|p| format!("{hide} (subpath {}))", quote(p))));
        lines.join("\n")
    }
}

fn quote(path: &Path) -> String {
    let text = path.to_string_lossy().replace('\\', "\\\\").replace('"', "\\\"");
    format!("\"{text}\"")
}

impl Sandbox for Seatbelt {
    fn paths(&self, category: Category) -> Vec<PathBuf> {
        let home = &self.home;
        match category {
            // /tmp 与 /var/folders 是 /private 下的链接（WritablePaths 换成真实路径）；$TMPDIR 在 /var/folders 下
            Category::Temp => vec!["/tmp".into(), "/var/tmp".into(), "/var/folders".into()],
            Category::Devices => vec!["/dev".into()],
            Category::UserCache => vec![home.join(HOME_CACHE), home.join("Library/Caches")],
            Category::Packages => {
                let mut v = vec![PathBuf::from("/opt/homebrew")];
                v.extend(under(home, HOME_PACKAGES));
                v
            }
            Category::Toolchains => under(home, HOME_TOOLCHAINS),
            // go 的用户配置目录：telemetry 计数（每次 go build 都写，#416 试跑）与 `go env -w`
            Category::ToolState => {
                let mut v = under(home, HOME_TOOL_STATE);
                v.push(home.join("Library/Application Support/go"));
                v
            }
            Category::PlatformInstall => platform_install(),
        }
    }

    fn wrap(&self, argv: &[String], writable: &WritablePaths, unreadable: &[RealPath]) -> Vec<String> {
        let mut out = vec![SANDBOX_EXEC.to_owned(), "-p".to_owned(), self.profile(writable, unreadable)];
        out.extend(argv.iter().cloned());
        out
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn last_covering_rule_wins() {
        let dir = tempfile::tempdir().unwrap();
        let base = real(dir.path()).unwrap();
        let w = WritablePaths::of([(true, base.to_path_buf()), (false, base.join("private"))], AgentId::new("a1"));
        assert!(w.permits(&base.join("x")));
        assert!(!w.permits(&base.join("private/x")));
        assert!(!w.permits(Path::new("/etc/hosts")));
    }

    #[test]
    fn rules_get_both_forms_of_links() {
        let dir = tempfile::tempdir().unwrap();
        let base = real(dir.path()).unwrap();
        std::fs::create_dir(base.join("t")).unwrap();
        std::os::unix::fs::symlink(base.join("t"), base.join("l")).unwrap();
        let w = WritablePaths::of([(true, base.join("l"))], AgentId::new("a1"));
        assert_eq!(w.rules(), &[(true, real_nofollow(base.join("l")).unwrap()), (true, real(base.join("t")).unwrap())]);
    }

    #[test]
    fn profile_quotes_paths() {
        let sb = Seatbelt::new("/Users/x".into());
        let w = WritablePaths { rules: vec![(true, RealPath::assumed("/a \"b\"".into()))], owner: AgentId::new("id1") };
        let p = sb.profile(&w, &[RealPath::assumed("/secret".into())]);
        assert!(p.contains(r#"(allow file-write* (subpath "/a \"b\""))"#), "{p}");
        assert!(
            p.contains(r#"(deny file-read-data (with message "muliao write sandbox agent=id1") (subpath "/secret"))"#),
            "{p}"
        );
    }
}
