# 部署与使用

## 安装（uv）

前端构建产物随包分发（`src/muliao/webui/`），安装端不需要 node：

```bash
deploy/install.sh          # 安装或升级到 main；也可以指定分支或 tag：deploy/install.sh <引用>
```

不要直接 `uv tool install git+…` 或 `uv tool upgrade`：它们不读 `uv.lock`，会重新解析依赖，装到测试没跑过的版本组合（2026-10-22 装上 anthropic 2.0，与 pydantic-ai 不兼容，所有模型调用失败；此后 pyproject.toml 直接限定 `anthropic<2`，放开条件见那里的注释）。脚本把锁文件导出为约束再安装，线上版本与测试一致。脚本会自己克隆仓库，部署机上不需要仓库副本，可以从开发机直接执行：`ssh <主机> 'zsh -l -s [引用]' < deploy/install.sh`（用登录 shell 是为了拿到 Homebrew 的 PATH 里的 uv）。安装后重启服务才生效（见下文 launchd）。

所有配置（监听地址、登录密码、密钥、LLM provider 与 API key）都在一个 TOML 文件里，模板见仓库根目录 `config.example.toml`，每项都有注释。程序自身不读环境变量；LLM 的 base_url 未填时使用官方地址，不受 `*_BASE_URL` 环境变量影响。

```bash
mkdir -p ~/.muliao
cp config.example.toml ~/.muliao/config.toml && chmod 600 ~/.muliao/config.toml   # 填写「必填」项
muliao                          # 默认读 ~/.muliao/config.toml
muliao --config /path/to.toml   # 或指定路径
```

`data_dir` 的相对路径以配置文件所在目录为基准，默认即 `~/.muliao/data`。agent 的模型写成 `<provider 名>:<模型名>`，provider 名是配置里 `[llm.providers.<名>]` 的键。

后台循环（worker、定时触发）意外退出时进程会主动退出，需要进程管理器托管并自动重启。macOS 用 launchd，示例见 `deploy/muliao.plist.example`：

```bash
cp deploy/muliao.plist.example ~/Library/LaunchAgents/com.muliao.plist   # 按注释改路径
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.muliao.plist
launchctl kickstart -k gui/$(id -u)/com.muliao    # 重启（升级后）
```

重启或停止时服务收到 SIGTERM：先最多等 3 秒让 HTTP 连接（界面的 SSE 长连接不会自己关）结束，再取消剩下的连接，进入关停——取消正在进行的运行、结束后台任务的进程组并通知 agent（重启后处理）。这一步最多约 20 秒，所以示例 plist 设了 `ExitTimeOut` 60：launchd 不设时 `kickstart -k` 约 5 秒就 SIGKILL，后台任务的进程会脱离服务继续运行、无人监督。改了 plist 要 `launchctl bootout` 再 `bootstrap` 才生效，`kickstart` 不重读。

agent 的 shell 与 stdio MCP 只继承服务的 `PATH`（示例 plist 里的那一串），不读 shell 配置：nvm 装的 node 在服务里找不到。agent 常用、要放进这些目录的命令用 Homebrew 装（落在 `/opt/homebrew/bin`）：`brew install ripgrep node@24 coreutils && brew link node@24`——`rg` 是工具说明里指定的搜索方式；`node`/`npm`/`npx` 给前端构建和 `npx` 起的 MCP（node@24 是 keg-only，要 `brew link` 才进 `/opt/homebrew/bin`）；coreutils 补上 macOS 没有的 `timeout`（它只把 macOS 没有的命令不加 `g` 前缀地装进去，`ls`/`sed` 等仍是系统自带的版本）。

## 开发

```bash
uv sync
uv run pytest                    # 后端测试（默认 -n auto 并行；调试单个用例用 -n0）
cd web && npm install
npm run dev                      # Vite 把 /api 代理到 127.0.0.1:8000（先 uv run muliao）
npm run build                    # 构建到 src/muliao/webui/，改了前端必须重新构建并提交产物
```

### 数据库迁移

数据库结构由 Alembic 迁移管理（`src/muliao/migrations/`），服务启动时自动升级到最新版本，升级不需要删库。

例外：2026-10-20 按领域重构时迁移历史重置为唯一的初始版本（见 docs/architecture.md「数据库迁移」）。从重构之前的版本升级时，已有数据库停在同一个修订号上却是旧结构，必须先备份 `data/muliao.db`，再删除它让服务重建；之后的升级照常自动迁移。改了各领域的 `tables.py`（全部表经 `src/muliao/schema.py` 汇总）后生成迁移：

```bash
uv run alembic upgrade head                          # 开发用的临时库 .alembic-dev.db
uv run alembic revision --autogenerate -m "add xxx"  # 检查生成的脚本后提交
```

`tests/test_migrations.py` 会比对「迁移后的结构」与模型定义，忘写迁移时测试失败。SQLite 改表采用「建新表-拷数据-删旧表」，迁移期间会关闭外键（否则删旧表会级联删除子表数据），提交前执行 `foreign_key_check`。整个迁移在一个事务里，任何失败都完整回滚，数据库保持迁移前的状态。

`src/muliao/webui/` 是 `web/src` 的派生产物：提交进仓库是为了让 `uv tool install` 不依赖 node。唯一来源仍是 `web/src`，由 `npm run build` 一条命令重建。

## iOS 主屏应用与推送

1. 服务必须通过 **HTTPS** 访问（反向代理如 Caddy、Nginx，或 Cloudflare Tunnel / Tailscale Funnel），配置里 `server.public_url` 设为该地址。反向代理需要关闭对 `/api/events` 的响应缓冲（SSE）。
2. iPhone 用 Safari 打开 → 分享 → 「添加到主屏幕」。
3. 从主屏幕图标打开，登录 → 设置 → 「开启推送」（iOS 16.4+）。
4. 「发送测试通知」验证。

VAPID 密钥在首次启动时生成于 `data/vapid_private.pem`，更换它会使所有已有订阅失效，需要各设备重新开启推送。

## 示例：每分钟处理 Slack 未读并分派

1. Vault 页存入 `SLACK_TOKEN`。
2. 新建 agent `slack-triage`，指令里说明：读取工作目录下的游标文件，取之后的未读消息，总结，`list_agents` 后用 `delegate_task` 把需要处理的事项交给合适的 agent，最后更新游标。
3. 给它接入 Slack：在对话里把 Slack MCP 的信息告诉它，让它用 `mcp_add` 创建（token 写成 `${vault:SLACK_TOKEN}`）；或者让它写一个用 `run_shell(secrets=["SLACK_TOKEN"])` 调 Slack API 的脚本并沉淀为 skill。
4. 「触发」页新建触发器：方式选「定时」，cron 表达式 `* * * * *`，agent 选 `slack-triage`。
5. 被委派的 agent 在自己的新 session 中处理，完成后推送通知；点开通知进入该 session 可继续回复。

定时触发不会重叠执行：上一次触发的 session 还没做完（在运行，或在等后台任务、委派、你的回答）时跳过本次；跳过会显示在触发页上，挡路的 session 在等待时会通知你一次（规则见 docs/architecture.md 的 `schedule`）。

## 数据

全部在配置的 `data_dir`：`muliao.db`（SQLite）、`skills/`（skill 目录的只读副本，由数据库生成，启动时重建；skill、MCP 配置、secret、记忆与它们的版本都在数据库里。#230 之前的 git 资源仓库迁移后改名为 `resources.imported` 留着，可人工删除）、`files/`（对话里的文件）、`workspaces/<agent id>/`（工作目录），布局见 docs/architecture.md「领域概念」。备份整个目录即可；配置文件里的 `vault.key` 需单独妥善保存，丢失后 vault 中的 secret 无法解密。
