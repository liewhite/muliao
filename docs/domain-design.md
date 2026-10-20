# 设计：领域边界与跨领域一致性（2026-10-20，草案，待审）

状态：方案讨论稿。审定后按第 10 节分阶段实施，完成后并入 architecture.md 并删除本文。技术栈不变（Python、FastAPI、pydantic-ai、SQLite）。生产库在上线时重建，表结构调整不写迁移。

## 0. 要解决的问题

上一轮重构修掉了几个方案级错误（`ask_agent` 改为挂起调用、步级压缩、反思按步数），但领域之间的集成方式没有变：**一个跨领域的大事务**。一次运行结束时，记 task 完成、结清委派、给发起方投信、反思调度、推送、唤醒下一次运行都在同一个事务里，于是每个领域都要在别人的事务里直接改自己的表、读别人的状态。现状（restructure 分支）：

- 73 个函数接收数据库会话 `s`，以便被并进别人的事务。
- 同一张表被多个领域直接查询：`Message`（消费者、停止、历史、反思、会话服务），`Mail`（消费者、委派、信箱），`Delegation`（会话删除、挂起调用、委派），`Task`（压缩、信箱、队列）。
- `runtime/consumer.py` 导入 16 个 runtime 模块；`finish.py` 判断「委派做完没有」要看后台任务和信箱；`conversation.delete_sessions` 直接查 `Job` 与 `Delegation` 表；`pending.py` 读 `Delegation` 表判断 `ask_agent` 是否挂着。
- 评审每发现一个一致性漏洞，就在收尾或停止里再加一个条件（`_work_done`、`handed_to`、`settle_in` 的四个调用点），领域彼此知道得越来越多。

根因是在领域之间坚持强一致，而工作契约第 0 条推荐的是相反的交换：用可自动恢复的最终正确性换架构简化。

## 1. 原则

1. **领域独占自己的表。** 别的领域不 import 它的表、不拿它的 ORM 对象、不在它的事务里写它的数据。领域之间的引用只是不透明的 id，没有跨领域外键。
2. **领域之间只有两种通信：命令与事件。**
   - **命令**：一个领域请另一个领域做一件事（「往这个会话投一封信」）。是对方公开接口上的一次方法调用，在**对方自己的事务**里执行，带幂等键，可安全重放。
   - **事件**：一个领域宣布自己发生了什么（「这次运行结束了」）。与状态变化在同一个本地事务里持久化，订阅方至少收到一次，处理幂等。
   - **查询**：只读，经对方的公开接口，返回对方定义的数据结构（不是 ORM 对象）。
3. **发起用命令，反应用事件。** 一个流程由发起方用一串幂等命令推进（例如 `ask_agent` 打开委派、开 thread、投信）；别的领域对状态变化的反应用事件（例如运行结束后推送、反思、交回委派结局）。不为发起方的顺序步骤造事件。
4. **强一致只在一个聚合内部。** 必须当场一致的状态放进同一个聚合，由一个领域在一个本地事务里修改。真正需要跨聚合原子的不变量应当极少；出现了先检查聚合边界是否划错。
5. **不用 2PC。** 所有领域在同一个 SQLite 文件里，聚合内的本地事务本来就原子；跨资源的地方（git 资源仓库、文件存储、进程、Web Push、模型接口）不支持准备阶段，2PC 也用不上，那里保持「失败可见 + 对账」。

## 2. 事务分级

| 级别 | 范围 | 机制 | 例子 |
|---|---|---|---|
| L1 强一致 | 一个聚合内 | 一个本地事务 | 保存一步并把它带上的信件记为已处理；投信并判断是否唤醒；停止会话时关闭历史、丢弃信件、清掉占用 |
| L2 最终一致 | 领域之间 | 事件（与状态同事务持久化，至少一次投递，处理幂等）+ 带幂等键的命令 | 运行结束 → 推送、反思调度；会话结清 → 委派交回结局；会话停止 → 结束后台任务、放弃委派 |
| L3 对账 | 数据库与外部资源之间 | 失败可见 + 启动或定期对账（现有机制不变） | 资源仓库的 reconcile、文件存储的 recover、临时目录清理、重启时被中断的后台任务 |

每处 L2 的三条判据见第 6 节。

## 3. 平台层（`muliao/platform/`）

平台层不含业务规则，所有领域都可以依赖它。

### 3.1 数据库

单写者 SQLite，`Database.transaction()` 在进程内串行化事务（不变）。**规则变化**：一个事务只写一个领域的表，外加平台的任务表（事件投递与运行调度）。由测试辅助检查：事务内写入的表都属于同一个领域（开发期断言，见第 9 节）。

### 3.2 任务队列即 actor 调度

现有的持久化任务队列（`tasks` 表、lane、重试、启动恢复、失败可见）不变，它就是 actor 调度器：同一 lane 内严格 FIFO、同一时刻一个执行者。会话的 lane `session:<id>` 就是会话 actor 的邮箱：运行、压缩、删除都作为这条 lane 上的任务串行执行。

### 3.3 事件：以投递任务作为 outbox

不新增事件基础设施。**发布一个事件 = 在发布方的本地事务里，为每个订阅者插入一条投递任务**（`TaskKind.EVENT`，payload 是事件类型、事件 id 与数据）：

- 状态变化与「一定会通知到订阅者」在同一个事务里，是原子的（transactional outbox）。
- 订阅者在组合根静态注册（事件类型 → 处理器、lane）。默认每个订阅者一条 lane（如 `sub:delegations`），保证它看到的事件按发布顺序；需要并行时按分区键分 lane（如 `sub:jobs:<session>`）。
- 处理器返回 `Commit`：它对**自己领域**的写入与「这条投递已完成」在同一个事务里提交（现有 worker 机制），所以对自己领域是恰好一次的效果。
- 处理器对**别的领域**的影响只能经命令，命令在对方事务里执行、带幂等键；处理器在提交前崩溃会被重放，命令按键去重。
- 契约内的失败（命令目标暂时忙、模型接口超时……）按队列规则重试；最终失败记为 failed，「任务」页可见可重试。契约违约（事件数据不合法、引用的东西不存在且不该不存在）直接失败，不重试。
- 可观测：每条投递任务记事件类型、事件 id、关联 id（会话、运行、委派）；处理开始、结果、重放去重都打结构化日志。

### 3.4 实时展示总线

`bus.LiveRuns` 与 SSE（`muliao/events.py`）是展示加速，不是领域事件，不持久化、不保证送达，保持现状。命名上区分：领域事件叫 domain event，SSE 推送叫 server event。

### 3.5 幂等键

所有会新建东西的命令接受 `key`，由调用方从稳定的来源派生（工具调用 id、事件 id、委派 id），在目标领域用唯一约束去重：

- `Sessions.post` → `mail.dedupe_key`（同一会话内唯一）；
- `Delegations.open` → `(owner_session_id, key)` 唯一；
- `Notifications.notify` → `notifications.dedupe_key`。

改状态的命令天然幂等（释放占用、停止、「仍在进行才结清」）。

## 4. 领域

每个领域一个包，`<domain>/api.py` 是唯一对外的模块（命令、查询、事件类型、数据结构），其余模块对外不可见（第 9 节的依赖规则检查）。

### 4.1 会话（`muliao/sessions/`）——聚合：会话

**职责**：会话、thread 树、信箱、历史、挂起的调用、运行（消费者与逐步保存）、压缩、停止、删除、会话种类的规则。会话 actor 是它的一致性边界。

**拥有的表**：`sessions`、`messages`、`mail`、`compactions`、`session_holds`（新）。

**新概念：占用（hold）**。别的领域为某个会话开始一项异步工作时，在会话上登记一个占用（不透明的 `holder`，如 `job:12`、`delegation:7`），工作结束时释放。会话只知道「有人替我在外面做事」，不知道是什么。它替代现在的 `SessionWork.active_in` 和 `finish._work_done` 对后台任务、委派的直接查询。占用可以带一个 `call_id`：表示这次占用会作为某个挂起调用的回答交回（`ask_agent`），它替代 `pending.delegated_in` 对 `Delegation` 表的查询。

**命令**：

| 命令 | 说明 |
|---|---|
| `receive(Inbound)` | 外部来信的唯一入口（用户、触发器），现有规则不变 |
| `post(session, mail, key, *, coalesce=False, release=None)` | 代码里的生产者投信（事件、检查）；`coalesce` 为真时同一来源已有信件排着就不投（检查信件的不堆积规则只在这里）；`release` 在同一事务里释放一个占用 |
| `open_thread(parent, agent, name, kind, key)` | 打开或找到 thread（同名续聊，种类不符报错），返回子会话 id |
| `send_to_thread(child, content, correlation, awaited, key)` | 往 thread 投一封带关联 id 的信件 |
| `hold(session, holder, call_id=None)` / `release(session, holder)` | 登记、释放占用；释放后重新判断会话是否结清（见下） |
| `reply(session, holder, result, key)` | 交回一项占用的结局：会话还停在 `holder` 对应的挂起调用上，就作为那次调用的结果（回答信件）；否则作为事件信件。同一事务里释放占用。「迟到的回答改为事件」这条规则只在这里 |
| `answer(session, answers)` | 用户回答 `ask_user`（现有校验规则不变） |
| `stop(session)` | 停止：结束正在进行的运行、关闭历史、丢弃信件、清掉全部占用、结清所有关联 |
| `delete(session)` | 先停止，再在会话 lane 上排一条删除任务，由 actor 删除自己及私有 thread |

**查询**：`detail(session)`（会话页读模型）、`find_thread(parent, name)`、`threads_of(parent)`、`is_busy(session)`（触发器跳过用）、`transcript(agent, after_message_id, through_message_id)` 与 `responses_since(agent, after_message_id)`（反思用）、`latest_answer(session)`。

**关联（correlation）与结清**：信件可以带一个不透明的关联 id（委派来的信件是 `delegation:<id>`）。会话负责判断这些关联什么时候「结束」，并发布 `SessionSettled`：

- **完成**：一次运行正常结束、没有停在挂起的调用上、信箱里没有排队的信件、没有占用 → 已处理但还没结清的关联以 `finished` 结清，带上回答与这段工作挂给用户的文件。有占用时等最后一个占用释放后再判断（`release` 触发）。
- **最终失败**：运行最终失败时，它处理过的、以及交给过它还排着的（现有 `handed_to` 规则）关联以 `failed` 结清，后者从信箱丢弃。
- **停止 / 删除**：所有未结清的关联以 `stopped` / `deleted` 结清。

信件上记录关联是否已结清（`mail.correlation_settled`），每个关联只结清一次。这条规则完全在会话内部，不读任何别的领域的表。

**会话种类**（第 7 节）：`private` 布尔改为种类枚举，相关规则收进一个策略对象。

**发布的事件**：

| 事件 | 时机 | 主要数据 |
|---|---|---|
| `RunEnded` | 运行结束（完成、挂起、停止） | 会话、agent、运行 id、结局、回答、是否问了用户、处理过的信件里是否有用户在等、本次保存的模型回复数 |
| `SessionSettled` | 关联结束（见上） | 会话、结局、回答、文件、关联 id 列表 |
| `SessionStopped` | 停止后 | 会话 |
| `SessionDeleted` | 删除后 | 会话、agent |

### 4.2 委派（`muliao/delegations/`）——流程管理者

**职责**：要交回结局的委派（`ask_agent`、`delegate_task(report_back)`）的生命周期：打开、跟踪、交回一次、放弃；运行中检查；委派工具组（`delegate_task`、`ask_agent`、`list_threads`、`stop_thread`、`list_agents`）。

**拥有的表**：`delegations`（无外键；`owner_session_id`、`child_session_id` 是不透明 id）。

**发起（工具里，一串幂等命令，键从工具调用 id 派生）**：

1. `Delegations.open(owner, call_id?, awaited, key)`：本地事务插入委派行（running）。
2. `Sessions.hold(owner, "delegation:<id>", call_id)`：发起方被占用；`ask_agent` 的占用带 `call_id`，这次调用因此算作挂起。
3. `Sessions.open_thread(...)` + `Sessions.send_to_thread(child, ..., correlation="delegation:<id>")`；把子会话 id 记到委派行上。
4. `ask_agent` 抛出 `CallDeferred`；`delegate_task` 返回。

续跑重新执行这次调用时，每一步按键去重。普通 `delegate_task`（不交回）只做第 3 步，不建委派行。

**订阅**：

| 事件 | 处理 |
|---|---|
| `SessionSettled`（子会话） | 对其中每个 `delegation:<id>`：委派还在 running 就记为 settled，并 `Sessions.reply(owner, "delegation:<id>", 结局, key)`；已 settled 或 abandoned 的忽略 |
| `SessionStopped`（发起方） | 它发起、还在 running 的委派记为 abandoned，并对每个子会话 `Sessions.stop(child)`（逐层由子会话自己的 `SessionStopped` 继续） |
| `SessionDeleted`（发起方） | 删除它发起的委派行（子会话的 `SessionSettled(deleted)` 交回时发起方已不存在，`reply` 报告目标不存在，记日志后记为 settled） |

**运行中检查**：定期给 running 且发起方没在等这次调用的委派，`Sessions.post(owner, 检查信件, coalesce=True)`。

### 4.3 后台任务（`muliao/jobs/`）

**职责**：后台 shell 进程的生命周期、日志、运行中检查、重启收敛；工具 `run_shell(background)`、`job_status`、`job_kill`。

**拥有的表**：`jobs`。

**发起**：`Sessions.hold(session, "job:<id>")` → 本地事务插入 job → 启动进程（失败则记为结束并释放）。

**结束**：记下状态与退出码，同一事务发布 `JobEnded`；自己的订阅者 `Sessions.post(session, 完成事件信件, key="job:<id>:end", release="job:<id>")`。

**订阅**：`SessionStopped` → 结束该会话的后台任务，不再投信（会话停止时已清掉占用）；`SessionDeleted` → 删除该会话的 job 行。

### 4.4 Agent 目录与资源（`muliao/agents/`）

**职责**：agent 的注册（名称、模型、工具组、反思间隔）、资源仓库（指令、记忆、skill、MCP 配置，git 版本管理）、自我改进工具、管理 agent 的工具。

**拥有的表与资源**：`agents`；`data/resources/`。

**删除 agent 是一个 saga**：

1. `Agents.delete(agent)`：本地事务把 agent 标为 deleting（不再出现在列表与名称解析里，不能被委派、不能新建会话或触发器），发布 `AgentDeleting`。
2. 订阅者各自清理：会话领域删除它的全部会话（每个会话照常停止、发布自己的事件）；触发器删除它的触发器；vault 删除它的私有 secret；反思删除它的检查点与记录。
3. agents 自己订阅 `SessionDeleted`：发现 deleting 的 agent 已经没有会话（`Sessions` 查询），就删除行与资源目录。

### 4.5 Vault（`muliao/vault/`）、文件（`muliao/files/`）

现状基本已是独立领域：`secrets` 表、`files` 表与内容存储。只需把对外接口收进 `api.py`，删掉与 agent 表之间的外键（`AgentDeleting` 时由 vault 自己删除私有 secret）。

### 4.6 反思（`muliao/reflection/`）

**拥有的表**：`reflections`、`reflection_checkpoints`（新，从 `agents.reflection_checkpoint` 移过来）。

**订阅** `RunEnded`：用 `Sessions.responses_since(agent, checkpoint)` 数步数（事实仍只在 messages 表里），达到 `reflect_every`（`Agents` 查询）就推进检查点并投递 reflection 任务；执行时用 `Sessions.transcript(...)` 取记录。

### 4.7 通知（`muliao/notifications/`）

**拥有的表**：`notifications`、`push_subscriptions`；Web Push 投递。

**命令**：`notify(...)`（`notify_user`、前置检查告警、推送测试）。

**订阅**：`RunEnded`（处理过的信件有用户在等，且做完了或问了用户 → 一条通知 + 投递，键为运行 id）；`SessionDeleted` → 删除指向它的通知。

### 4.8 触发器（`muliao/triggers/`）

**拥有的表**：`triggers`。触发 = `Sessions.receive(Inbound)`；「上一次触发的会话还在运行就跳过」用 `Sessions.is_busy` 查询。订阅 `AgentDeleting` 删除该 agent 的触发器。

### 4.9 agent 运行时（`muliao/agent_runtime/`）

不是业务领域，是会话与反思共用的端口：模型注册表、指令组装（读 `Agents`、`Vault` 的接口）、工具组合（`tool_catalog` + 各领域提供的工具集）、pydantic-ai 的逐步驱动适配（现在的 `runner.py`）。工具一律通过领域接口做事，不碰表。

## 5. 关键流程

### 5.1 用户消息 → 运行 → 收尾

1. `Sessions.receive`：L1 事务投信，判断唤醒，排运行任务。
2. 运行在会话 lane 上逐步执行、逐步保存（L1，现状不变）。
3. 运行结束的事务里：记 task 完成、更新会话的已读与时间、判断结清（可能发布 `SessionSettled`）、发布 `RunEnded`、判断是否唤醒下一次运行。**全部是会话自己的数据**。
4. 事件投递：通知领域推送；反思领域数步数。

### 5.2 `ask_agent`

1. 发起方的工具按 4.2 的四步命令打开委派、占用发起方、开 thread 投信、挂起。
2. 子会话正常运行；结清条件满足时发布 `SessionSettled(finished)`。
3. 委派领域收到后 `Sessions.reply(owner, ...)`：发起方还停在这次调用上 → 回答信件，唤醒它续跑；用户已经跳过 → 事件信件。
4. 用户在等发起方时，回答信件随它的 `awaited` 推送。

### 5.3 停止会话

`Sessions.stop` 在一个 L1 事务里完成会话自己的全部状态迁移，然后发布 `SessionStopped` 与 `SessionSettled(stopped)`。后台任务领域结束进程；委派领域放弃发起方的委派并逐层停止子会话，同时把子会话的「被停止」交回给发起方。

### 5.4 删除会话

`Sessions.delete` = `stop` + 在会话 lane 上排一条删除任务。删除任务在 actor 里执行（正在进行的运行停下之后），删除自己与私有 thread（同一领域内），发布 `SessionDeleted`。其他领域各自清理引用它的数据。

### 5.5 运行最终失败

队列记失败的事务里，会话按 4.1 的规则结清关联（`failed`），丢弃交给过它还排着的委派信件，发布 `SessionSettled(failed)`。

## 6. 最终一致的三条判据

| 跨领域效果 | 失败可见 | 可收敛 | 代价 |
|---|---|---|---|
| 委派结局交回发起方 | 投递任务失败可见、可重试 | 处理幂等（委派状态机 + `reply` 按键去重），重放安全 | 交回晚几百毫秒 |
| 推送 | 同上；Web Push 失败语义不变 | 通知按运行 id 去重 | 推送晚几百毫秒 |
| 反思调度 | 同上 | 步数从 messages 推导，检查点推进幂等 | 无 |
| 停止后结束后台任务、放弃委派 | 同上 | 结束进程与放弃委派都幂等；会话已清掉占用，后到的结局按事件进信箱或被丢弃 | 停止后进程可能多跑几百毫秒 |
| 删除后的清理 | 同上；启动时对账扫描引用已不存在会话的行，记 warning 并删除 | 删除幂等 | 短暂的孤儿行（不可见） |
| 删除 agent | saga 状态可见（deleting） | 每步幂等；最后一个会话删除后完成 | agent 从列表消失与目录删除之间有窗口 |

## 7. 会话种类与临时 agent

### 7.1 会话种类

`sessions.private` 改为 `kind`（`conversation` 顶层会话、`thread` 公开 thread、`private_thread`），一个策略对象一处定义：用户能否写、是否进收件箱、是否标未读、能否单独删除、回答交给谁（`Audience`）、续聊用哪个工具。现在散在 6 处的 `if session.private` 改为读策略。

### 7.2 临时 agent（待决）

现状：`run_temp_agent` 在调用方那一步里同步运行，失败时整步重做、副作用重放，有请求数上限。

方案：新增种类 `temporary_thread`，`run_temp_agent` 走与 `ask_agent` 相同的委派流程（挂起调用 + 占用 + 结清交回），会话带「指令覆盖」与「固定工具组合」，用完（结清后）删除，工作目录随之删除。收益：运行只剩一种模型，失败只重做自己，不需要请求数上限。代价：调用方那次运行会先结束、等结果再续跑；临时 agent 的过程变成一个只读 thread，可以看到（也可以不在界面上列出）；约 1 天工作量。

### 7.3 回答信件的判别

`mail.answers` 的值存成带工具名的信封（`{"tool": "ask_user" | "ask_agent", "result": ...}`），接口与前端按判别字段区分，删掉按形状区分。

## 8. agent loop 的归属（评估）

pydantic-ai 现在同时提供两样东西：各家模型的客户端，和 agent loop（逐步驱动、挂起调用、续跑）。现有的别扭代码都来自适配它的 loop：运行开始时规整历史（要另取保存基准）、续跑时重发挂起调用的事件、不允许在未处理的调用后追加消息（要显式补「没回答」）。

建议：本次不换。把 pydantic-ai 的 loop 收在 `agent_runtime` 的一个端口后面（「从这段历史推进一步，告诉我发生了什么」），会话领域只依赖这个端口与消息格式。以后如果自己写 loop（框架只做模型客户端），改动限于这个端口的实现。

## 9. 模块与依赖规则

```
muliao/
  platform/        数据库、任务队列（actor 调度）、事件发布与订阅、实时展示总线、时钟、id
  agent_runtime/   模型注册表、指令组装、工具组合、loop 适配
  sessions/        会话聚合（信箱、历史、运行、挂起调用、压缩、停止、删除、种类）
  delegations/     委派流程管理者与委派工具
  jobs/            后台任务
  agents/          agent 目录、资源仓库、自我改进工具
  vault/  files/  reflection/  notifications/  triggers/
  api/             HTTP 适配（每个领域一个路由模块，只调领域的 api.py）
  app.py           组合根：构造领域、注册事件订阅、组装工具组
```

依赖规则用 import-linter 写成检查，`uv run lint-imports` 进测试流程：

- 领域包之间只能 import 对方的 `api` 模块；
- 任何领域不得 import 别的领域的表模块；
- `platform` 不 import 任何领域；
- 领域不 import `api/`（HTTP）与 `app.py`。

表定义放在各领域包内（`<domain>/tables.py`），Alembic 汇总全部 metadata。跨领域外键全部删除；领域内的外键保留。开发期另加一个断言：一个事务里写入的表属于同一个领域（平台任务表除外），违反就报错。

## 10. 实施阶段

每个阶段独立提交、审查，测试全过；全部完成后重建生产库上线。

0. **平台**：事件即投递任务（发布、订阅注册、处理器提交语义）；命令幂等键（`mail.dedupe_key` 等）；import-linter 接入，现有违规记成基线。
1. **会话占用与结清**：`session_holds`、关联与 `SessionSettled`、`reply`；委派改为流程管理者订阅事件；删掉 `finish` 对后台任务与委派的依赖、`pending` 对 `Delegation` 表的依赖。
2. **停止、删除、失败经事件**：`SessionStopped` / `SessionDeleted` 的订阅者（后台任务、委派、通知）；删除会话改为停止加 actor 删除；agent 删除 saga；删除跨领域外键与启动对账。
3. **其余订阅者**：推送、反思改为订阅 `RunEnded`；`reflection_checkpoints` 移出 `agents`。
4. **按领域分包**：每个领域一个包与 `api.py`，表随领域；import-linter 去掉基线，全部规则生效；事务单领域断言生效。
5. **会话种类策略、回答信封**；临时 agent（若审定采纳 7.2）。
6. **文档并入 architecture.md、上线。**

测试：领域测试只经对方的 `api.py`（必要时用替身）；跨领域流程测试在 `drain` 之后断言（`drain` 同时处理运行与事件投递）。

## 11. 行为变化

- 删除会话：从「有运行或后台任务在跑就拒绝（409）」改为「先停止（结束运行与后台任务、放弃委派），再删除」。
- 删除 agent：从「有任务在跑就拒绝」改为「停止并删除它的全部会话后删除」，异步完成，期间 agent 在列表中不可见。
- 委派结局、推送、反思调度从运行结束的同一事务改为随后的事件投递，晚几百毫秒。
- 跨领域外键删除，引用清理由事件完成，启动时对账兜底。

## 12. 待决问题

1. 第 11 节前两条：删除会话或 agent 时直接停止正在进行的工作，是否可以接受？（否则保留拒绝删除，由会话领域查询占用与运行来判断。）
2. 第 7.2 节：临时 agent 是否改为临时 thread？
3. 事件订阅默认每个订阅者一条 lane（顺序简单、吞吐足够个人使用），是否接受？
