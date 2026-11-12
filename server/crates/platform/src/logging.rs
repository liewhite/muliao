//! 日志（tracing）：写 stderr，或写一个按大小轮转的文件。
//!
//! 写文件时把进程的 stdout/stderr（fd 1、2）也指到当前文件：launchd 的 StandardOutPath 只在启动时打开一次，外部工具改名
//! 轮转后它仍写旧文件；所以由进程自己轮转，每次换新文件后把 fd 1、2 重定向过去，panic 信息、库直接打印的内容也进当前文件。
//!
//! HTTP 客户端每个请求的日志（URL 里可能带着代入的 secret，如 MCP server 地址里的 ?token=）只留 WARN 以上。

use std::fs::{File, OpenOptions};
use std::io::{self, Write};
use std::os::fd::AsRawFd;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};

use tracing_subscriber::EnvFilter;
use tracing_subscriber::fmt::MakeWriter;

use crate::config::LogLevel;

/// 按大小轮转的日志文件：当前文件超过 max_bytes 就改名为 `<file>.1`（旧的依次后移），保留 backups 份。
pub struct RotatingFile {
    path: PathBuf,
    max_bytes: u64,
    backups: u32,
    file: File,
    written: u64,
    /// 把 fd 1、2 指到当前文件（测试里关掉，不动测试进程的输出）
    stdio: bool,
}

impl RotatingFile {
    pub fn open(path: &Path, max_bytes: u64, backups: u32) -> io::Result<Self> {
        Self::open_with(path, max_bytes, backups, true)
    }

    fn open_with(path: &Path, max_bytes: u64, backups: u32, stdio: bool) -> io::Result<Self> {
        if max_bytes == 0 || backups == 0 {
            // 0 在别的实现里常是「永不轮转」，漏传时别悄悄退化
            return Err(io::Error::other("a log file needs positive max_bytes and backups"));
        }
        if let Some(dir) = path.parent() {
            std::fs::create_dir_all(dir)?;
        }
        let file = OpenOptions::new().create(true).append(true).open(path)?;
        let written = file.metadata()?.len();
        let this = Self { path: path.to_path_buf(), max_bytes, backups, file, written, stdio };
        this.redirect_stdio()?;
        Ok(this)
    }

    fn redirect_stdio(&self) -> io::Result<()> {
        if !self.stdio {
            return Ok(());
        }
        let fd = self.file.as_raw_fd();
        for target in [1, 2] {
            // SAFETY: 两个都是本进程打开着的描述符
            if unsafe { libc::dup2(fd, target) } < 0 {
                return Err(io::Error::last_os_error());
            }
        }
        Ok(())
    }

    fn backup(&self, n: u32) -> PathBuf {
        let mut name = self.path.clone().into_os_string();
        name.push(format!(".{n}"));
        name.into()
    }

    fn rotate(&mut self) -> io::Result<()> {
        for n in (1..self.backups).rev() {
            let from = self.backup(n);
            if from.exists() {
                std::fs::rename(&from, self.backup(n + 1))?;
            }
        }
        std::fs::rename(&self.path, self.backup(1))?;
        self.file = OpenOptions::new().create(true).append(true).open(&self.path)?;
        self.written = 0;
        self.redirect_stdio()
    }
}

impl Write for RotatingFile {
    fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
        if self.written > 0 && self.written + buf.len() as u64 > self.max_bytes {
            self.rotate()?;
        }
        let n = self.file.write(buf)?;
        self.written += n as u64;
        Ok(n)
    }

    fn flush(&mut self) -> io::Result<()> {
        self.file.flush()
    }
}

#[derive(Clone)]
struct Shared(Arc<Mutex<RotatingFile>>);

struct Guard<'a>(std::sync::MutexGuard<'a, RotatingFile>);

impl Write for Guard<'_> {
    fn write(&mut self, buf: &[u8]) -> io::Result<usize> {
        self.0.write(buf)
    }
    fn flush(&mut self) -> io::Result<()> {
        self.0.flush()
    }
}

impl<'a> MakeWriter<'a> for Shared {
    type Writer = Guard<'a>;
    fn make_writer(&'a self) -> Self::Writer {
        Guard(self.0.lock().unwrap_or_else(|p| p.into_inner()))
    }
}

/// 只留 WARN 以上的依赖：HTTP 客户端（URL 里可能带 secret、量大）、文件监听
const QUIET: &[&str] = &["hyper", "hyper_util", "reqwest", "h2", "rustls", "notify"];

fn filter(level: LogLevel) -> EnvFilter {
    let level = match level {
        LogLevel::Debug => "debug",
        LogLevel::Info => "info",
        LogLevel::Warning => "warn",
        LogLevel::Error => "error",
    };
    let mut directives = vec![level.to_owned()];
    directives.extend(QUIET.iter().map(|c| format!("{c}=warn")));
    EnvFilter::new(directives.join(","))
}

/// 装上全局日志。file 为 None 时写 stderr（由启动方决定去向）。
pub fn init(level: LogLevel, file: Option<&Path>, max_mb: u64, backups: u32) -> io::Result<()> {
    let builder = tracing_subscriber::fmt().with_env_filter(filter(level)).with_ansi(false);
    let result = match file {
        None => builder.with_writer(io::stderr).try_init(),
        Some(path) => {
            let rotating = RotatingFile::open(path, max_mb * 1024 * 1024, backups)?;
            builder.with_writer(Shared(Arc::new(Mutex::new(rotating)))).try_init()
        }
    };
    result.map_err(|e| io::Error::other(e.to_string()))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rotates_by_size_and_keeps_backups() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("x.log");
        let mut r = RotatingFile::open_with(&path, 10, 2, false).unwrap();
        for line in ["aaaaaaaa\n", "bbbbbbbb\n", "cccccccc\n", "dddddddd\n"] {
            r.write_all(line.as_bytes()).unwrap();
        }
        assert_eq!(std::fs::read_to_string(&path).unwrap(), "dddddddd\n");
        assert_eq!(std::fs::read_to_string(r.backup(1)).unwrap(), "cccccccc\n");
        assert_eq!(std::fs::read_to_string(r.backup(2)).unwrap(), "bbbbbbbb\n");
        assert!(!r.backup(3).exists());
    }
}
