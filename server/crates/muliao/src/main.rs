//! muliao 服务：`muliao [-c 配置文件] [--web-dir 前端构建目录]`；`muliao openapi` 打印接口描述（web/openapi.json 由它生成）。

use std::net::SocketAddr;
use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;

use anyhow::{Context, bail};
use tokio::sync::Notify;

const DEFAULT_CONFIG: &str = "~/.muliao/config.toml";
/// 关停时等 HTTP 连接自己结束的时长；进程管理器给的退出时限（launchd 的 ExitTimeOut）要大于它加上之后的收尾
const GRACEFUL_SHUTDOWN: Duration = Duration::from_secs(3);

const USAGE: &str = "usage: muliao [-c CONFIG] [--web-dir DIR] | muliao openapi\n\
  -c, --config   TOML config file (default: ~/.muliao/config.toml)\n\
  --web-dir      built web UI (index.html, assets/); default: web/ next to this executable\n\
  openapi        print the API description (OpenAPI JSON) and exit";

struct Args {
    config: PathBuf,
    web_dir: Option<PathBuf>,
}

fn parse_args() -> anyhow::Result<Option<Args>> {
    let mut args = std::env::args().skip(1);
    let mut parsed = Args { config: PathBuf::from(DEFAULT_CONFIG), web_dir: None };
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "openapi" => {
                println!("{}", muliao_http::openapi().to_pretty_json().context("serializing the API description")?);
                return Ok(None);
            }
            "-c" | "--config" => parsed.config = args.next().context("--config needs a path")?.into(),
            "--web-dir" => parsed.web_dir = Some(args.next().context("--web-dir needs a path")?.into()),
            "-h" | "--help" => {
                println!("{USAGE}");
                return Ok(None);
            }
            other => bail!("unknown argument {other:?}\n{USAGE}"),
        }
    }
    Ok(Some(parsed))
}

fn default_web_dir() -> anyhow::Result<PathBuf> {
    // 解开链接：服务入口（如 ~/.local/bin/muliao）是指向 app/current/muliao 的链接，前端在它旁边的 web/（deploy/install.sh）
    let exe = std::env::current_exe().and_then(|p| p.canonicalize()).context("locating this executable")?;
    Ok(exe.parent().map(|d| d.join("web")).unwrap_or_else(|| PathBuf::from("web")))
}

/// SIGTERM（launchd 停服务）或 Ctrl-C
async fn terminated() {
    let mut term = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate()).expect("SIGTERM handler");
    tokio::select! {
        _ = term.recv() => {}
        _ = tokio::signal::ctrl_c() => {}
    }
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    let Some(args) = parse_args()? else { return Ok(()) };
    let config = muliao_server::Config::load(&args.config)?;
    let settings = &config.settings;
    muliao_platform::logging::init(
        settings.log_level,
        settings.log_file.as_deref(),
        settings.log_max_mb,
        settings.log_backups,
    )
    .context("setting up logging")?;
    let web_dir = match args.web_dir {
        Some(d) => d,
        None => default_web_dir()?,
    };
    if !web_dir.join("index.html").is_file() {
        tracing::warn!(web_dir = %web_dir.display(), "web UI not built, serving API only");
    }
    // 先占端口：同一个库上已有服务在跑时当场退出，不先续跑会话（同一个工具调用会被执行两次）
    let addr = format!("{}:{}", config.server.host, config.server.port);
    let listener = tokio::net::TcpListener::bind(&addr).await.with_context(|| format!("binding {addr}"))?;
    let app = muliao_server::assemble(config, web_dir).await?.start().await?;
    // pid：部署自检（deploy/selfcheck.sh）凭它认出本进程的启动行
    tracing::info!(%addr, pid = std::process::id(), "muliao is serving");

    // 关停：先关事件总线，界面的 SSE 长连接随之正常结束（浏览器自动重连），其余请求最多再等 GRACEFUL_SHUTDOWN
    let stopping = Arc::new(Notify::new());
    let (signal, bus) = (stopping.clone(), app.bus.clone());
    let graceful = async move {
        terminated().await;
        tracing::info!("shutting down");
        bus.close();
        signal.notify_one();
    };
    let serve = axum::serve(listener, app.router().into_make_service_with_connect_info::<SocketAddr>())
        .with_graceful_shutdown(graceful);
    tokio::select! {
        served = serve => served.context("serving HTTP")?,
        _ = async { stopping.notified().await; tokio::time::sleep(GRACEFUL_SHUTDOWN).await } => {
            tracing::warn!("some requests did not finish within the graceful shutdown period; dropping them");
        }
    }
    app.shutdown().await;
    tracing::info!("muliao stopped");
    Ok(())
}
