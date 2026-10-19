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
| Trigger | 创建 session 并投递 turn 的入口：用户在网页发起、cron 定时触发、外部事件触发；另有 agent 之间的委派 | `cron_jobs`、`event_triggers` 表 |

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
Web / cron / 外部事件 / delegate_task / ask_agent
          │  conversation.receive(Inbound)（唯一入口；payload 带 prompt 与 reply_to）
          ▼
 tasks(turn) ──► Worker ──► TurnHandler ──► pydantic-ai Agent.run
                                      │ 同一事务提交：
                                      ├─ messages（本轮新消息）
                                      ├─ session.unread / updated_at
                                      ├─ agent.turns_since_reflection += 1
                                      ├─ 达到阈值 → tasks(reflection)
                                      ├─ reply_to = user → tasks(deliver)
                                      ├─ 唤醒等这一轮的 waits
                                      └─ 输出为待完成工具调用 → 挂起为 waiting（见第 4 节）
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
  - `agents`：`list_agents`（返回 name、description）/ `delegate_task` / `ask_agent` / `wait_for`（按名称指定目标 agent、按 thread 名指定子会话，见第 4 节）。
  - `notify`：`notify_user`（显式 Web Push）。
  - `mcp_admin`：`mcp_list` / `mcp_add`（写入前实际连接并列出工具，连接失败直接把错误还给 agent）/ `mcp_remove`。新 MCP 从下一轮起生效。
  - `vault`：`vault_list` / `vault_set`。没有读取明文的工具；`vault_set` 的 value 参数在消息落库和推送到前端前被替换为 `[redacted]`（`runtime/redaction.py`）。
  - `cron`：`cron_list` / `cron_create(name, schedule, prompt, agent?)` / `cron_delete`，agent 同样按名称指定。
  - 该 agent 可见的 MCP server（公共 + 私有），以 `<server名>_` 为工具前缀；配置中的 `${vault:KEY}` 在运行时解析，引用不存在的 key 直接报错。
- **model**：agent 的 `model` 字段格式为 `<provider 名>:<模型名>`，provider 在配置文件 `[llm.providers.<名>]` 中定义（类型、api_key、base_url）。`llm.ModelRegistry` 是唯一按配置构造模型的地方，每个 provider 复用一个客户端。API key 与 base_url 只来自配置：base_url 未填时显式传入该类型的官方地址，不让 SDK 回退到 `*_BASE_URL` 环境变量。SDK 仍可能读取少数无法通过参数关闭的环境变量（如 `OPENAI_ORG_ID`、自定义请求头），launchd 服务只设置 `PATH`，不会带入这些变量。

## 4. 会话领域：thread 与结果路由

agent 之间的委派有两种形态，都统一为「对某个 thread 发起一轮」：

| 工具 | 父 agent | 子 session 归属 | 这一轮结果交给谁 |
|---|---|---|---|
| `delegate_task(agent, task, thread)` | 立即继续 | 用户可见（进收件箱，用户可追问） | 用户（推送）；父 agent 可用 `wait_for(thread)` 等它 |
| `ask_agent(agent, message, thread)` | 挂起，拿到结果后恢复 | 父会话私有（不进收件箱，用户只读） | 父 agent（作为工具返回值） |
| `wait_for(thread)` | 挂起，直到该 thread 最近一轮结束 | — | 父 agent |

- **thread**：父 session 内对子 session 的具名引用（`sessions.parent_session_id` + `thread`，同一父 session 内唯一）。模型只用 thread 名，不接触 session id。用同一个 thread 名再次 `ask_agent`，就是在原来的子 session 里继续，上下文保留。thread 的归属在创建时确定：私有 thread 不能再用 `delegate_task` 变成用户可见；用户可见的 thread 可以被 `ask_agent` 同步追问。
- **结果路由按轮、不按 session**：每个 turn task 的载荷带 `reply_to`（`user` 推送给用户；`agent` 交给发起的 agent，不推送；`none` 不推送，cron 使用，由 agent 自己决定是否 `notify_user`）。同一个子 session 里，用户追问的那一轮回给用户，父 agent 发起的那一轮回给父 agent。等待关系另存在 `waits` 表里，不靠 `reply_to`。
- **一轮 = 一个 turn task**：这个 task 的状态就是这一轮的状态（单一事实源）。等待、重试、`wait_for` 的判断都只看它。
- **持久化挂起**：`ask_agent` / `wait_for` 抛出 pydantic-ai 的 `CallDeferred`，这一轮以「待完成的工具调用」结束。提交时在同一事务里写入这些消息，为每个挂起的调用写一行 `waits`（等待方 turn、tool_call_id、所等的 turn），`ask_agent` 同时给子 thread 投递一轮（`reply_to = agent`）；然后 task 转为 `waiting`。`waiting` 和 `running` 一样挡住同一 session 的后续任务：用户此时的消息排在它后面，不会插进「工具调用」与「工具结果」之间。等待不占用 worker。
- **唤醒**：所等的 turn 完成（done）时把最终回答写进指向它的 `waits`，彻底失败或所在 session 被删除时写入错误说明。某个 turn 的全部 `waits` 都有结果后转回 `pending`；再次执行时带着 `DeferredToolResults` 接着跑，用过的 `waits` 随结果进入消息历史后删除。所等的 turn 自己挂起时仍是 `waiting`，不算结束，所以等到的一定是最终回答。若工具判断「仍在运行」之后、提交之前目标已经结束，提交时就地补上结果，task 直接回到 `pending`。
- **收敛**：每一轮都有重试上限和总时限，最终一定 done 或 failed；删除会话（包括删除 agent 带走的会话）统一经过 `conversation.delete_sessions`，给等待方写入错误。因此等待一定会结束。
- **thread 的占用**：`ask_agent` 在工具执行时就创建（占用）thread，同一响应里并行的调用若争用同名 thread，冲突作为可纠正的错误交回模型。占用它的那一轮回滚后，thread 可能暂时没有任何一轮，`wait_for` 会如实告诉模型。
- **一轮是原子的**：彻底失败时整轮回滚——删除这一轮已提交的消息和等待记录（否则历史里留下没有结果的工具调用，之后每一轮都会被模型 API 拒绝），等它的 agent 收到错误。人工重试就是从 prompt 重跑整轮；已收到错误的等待方不会再收到重试的结果。已经发出的委派不随回滚撤销，重跑时模型可能再委派一次，私有 thread 里会留下上一次的问答。
- **与反思的关系**：反思按消息 id 区间取材，可能包含一轮挂起时已提交的前半段；这一轮若随后回滚，反思已经看过被撤销的内容。只影响记忆质量，接受。
- **删除**：删除父 session 会一并删除它的私有 thread（逐层）；用户可见的 thread 保留（`parent_session_id` 置空）。有任务正在运行时拒绝删除。

## 5. 上下文压缩

与模型提供方无关的统一机制（`runtime/compaction.py`）。pydantic-ai 只为 Anthropic 与 OpenAI Responses 提供原生压缩，`openai` 兼容接口和 Gemini 没有，按提供方分支会产生两套语义，所以统一自己实现：

- **触发**：一轮结束时，最后一次模型请求的输入 token（pydantic-ai 已计入缓存 token）超过 `runtime.compaction_threshold_tokens`，在提交该轮的同一事务里投递 `compaction` task；也可在会话页手动触发。它与 turn 同一 lane，同一 session 最多一个待执行的压缩。排在已经排队的用户消息之后，所以那些轮次仍带完整上下文运行。模型不返回 usage（部分 OpenAI 兼容服务流式时如此）时记 warn 日志，自动触发对该模型失效。
- **执行**：把最近 `runtime.compaction_keep_turns` 轮之前的消息连同上一份摘要交给该 agent 的模型总结，写入 `compactions` 表（`through_message_id` + `summary`）。切点总在两轮之间，工具调用与其返回不会被拆开。单次只取总长不超过上限的最早若干轮，不截断内容；剩余部分在同一事务里续排下一次压缩，直到收敛。没有可压缩的轮次时什么也不做。
- **使用**：发给模型的上下文 = 最新摘要（放进 instructions）+ 切点之后的原始消息。原始消息不删除，UI 显示完整原文并标出压缩点；反思读取的仍是原始消息。

## 6. 数据库迁移

Alembic 管理表结构（`src/muliao/migrations/`，随包分发），启动时自动升级到最新版本。整个升级在一个真正的事务里：pysqlite 默认只在 DML 前隐式开事务、DDL 逐条自动提交，所以迁移使用独立连接，驱动设为 autocommit 并显式 `BEGIN IMMEDIATE`，任何失败都完整回滚。SQLite 改表用 batch 模式（建新表-拷数据-删旧表），迁移连接在建立时关闭外键以免删旧表时级联删除子表数据，提交前执行 `foreign_key_check`，有违例则回滚。`tests/test_migrations.py` 保证迁移后的结构与模型定义一致。

## 7. 进化机制

### Skill
agent 在任何一轮都可以主动 `write_skill`。公共 skill 对所有 agent 可见；私有 skill 只对自己可见，同名时私有遮蔽公共。

### Memory
agent 私有、有长度上限（`memory_max_chars`），每轮全文注入 instructions。

### 强制反思（reflection）
- 计数在 **agent** 上：每完成一轮 turn，`agents.turns_since_reflection += 1`。达到该 agent 的 `reflect_every` 时，在同一事务里清零计数、把检查点推进到当前最大消息 id，并投递一个 reflection task，载荷是消息 id 区间 `(from_id, to_id]`。
- 选择 agent 级而不是 session 级：cron 场景下每次触发都是一个新 session，只有一轮，按 session 计数永远到不了阈值。
- ReflectionHandler 把这个区间内该 agent 所有 turn 消息渲染成文字记录，连同固定的反思提示交给同一个 agent 运行，只开放 skill 和 memory 工具。没有需要修改的内容也要走完并给出结论。
- 反思的完整消息和结论写入 `reflections` 表，可在 UI 查看；不进入任何 session 的历史。

## 8. 触发源

触发点有三个：用户在网页发起对话、cron 定时任务、外部事件。agent 之间的委派是 agent 内部能力，在父会话下开 thread（第 4 节）。

**统一入口**：用户消息、cron 到点、外部事件、agent 委派都是同一件事——把一段内容交给某个 agent，在某个会话里处理一轮。各来源只做适配（鉴权、触发时机、组装内容），然后构造一个 `Inbound` 交给 `conversation.receive`：

| 字段 | 含义 |
|---|---|
| `source` / `source_ref` | 来源与具体发送方：用户（无）、cron job id、事件触发器 id、委派方（父）会话 id |
| `content` | 这一轮的 prompt |
| `session` | 已有会话的 id（在其中继续），或 `NewSession`（新建：agent、标题，委派时还有父会话、thread 名、是否私有） |
| `reply_to` | 这一轮的回答交给谁 |

`receive` 是唯一投递新一轮的地方；新建会话也只有 `create_session` 一处实现（`ask_agent` 提前占用 thread 时也用它）。**续写规则**：用户可以在任何非私有会话里继续；其他来源只能续写自己发起的会话（会话的来源与发送方都一致，委派会话的发送方就是它的父会话），否则 403。发送方身份是 cron job id、触发器 id（两张表都用 AUTOINCREMENT，删除后不复用）或父会话 id。

是否推送由每一轮的 `reply_to` 决定（唯一来源），由发起这一轮的入口设置：

| 入口 | session 策略 | `reply_to` |
|---|---|---|
| 用户发起 / 回复 | 新建；回复沿用（私有 thread 只读） | `user`（iOS 要求每个 push 都展示通知，所以页面可见时也会弹） |
| cron | 每次触发新建，`origin_ref` 记 cron job；上一次触发的 session 仍有未完成任务时跳过本次（不重叠） | `none`，需要通知时 agent 调 `notify_user` |
| 外部事件 | 不带 `session_id` 时新建，`origin_ref` 记触发器，响应返回会话 id；带上它的后续事件在同一会话里继续（只能续写本触发器发起的会话） | `none`，同 cron |
| `delegate_task` | 父会话下的公开 thread，同名续聊 | `user` |
| `ask_agent` | 父会话下的私有 thread，同名续聊 | `agent` |

**外部事件**：每个事件触发器有一个名称和一个 token。外部系统调用 `POST /api/hooks/{name}[?session_id=...]`，带 `Authorization: Bearer <token>`，响应 `{"session_id": ...}`；请求体（UTF-8 文本，最多 256 KiB）原样附在触发器的处理说明之后，作为这一轮的 prompt：

```
<处理说明>

<event>
<请求体>
</event>
```

- 该入口不走登录 cookie，只凭 token 鉴权，**先鉴权再读请求体**：未鉴权的调用方不能让进程读入任何内容；请求体按字节上限分块读取，超限返回 413。触发器不存在与 token 不匹配返回同一个 401，不暴露哪些名称存在；停用的触发器返回 409；续写不是本触发器发起的会话返回 403。
- token 只存 SHA-256，明文只在创建或重置时返回一次；重置后旧 token 立即失效。
- 不校验各平台自己的签名格式（GitHub、Slack 等各不相同）。需要时由发送方把 token 放进 Authorization 头，或在中间加一层转发。

通知只有 Web Push 一种渠道。不支持主动推送的外部消息源（例如只能拉取的 Slack 未读）由 cron 定时拉取：例如每分钟让 agent 通过 Slack MCP（或 skill 中的脚本 + vault token）读取未读消息，游标由 agent 存在自己的工作目录。

## 9. 安全边界

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

## 10. 部署形态与边界

- **单进程**：事务串行化与 task 领取依赖进程内锁，所以只跑一个进程。这不影响 agent 并发：worker 同时执行 `runtime.worker_concurrency` 个 task（不同 session、不同 agent 并行，同一 session 串行）。模型调用与工具执行都是异步 IO，不占事务锁。
- **外部消息**：能主动推送的外部系统接入事件触发器；只能拉取的由 cron 定时拉取（见第 8 节）。
- **上下文压缩的覆盖范围**：只在两轮之间压缩。单轮内工具调用极多导致上下文超限时，该轮失败可见；在会话页点「压缩」后重试该轮即可。不自动识别各提供方的超限错误，因为错误格式各不相同，无法可靠识别。
