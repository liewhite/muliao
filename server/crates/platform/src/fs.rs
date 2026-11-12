//! 文件系统的小工具（各领域共用）：路径的身份（[`real`]：判断两个路径是不是同一个、在不在某目录下只经它）、
//! 原子写、目录内的相对路径解析。
//!
//! 路径的身份只在这里算：macOS 的 APFS 默认不分大小写、不分 Unicode 正规化形式，`~/.MULIAO/CONFIG.toml` 与
//! `~/.muliao/config.toml` 是同一个文件，而 realpath 不纠正大小写，按字符串比就会绕过读写边界。

use std::ffi::{OsStr, OsString};
use std::fs;
use std::io::{self, Write};
use std::os::unix::ffi::OsStrExt;
use std::os::unix::fs::PermissionsExt;
use std::path::{Component, Path, PathBuf};

use crate::errors::DomainError;

/// 解析符号链接时最多跟几层，超过按循环链接处理：原样保留，之后的 stat 自然失败（同 Python 的 realpath）
const MAX_LINKS: usize = 40;

/// 一个路径的身份（[`real`] / [`real_nofollow`] 算出的）。只能由这两个函数构造，所以收它的接口（「在不在某目录下」
/// [`is_within`]、写沙箱的规则）拿到的一定是算过身份的路径，换个大小写或经符号链接绕不过去。
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Hash)]
pub struct RealPath(PathBuf);

impl RealPath {
    /// 认不出身份时按字面写法当作身份（只给 #624 那种「一条规则认不出只影响这一条」的场合用）。
    pub(crate) fn assumed(path: PathBuf) -> Self {
        Self(path)
    }

    pub fn as_path(&self) -> &Path {
        &self.0
    }

    pub fn into_path_buf(self) -> PathBuf {
        self.0
    }
}

impl std::ops::Deref for RealPath {
    type Target = Path;
    fn deref(&self) -> &Path {
        &self.0
    }
}

impl AsRef<Path> for RealPath {
    fn as_ref(&self) -> &Path {
        &self.0
    }
}

/// 路径的身份：绝对路径，解析符号链接，每一段换成盘上的真实写法（大小写、正规化形式）。两个路径指同一个文件当且仅当
/// `real` 相等；「在某目录下」按 `real` 比（[`is_within`]）。不存在的部分保持原样拼在最近的存在的上级后面。
///
/// 只在系统拒绝打开路径上的某一级、又认不出它在盘上的写法时报错，不退回给的写法。
pub fn real(path: impl AsRef<Path>) -> io::Result<RealPath> {
    let absolute = std::path::absolute(path.as_ref())?;
    on_disk(realpath(&absolute)).map(RealPath)
}

/// 同 [`real`]，但不解析最后一段：链接本身的身份。
pub fn real_nofollow(path: impl AsRef<Path>) -> io::Result<RealPath> {
    let absolute = normalize(&std::path::absolute(path.as_ref())?);
    let resolved = match (absolute.parent(), absolute.file_name()) {
        (Some(parent), Some(name)) => realpath(parent).join(name),
        _ => realpath(&absolute),
    };
    on_disk(resolved).map(RealPath)
}

/// 词法上去掉 `.` 与 `..`（同 Python 的 os.path.abspath）。
fn normalize(path: &Path) -> PathBuf {
    let mut out = PathBuf::from("/");
    for c in path.components() {
        match c {
            Component::Normal(name) => out.push(name),
            Component::ParentDir => {
                out.pop();
            }
            Component::RootDir | Component::CurDir | Component::Prefix(_) => {}
        }
    }
    out
}

/// 逐段解析符号链接（不要求路径存在；同 Python 的非严格 os.path.realpath）。`..` 作用在解析之后的上级上。
fn realpath(absolute: &Path) -> PathBuf {
    let mut out = PathBuf::from("/");
    // 待处理的各段，栈顶是下一段
    let mut pending: Vec<OsString> = absolute
        .components()
        .rev()
        .filter_map(|c| match c {
            Component::Normal(n) => Some(n.to_owned()),
            Component::ParentDir => Some("..".into()),
            _ => None,
        })
        .collect();
    let mut links = 0;
    while let Some(name) = pending.pop() {
        if name == ".." {
            out.pop();
            continue;
        }
        let next = out.join(&name);
        let is_link = fs::symlink_metadata(&next).is_ok_and(|m| m.file_type().is_symlink());
        if !is_link || links >= MAX_LINKS {
            out = next;
            continue;
        }
        links += 1;
        match fs::read_link(&next) {
            Ok(target) => {
                if target.is_absolute() {
                    out = PathBuf::from("/");
                }
                pending.extend(target.components().rev().filter_map(|c| match c {
                    Component::Normal(n) => Some(n.to_owned()),
                    Component::ParentDir => Some("..".into()),
                    _ => None,
                }));
            }
            Err(_) => out = next,
        }
    }
    out
}

/// 绝对路径（上级目录里没有符号链接）每一段换成盘上的写法：最深的能打开的目录取内核记的路径（F_GETPATH），其下
/// 存在的各段按目录列表认（不分大小写与正规化形式）。只有 macOS 需要；别的系统的文件系统按字节区分。
#[cfg(target_os = "macos")]
fn on_disk(path: PathBuf) -> io::Result<PathBuf> {
    use macos::{entry, kernel_path, open};

    let mut head = path.clone();
    // 打不开的各段，最深的在前
    let mut tail: Vec<OsString> = Vec::new();
    let mut head = loop {
        match open(&head, libc::O_EVTONLY | libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC) {
            Ok(fd) => break kernel_path(&fd)?,
            Err(_) => match (head.parent(), head.file_name()) {
                (Some(parent), Some(name)) => {
                    tail.push(name.to_owned());
                    head = parent.to_path_buf();
                }
                _ => return Ok(path),
            },
        }
    };
    for i in (0..tail.len()).rev() {
        match entry(&head, &tail[i])? {
            Some(found) => head.push(found),
            None => {
                head.extend(tail[..=i].iter().rev());
                return Ok(head);
            }
        }
    }
    Ok(head)
}

#[cfg(not(target_os = "macos"))]
fn on_disk(path: PathBuf) -> io::Result<PathBuf> {
    Ok(path)
}

#[cfg(target_os = "macos")]
mod macos {
    use super::*;
    use std::ffi::CString;
    use std::os::fd::{AsRawFd, FromRawFd, OwnedFd};
    use unicode_normalization::UnicodeNormalization;

    /// F_GETPATH 的缓冲区（macOS 的 MAXPATHLEN）
    const MAXPATHLEN: usize = 1024;

    pub(super) fn open(path: &Path, flags: libc::c_int) -> io::Result<OwnedFd> {
        let c = CString::new(path.as_os_str().as_bytes())?;
        // SAFETY: c 是以 NUL 结尾的字符串，open 成功返回的 fd 归我们所有
        let fd = unsafe { libc::open(c.as_ptr(), flags) };
        if fd < 0 {
            return Err(io::Error::last_os_error());
        }
        // SAFETY: fd 是刚打开的、没有别人持有的描述符
        Ok(unsafe { OwnedFd::from_raw_fd(fd) })
    }

    /// 内核记的这个 fd 的路径（盘上的写法）。
    pub(super) fn kernel_path(fd: &OwnedFd) -> io::Result<PathBuf> {
        let mut buf = vec![0u8; MAXPATHLEN];
        // SAFETY: F_GETPATH 往 buf 写至多 MAXPATHLEN 字节、以 NUL 结尾
        let r = unsafe { libc::fcntl(fd.as_raw_fd(), libc::F_GETPATH, buf.as_mut_ptr()) };
        if r == -1 {
            return Err(io::Error::last_os_error());
        }
        let len = buf.iter().position(|&b| b == 0).unwrap_or(buf.len());
        Ok(PathBuf::from(OsStr::from_bytes(&buf[..len])))
    }

    /// 不分大小写与正规化形式的比较键（同 Python 的 NFD + casefold）
    fn fold(name: &OsStr) -> String {
        caseless::default_case_fold_str(&name.to_string_lossy().nfd().collect::<String>())
    }

    /// directory 里叫 name 的那一项在盘上的写法；不存在为 None。目录列不出（只有 x 没有 r 权限）时打开这一项本身取内核
    /// 记的路径（不要读权限）。都不行时报错，不退回给的写法。
    pub(super) fn entry(directory: &Path, name: &OsStr) -> io::Result<Option<OsString>> {
        let path = directory.join(name);
        let Ok(meta) = fs::symlink_metadata(&path) else {
            return Ok(None);
        };
        let names: Vec<OsString> = match fs::read_dir(directory) {
            Ok(entries) => entries.filter_map(|e| e.ok().map(|e| e.file_name())).collect(),
            Err(_) => {
                let link = if meta.file_type().is_symlink() { libc::O_SYMLINK } else { libc::O_NOFOLLOW };
                let fd = open(&path, libc::O_EVTONLY | libc::O_NONBLOCK | libc::O_CLOEXEC | link)?;
                let got = kernel_path(&fd)?;
                let found = got.file_name().map(OsStr::to_owned).unwrap_or_default();
                if fold(&found) != fold(name) {
                    // 有多个硬链接的文件可能报出另一个链接的名字
                    return Err(io::Error::other(format!(
                        "cannot tell how {} is named on disk (got {:?})",
                        path.display(),
                        found
                    )));
                }
                return Ok(Some(found));
            }
        };
        if names.iter().any(|n| n == name) {
            return Ok(Some(name.to_owned()));
        }
        let key = fold(name);
        Ok(Some(names.into_iter().find(|n| fold(n) == key).unwrap_or_else(|| name.to_owned())))
    }
}

/// path 在 root 里：就是 root，或在它下面（按路径段比，两边都是算过身份的）。
pub fn is_within(root: &RealPath, path: &RealPath) -> bool {
    path.0.starts_with(&root.0)
}

impl PartialEq<PathBuf> for RealPath {
    fn eq(&self, other: &PathBuf) -> bool {
        &self.0 == other
    }
}

/// root 下的相对 posix 路径 rel 的身份，必须仍在 root 内（按 real 比，经符号链接走出去也不行）；不查是否存在。
/// 界面只读浏览目录、找 skill 脚本用：只防误走出去，不是安全隔离。
pub fn resolve_within(root: &Path, rel: &str) -> Result<RealPath, DomainError> {
    if rel.contains('\0') {
        return Err(DomainError::invalid(format!("path contains a NUL character: {rel:?}")));
    }
    if rel.starts_with('/') {
        return Err(DomainError::invalid(format!("path must be relative: {rel:?}")));
    }
    let cannot = |e: io::Error| DomainError::invalid(format!("cannot resolve {rel:?}: {e}"));
    let root = real(root).map_err(cannot)?;
    let target = real(root.join(rel)).map_err(cannot)?;
    if !is_within(&root, &target) {
        return Err(DomainError::invalid(format!("path is outside its directory: {rel:?}")));
    }
    Ok(target)
}

/// 读 UTF-8 文本，换行符原样保留。不是 UTF-8 时报 InvalidData，说明在哪个字节（文件工具把它当调用方的错交还模型）。
pub fn read_text(path: &Path) -> io::Result<String> {
    let data = fs::read(path)?;
    String::from_utf8(data).map_err(|e| {
        io::Error::new(
            io::ErrorKind::InvalidData,
            format!(
                "{} is not UTF-8 text (invalid byte at offset {}); it can only be changed with a command",
                path.display(),
                e.utf8_error().valid_up_to()
            ),
        )
    })
}

/// 原子写入：先写同目录的临时文件再 rename，读的人看不到写了一半的文件，写到一半失败原文件不动。path 是符号链接时
/// 写它指向的文件（链接保留）。已有的文件保留原来的权限位，新文件 0644。
pub fn write_atomic(path: &Path, content: &[u8]) -> io::Result<()> {
    let path = real(path)?;
    let parent = path.parent().ok_or_else(|| io::Error::other(format!("{} has no parent", path.display())))?;
    fs::create_dir_all(parent)?;
    let mode = match fs::metadata(&path) {
        Ok(m) if m.is_file() => m.permissions().mode() & 0o7777,
        _ => 0o644,
    };
    let name = path.file_name().unwrap_or_default().to_string_lossy();
    let mut tmp = tempfile::Builder::new().prefix(&format!(".{name}.")).tempfile_in(parent)?;
    tmp.write_all(content)?;
    tmp.as_file().sync_all()?;
    fs::set_permissions(tmp.path(), fs::Permissions::from_mode(mode))?;
    tmp.persist(&path).map_err(|e| e.error)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn real_follows_links_and_keeps_missing_tail() {
        let dir = tempfile::tempdir().unwrap();
        let base = real(dir.path()).unwrap();
        fs::create_dir(base.join("target")).unwrap();
        std::os::unix::fs::symlink(base.join("target"), base.join("link")).unwrap();
        assert_eq!(real(base.join("link/missing/x")).unwrap(), base.join("target/missing/x"));
        assert_eq!(real_nofollow(base.join("link")).unwrap(), base.join("link"));
        assert_eq!(real(base.join("target/../link")).unwrap(), base.join("target"));
        // 相对链接
        std::os::unix::fs::symlink("target", base.join("rel")).unwrap();
        assert_eq!(real(base.join("rel")).unwrap(), base.join("target"));
    }

    #[test]
    fn real_survives_link_loops() {
        let dir = tempfile::tempdir().unwrap();
        let base = real(dir.path()).unwrap();
        std::os::unix::fs::symlink(base.join("b"), base.join("a")).unwrap();
        std::os::unix::fs::symlink(base.join("a"), base.join("b")).unwrap();
        // 不卡死、不报错：之后的 stat 自然失败
        let r = real(base.join("a")).unwrap();
        assert!(fs::metadata(r).is_err());
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn real_corrects_case_and_normalization_on_apfs() {
        let dir = tempfile::tempdir().unwrap();
        let base = real(dir.path()).unwrap();
        // 临时目录本身经过 /var -> /private/var 的链接
        assert!(base.starts_with("/private"));
        fs::create_dir(base.join("Config")).unwrap();
        fs::write(base.join("Config/caf\u{e9}.toml"), "").unwrap();
        let upper = base.join("CONFIG/CAFE\u{301}.TOML");
        assert_eq!(real(&upper).unwrap(), base.join("Config/caf\u{e9}.toml"));
        // 不存在的部分原样拼上
        assert_eq!(real(base.join("CONFIG/New")).unwrap(), base.join("Config/New"));
    }

    #[test]
    fn resolve_within_rejects_escapes() {
        let dir = tempfile::tempdir().unwrap();
        let base = real(dir.path()).unwrap();
        fs::create_dir(base.join("root")).unwrap();
        std::os::unix::fs::symlink(&base, base.join("root/out")).unwrap();
        let root = base.join("root");
        assert_eq!(resolve_within(&root, "a/b").unwrap(), root.join("a/b"));
        assert!(resolve_within(&root, "../x").is_err());
        assert!(resolve_within(&root, "out/x").is_err());
        assert!(resolve_within(&root, "/etc").is_err());
    }

    #[test]
    fn write_atomic_keeps_mode_and_writes_through_links() {
        let dir = tempfile::tempdir().unwrap();
        let base = real(dir.path()).unwrap();
        let file = base.join("f");
        fs::write(&file, "old").unwrap();
        fs::set_permissions(&file, fs::Permissions::from_mode(0o600)).unwrap();
        std::os::unix::fs::symlink(&file, base.join("l")).unwrap();
        write_atomic(&base.join("l"), b"new").unwrap();
        assert_eq!(fs::read_to_string(&file).unwrap(), "new");
        assert!(fs::symlink_metadata(base.join("l")).unwrap().file_type().is_symlink());
        assert_eq!(fs::metadata(&file).unwrap().permissions().mode() & 0o777, 0o600);
        write_atomic(&base.join("sub/new"), b"x").unwrap();
        assert_eq!(fs::metadata(base.join("sub/new")).unwrap().permissions().mode() & 0o777, 0o644);
    }
}
