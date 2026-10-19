# 部署与使用

## 安装（uv）

前端构建产物随包分发（`src/muliao/webui/`），安装端不需要 node：

```bash
uv tool install git+https://github.com/liewhite/muliao.git
# 升级
uv tool upgrade muliao     # 或 uv tool install --force git+https://github.com/liewhite/muliao.git
```

配置全部来自环境变量或**当前工作目录**下的 `.env`（模板见仓库 `.env.example`）。建议为服务建一个目录放 `.env` 和数据：

```bash
mkdir -p ~/.muliao && cd ~/.muliao   # 放 .env，MULIAO_DATA_DIR 默认即 ./data
muliao
```

后台循环（worker、cron、Telegram）意外退出时进程会主动退出，需要进程管理器托管并自动重启。macOS 用 launchd，示例见 `deploy/muliao.plist.example`：

```bash
cp deploy/muliao.plist.example ~/Library/LaunchAgents/com.muliao.plist   # 按注释改路径
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.muliao.plist
launchctl kickstart -k gui/$(id -u)/com.muliao    # 重启（升级后）
```

## 开发

```bash
uv sync
uv run pytest                    # 后端测试
cd web && npm install
npm run dev                      # Vite 把 /api、/hooks 代理到 127.0.0.1:8000（先 uv run muliao）
npm run build                    # 构建到 src/muliao/webui/，改了前端必须重新构建并提交产物
```

`src/muliao/webui/` 是 `web/src` 的派生产物：提交进仓库是为了让 `uv tool install` 不依赖 node。唯一来源仍是 `web/src`，由 `npm run build` 一条命令重建。

## iOS 主屏应用与推送

1. 服务必须通过 **HTTPS** 访问（反向代理如 Caddy、Nginx，或 Cloudflare Tunnel / Tailscale Funnel），`MULIAO_PUBLIC_URL` 设为该地址。反向代理需要关闭对 `/api/events` 的响应缓冲（SSE）。
2. iPhone 用 Safari 打开 → 分享 → 「添加到主屏幕」。
3. 从主屏幕图标打开，登录 → 设置 → 「开启推送」（iOS 16.4+）。
4. 「发送测试通知」验证。

VAPID 密钥在首次启动时生成于 `data/vapid_private.pem`，更换它会使所有已有订阅失效，需要各设备重新开启推送。

## 示例：每分钟处理 Slack 未读并分派

1. Vault 页存入 `SLACK_TOKEN`。
2. 新建 agent `slack-triage`，指令里说明：读取工作目录下的游标文件，取之后的未读消息，总结，`list_agents` 后用 `delegate_task` 把需要处理的事项交给合适的 agent，最后更新游标。
3. 给它接入 Slack：在对话里把 Slack MCP 的信息告诉它，让它用 `mcp_add` 创建（token 写成 `${vault:SLACK_TOKEN}`）；或者让它写一个用 `run_shell(secrets=["SLACK_TOKEN"])` 调 Slack API 的脚本并沉淀为 skill。
4. 定时页新建 `* * * * *` 的任务，agent 选 `slack-triage`。
5. 被委派的 agent 在自己的新 session 中处理，完成后推送通知；点开通知进入该 session 可继续回复。

定时任务不会重叠执行：上一次触发的 session 仍在运行时会跳过本次。

## 数据

全部在 `MULIAO_DATA_DIR`：`muliao.db`（SQLite）、`skills/`（公共 skill）、`agents/<name>/`（记忆、私有 skill、工作目录）。备份整个目录即可；`MULIAO_VAULT_KEY` 需单独妥善保存。
