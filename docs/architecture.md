# Muliao 架构

个人多 agent 助手。后端 Python（FastAPI + pydantic-ai），前端 React PWA（可添加到 iOS 主屏并接收 Web Push）。单机部署，SQLite + 文件目录。

## 1. 领域概念

| 概念 | 含义 | 事实存放处（唯一来源） |
|---|---|---|
| Agent | 一个具名的助手：模型、指令、私有记忆、私有 skill、工作目录 | `agents` 表；文件在 `data/agents/<name>/` |
| Session | 某个 agent 的一段对话，由某个触发源创建 | `sessions` 表 |
| Message | pydantic-ai `ModelMessage` 原样序列化 | `messages` 表（每行一条 ModelMessage） |
| Task | 一个待执行的工作单元：`turn`（对话一轮）、`reflection`（强制反思）、`deliver`（投递通知） | `tasks` 表 |
| Skill | `SKILL.md`（YAML frontmatter: name, description + 正文）。公共 skill 与 agent 私有 skill | `data/skills/<name>/`、`data/agents/<a>/skills/<name>/` |
| Memory | agent 私有记忆，一份有长度上限的 markdown | `data/agents/<a>/memory.md` |
| MCP server | agent 私有或公共的 MCP 连接配置，可引用 vault | `mcp_servers` 表 |
| Secret | 加密保存的密码/token，通过 key 引用 | `secrets` 表（Fernet 加密） |
| Trigger | 创建 session 并投递 turn 的入口：Web、cron、webhook、Telegram、agent 委派 | `cron_jobs` / `webhooks` 表；Telegram 走配置 |

目录：

```
data/
  muliao.db
  vapid_private.pem          # Web Push 密钥，首次启动生成
  skills/<name>/SKILL.md     # 公共 skill
  agents/<name>/
    memory.md                # 私有记忆
    skills/<name>/SKILL.md   # 私有 skill（同名时遮蔽公共 skill）
    workspace/               # 文件工具与 shell 的工作目录
```

## 2. 核心数据流：一切都是 Task

所有触发源只做一件事：**创建 session（或选中已有 session）并在同一事务里写入一个 `turn` task**。之后的执行、重试、反思、通知全部由同一个任务队列驱动。

```
Web / cron / webhook / Telegram / delegate_task
          │  SessionService.start() / .reply()
          ▼
      tasks(turn) ──► Worker ──► TurnHandler ──► pydantic-ai Agent.run
                                      │ 同一事务提交：
                                      ├─ messages（本轮新消息）
                                      ├─ session.unread / updated_at
                                      ├─ agent.turns_since_reflection += 1
                                      ├─ 达到阈值 → tasks(reflection)
                                      └─ session.delivery != none → tasks(deliver)
                                                     │
                              ReflectionHandler ◄────┘     DeliverHandler ──► Web Push / Telegram
```

- **Lane（串行通道）**：每个 task 带 `lane`。同一 lane 内严格 FIFO、同一时刻最多一个 running；不同 lane 并发。turn 的 lane 是 session id，reflection 的 lane 是 `reflection:<agent>`，deliver 的 lane 是 `deliver:<session>`。
- **TaskHandler 接口**：每种 kind 一个实现，worker 只负责领取、调用、按结果记账，不关心 kind 的细节。
- **实时展示**：运行中的事件（文本增量、工具调用/结果）经进程内 EventBus 推到前端 SSE；运行结束后前端从数据库重新拉取消息。数据库是唯一事实源，事件流只是展示加速。

### 用最终正确性换架构简化

换掉的：「每个触发点、每次模型调用、每次推送都必须当场成功」。

换来的：所有触发源、反思、通知共用一套「持久化 task + 重试 + 启动恢复」语义，没有各自的补偿逻辑。

三条判据：

1. **失败可见**：task 失败记录 `error`、打 error 日志，UI 的「失败任务」页可查并可手动重试；session 页显示该轮失败。
2. **可收敛**：契约内的失败（模型接口 408/429/5xx/529、网络错误、超时、推送服务 5xx）按指数退避重试，上限 `task_max_attempts`；进程崩溃后启动时把 `running` 复位为 `pending` 重新执行。turn 的消息、计数、后续 task 在**一个事务**里提交，所以重放不会产生重复消息。
3. **代价可控**：重放一轮 turn 可能重复执行工具副作用（如 shell 命令）——仅在崩溃或模型调用中途失败时发生，个人助手可接受。多订阅的 Web Push 重试可能对已成功的设备重复推送，可接受。

契约外的失败（代码 bug、模型返回 400、配置错误、agent 不存在）不重试，task 直接 `failed` 并告警。

## 3. Agent 运行时

一次 turn 的组装（`runtime/turn.py`）：

- **instructions** = agent 自身指令 + 运行上下文（agent 名、工作目录、当前时间、session 来源）+ memory 全文 + skill 索引（名称、描述、作用域）+ 自我进化守则。
- **toolsets**：
  - `workspace`：`read_file` / `write_file` / `edit_file` / `list_files` / `run_shell`（cwd = 工作目录，最小环境变量，可按 key 注入 secret，输出中的 secret 明文被替换为 `[secret:KEY]`）。不做强隔离——按需求为单人本机使用。
  - `skills`：`read_skill` / `write_skill` / `delete_skill`（scope = `private` | `public`）。
  - `memory`：`memory_append` / `memory_replace`，超长时 `ModelRetry` 要求先精简。
  - `agents`：`list_agents` / `delegate_task`（为目标 agent 新建 session 并投递 turn，origin=`delegation`，完成后推送）。
  - `notify`：`notify_user`（显式 Web Push）。
  - `mcp_admin`：`mcp_list` / `mcp_add`（写入前实际连接并列出工具，连接失败直接把错误还给 agent）/ `mcp_remove`。新 MCP 从下一轮起生效。
  - `vault`：`vault_list` / `vault_set`。没有读取明文的工具。
  - `cron`：`cron_list` / `cron_create` / `cron_delete`。
  - 该 agent 可见的 MCP server（公共 + 私有），以 `<server名>_` 为工具前缀；配置中的 `${vault:KEY}` 在运行时解析，引用不存在的 key 直接报错。
- **model**：agent 的 `model` 字段是 pydantic-ai 模型字符串（`anthropic:...`、`openai:...`、`google-gla:...` 等），密钥由对应环境变量提供。

## 4. 进化机制

### Skill
agent 在任何一轮都可以主动 `write_skill`。公共 skill 对所有 agent 可见；私有 skill 只对自己可见，同名时私有遮蔽公共。

### Memory
agent 私有、有长度上限（`memory_max_chars`），每轮全文注入 instructions。

### 强制反思（reflection）
- 计数在 **agent** 上：每完成一轮 turn，`agents.turns_since_reflection += 1`。达到该 agent 的 `reflect_every` 时，在同一事务里清零计数、把检查点推进到当前最大消息 id，并投递一个 reflection task，载荷是消息 id 区间 `(from_id, to_id]`。
- 选择 agent 级而不是 session 级：cron 场景下每次触发都是一个新 session，只有一轮，按 session 计数永远到不了阈值。
- ReflectionHandler 把这个区间内该 agent 所有 turn 消息渲染成文字记录，连同固定的反思提示交给同一个 agent 运行，只开放 skill 和 memory 工具。没有需要修改的内容也要走完并给出结论。
- 反思的完整消息和结论写入 `reflections` 表，可在 UI 查看；不进入任何 session 的历史。

## 5. 触发源

| 触发源 | session 策略 | 通知（`session.delivery`） |
|---|---|---|
| Web 用户发起 | 新建；回复沿用 | `webpush`（Service Worker 在页面可见时不弹） |
| cron | 每次触发新建；上一次触发的 session 仍有未完成 turn 时跳过本次（不重叠） | `none`，需要通知时 agent 调 `notify_user` |
| webhook | 每个事件新建，事件体附在 prompt 后 | `webpush` |
| Telegram | 每个 chat 复用最近的 session，`/new` 新开 | `telegram`（回复发回原 chat） |
| delegate_task | 新建，`origin_ref` 记父 session | `webpush` |

Slack 入站事件尚未实现：接入方式是新增一个 trigger（Events API 回调）和一个 delivery 实现，与 Telegram 同构。示例中的「每分钟拉取 Slack 未读」用 cron + Slack MCP（或 skill 中的脚本 + vault token）即可完成，游标由 agent 存在自己的工作目录。

## 6. 安全边界

- Web 与 API：单用户密码登录，HMAC 签名 cookie。
- webhook：每个 webhook 独立 token。
- Telegram：只接受白名单 chat id，其余记 warn 日志后丢弃。
- Vault：Fernet 加密，主密钥来自 `MULIAO_VAULT_KEY`，缺失时拒绝启动。
- 日志不打印 secret 值与 token。

## 7. 已知限制

- 长 session 的上下文没有自动压缩，超长会触发模型上下文错误（task 失败可见）。
- 只支持单进程部署（task 领取依赖进程内锁）。
- 数据库结构用 `create_all` 初始化，尚无迁移工具。
