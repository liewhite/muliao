# Muliao 架构

个人多 agent 助手。后端 Python（FastAPI + pydantic-ai），前端 React PWA（可添加到 iOS 主屏并接收 Web Push）。单机部署，SQLite + 文件目录。

## 1. 领域概念

| 概念 | 含义 | 事实存放处（唯一来源） |
|---|---|---|
| Agent | 一个助手：模型、指令、私有记忆、私有 skill、工作目录。以系统生成、不可变的 `id`（12 位十六进制）标识——主键、外键、目录名、URL、工具参数、配置引用都用它；`name` 只用于展示，可改、唯一（NFC 规范化，任意可见字符） | `agents` 表；文件在 `data/agents/<id>/` |
| Session | 某个 agent 的一段对话，由某个触发源创建 | `sessions` 表 |
| Message | pydantic-ai `ModelMessage` 原样序列化 | `messages` 表（每行一条 ModelMessage） |
| Task | 一个待执行的工作单元：`turn`（对话一轮）、`compaction`（上下文压缩）、`reflection`（强制反思）、`deliver`（投递通知） | `tasks` 表 |
| Compaction | session 早期消息的摘要，之后发给模型的上下文用它代替 `through_message_id` 及之前的消息 | `compactions` 表 |
| Skill | `SKILL.md`（YAML frontmatter: name, description + 正文）。公共 skill 与 agent 私有 skill | `data/skills/<name>/`、`data/agents/<id>/skills/<name>/` |
| Memory | agent 私有记忆，一份有长度上限的 markdown | `data/agents/<id>/memory.md` |
| MCP server | agent 私有或公共的 MCP 连接配置，可引用 vault | `mcp_servers` 表 |
| Secret | 加密保存的密码/token，通过 key 引用 | `secrets` 表（Fernet 加密） |
| Trigger | 创建 session 并投递 turn 的入口：用户在网页发起、cron 定时触发；另有 agent 之间的委派（`delegate_task`） | `cron_jobs` 表 |

目录：

```
data/
  muliao.db
  vapid_private.pem          # Web Push 密钥，首次启动生成
  skills/<name>/SKILL.md     # 公共 skill
  agents/<id>/
    memory.md                # 私有记忆
    skills/<name>/SKILL.md   # 私有 skill（同名时遮蔽公共 skill）
    workspace/               # 文件工具与 shell 的工作目录
```

## 2. 核心数据流：一切都是 Task

所有触发源只做一件事：**创建 session（或选中已有 session）并在同一事务里写入一个 `turn` task**。之后的执行、重试、反思、通知全部由同一个任务队列驱动。

```
Web / cron / delegate_task
          │  SessionService.start() / .reply()
          ▼
      tasks(turn) ──► Worker ──► TurnHandler ──► pydantic-ai Agent.run
                                      │ 同一事务提交：
                                      ├─ messages（本轮新消息）
                                      ├─ session.unread / updated_at
                                      ├─ agent.turns_since_reflection += 1
                                      ├─ 达到阈值 → tasks(reflection)
                                      └─ origin.notifies → tasks(deliver)
                                                     │
                              ReflectionHandler ◄────┘     DeliverHandler ──► Web Push
```

- **Lane（串行通道）**：每个 task 带 `lane`。同一 lane 内严格 FIFO、同一时刻最多一个 running；不同 lane 并发。turn 与 compaction 的 lane 是 session id，reflection 的 lane 是 `reflection:<agent id>`，deliver 的 lane 是 `deliver:<session>`。
- **TaskHandler 接口**：每种 kind 一个实现，worker 只负责领取、调用、按结果记账，不关心 kind 的细节。每个 task 有总时限（`task_timeout_seconds`），超时按暂时性失败重试。
- **事务**：SQLite 只有一个写者，`Database.transaction()` 在进程内用锁串行化所有事务，换来无丢失更新、无 `SQLITE_BUSY`；代价是事务内不得做模型调用、网络 IO 等慢操作。嵌套开事务会立即报错。
- **删除约束**：session 或 agent 有正在运行的 task 时拒绝删除（409），避免运行结果无处提交。
- **实时展示**：运行中的事件（文本增量、工具调用/结果）经进程内 EventBus 推到前端 SSE；运行结束后前端从数据库重新拉取消息。数据库是唯一事实源，事件流只是展示加速。

### 用最终正确性换架构简化

换掉的：「每个触发点、每次模型调用、每次推送都必须当场成功」。

换来的：所有触发源、反思、通知共用一套「持久化 task + 重试 + 启动恢复」语义，没有各自的补偿逻辑。

三条判据：

1. **失败可见**：task 失败记录 `error`、打 error 日志，UI 的「任务」页可查并可手动重试；session 页显示该轮失败；重试运行期间保留上一次的错误。
2. **可收敛**：契约内的失败（模型接口 408/409/425/429/5xx/529、底层为连接错误的 `ModelAPIError`、网络错误、超时、推送服务 5xx/429）按指数退避重试，上限 `task_max_attempts`；进程崩溃后启动时把 `running` 复位为 `pending` 重新执行。turn 的消息、计数、后续 task 在**一个事务**里提交，所以重放不会产生重复消息。
3. **代价可控**：重放一轮 turn 可能重复执行工具副作用（如 shell 命令）——仅在崩溃、超时或模型调用中途失败时发生，个人助手可接受。Web Push 逐个订阅投递，只有 404/410（RFC 8030 的订阅失效）才删除订阅，其他 4xx 视为本端问题让 task 失败、不删数据；只有暂时性失败才整体重试，可能对已成功的设备重复推送，可接受。

契约外的失败（代码 bug、模型返回 400、请求超限、响应解码失败、配置错误）不重试，task 直接 `failed` 并告警。

后台循环（worker、cron）意外退出视为 bug，进程直接退出交给进程管理器重启。cron 中单个 job 的数据不合法时只停用该 job 并记录 `last_error`，不影响其他 job。

## 3. Agent 运行时

一次 turn 的组装（`runtime/turn.py`）：

- **原则：给模型看的是名称和描述，给程序用的是 id。** 工具参数、返回值、instructions 里只出现 agent 名称；名称唯一，由工具层（`AgentService.find_by_name`）解析为 id，写错时把可用名称列表作为错误交还模型。id 只会作为文件路径的一部分（`agents/<id>/…`）出现在工作目录里。`tests/test_flow.py::test_model_sees_agent_names_never_ids` 守住这条规则。
- **instructions** = agent 自身指令 + 运行上下文（agent 名、工作目录、当前时间、session 来源）+ memory 全文 + skill 索引（名称、描述、作用域）+ 自我进化守则。
- **toolsets**：
  - `workspace`：`read_file` / `write_file` / `edit_file` / `list_files` / `run_shell`（cwd = 工作目录，最小环境变量，可按 key 注入 secret，输出中的 secret 明文被替换为 `[secret:KEY]`）。不做强隔离——按需求为单人本机使用。
  - `skills`：`read_skill` / `write_skill` / `delete_skill`（scope = `private` | `public`）。
  - `memory`：`memory_append` / `memory_replace`，超长时 `ModelRetry` 要求先精简。
  - `agents`：`list_agents`（返回 name、description）/ `delegate_task(agent, task)`（按名称指定目标 agent，为它新建 session 并投递 turn，origin=`delegation`，完成后推送）。
  - `notify`：`notify_user`（显式 Web Push）。
  - `mcp_admin`：`mcp_list` / `mcp_add`（写入前实际连接并列出工具，连接失败直接把错误还给 agent）/ `mcp_remove`。新 MCP 从下一轮起生效。
  - `vault`：`vault_list` / `vault_set`。没有读取明文的工具；`vault_set` 的 value 参数在消息落库和推送到前端前被替换为 `[redacted]`（`runtime/redaction.py`）。
  - `cron`：`cron_list` / `cron_create(name, schedule, prompt, agent?)` / `cron_delete`，agent 同样按名称指定。
  - 该 agent 可见的 MCP server（公共 + 私有），以 `<server名>_` 为工具前缀；配置中的 `${vault:KEY}` 在运行时解析，引用不存在的 key 直接报错。
- **model**：agent 的 `model` 字段格式为 `<provider 名>:<模型名>`，provider 在配置文件 `[llm.providers.<名>]` 中定义（类型、api_key、base_url）。`llm.ModelRegistry` 是唯一按配置构造模型的地方，每个 provider 复用一个客户端。API key 与 base_url 只来自配置：base_url 未填时显式传入该类型的官方地址，不让 SDK 回退到 `*_BASE_URL` 环境变量。SDK 仍可能读取少数无法通过参数关闭的环境变量（如 `OPENAI_ORG_ID`、自定义请求头），launchd 服务只设置 `PATH`，不会带入这些变量。

## 4. 上下文压缩

与模型提供方无关的统一机制（`runtime/compaction.py`）。pydantic-ai 只为 Anthropic 与 OpenAI Responses 提供原生压缩，`openai` 兼容接口和 Gemini 没有，按提供方分支会产生两套语义，所以统一自己实现：

- **触发**：一轮结束时，最后一次模型请求的输入 token（pydantic-ai 已计入缓存 token）超过 `runtime.compaction_threshold_tokens`，在提交该轮的同一事务里投递 `compaction` task；也可在会话页手动触发。它与 turn 同一 lane，同一 session 最多一个待执行的压缩。排在已经排队的用户消息之后，所以那些轮次仍带完整上下文运行。模型不返回 usage（部分 OpenAI 兼容服务流式时如此）时记 warn 日志，自动触发对该模型失效。
- **执行**：把最近 `runtime.compaction_keep_turns` 轮之前的消息连同上一份摘要交给该 agent 的模型总结，写入 `compactions` 表（`through_message_id` + `summary`）。切点总在两轮之间，工具调用与其返回不会被拆开。单次只取总长不超过上限的最早若干轮，不截断内容；剩余部分在同一事务里续排下一次压缩，直到收敛。没有可压缩的轮次时什么也不做。
- **使用**：发给模型的上下文 = 最新摘要（放进 instructions）+ 切点之后的原始消息。原始消息不删除，UI 显示完整原文并标出压缩点；反思读取的仍是原始消息。

## 5. 数据库迁移

Alembic 管理表结构（`src/muliao/migrations/`，随包分发），启动时自动升级到最新版本。整个升级在一个真正的事务里：pysqlite 默认只在 DML 前隐式开事务、DDL 逐条自动提交，所以迁移使用独立连接，驱动设为 autocommit 并显式 `BEGIN IMMEDIATE`，任何失败都完整回滚。SQLite 改表用 batch 模式（建新表-拷数据-删旧表），迁移连接在建立时关闭外键以免删旧表时级联删除子表数据，提交前执行 `foreign_key_check`，有违例则回滚。`tests/test_migrations.py` 保证迁移后的结构与模型定义一致。

## 6. 进化机制

### Skill
agent 在任何一轮都可以主动 `write_skill`。公共 skill 对所有 agent 可见；私有 skill 只对自己可见，同名时私有遮蔽公共。

### Memory
agent 私有、有长度上限（`memory_max_chars`），每轮全文注入 instructions。

### 强制反思（reflection）
- 计数在 **agent** 上：每完成一轮 turn，`agents.turns_since_reflection += 1`。达到该 agent 的 `reflect_every` 时，在同一事务里清零计数、把检查点推进到当前最大消息 id，并投递一个 reflection task，载荷是消息 id 区间 `(from_id, to_id]`。
- 选择 agent 级而不是 session 级：cron 场景下每次触发都是一个新 session，只有一轮，按 session 计数永远到不了阈值。
- ReflectionHandler 把这个区间内该 agent 所有 turn 消息渲染成文字记录，连同固定的反思提示交给同一个 agent 运行，只开放 skill 和 memory 工具。没有需要修改的内容也要走完并给出结论。
- 反思的完整消息和结论写入 `reflections` 表，可在 UI 查看；不进入任何 session 的历史。

## 7. 触发源

触发点只有两个：用户在网页发起对话、cron 定时任务。agent 之间的委派（`delegate_task`）是 agent 内部能力，也会新建 session。

| 来源 | session 策略 | 完成后推送（`Origin.notifies`，唯一来源） |
|---|---|---|
| 用户发起 | 新建；回复沿用 | 是（iOS 要求每个 push 都展示通知，所以页面可见时也会弹） |
| cron | 每次触发新建；上一次触发的 session 仍有未完成 turn 时跳过本次（不重叠） | 否，需要通知时 agent 调 `notify_user` |
| 委派 | 新建，`origin_ref` 记父 session | 是 |

通知只有 Web Push 一种渠道。外部消息（Slack 等）不做实时接入，统一由 cron 定时拉取：例如每分钟让 agent 通过 Slack MCP（或 skill 中的脚本 + vault token）读取未读消息，游标由 agent 存在自己的工作目录。

## 8. 安全边界

**威胁模型：agent 与运行服务的系统用户同等可信。** agent 能执行任意 shell 命令，因此能读到服务能读到的一切——包括配置文件里的 `vault.key` 与 LLM API key、数据库文件。下面的措施只防止 secret **意外**进入对话记录、日志和前端，不防 agent 本身：

- shell 只继承少量环境变量（`PATH`、`HOME` 等）；
- `run_shell(secrets=[KEY])` 输出中的 secret 明文被替换为 `[secret:KEY]`（逐字匹配，编码后的输出不会被替换）；
- `vault_set` 的参数在持久化前脱敏；但用户在 prompt 里直接写的明文会原样保存并在后续轮次发给模型——敏感值应在 UI 的 Vault 页录入。

如需真正隔离，应让服务以独立系统用户运行、把配置文件与数据目录放在 agent 可读范围之外并限制权限，或把 shell 放进沙箱。

其他边界：

- Web 与 API：单用户密码登录（至少 8 位），HMAC 签名 cookie（密钥至少 32 位），有效期 90 天。登录接口未做频率限制，部署在公网时应在反向代理层限流。
- Vault：Fernet 加密，主密钥来自配置 `vault.key`，缺失或格式错误时拒绝启动。
- 配置文件：唯一配置来源（`config.example.toml`），未知键直接报错，避免拼写错误被静默忽略。
- 日志不打印 secret 值与 token。

## 9. 部署形态与边界

- **单进程**：事务串行化与 task 领取依赖进程内锁，所以只跑一个进程。这不影响 agent 并发：worker 同时执行 `runtime.worker_concurrency` 个 task（不同 session、不同 agent 并行，同一 session 串行）。模型调用与工具执行都是异步 IO，不占事务锁。
- **外部消息**：不做实时推送接入（Slack 等）。需要定时处理外部消息时，用 cron 让 agent 按时拉取（见第 7 节）。
- **上下文压缩的覆盖范围**：只在两轮之间压缩。单轮内工具调用极多导致上下文超限时，该轮失败可见；在会话页点「压缩」后重试该轮即可。不自动识别各提供方的超限错误，因为错误格式各不相同，无法可靠识别。
