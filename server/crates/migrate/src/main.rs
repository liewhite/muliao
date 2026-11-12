//! 旧数据迁移（#688）：只读打开 Python 版的库，写一个新库，再在新库的副本上启动新版逐页检查，出迁移报告。
//!
//! 用法：`muliao-migrate <旧库> <新库> [-c 配置文件] [--no-verify]`
//! - 新库不能已存在；中途失败不留半个库（先写 `<新库>.partial`，成功才改名）。
//! - 报告写到 `<新库>.report.md`，同时打印出来。检查发现问题时退出码为 1；有转不了的消息时不写新库、报错退出。
//! - 检查要用配置文件（默认 ~/.muliao/config.toml）里的密码、vault 等装配新版；`--no-verify` 只迁不查。

mod copy;
mod messages;
mod report;
mod verify;

use std::path::PathBuf;

use anyhow::{Context, bail};

const USAGE: &str = "usage: muliao-migrate OLD_DB NEW_DB [-c CONFIG] [--no-verify]";

struct Args {
    old: PathBuf,
    new: PathBuf,
    config: PathBuf,
    verify: bool,
}

fn parse_args() -> anyhow::Result<Args> {
    let mut positional = Vec::new();
    let mut config = PathBuf::from("~/.muliao/config.toml");
    let mut verify = true;
    let mut args = std::env::args().skip(1);
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "-c" | "--config" => config = args.next().context("--config needs a path")?.into(),
            "--no-verify" => verify = false,
            "-h" | "--help" => {
                println!("{USAGE}");
                std::process::exit(0);
            }
            other if other.starts_with('-') => bail!("unknown option {other:?}\n{USAGE}"),
            other => positional.push(PathBuf::from(other)),
        }
    }
    let [old, new]: [PathBuf; 2] = positional.try_into().map_err(|_| anyhow::anyhow!(USAGE))?;
    Ok(Args { old, new, config, verify })
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    let args = parse_args()?;
    // 新版启动时的警告与错误打到终端（检查时看得到）
    let _ = tracing_subscriber::fmt().with_env_filter("warn").with_writer(std::io::stderr).try_init();
    let mut report = report::Report::default();
    let started = std::time::Instant::now();
    copy::migrate(&args.old, &args.new, &mut report)?;
    eprintln!("migrated in {:.1}s", started.elapsed().as_secs_f64());
    if args.verify {
        let started = std::time::Instant::now();
        verify::verify(&args.new, &args.config, &mut report).await.context("checking the migrated database")?;
        eprintln!("checked in {:.1}s", started.elapsed().as_secs_f64());
    }
    let text = report.to_markdown();
    let path = args.new.with_file_name(format!(
        "{}.report.md",
        args.new.file_name().context("the new database path has no file name")?.to_string_lossy()
    ));
    std::fs::write(&path, &text).with_context(|| format!("writing {}", path.display()))?;
    println!("{text}");
    eprintln!("report: {}", path.display());
    if !report.problems.is_empty() {
        std::process::exit(1);
    }
    Ok(())
}
