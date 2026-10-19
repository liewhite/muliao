# Muliao 架构

个人多 agent 助手。后端 Python（FastAPI + pydantic-ai），前端 React PWA（可添加到 iOS 主屏并接收 Web Push）。单机部署，SQLite + 文件目录。

## 1. 领域概念

| 概念 | 含义 | 事实存放处（唯一来源） |
|---|---|---|
| Agent | 一个助手：模型、指令、私有记忆、私有 skill、工作目录。以系统生成、不可变的 `id`（12 位十六进制）标识——主键、外键、目录名、URL、工具参数、配置引用都用它；`name` 只用于展示，可改、唯一（NFC 规范化，任意可见字符） | `agents` 表；私有资源在 `data/resources/agents/<id>/`，工作目录 `data/workspaces/<id>/` |
| Session | 某个 agent 的一段对话，由某个触发源创建 | `sessions` 表 |
| Message | pydantic-ai `ModelMessage` 原样序列化 | `messages` 表（每行一条 ModelMessage） |
| Task | 一个待执行的工作单元：`turn`（对话一轮）、`compaction`（上下文压缩）、`reflection`（强制反思）、`deliver`（投递通知） | `tasks` 表 |
| Compaction | session 早期消息的摘要，之后发给模型的上下文用它代替 `through_message_id` 及之前的消息 | `compactions` 表 |
| Skill | `SKILL.md`（YAML frontmatter: name, description + 正文）。公共 skill 与 agent 私有 skill | `resources/public/skills/<name>/`、`resources/agents/<id>/skills/<name>/` |
| Prompt | agent 自己的指令，或合并进所有 agent 的公共 prompt | `resources/agents/<id>/PROMPT.md`、`resources/public/PROMPT.md` |
| Memory | agent 私有记忆，一份有长度上限的 markdown | `resources/agents/<id>/MEMORY.md` |
| MCP server | agent 私有或公共的 MCP 连接配置，可引用 vault | `resources/{public,agents/<id>}/mcp/<name>.json` |
| Secret | 加密保存的密码/token，通过 key 引用；公共或 agent 私有 | `secrets` 表（Fernet 加密） |
| Trigger | 具名、持久的事件来源，把外部刺激变成交给某个 agent 的一轮；触发方式（定时、webhook……）多态实现 | `triggers` 表 |

目录：

```
data/
  muliao.db
  vapid_private.pem            # Web Push 密钥，首次启动生成
  resources/                   # git 仓库：agent 能改的文本资源，每次修改提交一版（见 muliao.resources）
    public/
      PROMPT.md                # 公共 prompt（合并进所有 agent）
      skills/<name>/SKILL.md   # 公共 skill
      mcp/<name>.json          # 公共 MCP server
    agents/<id>/
      PROMPT.md                # agent 的指令
      MEMORY.md                # 私有记忆
      skills/<name>/SKILL.md   # 私有 skill（同名时遮蔽公共 skill）
      mcp/<name>.json          # 私有 MCP server（同名时遮蔽公共的）
  workspaces/<id>/             # 文件工具与 shell 的工作目录（不做版本管理）
  tmp/                         # 临时 agent 的工作目录，用完即删
```

**资源的版本管理**：agent 能改的文本资源都存成文件，用一个 git 仓库（`data/resources/`）管理版本：每次修改提交一次，作者是 `user` 或做出修改的 agent 的 id；历史与回滚都来自 git（回滚 = 把旧版本内容写回并作为新的一次提交）。secret 不在这里——它们加密存在数据库的 vault 里，不需要版本管理。

用最终正确性换简化：文件与 git 不在数据库事务里，不追求两者的强一致。
- 换掉的：跨数据库与文件的分布式事务（例如删除 agent 时数据库行与资源目录同时成功）。
- 失败可见：每次写入（`ResourceRepo.update`）的读取—变换—写入—提交都在同一把锁里，任何一步失败都把涉及的路径在文件系统上还原成写入前的样子（写入前先保存旧内容，不依赖 git——失败的往往正是 git，比如锁被别的进程占用）并向上报错，所以「报错」等于「没有生效」，调用方可以放心重试；还原本身失败时记 error 并与原错误一起抛出，不声称已回滚，留下的修改由下次启动的 recover 提交；启动时把数据库里已删除、仓库里还留着目录的 agent 记为 warning。
- 可收敛：只有进程在写入中途被杀会留下不一致，由启动时（worker 开始之前）的 `ResourceRepo.recover` 统一收敛：删除 `.git` 下遗留的锁与半写的 `*.tmp`，把写完未提交的变化提交为一次 `reconcile`（作者记为系统，日志记录涉及的路径）。
- 代价可控：不一致只发生在进程被杀的那一刻，窗口持续到下次启动；孤儿目录只占空间、出现在 MCP 管理列表里，由人清理。仓库不受部署机全局 git 配置的 hook 与签名影响。

## 2. 核心数据流：一切都是 Task

所有触发源只做一件事：**创建 session（或选中已有 session）并在同一事务里写入一个 `turn` task**。之后的执行、重试、反思、通知全部由同一个任务队列驱动。唯一的例外是 `ask_agent`：它在调用方这一轮里直接跑完一轮，提交时补记一条已完成的 turn task（见第 4 节）。

```
Web / 触发器（定时、webhook……）/ delegate_task
          │  conversation.receive(Inbound)（唯一入口；payload 带 prompt 与 reply_to）
          ▼
 tasks(turn) ──► Worker ──► TurnHandler ──┐
                                           ├─► TurnRunner ──► pydantic-ai Agent.run
 ask_agent（在调用方这一轮里内联）─────────┘
                                      │ 同一事务提交：
                                      ├─ messages（本轮新消息）
                                      ├─ session.unread / updated_at
                                      ├─ agent.turns_since_reflection += 1
                                      ├─ 达到阈值 → tasks(reflection)
                                      └─ reply_to = user → tasks(deliver)
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
3. **代价可控**：重放一轮 turn 可能重复执行工具副作用（如 shell 命令）——仅在崩溃、超时或模型调用中途失败时发生，个人助手可接受。临时 agent 会放大这一点：一轮里可能并行多条模型调用链，任何一条的暂时性失败都会取消其余并整轮重放（含各临时 agent 的 shell 与 MCP 副作用）。为此临时 agent 有请求数上限（`runtime.temp_agent_request_limit`），做不完的任务在上限处失败并交回调用方拆分，而不是靠整轮超时反复重试；剩余的重放代价仍按个人助手可接受。Web Push 逐个订阅投递，只有 404/410（RFC 8030 的订阅失效）才删除订阅，其他 4xx 视为本端问题让 task 失败、不删数据；只有暂时性失败才整体重试，可能对已成功的设备重复推送，可接受。

契约外的失败（代码 bug、模型返回 400、请求超限、响应解码失败、配置错误）不重试，task 直接 `failed` 并告警。

后台循环（worker、定时触发）意外退出视为 bug，进程直接退出交给进程管理器重启。单个定时触发器的数据不合法时只停用它并记录 `last_error`，不影响其他触发器。

## 3. Agent 运行时

一次 turn 的组装（`runtime/turn.py`）：

- **原则：给模型看的是名称和描述，给程序用的是 id。** 工具参数、返回值、instructions 里只出现 agent 名称；名称唯一，由工具层（`AgentService.find_by_name`）解析为 id，写错时把可用名称列表作为错误交还模型。id 只会作为文件路径的一部分（`agents/<id>/…`）出现在工作目录里。`tests/test_flow.py::test_model_sees_agent_names_never_ids` 守住这条规则。
- **instructions** = 公共 prompt + agent 自身指令 + 运行上下文（agent 名、工作目录、当前时间、session 来源）+ memory 全文 + skill 索引（名称、描述、作用域）+ 自我进化守则。
- **toolsets**（每个领域一个模块，在 `runtime/tools/` 下）。对话里开放哪些由 agent 的 `tool_groups` 决定（`runtime.runner.ToolGroups`，名称在组合根 `TOOL_GROUPS` 唯一声明）：`workspace`、`temp_agent`、`delegation`、`notify`、`agents`、`public_skills`（写公共 skill）、`mcp`（使用 MCP server）、`mcp_admin`（管理 MCP）、`vault`、`triggers`；私有 skill、memory 与自己的指令（自我改进）总是开放，读公共 skill 也总是可以。反思只开放自我改进，以及该 agent 本来就有的 `public_skills`。新建 agent 默认全部开放；处理不可信输入（外部事件、网页内容）的 agent 应只开放需要的组，限制提示注入能造成的后果。临时 agent 有固定的工具集（文件与 shell，加上调用方可见的 MCP，不看调用方是否选了 `mcp`；`temp_agent` 本来就等同于全部权限），不受 `tool_groups` 影响：
  - 局部修改（`update_agent`、`trigger_update`）由领域服务在一个事务里读取并只改给出的字段，工具层不读后整体写回：同一响应里并行的多个修改、以及用户在界面上的同时修改互不覆盖。面向模型的错误信息只带名称，不带 id。
  - `workspace`：`read_file` / `write_file` / `edit_file` / `list_files` / `run_shell`（cwd = 工作目录，最小环境变量，可按 key 注入 secret，输出中的 secret 明文被替换为 `[secret:KEY]`）。不做强隔离——按需求为单人本机使用。
  - `skills`：`read_skill`（私有或公共）/ `write_skill` / `delete_skill`（只限私有）；写公共 skill 在 `public_skills` 组：`write_public_skill` / `delete_public_skill`。
  - `memory`：`memory_append` / `memory_replace`，超长时 `ModelRetry` 要求先精简。
  - `instructions`：`instructions_replace`（改自己的指令：替换恰好出现一次的片段，或追加），每次修改记一版、作者是自己。
  - `agents`：`get_agent` / `create_agent` / `update_agent`（只改给出的字段，可以改自己）/ `delete_agent`（不能删除自己） / `get_public_prompt` / `set_public_prompt`。指令与公共 prompt 的修改都留版本，作者记为做出修改的 agent，用户可回滚。
  - `delegation`：`list_agents`（返回 name、description）/ `delegate_task` / `ask_agent` / `list_threads`（按名称指定目标 agent、按 thread 名指定子会话，见第 4 节）。
  - `notify`：`notify_user`（显式 Web Push）。
  - `temp_agent`：`run_temp_agent(task, instructions?, model?)`：一次性的临时 agent，在调用方这一轮里同步运行（同一响应中的多次调用并行），返回它的最终回答。它只有文件与 shell 工具（工作目录在 `data/tmp/` 下临时创建、结束即删除）和调用方的 MCP，secret 按调用方的视角解析；没有记忆、skill、会话、反思与委派，也不能再开临时 agent。适合抓取网页并总结、分析文件这类做完即丢的事；需要长期积累的专长应 `create_agent` 后委派。临时 agent 不是沙箱：它和调用方有相同的 shell 权限、secret 与 MCP，工具说明里也这样告诉模型。模型请求数超过 `runtime.temp_agent_request_limit` 时以可纠正的错误交回调用方；其他失败（如模型 API 错误）向上传播，按调用方这一轮的失败统一处理（暂时性失败整轮重试）。工作目录在 finally 里删除；删除失败或进程中途被杀时留到下次启动，由启动时清空 `data/tmp`（只归临时 agent 使用）统一收敛，删除失败记 error 日志、不改变运行结果。启动时删不掉的目录（例如脱离进程组的后台进程还在写）同样记 error 日志后跳过，不阻止服务启动：残留目录只占空间，不影响正确性。
  - `mcp_admin`：`mcp_list`（含已停用的）/ `mcp_add`（写入前实际连接并列出工具，连接失败直接把错误还给 agent）/ `mcp_set_enabled` / `mcp_remove`。改动从下一轮起生效。
  - `vault`：`vault_list` / `vault_set` / `vault_delete`（scope = `private` | `public`，默认私有）。没有读取明文的工具；`vault_set` 的 value 参数在消息落库和推送到前端前被替换为 `[redacted]`（`runtime/redaction.py`）。
  - `triggers`：`trigger_kinds` / `trigger_list` / `trigger_create(name, kind, prompt, config, agent?)` / `trigger_update` / `trigger_run` / `trigger_delete`，一组工具覆盖所有触发方式，差异只在 `kind` 与 `config`。agent 同样按名称指定。webhook 的 token 只能由用户在界面上生成，agent 拿不到也不需要。
  - 该 agent 可见的 MCP server（公共 + 私有），以 `<server名>_` 为工具前缀；配置中的 `${vault:KEY}` 在运行时按 server 归属者的视角解析（公共 server 只能引用公共 secret），引用不可见的 key 直接报错。
- **model**：agent 的 `model` 字段格式为 `<provider 名>:<模型名>`，provider 在配置文件 `[llm.providers.<名>]` 中定义（类型、api_key、base_url）。`llm.ModelRegistry` 是唯一按配置构造模型的地方，每个 provider 复用一个客户端。API key 与 base_url 只来自配置：base_url 未填时显式传入该类型的官方地址，不让 SDK 回退到 `*_BASE_URL` 环境变量。SDK 仍可能读取少数无法通过参数关闭的环境变量（如 `OPENAI_ORG_ID`、自定义请求头），launchd 服务只设置 `PATH`，不会带入这些变量。

## 4. 会话领域：thread 与结果路由

agent 之间的委派有两种形态，都是「对某个 thread 发起一轮」：

| 工具 | 父 agent | 子 session | 这一轮的回答 |
|---|---|---|---|
| `delegate_task(agent, task, thread)` | 立即继续，不等 | 公开 thread：进收件箱，用户可追问 | 推送给用户；父 agent 拿不到 |
| `ask_agent(agent, message, thread)` | 等它答完 | 私有 thread：不进收件箱，用户只读 | 作为工具结果交回父 agent |

- **thread**：父 session 内对子 session 的具名引用（`sessions.parent_session_id` + `thread`，同一父 session 内唯一）。模型只用 thread 名，不接触 session id。同名即续聊，对方的上下文保留（例如审查 → 修改 → 再审查）。thread 的 agent 与种类在创建时确定：私有 thread 只能用 `ask_agent` 续聊，公开 thread 只能用 `delegate_task`。
- **结果路由按轮、不按 session**：每个 turn task 的载荷带 `reply_to`（`user` 推送给用户；`agent` 交给发问的 agent，不推送；`none` 不推送，触发器使用，由 agent 自己决定是否 `notify_user`）。同一个公开 thread 里，父 agent 投递的那一轮与用户追问的那一轮都回给用户。
- **`delegate_task` 走队列**：往公开 thread 投递一轮，与用户消息一样排在该会话的 lane 里。
- **`ask_agent` 在调用方这一轮里直接跑完**（`TurnRunner.run_inline`）：与队列里的 turn 用同一个回合执行器（组装上下文、运行、在一个事务里提交消息、计数、压缩），提交时补记一条已完成的 turn task，所以「每一轮都是一个 task」在两条路径上都成立。私有 thread 不进队列（用户不能插话），同一 thread 的并行 ask 由进程内的锁串行。同一响应里对不同 thread 的多次 ask 并行执行；嵌套 ask（子 agent 再 ask）在同一调用链里依次完成。
- **用最终正确性换简化**：内联运行不做持久化挂起。换掉的是「等待期间不占 worker、进程重启后从等待处继续」；换来的是没有等待表、挂起状态、恢复与回滚逻辑，队列里的 turn 与 ask_agent 共用同一个回合执行器，临时 agent 也走同一个运行入口（`AgentRuntime._execute`）。三条判据：失败可见——子 agent 的一轮失败直接作为调用方这一轮的失败（暂时性失败整轮重试，其余进入 failed 可人工重试）；可收敛——一轮是原子的，失败或崩溃时子 thread 不留半截记录，调用方这一轮按队列语义重放即可；代价可控——等待期间占用调用方的 worker 槽位和任务总时限，嵌套越深越耗时；调用方这一轮跨多个子 thread 时并不原子：已完成并提交的子轮次（同一响应里先答完的那个 ask、嵌套里先完成的孙一层）不会随调用方失败而撤销，重放时子 thread 会多一轮同样的问答（子 agent 能看到上一次的回答），子 agent 的工具副作用也会重复，反思计数会多计。个人助手可接受。
- **删除**：删除父 session 会一并删除它的私有 thread（逐层）；用户可见的 thread 保留（`parent_session_id` 置空）。有任务正在运行时拒绝删除。私有 thread 的内联轮次不留 running 记录，所以判断它是否在跑看承载它的、走队列的祖先会话：祖先在跑任何一轮就拒绝删除这个私有 thread（单独删除，或删除它的 agent 时）。偏保守，但不需要额外状态。

## 5. 上下文压缩

与模型提供方无关的统一机制（`runtime/compaction.py`）。pydantic-ai 只为 Anthropic 与 OpenAI Responses 提供原生压缩，`openai` 兼容接口和 Gemini 没有，按提供方分支会产生两套语义，所以统一自己实现：

- **触发**：一轮结束时，最后一次模型请求的输入 token（pydantic-ai 已计入缓存 token）超过 `runtime.compaction_threshold_tokens`，在提交该轮的同一事务里投递 `compaction` task；也可在会话页手动触发。它与 turn 同一 lane，同一 session 最多一个待执行的压缩。排在已经排队的用户消息之后，所以那些轮次仍带完整上下文运行。模型不返回 usage（部分 OpenAI 兼容服务流式时如此）时记 warn 日志，自动触发对该模型失效。
- **执行**：把最近 `runtime.compaction_keep_turns` 轮之前的消息连同上一份摘要交给该 agent 的模型总结，写入 `compactions` 表（`through_message_id` + `summary`）。切点总在两轮之间，工具调用与其返回不会被拆开。单次只取总长不超过上限的最早若干轮，不截断内容；剩余部分在同一事务里续排下一次压缩，直到收敛。没有可压缩的轮次时什么也不做。
- **使用**：发给模型的上下文 = 最新摘要（放进 instructions）+ 切点之后的原始消息。原始消息不删除，UI 显示完整原文并标出压缩点；反思读取的仍是原始消息。

## 6. 数据库迁移

Alembic 管理表结构（`src/muliao/migrations/`，随包分发），启动时自动升级到最新版本。整个升级在一个真正的事务里：pysqlite 默认只在 DML 前隐式开事务、DDL 逐条自动提交，所以迁移使用独立连接，驱动设为 autocommit 并显式 `BEGIN IMMEDIATE`，任何失败都完整回滚。SQLite 改表用 batch 模式（建新表-拷数据-删旧表），迁移连接在建立时关闭外键以免删旧表时级联删除子表数据，提交前执行 `foreign_key_check`，有违例则回滚。`tests/test_migrations.py` 保证迁移后的结构与模型定义一致。

首次部署之前不积累迁移历史：所有结构变化都直接改唯一的初始版本（重新 autogenerate）。首次部署之后，每次结构变化新增一个迁移，已部署的数据只通过迁移升级，不删库。

## 7. 进化机制

### Skill
agent 在任何一轮都可以主动 `write_skill`。公共 skill 对所有 agent 可见；私有 skill 只对自己可见，同名时私有遮蔽公共。

**归属与合并规则（skill、MCP server、secret 共用，只在 `ownership.visible` 实现一次）**：资源归属某个 agent（私有）或公共；agent 看到自己的私有资源加上公共资源，同名时私有遮蔽公共，其他 agent 的私有资源不可见。prompt 不是按名称合并，而是公共 prompt 与 agent 指令拼接（见下）。「私有」是可见范围和命名空间的划分，不是安全隔离：agent 能执行任意命令、读到数据库与 vault 主密钥（见第 9 节）。

### Prompt（公共 prompt 与 agent 指令）
agent 的指令是资源仓库里的 `agents/<id>/PROMPT.md`，公共 prompt 是 `public/PROMPT.md`（合并进所有 agent，放在各自的指令之前）。当前生效的就是文件内容（`agents` 表里没有指令列），读写都经过 `PromptStore`；修改即提交一版并记录作者（`user` 或做出修改的 agent），回滚即把旧版内容作为新的一版写入，历史只增不改。内容未变时不提交；空的公共 prompt 等同于没有（清空也提交一版空文件，历史里不会出现「文件不存在」的版本）。指令与记忆的历史和恢复通过 `/api/resources/{prompt|memory}/history|restore`，界面在公共指令页和 agent 的配置、记忆标签里；接口只列最近 50 版，更早的仍在仓库里。

### Memory
agent 私有、有长度上限（`memory_max_chars`），每轮全文注入 instructions。

### 强制反思（reflection）
反思可以更新记忆、私有 skill（有 `public_skills` 时也可写公共 skill），以及自己的指令（`instructions_replace`，只做小而准的修改）；这三者都是资源仓库里的文件，每次修改都有版本，用户可回滚。
- 计数在 **agent** 上：每完成一轮 turn，`agents.turns_since_reflection += 1`。达到该 agent 的 `reflect_every` 时，在同一事务里清零计数、把检查点推进到当前最大消息 id，并投递一个 reflection task，载荷是消息 id 区间 `(from_id, to_id]`。
- 选择 agent 级而不是 session 级：定时触发每次都是一个新 session，只有一轮，按 session 计数永远到不了阈值。
- ReflectionHandler 把这个区间内该 agent 所有 turn 消息渲染成文字记录，连同固定的反思提示交给同一个 agent 运行，只开放自我改进的工具（记忆、私有 skill、自己的指令，加上该 agent 已有的写公共 skill）。没有需要修改的内容也要走完并给出结论。
- 反思的完整消息和结论写入 `reflections` 表，可在 UI 查看；不进入任何 session 的历史。

## 8. 触发源

触发点有两类：用户在网页发起对话，以及触发器。agent 之间的委派是 agent 内部能力，在父会话下开 thread（第 4 节）。

**统一入口**：用户消息、触发器、agent 委派都是同一件事——把一段内容交给某个 agent，在某个会话里处理一轮。各来源只做适配（鉴权、触发时机、组装内容），然后构造一个 `Inbound` 交给 `conversation.receive`：

| 字段 | 含义 |
|---|---|
| `origin` / `origin_ref` | 来源与具体发送方：用户（无）、触发器 id、委派方（父）会话 id；与会话上的同名字段是同一个概念（会话记的是发起它的那一个事件的来源） |
| `content` | 这一轮的 prompt |
| `session` | 已有会话的 id（在其中继续），或 `NewSession`（新建：agent、标题，委派时还有父会话、thread 名、是否私有） |
| `reply_to` | 这一轮的回答交给谁 |

`receive` 是唯一投递新一轮的地方；新建会话也只有 `create_session` 一处实现。**续写规则**：用户可以在任何非私有会话里继续；其他来源只能续写自己发起的会话（会话的来源与发送方都一致，委派会话的发送方就是它的父会话），否则 403。发送方身份是触发器 id（AUTOINCREMENT，删除后不复用）或父会话 id。

是否推送由每一轮的 `reply_to` 决定（唯一来源），由发起这一轮的入口设置：

| 入口 | session 策略 | `reply_to` |
|---|---|---|
| 用户发起 / 回复 | 新建；回复沿用（私有 thread 只读） | `user`（iOS 要求每个 push 都展示通知，所以页面可见时也会弹） |
| 触发器 | 默认每次新建，`origin_ref` 记触发器；带回会话 id 的事件在原会话里续写（只能续写本触发器发起的会话） | `none`，需要通知时 agent 调 `notify_user` |
| `delegate_task` | 父会话下的公开 thread，同名续聊 | `user` |
| `ask_agent` | 父会话下的私有 thread，同名续聊（内联执行，不走 `receive`） | `agent` |

**触发器**：具名、持久的事件来源（`triggers` 表）。公共字段只有名称、agent、处理说明、启用与最近一次触发的记录；怎么被触发由 `kind` 决定，**只在代码里多态实现**，表里不为某种方式开专门的列：`config`（JSON）是这种方式的配置，`state`（JSON）是这种方式自己维护的运行状态。每种方式分两部分：

- `TriggerKind`（纯逻辑，注册给 `TriggerService`）：校验并规范化 `config`、由配置推导 `state`、给出展示用的运行状态（不含凭证）。推导在创建、修改配置与停用后重新启用时进行，由 `TriggerService` 合并进已有 `state`（实现不负责保留与配置无关的部分，如 token 摘要）；
- 激活机制：怎么被触发，都只调用 `TriggerService.fire_in`（唯一出口）组装成事件交给统一入口；需要感知触发器变化的激活机制各自 `subscribe()` 一个通知，互不干扰；某种方式专有的接口放在它自己的 API 模块（如 `api/webhook.py`）。

新增一种方式（例如消息队列）= 一个 `TriggerKind` + 它的激活机制（消费循环），在组合根注册并启动；表、工具、统一入口与界面的通用部分都不用改（界面对未知方式用 JSON 编辑配置）。现有两种：

| kind | config | state | 激活机制 |
|---|---|---|---|
| `schedule` | `{"cron": "<5 段表达式>"}`，按配置的时区解释 | `next_run_at` | `ScheduleRunner` 循环：到点触发；上一次触发的会话仍有未完成的一轮时跳过本次；停机期间错过的只补一次，暂停期间错过的不补（重新启用时从当时算起）；触发前按最新的行重新判断（列出之后被删除、停用或改了配置就跳过）；触发被拒绝时停用并记 `last_error` |
| `webhook` | `{}` | `token_hash` | `Webhooks`：`POST /api/hooks/{name}[?session_id=]` 凭 token 鉴权 |

任何触发器都可以在界面上或用 `trigger_run` 立即运行一次（不带事件内容）。事件内容（若有）原样附在处理说明之后：

```
<处理说明>

<event>
<请求体>
</event>
```

**webhook**：
- 带 `Authorization: Bearer <token>`，响应 `{"session_id": ...}`；请求体为 UTF-8 文本，最多 256 KiB。
- 不走登录 cookie，**先鉴权再读请求体**：未鉴权的调用方不能让进程读入任何内容；请求体按字节上限分块读取，超限返回 413。触发器不存在、不是 webhook、还没有 token、token 不匹配，都返回同一个 401，不暴露哪些名称存在；停用的返回 409；续写不是本触发器发起的会话返回 403。
- token 是我们签发给外部系统的凭证，事实来源是触发器自己：`state` 里只存 SHA-256，明文只在生成时返回一次，**只能由用户在界面上生成**（agent 创建的 webhook 在用户生成 token 之前收不到事件）。不放进 vault——vault 存的是外部系统发给我们的凭证。
- 不校验各平台自己的签名格式（GitHub、Slack 等各不相同）。需要时由发送方把 token 放进 Authorization 头，或在中间加一层转发。

通知只有 Web Push 一种渠道。不支持主动推送的外部消息源（例如只能拉取的 Slack 未读）用定时触发器拉取：例如每分钟让 agent 通过 Slack MCP（或 skill 中的脚本 + vault token）读取未读消息，游标由 agent 存在自己的工作目录。

## 9. 安全边界

**威胁模型：agent 与运行服务的系统用户同等可信。** agent 能执行任意 shell 命令，因此能读到服务能读到的一切——包括配置文件里的 `vault.key` 与 LLM API key、数据库文件。下面的措施只防止 secret **意外**进入对话记录、日志和前端，不防 agent 本身：

能缩小的是**提示注入的影响面**：处理外部事件或网页内容的 agent 用 `tool_groups` 只开放它需要的工具组（例如只开 `notify`），这样即使输入里带有恶意指令，它也删不了 agent、改不了公共指令和公共 skill（两者都会进入所有 agent 的 instructions）、用不了 MCP、动不了 vault 和触发器。下面这些组**任何一个都等同于全部权限**，只能给可信的 agent：`workspace`（shell）、`temp_agent`（临时 agent 自带 shell）、`delegation`（借目标 agent 自己的完整工具组之手）、`agents`（可以给自己加组）、`triggers`（可以让任意 agent 带着任意说明定时运行）、`mcp_admin`（可以添加运行任意命令的 stdio MCP）。`public_skills`、`mcp`、`vault` 按各自的影响判断。

注入还可能**持久化**：记忆、私有 skill 与自己的指令总是开放（不受 `tool_groups` 限制），处理不可信输入的 agent、以及读取含不可信输入的转录的反思，可能把注入的内容写进它们，之后该 agent 的每一轮都会带着它。影响限于这个 agent 自己（工具组不变，不能越权），但指令在 system prompt 里的权威性比记忆高。缓解手段是版本历史与作者记录：界面上能看到每一版是谁改的，随时回滚。

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
- **外部消息**：能主动推送的外部系统接入 webhook 触发器；只能拉取的由定时触发器拉取（见第 8 节）。
- **上下文压缩的覆盖范围**：只在两轮之间压缩。单轮内工具调用极多导致上下文超限时，该轮失败可见；在会话页点「压缩」后重试该轮即可。不自动识别各提供方的超限错误，因为错误格式各不相同，无法可靠识别。
