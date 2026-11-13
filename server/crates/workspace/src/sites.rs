//! agent 自定义页面（#355）：agent 在工作目录 `site/<页面名>/` 下写的网页（监控、数据展示），界面列出并打开。
//!
//! 约定只有目录：`site/<页面名>/index.html` 是页面入口，同目录的其他文件（脚本、样式、数据 JSON）随页面一起托管；标题取
//! index.html 的 `<title>`，没有就用页面名。页面的数据由 agent 用现有手段刷新（触发器、常驻脚本写 data.json），页面自己
//! 轮询，平台不运行 agent 的服务。
//!
//! 路径规则同工作目录浏览（[`crate::browse`]）：解析符号链接后必须仍在页面目录内，只防误走出去，不是安全隔离。页面内容是
//! agent 写的，HTTP 层按不可信内容托管（签名地址、CSP sandbox、不同源）。

use std::io::Read;
use std::path::{Path, PathBuf};
use std::sync::LazyLock;

use chrono::{DateTime, Utc};
use muliao_assets::service::workspace_of;
use muliao_platform::errors::DomainError;
use muliao_platform::fs::{RealPath, is_within, real, resolve_within};
use regex::bytes::Regex;
use serde::Serialize;

use crate::browse::WorkspaceFile;

pub const SITE_DIR: &str = "site";
pub const INDEX: &str = "index.html";
/// 只在 index.html 开头找 `<title>`：标题总在 `<head>` 里
const TITLE_SCAN_BYTES: u64 = 64 * 1024;
static TITLE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"(?is)<title[^>]*>(.*?)</title\s*>").expect("valid regex"));

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct SitePage {
    pub name: String,
    pub title: String,
    /// 页面目录里（不含子目录）最近改动的文件的时间：数据文件刷新也算
    pub modified: DateTime<Utc>,
}

fn title_of(index: &Path, fallback: &str) -> std::io::Result<String> {
    let mut head = Vec::new();
    std::fs::File::open(index)?.take(TITLE_SCAN_BYTES).read_to_end(&mut head)?;
    let title = TITLE
        .captures(&head)
        .map(|m| html_escape::decode_html_entities(&String::from_utf8_lossy(&m[1])).trim().to_owned())
        .unwrap_or_default();
    let title = title.split_whitespace().collect::<Vec<_>>().join(" ");
    Ok(if title.is_empty() { fallback.to_owned() } else { title })
}

/// site 下名为 name 的页面：(页面目录, index.html)，都已解析符号链接；页面目录必须在 site 内、index.html 必须在页面目录内
/// 且是文件，否则不是页面。
fn page_of(site: &RealPath, name: &str) -> Option<(RealPath, RealPath)> {
    if matches!(name, "" | "." | "..") || name.contains('/') || name.contains('\0') {
        return None;
    }
    let page_dir = real(site.as_path().join(name)).ok()?;
    if page_dir == *site || !is_within(site, &page_dir) {
        return None;
    }
    let index = real(page_dir.as_path().join(INDEX)).ok()?;
    if !is_within(&page_dir, &index) || !index.as_path().is_file() {
        return None;
    }
    Some((page_dir, index))
}

fn mtime(path: &Path) -> std::io::Result<DateTime<Utc>> {
    Ok(DateTime::<Utc>::from(std::fs::metadata(path)?.modified()?))
}

#[derive(Clone)]
pub struct Sites {
    workspaces_dir: PathBuf,
}

impl Sites {
    pub fn new(workspaces_dir: PathBuf) -> Self {
        Self { workspaces_dir }
    }

    /// site 目录（已解析符号链接）；没有或不在工作目录内为 None。
    fn site(&self, agent_id: &str) -> Option<RealPath> {
        let root = real(workspace_of(&self.workspaces_dir, agent_id)).ok()?;
        let site = real(root.as_path().join(SITE_DIR)).ok()?;
        (is_within(&root, &site) && site.as_path().is_dir()).then_some(site)
    }

    /// agent 的全部页面，按名称排序：site 下每个含 index.html 的子目录。没有 site 目录就是没有页面；与工作目录浏览一样，
    /// 读不了（stat、读标题时出错，如正被删除）的不列出。阻塞 IO。
    pub fn pages(&self, agent_id: &str) -> Vec<SitePage> {
        let Some(site) = self.site(agent_id) else { return Vec::new() };
        let Ok(dirs) = std::fs::read_dir(site.as_path()) else { return Vec::new() };
        let mut pages: Vec<SitePage> = dirs
            .filter_map(|d| d.ok())
            .filter_map(|d| {
                let name = d.file_name().to_string_lossy().into_owned();
                let (page_dir, index) = page_of(&site, &name)?;
                let mut modified = mtime(index.as_path()).ok()?;
                for f in std::fs::read_dir(page_dir.as_path()).ok()?.filter_map(|f| f.ok()) {
                    let path = f.path();
                    if path.is_file() {
                        modified = modified.max(mtime(&path).ok()?);
                    }
                }
                let title = title_of(index.as_path(), &name).ok()?;
                Some(SitePage { name, title, modified })
            })
            .collect();
        pages.sort_by(|a, b| a.name.cmp(&b.name));
        pages
    }

    /// 页面目录里的一个文件；path 为空或以 / 结尾时取那一层的 index.html（不带 / 的目录不补：页面里的相对路径会解析到
    /// 上一级）。阻塞 IO。
    pub fn file(&self, agent_id: &str, page: &str, path: &str) -> Result<WorkspaceFile, DomainError> {
        let found = self.site(agent_id).and_then(|site| page_of(&site, page));
        let Some((page_dir, _)) = found else {
            return Err(DomainError::not_found(format!("agent {agent_id:?} has no page {page:?}")));
        };
        let rel = if path.is_empty() || path.ends_with('/') { format!("{path}{INDEX}") } else { path.to_owned() };
        let target = resolve_within(page_dir.as_path(), &rel)?;
        if !target.as_path().is_file() {
            return Err(DomainError::not_found(format!("no such file in page {page:?}: {path:?}")));
        }
        Ok(WorkspaceFile::of(target.into_path_buf()))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn pages_have_titles_and_files_stay_inside() {
        let dir = tempfile::tempdir().unwrap();
        let ws = dir.path().join("a1");
        let page = ws.join("site/dash");
        std::fs::create_dir_all(page.join("sub")).unwrap();
        std::fs::write(page.join("index.html"), "<html><head><TITLE>\n  Gas &amp; fees\n</title></head></html>")
            .unwrap();
        std::fs::write(page.join("data.json"), "{}").unwrap();
        std::fs::write(page.join("sub/index.html"), "x").unwrap();
        std::fs::create_dir_all(ws.join("site/notapage")).unwrap();
        std::fs::create_dir_all(ws.join("site/plain")).unwrap();
        std::fs::write(ws.join("site/plain/index.html"), "no title").unwrap();
        std::fs::write(dir.path().join("outside"), "x").unwrap();
        std::os::unix::fs::symlink(dir.path().join("outside"), page.join("leak")).unwrap();
        let sites = Sites::new(dir.path().to_path_buf());
        let pages: Vec<(String, String)> = sites.pages("a1").into_iter().map(|p| (p.name, p.title)).collect();
        assert_eq!(pages, [("dash".into(), "Gas & fees".into()), ("plain".into(), "plain".into())]);
        assert_eq!(sites.file("a1", "dash", "").unwrap().media_type, "text/html");
        assert_eq!(sites.file("a1", "dash", "data.json").unwrap().media_type, "application/json");
        assert!(sites.file("a1", "dash", "sub/").unwrap().path.ends_with("sub/index.html"));
        assert!(sites.file("a1", "dash", "leak").is_err());
        assert!(sites.file("a1", "dash", "../../x").is_err());
        assert_eq!(sites.file("a1", "nope", "").unwrap_err().message, r#"agent "a1" has no page "nope""#);
        assert!(sites.pages("other").is_empty());
    }
}
