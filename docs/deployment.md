# 部署与使用

## 安装

```bash
deploy/install.sh          # 编译并安装 main；也可以指定分支或 tag：deploy/install.sh <引用>
```

脚本在常驻的仓库副本 `~/.muliao/build/src`（`MULIAO_BUILD` 可改；保留 Rust 编译缓存和 `node_modules`，第二次起只编改了的部分）检出要装的版本，`cargo build --release --locked` 编出 `muliao` 与 `muliao-migrate`，`npm ci && npm run build` 构建前端，用固定证书签名 `muliao`（见下「macOS 文件访问授权」），放进新目录 `~/.muliao/app/<时间>-<commit>/`（`muliao`、`muliao-migrate`、`web/`、`COMMIT` 文件写着完整 commit），再把这个目录整个复制成 `~/.muliao/app/current`（真目录，不是链接）。程序要重启服务后才用上；前端装完就是新的（服务从 `current/web` 读），所以装完尽快重启。保留最近 3 个版本目录；退回上一版：`deploy/activate.sh ~/.muliao/app/<上一版目录>` 再重启。依赖：rustup 装的 `~/.cargo/bin/cargo`、`~/.cargo/bin/rcodesign`（`cargo install --locked apple-codesign`）、Homebrew 的 node/npm 与 openssl；锁文件（`Cargo.lock`、`package-lock.json`）定版本。

### macOS 文件访问授权（#715）

agent 的命令是服务起的子进程，macOS 把它们访问受保护位置（iCloud Drive、文稿、桌面、系统授权库等）算在服务程序头上。没授权时会弹窗等人点，命令就一直卡着。在「系统设置 → 隐私与安全性 → 完全磁盘访问」里加 `~/.muliao/app/current/muliao`。

macOS 记授权时记的是程序的真实路径（解开链接后的）加签名要求：编译器自动加的临时签名只认这一份文件的指纹，重编一次就对不上。所以两样都固定：
- 路径：程序总在 `app/current/muliao`（current 是复制出来的真目录）。
- 签名：第一次安装时在 `~/.muliao/codesign/` 生成自签证书和私钥，之后每次都用它签，标识写死 `muliao`；签名要求是 `identifier muliao and certificate root = H"…"`，重编的程序也满足。用 rcodesign 签而不是系统的 codesign：codesign 只认钥匙串里受信任的证书，设受信任要在图形界面上输密码。

`~/.muliao/codesign/` 丢了（或退回到 #715 之前装的版本）就要重新授权一次。查当前签名：`codesign -d -r- ~/.muliao/app/current/muliao`。

所有配置（监听地址、登录密码、密钥、LLM provider 与 API key）都在一个 TOML 文件里，模板见仓库根目录 `config.example.toml`，每项都有注释。未知的键启动即报错。程序自身不读环境变量。

```bash
mkdir -p ~/.muliao
cp config.example.toml ~/.muliao/config.toml && chmod 600 ~/.muliao/config.toml   # 填写「必填」项
~/.muliao/app/current/muliao --config ~/.muliao/config.toml    # 前端默认在程序旁边的 web/，也可以 --web-dir 指定
~/.muliao/app/current/muliao openapi                            # 打印接口描述（web/openapi.json 由它生成）
```

`data_dir` 的相对路径以配置文件所在目录为基准，默认即 `~/.muliao/data`。agent 的模型写成 `<provider 名>:<模型名>`，provider 名是配置里 `[llm.providers.<名>]` 的键。

## 服务（launchd）

macOS 用 launchd 托管并自动重启，示例见 `deploy/muliao.plist.example`。plist 运行的是**服务入口** `~/.local/bin/muliao`，它是指向 `~/.muliao/app/current/muliao` 的链接（程序先解开链接，再到它旁边的 `web/` 找前端）：

```bash
ln -sfn ~/.muliao/app/current/muliao ~/.local/bin/muliao
cp deploy/muliao.plist.example ~/Library/LaunchAgents/com.muliao.plist   # 按注释改路径
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.muliao.plist # 在登录的终端里做，见下
launchctl kickstart -k gui/$(id -u)/com.muliao    # 重启（升级后）
deploy/selfcheck.sh <commit>                      # 重启后自检（在仓库里跑）
```

为什么经过一个链接、换版本不改 plist：从服务里起的进程（agent 的命令和它脱离出去的脚本）执行 `launchctl bootstrap` 一律报「5: Input/output error」（#706 实测，#644 服务停了起不来也是它），只有 `kickstart`、`kill` 能用。所以 plist 装好就不动，换版本只换 `app/current` 再 `kickstart -k`。要让服务停着换库时（如恢复库备份）也照这个做：把入口暂时指向 `deploy/hold.sh`（launchd 拉起的只是一个在等的脚本），换好库再把入口指回程序，hold.sh 随即 exec 过去（见 `deploy/lib.sh` 的 `hold_service` / `release`）。现成的入口是 `deploy/offline_restart.sh '<停着时执行的命令>'`：自己转到后台，停服务、跑命令、再放行并做健康检查，日志在 `~/.muliao/deploy.log`。改了 plist 本身（环境变量、ExitTimeOut）要在登录的终端里 `launchctl bootout` 再 `bootstrap` 才生效，`kickstart` 不重读。

`deploy/selfcheck.sh` 做部署后的通用检查并逐项打印 PASS/FAIL 和依据：进程启动时间晚于安装时间（`app/current` 目录的修改时间）、已装版本（`app/current/COMMIT`）包含目标 commit、日志里本进程的启动行（`muliao is serving ... pid=<pid>`）之后没有 ERROR、端口返回 200、`launchctl` 的 runs 隔 60 秒（`RUNS_WAIT`）不增长（不增长＝没有循环重启）。任一失败退出码为 1；只报告，不处理。

重启或停止时服务收到 SIGTERM，按 docs/architecture.md 的关停流程收尾（界面的 SSE 长连接先结束、浏览器稍后自动重连；在跑的运行停在已保存的一步；后台任务的进程组被结束并通知 agent）。示例 plist 设了 `ExitTimeOut` 60：launchd 不设时 `kickstart -k` 约 5 秒就 SIGKILL，后台任务的进程会脱离服务继续运行、无人监督。

日志：配置 `log_file`（和 plist 的 `StandardOutPath` 写同一个文件）后由进程自己按大小轮转（`log_max_mb` 默认 50，留 `log_backups` 默认 4 份 `.1`…`.4`），每次换新文件时把 stdout/stderr 也重定向过去；轮转前起的子进程（stdio MCP 的 stderr 继承 fd 2）仍写旧文件。自检把 `.1` 和当前文件拼起来看。不用 newsyslog：launchd 只在启动时打开 `StandardOutPath`，外部改名轮转后进程仍写旧文件。不设 `log_file` 就只写 stderr，不轮转。

agent 的 shell 与 stdio MCP 只继承服务的 `PATH`（示例 plist 里的那一串），不读 shell 配置：nvm 装的 node 在服务里找不到。agent 常用、要放进这些目录的命令用 Homebrew 装（落在 `/opt/homebrew/bin`）：`brew install ripgrep node@24 coreutils && brew link node@24`——`rg` 是工具说明里指定的搜索方式；`node`/`npm`/`npx` 给前端构建和 `npx` 起的 MCP（node@24 是 keg-only，要 `brew link` 才进 `/opt/homebrew/bin`）；coreutils 补上 macOS 没有的 `timeout`（它只把 macOS 没有的命令不加 `g` 前缀地装进去，`ls`/`sed` 等仍是系统自带的版本）。

## 开发

```bash
cd server && cargo test --workspace && cargo clippy --workspace --all-targets && cargo fmt --check   # 后端，见 server/README.md
cd web && npm install
npm run dev                      # Vite 把 /api 代理到本机后端
npm run build                    # 构建到 web/dist（安装时由 install.sh 构建，产物不进仓库）
```

数据库结构由各领域 crate 自己的一串迁移 SQL 管理，服务启动时自动升级（见 server/README.md）。

## iOS 主屏应用与推送

1. 服务必须通过 **HTTPS** 访问（反向代理如 Caddy、Nginx，或 Cloudflare Tunnel / Tailscale Funnel），配置里 `server.public_url` 设为该地址。反向代理需要关闭对 `/api/events` 的响应缓冲（SSE）。
2. iPhone 用 Safari 打开 → 分享 → 「添加到主屏幕」。
3. 从主屏幕图标打开，登录 → 设置 → 「开启推送」（iOS 16.4+）。
4. 「发送测试通知」验证。

VAPID 密钥在首次启动时生成于 `data/vapid_private.pem`（Rust 版沿用 Python 版生成的那一个，公钥相同，设备不用重新订阅），更换它会使所有已有订阅失效，需要各设备重新开启推送。

## 示例：每分钟处理 Slack 未读并分派

1. Vault 页存入 `SLACK_TOKEN`。
2. 新建 agent `slack-triage`，指令里说明：读取工作目录下的游标文件，取之后的未读消息，总结，`list_agents` 后用 `delegate_task` 把需要处理的事项交给合适的 agent，最后更新游标。
3. 给它接入 Slack：在对话里把 Slack MCP 的信息告诉它，让它在自己的资源目录写 `mcp/slack.json`（token 写成 `${vault:SLACK_TOKEN}`，格式见公共 skill assets-files，下次运行连上）；或者让它写一个用 `run_shell(secrets=["SLACK_TOKEN"])` 调 Slack API 的脚本并沉淀为 skill。
4. 管理菜单的「触发器」页新建触发器：方式选「定时」，cron 表达式 `* * * * *`，agent 选 `slack-triage`。
5. 被委派的 agent 在自己的新 session 中处理，完成后推送通知；点开通知进入该 session 可继续回复。

定时触发不会重叠执行：上一次触发的 session 还没做完（在运行，或在等后台任务、委派、你的回答）时跳过本次；跳过会显示在触发页上，挡路的 session 在等待时会通知你一次（规则见 docs/architecture.md 的 `schedule`）。

## 数据

全部在配置的 `data_dir`：`muliao.db`（SQLite）、`files/`（对话里的文件）、`workspaces/<agent id>/`（工作目录）、`vapid_private.pem`（推送私钥）；记忆、skill、MCP、secret、agent 配置在 `assets_dir`（默认 `~/.muliao/assets`）的文件树里。布局见 docs/architecture.md「领域概念」。备份这两个目录即可；配置文件里的 `vault.key` 需单独妥善保存，丢失后 vault 中的 secret 无法解密。
