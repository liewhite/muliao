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

use std::path::{Component, Path, PathBuf};
use std::sync::Arc;

use muliao_assets::distribution::Distribution;
use muliao_assets::error::Result as AssetsResult;
use muliao_assets::service::workspace_of;
use muliao_platform::errors::{DomainError, ErrorKind};
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
}

impl Confiner {
    pub fn new(
        sandbox: Arc<dyn Sandbox>,
        workspaces_dir: PathBuf,
        distribution: Arc<Distribution>,
        home: PathBuf,
        private: Vec<PathBuf>,
    ) -> Self {
        Self { sandbox, workspaces_dir, distribution, home, private }
    }

    pub async fn of(&self, agent_id: &str) -> AssetsResult<Confinement> {
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
        // 换成真实路径要读文件系统
        let owner = agent_id.to_owned();
        let (writable, unreadable) = tokio::task::spawn_blocking(move || {
            let unreadable: Vec<_> = private.iter().flat_map(|p| real_forms(p)).collect();
            (WritablePaths::of(rules, owner), unreadable)
        })
        .await
        .expect("resolving writable paths panicked");
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
/// 说明；只记录不拦的模式下记一条日志放行。返回的是模型的写法（展示、报错里用），身份另按 real 取。
pub fn reach(confinement: &Confinement, workspace: &Path, path: &str, access: Access) -> Result<PathBuf, DomainError> {
    let target = absolute(workspace, &confinement.home, path);
    if confinement.readable(&target) && (access == Access::Read || confinement.writable.permits(&target)) {
        return Ok(target);
    }
    if confinement.enforcing() {
        return Err(DomainError::new(
            ErrorKind::Forbidden,
            format!("[Errno 1] Operation not permitted: '{}'\n{}", target.display(), (confinement.explain)(&target)),
        ));
    }
    tracing::warn!(
        agent = confinement.writable.owner(),
        path = %target.display(),
        ?access,
        "file tool access outside the agent's paths (report mode)"
    );
    Ok(target)
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
    if !confinement.enforcing() {
        return None;
    }
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
}
