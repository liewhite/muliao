//! 文件工具：read_file / write_file / edit_file / share_file。相对路径以 agent 工作目录为基准；读只拦平台的私有文件
//! （非 admin），写只能写 agent 的可写路径（[`crate::writable`]，防误操作不防故意）。edit_file 的定位与拼接规则在
//! [`crate::edits`]，行的切分与 `cat -n` 格式在 [`crate::text`]。
//!
//! 文件工具都是屏障（sequential）：同一次回复里排在它前面的调用先做完、它再执行，排在后面的等它做完。模型常在一次回复里先
//! run_shell 生成文件、再 read_file/share_file 它；并行执行时文件还没生成，报 file not found（#224）。文件工具都是瞬时的
//! 本地操作，串行几乎没有代价；不夹着文件工具时 run_shell 等之间照旧并行。

use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::SystemTime;

use agenter::{Content, File, FileSource, Tool, ToolCtx, ToolError, ToolOutput};
use muliao_assets::source::AssetSource;
use muliao_assets::tree::Expect;
use muliao_files::{FileStore, FilesError, INLINE_IMAGE_TYPES};
use muliao_platform::errors::DomainError;
use muliao_platform::fs::{read_text, real};
use muliao_platform::sandbox::Confinement;
use muliao_platform::text::truncate;
use muliao_platform::tool_views::{ToolRender, ViewBlock, diff};
use muliao_platform::tooling::{BuiltinTool, RunScope, ToolTraits, builtin};
use muliao_runtime::conventions::{SharedFile, attach};
use schemars::JsonSchema;
use serde::Deserialize;

use crate::edits::{Change, apply_each, changed_ranges};
use crate::text::{chars, numbered, split_lines};
use crate::writable::{Access, Confiner, reach};

/// edit_file 成功后展示的改动前后上下文行数：够确认改对了，不必再回读文件
const EDIT_CONTEXT_LINES: usize = 2;
const DEFAULT_READ_LIMIT: i64 = 2000;
/// read_file 按行截断时，正文之外每段还要放的字符数的上界：`==> path lines A-B of N <==` 标题里路径以外的部分、续读说明、
/// 段间分隔（正文预算 = 剩余预算 - 路径长 - 它）
const PART_OVERHEAD: i64 = 200;

/// 文件工具共用的依赖。
pub struct FileTools {
    pub confiner: Arc<Confiner>,
    pub assets: Arc<AssetSource>,
    pub files: Arc<FileStore>,
    /// 工具输出交给模型的上限（runtime.tool_output_max_chars）：run_shell 与文件工具共用
    pub max_output_chars: usize,
}

/// 文件工具组。
pub fn tools(deps: Arc<FileTools>) -> Vec<BuiltinTool> {
    vec![
        builtin(ReadFile(deps.clone()), ToolTraits::read().sequential().render(read_render())),
        builtin(WriteFile(deps.clone()), ToolTraits::write().sequential().render(write_render())),
        builtin(EditFile(deps.clone()), ToolTraits::write().sequential().render(edit_render())),
        // 工具声明的读写说的是它的效果：它把文件交给用户（存进文件库、随回复发出），算写；对路径本身只读
        builtin(ShareFile(deps), ToolTraits::write().sequential()),
    ]
}

/// 文件工具里的 IO 失败交还给模型，不打崩整次运行（#483）。
fn io_error(e: std::io::Error, note: &str) -> ToolError {
    ToolError::Retry(format!("{e}.{note}"))
}

fn not_found(message: String) -> DomainError {
    DomainError::not_found(message)
}

fn invalid(message: impl Into<String>) -> ToolError {
    ToolError::Retry(message.into())
}

async fn confinement(deps: &FileTools, agent_id: &str) -> Result<Confinement, ToolError> {
    Ok(deps.confiner.of(agent_id).await?)
}

/// 阻塞的文件操作放到阻塞线程池（文件可能很大、在网络盘上）。
async fn blocking<T: Send + 'static>(f: impl FnOnce() -> T + Send + 'static) -> T {
    tokio::task::spawn_blocking(f).await.expect("file operation panicked")
}

// ---- 文件类型 ----

/// 只按扩展名认、内容被压缩过的（同 Python mimetypes 的 encodings_map 与 suffix_map）：x.tar.gz 不当成 tar
const COMPRESSED: &[&str] = &["gz", "z", "bz2", "xz", "br", "tgz", "taz", "tz", "tbz2", "txz"];

/// 按扩展名定工作目录里文件的类型（share_file、read_file 与工作目录浏览共用）。认不出、或是压缩过的时，用 RFC 2046 的
/// 「未知二进制」类型。
pub fn media_type_of(name: &str) -> String {
    let ext = Path::new(name).extension().map(|e| e.to_string_lossy().to_lowercase());
    if ext.as_deref().is_some_and(|e| COMPRESSED.contains(&e)) {
        return "application/octet-stream".to_owned();
    }
    mime_guess::from_path(name).first_raw().unwrap_or("application/octet-stream").to_owned()
}

/// 能交给模型看的图片的类型；其余文件当文本读。
fn image_type(path: &Path) -> Option<String> {
    let media_type = media_type_of(&path.file_name().unwrap_or_default().to_string_lossy());
    INLINE_IMAGE_TYPES.contains(&media_type.as_str()).then_some(media_type)
}

// ---- 读过的文件（write_file 覆盖资源文件前核对） ----

fn modified(path: &Path) -> Option<SystemTime> {
    std::fs::metadata(path).and_then(|m| m.modified()).ok()
}

/// 记下这次运行读到（或刚写成）的文件当时的修改时间（`RunScope::reads`）。读之前记：读的过程中被改了，记下的是旧的，之后
/// 覆盖时按「改过了」拒绝，不会放过。写入口写成的给出它在锁里取的时间（之后别处的写入不会被记成这次的）。
fn remember(scope: &RunScope, target: &Path, at: Option<SystemTime>) {
    let (Ok(identity), Some(at)) = (real(target), at.or_else(|| modified(target))) else { return };
    scope.reads.lock().expect("reads lock").insert(identity.into_path_buf(), at);
}

/// write_file 整体覆盖资源目录里的文件：已有的要这次运行读过、之后没被改过（写入口核对），免得盖掉别处刚做的修改（共享
/// 记忆、access.toml 可能有几处同时在改）；没读过就拒绝。
fn expect_for_write(scope: &RunScope, target: &Path) -> Result<Expect, DomainError> {
    if !target.is_file() {
        return Ok(Expect::Absent);
    }
    let seen = real(target).ok().and_then(|r| scope.reads.lock().expect("reads lock").get(r.as_path()).copied());
    seen.map(Expect::Modified).ok_or_else(|| {
        DomainError::invalid(format!(
            "{} is an existing AI asset file and write_file replaces it whole: read it with read_file first (in this \
             run), then write; or change just the part you mean with edit_file. Nothing was written.",
            target.display()
        ))
    })
}

// ---- read_file ----

fn default_offset() -> i64 {
    1
}

fn default_limit() -> i64 {
    DEFAULT_READ_LIMIT
}

#[derive(Debug, Clone, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct FileRange {
    /// File path.
    path: String,
    /// Line number to start from (1-based, as numbered in the output).
    #[serde(default = "default_offset")]
    offset: i64,
    /// Maximum number of lines to return.
    #[serde(default = "default_limit")]
    limit: i64,
}

#[derive(Deserialize, JsonSchema)]
pub struct ReadArgs {
    /// File path (omit when using `reads`).
    #[serde(default)]
    path: Option<String>,
    /// Line number to start from (1-based, as numbered in the output).
    #[serde(default = "default_offset")]
    offset: i64,
    /// Maximum number of lines to return.
    #[serde(default = "default_limit")]
    limit: i64,
    /// List of {path, offset, limit} parts to read, shown in this order; a part that cannot be read shows its error
    /// and the others are still returned.
    #[serde(default)]
    reads: Vec<FileRange>,
}

fn read_render() -> ToolRender {
    ToolRender::new()
        .call(|a: &ReadArgs| {
            // 要读的文件，一行一个；不是从头读整份的注明行范围。两种都没给（会被退回）显示原文
            let parts: Vec<FileRange> = if !a.reads.is_empty() {
                a.reads.clone()
            } else if let Some(path) = &a.path {
                vec![FileRange { path: path.clone(), offset: a.offset, limit: a.limit }]
            } else {
                return None;
            };
            let line = |p: &FileRange| {
                if p.offset == 1 && p.limit == DEFAULT_READ_LIMIT {
                    p.path.clone()
                } else {
                    format!("{}  (lines {}-{})", p.path, p.offset, p.offset + p.limit - 1)
                }
            };
            Some(vec![ViewBlock::Code { title: None, text: parts.iter().map(line).collect::<Vec<_>>().join("\n") }])
        })
        .result_text(|text| Some(vec![ViewBlock::Code { title: None, text: text.to_owned() }]))
}

pub struct ReadFile(Arc<FileTools>);

impl Tool<RunScope> for ReadFile {
    type Args = ReadArgs;

    fn name(&self) -> &str {
        "read_file"
    }

    fn description(&self) -> &str {
        "Read a text file. Output is in `cat -n` format: each line is its 1-based line number right-aligned in 6 \
         columns, a tab, then the line as it is in the file. Relative paths are resolved against your workspace.\n\
         To read several files or several parts of one file in one call, pass `reads` instead of \
         `path`/`offset`/`limit`; like `head` with several files, each part comes under a header \
         `==> <path> lines <first>-<last> of <total> <==` (`==> <path>: <error> <==` for a part that cannot be read).\n\
         Output is limited in size: reading stops after a whole line, and a note in square brackets on its own \
         line says where to continue, e.g. `[output limit reached at line 812 of 3000; continue with \
         offset=813]` or `[188 more lines; continue with offset=2001]`; parts not read at all are listed in a last \
         `[not read, ...]` note.\n\
         An image file (PNG, JPEG, GIF, WebP) passed as `path` is returned as an image you can see, e.g. a \
         screenshot or a chart you made."
    }

    async fn call(&self, ctx: ToolCtx<'_, RunScope>, a: ReadArgs) -> Result<ToolOutput, ToolError> {
        let deps = &self.0;
        if !a.reads.is_empty() && (a.path.is_some() || a.offset != 1 || a.limit != DEFAULT_READ_LIMIT) {
            return Err(invalid("pass either path/offset/limit or reads, not both"));
        }
        let confinement = confinement(deps, &ctx.deps.agent_id).await?;
        let max = deps.max_output_chars;
        if !a.reads.is_empty() {
            let scope = ctx.deps;
            let workspace = scope.workspace.clone();
            let (text, seen) = blocking(move || read_parts(&confinement, &workspace, &a.reads, max as i64)).await;
            for (target, at) in seen {
                remember(scope, &target, at);
            }
            return Ok(agenter::tool::text(text));
        }
        let Some(path) = a.path else { return Err(invalid("path is required (or pass reads)")) };
        let target = reach(&confinement, &ctx.deps.workspace, &path, Access::Read)?;
        if let Some(media_type) = image_type(&target) {
            if a.offset != 1 || a.limit != DEFAULT_READ_LIMIT {
                return Err(invalid(format!("offset/limit do not apply to an image file: {}", target.display())));
            }
            return read_image(&target, &media_type, deps.files.max_bytes()).await;
        }
        remember(ctx.deps, &target, None);
        let budget = max as i64 - PART_OVERHEAD;
        let excerpt = {
            let target = target.clone();
            blocking(move || read_range(&target, a.offset, a.limit, budget)).await
        };
        let excerpt = excerpt.map_err(|e| match e {
            ReadError::Domain(d) => d.into(),
            ReadError::Io(e) => io_error(e, ""),
        })?;
        // 按行截断后正文已在预算内；只有第一行就比整个预算还长时这里才会再截
        Ok(agenter::tool::text(truncate(&excerpt.body, max) + &excerpt.more_note))
    }
}

/// 图片以内容交回；运行端口把它存进文件存储，历史里只留引用，发请求时再展开给模型（超过模型接口上限的在那里整理）。存
/// 不进文件存储的（超过 max_bytes）在这里就拒绝，模型可以先缩小一份。
async fn read_image(target: &Path, media_type: &str, max_bytes: u64) -> Result<ToolOutput, ToolError> {
    let meta = match tokio::fs::metadata(target).await {
        Ok(m) if m.is_file() => m,
        _ => return Err(not_found(format!("file not found: {}", target.display())).into()),
    };
    if meta.len() > max_bytes {
        return Err(invalid(format!(
            "image too large to view: {} is {} bytes, the limit is {max_bytes} bytes; make a smaller copy first (e.g. \
             `sips -Z 1568 big.png --out small.png`) and read that",
            target.display(),
            meta.len()
        )));
    }
    let data = tokio::fs::read(target).await.map_err(|e| io_error(e, ""))?;
    Ok(vec![
        Content::text(format!("{} ({media_type}, {} bytes)", target.display(), data.len())),
        Content::File(File { media_type: media_type.to_owned(), source: FileSource::Data(data) }),
    ])
}

enum ReadError {
    Domain(DomainError),
    Io(std::io::Error),
}

impl std::fmt::Display for ReadError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            ReadError::Domain(d) => write!(f, "{d}"),
            ReadError::Io(e) => write!(f, "{e}"),
        }
    }
}

/// 读到的一段。
struct Excerpt {
    /// 「lines A-B of N」，多段读取时作为每段的标题
    span: String,
    /// 带行号的正文
    body: String,
    /// 后面还有行时注明还剩多少（因输出上限停下时写明从哪一行接着读），否则为空；与正文分开，正文被截断时它也还在
    more_note: String,
    /// 是否因输出上限没读完要求的行
    cut: bool,
}

/// 读第 offset 行（1 起算）起的至多 limit 行，带行号的正文不超过 budget 个字符：在最后一整行停下（至少给一行），并在
/// more_note 里写明读到第几行、用什么 offset 接着读。
fn read_range(target: &Path, offset: i64, limit: i64, budget: i64) -> Result<Excerpt, ReadError> {
    if offset < 1 || limit < 1 {
        // 0 或负数：0 起算的习惯，或想从末尾数；都不是这里的约定，照着切片会返回看起来正常、其实不对的内容
        return Err(ReadError::Domain(DomainError::invalid(format!(
            "offset is a 1-based line number (offset=1 is the first line) and limit must be >= 1, got offset={offset} \
             limit={limit}"
        ))));
    }
    if !target.is_file() {
        return Err(ReadError::Domain(not_found(format!("file not found: {}", target.display()))));
    }
    // 只是看：不是 UTF-8 的字节显示成 �，不拒绝（日志里偶有坏字节）；改它时 edit_file 按 read_text 拒绝
    let data = std::fs::read(target).map_err(ReadError::Io)?;
    let text = String::from_utf8_lossy(&data);
    let lines = split_lines(&text);
    let n = lines.len();
    let (offset, limit) = (offset as usize, limit as usize);
    let first = offset - 1;
    if offset > 1 && first >= n {
        return Err(ReadError::Domain(DomainError::invalid(format!(
            "offset {offset} is past the end: {} has {n} lines",
            target.display()
        ))));
    }
    let end = first.saturating_add(limit).min(n);
    let mut rows: Vec<String> = Vec::new();
    let mut used: i64 = 0;
    for i in first..end {
        let row = numbered(&lines, i, i + 1);
        used += chars(&row) as i64 + if rows.is_empty() { 0 } else { 1 };
        if !rows.is_empty() && used > budget {
            break;
        }
        rows.push(row);
    }
    // stop：读到的最后一行的行号（1 起算），即下一行的 0 起下标
    let stop = first + rows.len();
    let more_note = if stop < end {
        // 给了 limit 且没读到文件末尾时才提 limit；默认 limit 的窗口不是模型要的，照着读会以为文件到那里就完了
        let explicit = limit as i64 != DEFAULT_READ_LIMIT && end < n;
        let rest = if explicit { format!(", limit={}", end - stop) } else { String::new() };
        format!("\n[output limit reached at line {stop} of {n}; continue with offset={}{rest}]", stop + 1)
    } else {
        let more = n - end;
        if more > 0 { format!("\n[{more} more lines; continue with offset={}]", end + 1) } else { String::new() }
    };
    Ok(Excerpt {
        span: if n > 0 { format!("lines {offset}-{stop} of {n}") } else { "(empty file)".to_owned() },
        body: rows.join("\n"),
        more_note,
        cut: stop < end,
    })
}

/// read_file 的多段读取：按次序读，共用一个输出预算。预算用完的那段在整行处停下并写明续读的 offset；之后的段不再读，
/// 最后一行逐个列出（这一行不参与截断），模型照着另起一次调用、不必猜。返回正文与读过的文件（读之前的修改时间）。
fn read_parts(
    confinement: &Confinement,
    workspace: &Path,
    reads: &[FileRange],
    max_chars: i64,
) -> (String, Vec<(PathBuf, Option<SystemTime>)>) {
    let mut sections: Vec<(&FileRange, String)> = Vec::new();
    let mut skipped: Vec<&FileRange> = Vec::new();
    let mut seen = Vec::new();
    let mut budget = max_chars;
    let workspace_chars = chars(&workspace.to_string_lossy()) as i64;
    for part in reads {
        // 标题里的路径是绝对路径（reach 的结果）；还没解析时按工作目录加上给的写法估（只会多估）
        let body_budget = budget - workspace_chars - chars(&part.path) as i64 - 1 - PART_OVERHEAD;
        if !skipped.is_empty() || body_budget <= 0 {
            skipped.push(part);
            continue;
        }
        let mut read = || -> Result<(PathBuf, Excerpt), ReadError> {
            let target = reach(confinement, workspace, &part.path, Access::Read).map_err(ReadError::Domain)?;
            if image_type(&target).is_some() {
                return Err(ReadError::Domain(DomainError::invalid(
                    "an image file cannot be read in reads; read it alone with path",
                )));
            }
            seen.push((target.clone(), modified(&target)));
            let excerpt = read_range(&target, part.offset, part.limit, body_budget)?;
            Ok((target, excerpt))
        };
        let section = match read() {
            // 标题用模型给的路径认出是哪一段（有的错误信息里不带路径）
            Err(e) => format!("==> {}: {e} <==", part.path),
            Ok((target, excerpt)) => {
                if excerpt.cut {
                    budget = 0;
                }
                format!("==> {} {} <==\n{}{}", target.display(), excerpt.span, excerpt.body, excerpt.more_note)
            }
        };
        budget -= chars(&section) as i64 + 2;
        sections.push((part, section));
    }
    let (body, tail) = loop {
        let body = sections.iter().map(|(_, s)| s.as_str()).collect::<Vec<_>>().join("\n\n");
        let tail = not_read_note(&skipped);
        if (chars(&body) + chars(&tail)) as i64 <= max_chars || sections.len() <= 1 {
            break (body, tail);
        }
        // 放不下（未读列表太长，或某段第一行就超预算——read_range 至少给一行）：把最后读的一段挤进列表，不在正文中间截
        let (part, _) = sections.pop().expect("more than one section");
        skipped.insert(0, part);
    };
    // 兜底：只有第一段的第一行就比预算还长时才会截到正文
    let limit = (max_chars - chars(&tail) as i64).max(0) as usize;
    ((truncate(&body, limit) + &tail).trim_start_matches('\n').to_owned(), seen)
}

fn not_read_note(skipped: &[&FileRange]) -> String {
    if skipped.is_empty() {
        return String::new();
    }
    let listed: Vec<String> = skipped
        .iter()
        .map(|p| {
            let limit = if p.limit != DEFAULT_READ_LIMIT { format!(" limit={}", p.limit) } else { String::new() };
            format!("{} offset={}{limit}", p.path, p.offset)
        })
        .collect();
    format!("\n\n[not read, output limit reached; read in another call: {}]", listed.join("; "))
}

// ---- write_file ----

#[derive(Deserialize, JsonSchema)]
pub struct WriteArgs {
    /// File path.
    path: String,
    /// Full file content.
    content: String,
}

fn write_render() -> ToolRender {
    ToolRender::new()
        .call(|a: &WriteArgs| Some(vec![ViewBlock::Code { title: Some(a.path.clone()), text: a.content.clone() }]))
}

/// 按 UTF-8 原样写入（换行符不转换），建好上级目录。
fn write_text(path: &Path, text: &str) -> std::io::Result<()> {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    std::fs::write(path, text)
}

pub struct WriteFile(Arc<FileTools>);

impl Tool<RunScope> for WriteFile {
    type Args = WriteArgs;

    fn name(&self) -> &str {
        "write_file"
    }

    fn description(&self) -> &str {
        "Create or overwrite a text file. Parent directories are created as needed.\n\
         In the AI asset tree (where memory and skills live), write_file and edit_file check a file's format \
         before writing and refuse it with the reason if it is wrong; write_file overwrites an existing asset \
         file only if you read it with read_file in this run and nobody changed it since (else read it again, \
         or change just a part with edit_file)."
    }

    async fn call(&self, ctx: ToolCtx<'_, RunScope>, a: WriteArgs) -> Result<ToolOutput, ToolError> {
        let deps = &self.0;
        let scope = ctx.deps;
        let target = reach(&confinement(deps, &scope.agent_id).await?, &scope.workspace, &a.path, Access::Write)?;
        let existed = target.is_file();
        let content = if deps.assets.contains(&target) {
            let expect = expect_for_write(scope, &target)?;
            let mut written = deps.assets.write(&[(target.clone(), a.content, expect)]).await.map_err(asset_error)?;
            let (_, done) = written.pop().expect("one file written");
            remember(scope, &target, Some(done.modified.into()));
            done.text
        } else {
            let (path, content) = (target.clone(), a.content);
            let content =
                blocking(move || write_text(&path, &content).map(|()| content)).await.map_err(|e| io_error(e, ""))?;
            remember(scope, &target, None);
            content
        };
        let verb = if existed { "overwrote" } else { "created" };
        Ok(agenter::tool::text(format!(
            "{verb} {} ({} lines, {} chars)",
            target.display(),
            split_lines(&content).len(),
            chars(&content)
        )))
    }
}

/// 写入口的错误：被拒时交给 agent 的是完整说明（是谁的、看 assets-files 哪一节）；文件系统出错交还模型。
fn asset_error(e: muliao_assets::error::Error) -> ToolError {
    match e {
        muliao_assets::error::Error::Io(e) => io_error(e, ""),
        other => other.into(),
    }
}

// ---- edit_file ----

#[derive(Debug, Clone, Deserialize, JsonSchema)]
#[serde(deny_unknown_fields)]
pub struct TextEdit {
    /// File to change; defaults to the top-level path.
    #[serde(default)]
    path: Option<String>,
    /// Exact text to replace.
    #[serde(default)]
    old_text: Option<String>,
    /// First line to replace (1-based).
    #[serde(default)]
    start_line: Option<i64>,
    /// Last line to replace (inclusive).
    #[serde(default)]
    end_line: Option<i64>,
    /// The whole text of line start_line.
    #[serde(default)]
    anchor: Option<String>,
    /// Replacement text.
    new_text: String,
    /// Replace every occurrence of old_text instead of exactly one.
    #[serde(default)]
    replace_all: bool,
}

impl TextEdit {
    /// 按它给的定位方式（恰好一种）换成对应的修改。
    fn change(&self) -> Result<Change, DomainError> {
        let given = [self.start_line.is_some(), self.end_line.is_some(), self.anchor.is_some()];
        if given.iter().any(|g| *g) && !given.iter().all(|g| *g) {
            return Err(DomainError::invalid("start_line, end_line and anchor go together: give all three"));
        }
        if self.old_text.is_none() == self.start_line.is_none() {
            return Err(DomainError::invalid(
                "locate the text in exactly one way: old_text or start_line/end_line/anchor",
            ));
        }
        if let Some(old) = &self.old_text {
            return Ok(Change::Replace { old: old.clone(), new: self.new_text.clone(), every: self.replace_all });
        }
        if self.replace_all {
            return Err(DomainError::invalid("replace_all does not apply to a line range"));
        }
        let (Some(start), Some(end), Some(anchor)) = (self.start_line, self.end_line, &self.anchor) else {
            unreachable!("checked above")
        };
        // 负数与 0 按「不在 1..=行数」报（换成 0 不改变结论）
        let line = |n: i64| usize::try_from(n).unwrap_or(0);
        if start < 1 || start > end {
            return Err(DomainError::invalid(format!(
                "need 1 <= start_line <= end_line, got start_line={start} end_line={end}"
            )));
        }
        Ok(Change::Lines { start: line(start), end: line(end), anchor: anchor.clone(), new: self.new_text.clone() })
    }
}

#[derive(Deserialize, JsonSchema)]
pub struct EditArgs {
    /// File path (with `edits`: the file of the edits that give no path of their own).
    #[serde(default)]
    path: Option<String>,
    /// Exact text to replace (omit when using `edits`).
    #[serde(default)]
    old_text: Option<String>,
    /// First line to replace (1-based); give with `end_line` and `anchor`.
    #[serde(default)]
    start_line: Option<i64>,
    /// Last line to replace (inclusive).
    #[serde(default)]
    end_line: Option<i64>,
    /// The whole text of line start_line, not just its beginning (leading/trailing whitespace ignored).
    #[serde(default)]
    anchor: Option<String>,
    /// Replacement text; empty deletes the located text (omit when using `edits`).
    #[serde(default)]
    new_text: Option<String>,
    /// Replace every occurrence of `old_text` (e.g. renaming a variable) instead of exactly one (omit when using
    /// `edits`).
    #[serde(default)]
    replace_all: bool,
    /// List of changes, each {new_text} plus one way to locate it (old_text, start_line/end_line/anchor), optionally
    /// replace_all, and optionally its own path, applied in order (line numbers always refer to the file before this
    /// call).
    #[serde(default)]
    edits: Vec<TextEdit>,
}

fn edit_render() -> ToolRender {
    ToolRender::new().call(|a: &EditArgs| {
        // 每处修改一个 diff：按原文定位的是原文 → 新文；按行号定位的看不到原文，标题写行号，只显示新文。缺定位方式或新文
        // （会被退回）显示原文
        let edits = if !a.edits.is_empty() {
            a.edits.clone()
        } else {
            let new_text = a.new_text.clone()?;
            vec![TextEdit {
                path: None,
                old_text: a.old_text.clone(),
                start_line: a.start_line,
                end_line: a.end_line,
                anchor: None,
                new_text,
                replace_all: false,
            }]
        };
        let mut blocks = Vec::new();
        for e in &edits {
            let path = e.path.clone().or_else(|| a.path.clone()).unwrap_or_default();
            if let Some(old) = &e.old_text {
                blocks.push(diff(Some(path), old, &e.new_text));
            } else if let (Some(start), Some(end)) = (e.start_line, e.end_line) {
                blocks.push(diff(Some(format!("{path} · lines {start}-{end}")), "", &e.new_text));
            } else {
                return None;
            }
        }
        Some(blocks)
    })
}

/// 编辑用的文本，以及它是不是通篇 CRLF：模型给的 old_text / new_text 只用 \n，所以通篇 CRLF 的文件按 LF 编辑、写回时
/// 换回 CRLF（不悄悄改掉文件的换行符）。两种混用的原样编辑。
fn as_lf(text: &str) -> (String, bool) {
    if text.contains("\r\n") && !text.replace("\r\n", "").contains('\n') {
        return (text.replace("\r\n", "\n"), true);
    }
    (text.to_owned(), false)
}

/// 一个文件改完后的回显：替换数、行数和改动处的行（带上下文与行号），省掉回读确认。
fn edit_report(target: &Path, before: &str, content: &str, count: usize) -> String {
    let lines = split_lines(content);
    let head = format!(
        "edited {} ({count} replacement{}; now {} lines)",
        target.display(),
        if count > 1 { "s" } else { "" },
        lines.len()
    );
    let ranges = changed_ranges(before, content, EDIT_CONTEXT_LINES);
    if ranges.is_empty() {
        // 没有可展示的行：没变、删空了，或只改了行尾（换行符、末尾换行）
        if before == content {
            return format!("{head}; the text is unchanged");
        }
        return if lines.is_empty() {
            format!("{head}; the file is now empty")
        } else {
            format!("{head}; only line endings changed")
        };
    }
    // 与 read_file 同为 cat -n 格式（行号在每行里，按它接着按行号改不必推算）；不连续的几块之间用 grep -C 的 `--` 分隔
    let excerpts: Vec<String> = ranges.iter().map(|(start, end)| numbered(&lines, *start, *end)).collect();
    format!(
        "{head}; cat -n of the changed lines with {EDIT_CONTEXT_LINES} lines of context:\n{}",
        excerpts.join("\n--\n")
    )
}

/// 文件可写（同 Python 的 os.access(W_OK)：只读文件在任何写盘之前挡住）。
fn writable_file(path: &Path) -> bool {
    use std::os::unix::ffi::OsStrExt;
    let Ok(c) = std::ffi::CString::new(path.as_os_str().as_bytes()) else { return false };
    // SAFETY: access 只读这个以 NUL 结尾的路径
    unsafe { libc::access(c.as_ptr(), libc::W_OK) == 0 }
}

pub struct EditFile(Arc<FileTools>);

impl Tool<RunScope> for EditFile {
    type Args = EditArgs;

    fn name(&self) -> &str {
        "edit_file"
    }

    fn description(&self) -> &str {
        "Change a file: locate a block in exactly one of two ways and replace it with `new_text`.\n\
         - `old_text`: exact text that must occur exactly once (unless `replace_all`). Copy it from the file as it \
         is now (without the line-number prefix of read_file's `cat -n` output), with enough surrounding lines to be unique.\n\
         - `start_line` + `end_line` + `anchor`: whole lines start_line..end_line as numbered by read_file; \
         `anchor` is the whole text of line start_line (not just its beginning), checked so that stale line numbers fail instead of \
         changing the wrong lines. Good for replacing or deleting a long block without copying it.\n\
         To change several places in one call, in one file or in several files, pass `edits` instead: each edit \
         changes its own `path` (or the top-level `path` when it has none); they are applied in order: `old_text` \
         is found in the file as the earlier edits left it, while line numbers and anchors always \
         refer to the file before this call (as read_file showed it), in any order; a line range that overlaps an \
         earlier edit to the same file is rejected. If any edit fails no file is written. On success each file \
         gets `edited <path> (<n> replacements; now <total> lines); cat -n of the changed lines ...:` followed by \
         the changed lines with context in the same `cat -n` format as read_file (separate blocks split by `--`), \
         so you can check the result and go on editing by line number without reading the file again."
    }

    async fn call(&self, ctx: ToolCtx<'_, RunScope>, a: EditArgs) -> Result<ToolOutput, ToolError> {
        let deps = &self.0;
        let scope = ctx.deps;
        let single = a.old_text.is_some()
            || a.start_line.is_some()
            || a.end_line.is_some()
            || a.anchor.is_some()
            || a.new_text.is_some();
        if !a.edits.is_empty() && (single || a.replace_all) {
            return Err(invalid("pass either one change (old_text/.../new_text) or edits, not both"));
        }
        let edits = if !a.edits.is_empty() {
            a.edits
        } else {
            if a.path.is_none() {
                return Err(invalid("path is required"));
            }
            let Some(new_text) = a.new_text else { return Err(invalid("new_text is required (or pass edits)")) };
            vec![TextEdit {
                path: None,
                old_text: a.old_text,
                start_line: a.start_line,
                end_line: a.end_line,
                anchor: a.anchor,
                new_text,
                replace_all: a.replace_all,
            }]
        };
        let confinement = confinement(deps, &scope.agent_id).await?;
        let n = edits.len();
        // 同一个文件可能写成不同路径（相对/绝对、带 ..、大小写不同）：按身份认成一份，用第一次出现时的写法展示，否则后写的
        // 一份会覆盖先写的
        let mut same: HashMap<PathBuf, PathBuf> = HashMap::new();
        // 改到的文件，按第一次出现的次序（回显与写盘的次序）
        let mut order: Vec<PathBuf> = Vec::new();
        let mut before: HashMap<PathBuf, String> = HashMap::new();
        // 按 CRLF 编辑的文件：写回时换回 CRLF
        let mut crlf: HashSet<PathBuf> = HashSet::new();
        let mut located: Vec<(PathBuf, Result<Change, DomainError>)> = Vec::new();
        for (i, e) in edits.iter().enumerate() {
            let i = i + 1;
            let Some(chosen) = e.path.as_ref().or(a.path.as_ref()) else {
                return Err(invalid(format!("edit {i} has no path: give it its own path or pass the top-level path")));
            };
            let given = reach(&confinement, &scope.workspace, chosen, Access::Write)?;
            let place = if n > 1 { format!("edit {i}: ") } else { String::new() };
            if !given.is_file() {
                return Err(not_found(format!("{place}file not found: {}", given.display())).into());
            }
            // 只读文件在任何写盘之前挡住，不留下改了一半的几个文件（写盘时的其他 IO 失败不在此保证内）
            if !writable_file(&given) {
                return Err(invalid(format!("{place}file is not writable: {}; nothing was written.", given.display())));
            }
            let identity = real(&given).map_err(|e| io_error(e, ""))?.into_path_buf();
            let target = same.entry(identity).or_insert_with(|| given.clone()).clone();
            if !before.contains_key(&target) {
                let (text, was_crlf) = as_lf(&read_text(&target).map_err(|e| io_error(e, ""))?);
                if was_crlf {
                    crlf.insert(target.clone());
                }
                before.insert(target.clone(), text);
                order.push(target.clone());
            }
            located.push((target, e.change()));
        }
        let (mut after, counts) = apply_each(&before, &located, |k| k.display().to_string())?;
        // 盘上要写的文本（按 CRLF 编辑的换回 CRLF）：校验与写盘是同一份
        let stored: HashMap<PathBuf, String> = after
            .iter()
            .map(|(t, c)| (t.clone(), if crlf.contains(t) { c.replace('\n', "\r\n") } else { c.clone() }))
            .collect();
        let changed: Vec<PathBuf> = order.iter().filter(|t| after.contains_key(*t)).cloned().collect();
        // 读与写之间只隔几毫秒：期间别处改了同一个文件就盖掉那次改动（重做即可），不再核对
        let in_tree: Vec<(PathBuf, String, Expect)> =
            changed.iter().filter(|t| deps.assets.contains(t)).map(|t| (t.clone(), stored[t].clone(), Expect::Any)).collect();

        // 资源目录里的先经写入口：全部校验通过才写（原子写入，记忆规整空白），回显按实际存下的
        if !in_tree.is_empty() {
            let written = deps.assets.write(&in_tree).await.map_err(|e| match e {
                muliao_assets::error::Error::Io(e) => {
                    io_error(e, " Asset files of this edit may be partly written; nothing else was written.")
                }
                other => other.into(),
            })?;
            for (target, done) in written {
                after.insert(target.clone(), as_lf(&done.text).0);
                remember(scope, &target, Some(done.modified.into()));
            }
        }
        let mut written: Vec<PathBuf> = in_tree.iter().map(|(t, _, _)| t.clone()).collect();
        for target in &changed {
            if written.contains(target) {
                continue;
            }
            let (path, content) = (target.clone(), stored[target].clone());
            if let Err(e) = blocking(move || write_text(&path, &content)).await {
                let done = if written.is_empty() {
                    " Nothing was written.".to_owned()
                } else {
                    let names: Vec<String> = written.iter().map(|p| p.display().to_string()).collect();
                    format!(" Already written: {}.", names.join(", "))
                };
                // 权限错误在 open 时就失败、不动文件；其他错误（如磁盘满）可能在截断之后发生
                let torn = if e.kind() == std::io::ErrorKind::PermissionDenied {
                    String::new()
                } else {
                    format!(" {} may be truncated or partially written.", target.display())
                };
                return Err(io_error(e, &format!("{done}{torn}")));
            }
            written.push(target.clone());
            remember(scope, target, None);
        }
        let reports: Vec<String> = changed.iter().map(|t| edit_report(t, &before[t], &after[t], counts[t])).collect();
        Ok(agenter::tool::text(truncate(&reports.join("\n\n"), deps.max_output_chars)))
    }
}

// ---- share_file ----

#[derive(Deserialize, JsonSchema)]
pub struct ShareArgs {
    /// File path.
    path: String,
}

pub struct ShareFile(Arc<FileTools>);

impl Tool<RunScope> for ShareFile {
    type Args = ShareArgs;

    fn name(&self) -> &str {
        "share_file"
    }

    fn description(&self) -> &str {
        "Share a file from your workspace with the user, e.g. a chart you made or a document you wrote.\n\
         It is shown to the user with your reply automatically (images inline, other files as downloads), so do \
         not embed or link it yourself; just refer to it by name. The user gets a snapshot; later changes to the \
         file are not reflected."
    }

    async fn call(&self, ctx: ToolCtx<'_, RunScope>, a: ShareArgs) -> Result<ToolOutput, ToolError> {
        let deps = &self.0;
        let scope = ctx.deps;
        let target = reach(&confinement(deps, &scope.agent_id).await?, &scope.workspace, &a.path, Access::Read)?;
        if !target.is_file() {
            return Err(not_found(format!("file not found: {}", target.display())).into());
        }
        let name = target.file_name().unwrap_or_default().to_string_lossy().into_owned();
        let meta = deps.files.put_path(&name, &media_type_of(&name), &target).await.map_err(|e| match e {
            FilesError::Domain(d) => d.into(),
            FilesError::Io(e) => io_error(e, ""),
            other => ToolError::Abort(Box::new(other)),
        })?;
        tracing::info!(agent = %scope.agent_id, session_id = %scope.session_id, file_id = %meta.id, "file shared with user");
        attach(scope, &ctx.call.id, &[SharedFile::of(&meta)]);
        Ok(agenter::tool::text(format!("shared file {} with the user; it is shown with your reply", meta.name)))
    }
}
