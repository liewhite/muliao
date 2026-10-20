# 设计：领域边界与跨领域一致性（2026-10-20，第 5 版，已审定，实施中）

状态：用户已审定，按第 11 节分阶段实施，完成后并入 architecture.md 并删除本文。技术栈不变（Python、FastAPI、pydantic-ai、SQLite），并发改用 anyio（第 9 节）。生产库在上线时重建，表结构调整不写迁移。

第 5 版记下用户的决定：每个订阅者一条 lane；平台增加定向命令 `send`，后台任务结束改用它。

第 4 版根据第三轮审查修订：会话删除后委派行与 job 行保留终态、不删除（「先停止再删除」是正常路径，迟到事件不能撞上被删的行）；`post` 增加 `Gone`；后台任务启动失败与崩溃对账共用一条规则；holder 写法统一；失败投递的恢复顺序；运行开始时检查是否已被停止。

第 3 版根据第二轮审查修订：委派行只由委派领域的事件处理器创建（`dispatch` 同一事务发布 `DelegationDispatched`），删掉「打开中」状态与按时间的对账；停止事务在运行退出之后执行；后台任务启动失败走正常的结束路径；并发模型写明异常边界与屏蔽点。用户已决定：删除会话与 agent 保留忙时拒绝。

第 2 版根据第一轮审查修订：占用行永久保留并带状态（「调用已交出」是永久事实）；发起委派改为会话领域的一个事务加委派领域自己的对账；停止之后迟到的结局一律不投；删除「释放触发结清」；最终失败按会话范围结清；删除会话保留「忙时拒绝」；写明跨领域查询与事务的关系。

## 0. 要解决的问题

上一轮重构修掉了几个方案级错误（`ask_agent` 改为挂起调用、步级压缩、反思按步数），但领域之间的集成方式没有变：**一个跨领域的大事务**。一次运行结束时，记 task 完成、结清委派、给发起方投信、反思调度、推送、唤醒下一次运行都在同一个事务里，于是每个领域都要在别人的事务里直接改自己的表、读别人的状态。现状（restructure 分支）：

- 73 个函数接收数据库会话 `s`，以便被并进别人的事务。
- 同一张表被多个领域直接查询：`Message`（消费者、停止、历史、反思、会话服务），`Mail`（消费者、委派、信箱），`Delegation`（会话删除、挂起调用、委派），`Task`（压缩、信箱、队列）。
- `runtime/consumer.py` 导入 16 个 runtime 模块；`finish.py` 判断「委派做完没有」要看后台任务和信箱；`conversation.delete_sessions` 直接查 `Job` 与 `Delegation` 表；`pending.py` 读 `Delegation` 表判断 `ask_agent` 是否挂着。
- 评审每发现一个一致性漏洞，就在收尾或停止里再加一个条件，领域彼此知道得越来越多。

根因是在领域之间坚持强一致，而工作契约第 0 条推荐的是相反的交换：用可自动恢复的最终正确性换架构简化。

## 1. 原则

1. **领域独占自己的表。** 别的领域不 import 它的表、不拿它的 ORM 对象、不在它的事务里写它的数据。领域之间的引用只是不透明的 id，没有跨领域外键。
2. **领域之间只有三种通信：**
   - **命令**：请另一个领域做一件事（接收方由发送方指定），是对方公开接口上的一次方法调用，在**对方自己的事务**里执行，带幂等键，可安全重放，返回明确的结果类型（包括「目标已不存在」这类契约内的结果，不靠异常表达）。通常同步调用；发送方先提交了自己的状态、又必须保证命令执行时，用定向命令 `send` 持久化（3.3）。
   - **事件**：宣布自己发生了什么，与状态变化在同一个本地事务里持久化，订阅方至少收到一次，处理幂等。
   - **查询**：只读，经对方的公开接口，返回对方定义的数据结构（不是 ORM 对象），在**自己的只读事务**里执行（第 1.1 节）。
3. **发起用命令，反应用事件。** 一个流程由发起方用一串幂等命令推进；别的领域对状态变化的反应用事件。
4. **强一致只在一个领域内部，并且尽量只在一个聚合（一个会话）内部。** 必须当场一致的状态放进同一个领域，由它在一个本地事务里修改。同时改动多个聚合的事务应当极少，出现了先检查边界是否划错；目前唯一的例外是委派投递 `dispatch`（4.1）。这条防止原来的大事务在会话领域内部重新长出来。
5. **不用 2PC。** 所有领域在同一个 SQLite 文件里，本地事务本来就原子；跨资源的地方（git 资源仓库、文件存储、进程、Web Push、模型接口）不支持准备阶段，保持「失败可见 + 对账」。
6. **多步发起的中间状态只能留在拥有对账的领域里。** 一串命令中途崩溃或失败时，留下的半成品必须归某个领域所有，并由它的对账收敛；不允许留在别人那里无人认领。

### 1.1 跨领域查询与事务

数据库禁止嵌套事务，所以跨领域查询不在调用方的写事务里执行：

- 查询在自己的只读事务里取快照，调用方先查后写。
- **一个领域的写入判断不得依赖在同一事务里读到的别的领域的数据。** 需要时二选一：
  - 被查的事实不可变或只会单向变化（文件写入后不变；agent 只会从存在变为删除），先查后写的竞态无害，或由下游收敛；
  - 否则在自己领域里维护一份由事件更新的投影（例如会话领域记下已删除的 agent，见 4.4），在本地事务里判断。

## 2. 事务分级

| 级别 | 范围 | 机制 | 例子 |
|---|---|---|---|
| L1 强一致 | 一个领域内 | 一个本地事务 | 保存一步并把它带上的信件记为已处理；投信并判断是否唤醒；委派投递（占用发起方 + 投信给 thread）；停止会话 |
| L2 最终一致 | 领域之间 | 事件 + 带幂等键的命令 | 运行结束 → 推送、反思调度；会话结清 → 委派交回结局；会话停止 → 结束后台任务、放弃委派 |
| L3 对账 | 数据库与外部资源之间，或多步发起的中间状态 | 失败可见 + 定期或启动时对账 | 资源仓库 reconcile、文件存储 recover、临时目录清理、重启时被中断或卡在启动中的后台任务 |

每处 L2 的三条判据见第 7 节。

## 3. 平台层（`muliao/platform/`）

### 3.1 数据库

单写者 SQLite，`Database.transaction()` 在进程内串行化事务（不变）。**规则变化**：一个写事务只写一个领域的表，外加平台的任务表。开发期断言检查（第 10 节）。

### 3.2 任务队列即 actor 调度

现有的持久化任务队列（`tasks` 表、lane、重试、启动恢复、失败可见）不变，它就是 actor 调度器：同一 lane 内严格 FIFO、同一时刻一个执行者。会话的 lane `session:<id>` 就是会话 actor 的邮箱：运行、压缩都作为这条 lane 上的任务串行执行。

### 3.3 事件：以投递任务作为 outbox

**发布一个事件 = 在发布方的本地事务里，为每个订阅者插入一条投递任务**（`TaskKind.EVENT`，payload 是事件类型、事件 id 与数据）。

- 状态变化与「一定会通知到订阅者」在同一个事务里（transactional outbox）。
- 订阅者在组合根静态注册（事件类型 → 处理器、lane）。默认每个订阅者一条 lane（如 `sub:delegations`）。
- 处理器分两段：`execute` 里调用别的领域的命令（各自的事务、带幂等键）；返回的 `Commit` 只写**自己领域**的表，与「投递完成」在同一个事务里提交（`TaskQueue.complete` 现有机制）。所以对自己领域是恰好一次，对别的领域靠幂等键去重。
- 这套语义成立的前提，实现时必须遵守：
  1. 一个订阅者领域的状态只由它的投递 lane 写（单写者），否则「`execute` 里读、`Commit` 里写」是先查后写的竞态。委派领域连新行也只由它的 lane 创建（4.2）。
  2. `Commit` 里不调用别的领域的命令。
  3. 一个事件涉及多个对象时（例如一次结清带多个委派），命令的幂等键是「事件 id + 对象 id」。
  4. 事件 payload 校验失败是契约违约（例如跨版本部署），投递直接失败，「任务」页可见，人工处理。
- 正确性只依赖一处投递顺序：同一个委派的 `DelegationDispatched` 先于它的 `SessionSettled` 到达委派 lane（4.2 说明为什么一定如此）。其余处理器都按目标对象的当前状态判断（占用状态、委派状态），乱序到达也收敛。
- 失败投递的恢复：投递最终失败后在「任务」页人工重试。重试保留原 id，所以仍按原顺序处理；`TaskQueue.retry` 改为允许在 lane 上还有更晚的任务时重试更早的那条（现在会拒绝），否则共享 lane 上可能一直找不到重试时机。同一委派的 `DelegationDispatched` 失败时，它的 `SessionSettled` 也会失败，按 id 顺序先后重试即可。
- 订阅者 lane：每个订阅者一条（用户已决定），发给同一订阅者的投递按发布顺序逐条处理，不同订阅者之间并行。
- 队头阻塞：一条投递暂时失败、按退避重试时会挡住同一 lane 的后续投递。处理器只能执行短命令，不做长任务；需要时按分区键拆 lane。
- 可观测：每条投递任务记事件类型、事件 id、关联 id；处理开始、结果、去重、被丢弃的迟到结局都打结构化日志。

### 3.4 定向命令：`send`

命令通常同步调用：发送方在调用之前没有提交自己的状态，失败或崩溃时整件事被重做，命令按幂等键去重。只有一种情况不够：**发送方先在自己的事务里提交了状态，之后必须保证一条指定的命令一定执行**（例如后台任务记下退出码，然后要给会话投完成信件）。

这时用 `send(命令, 参数, key)`：在发送方的事务里插一条指定执行这个命令的投递任务。它与事件共用同一个 outbox、同一套重试与失败可见，区别只是接收方由发送方指定，不查订阅表。规则：先改了自己、又必须可靠通知确定的接收方时用 `send`，不用「自己订阅自己的事件」绕路。

### 3.5 实时展示总线

`bus.LiveRuns` 与 SSE（`muliao/events.py`）是展示加速，不是领域事件，不持久化、不保证送达，保持现状。命名上区分：领域事件叫 domain event，SSE 推送叫 server event。

### 3.6 幂等键

所有会新建东西的命令接受 `key`，由调用方从稳定的来源派生（工具调用 id、事件 id + 对象 id），在目标领域用唯一约束去重：`mail.dedupe_key`（同一会话内唯一）、`delegations.key`、`jobs.key`、`notifications.dedupe_key`。改状态的命令按目标的当前状态判断，天然幂等。

## 4. 领域

每个领域一个包，`<domain>/api.py` 是唯一对外的模块（命令、查询、事件类型、数据结构），其余模块对外不可见（第 10 节的依赖规则检查）。

### 4.1 会话（`muliao/sessions/`）

**职责**：会话、thread 树、信箱、历史、挂起的调用、运行（消费者与逐步保存）、压缩、停止、删除、会话种类的规则。会话 actor（lane）是运行的串行边界；会话领域的一个本地事务是状态的一致性边界。

**拥有的表**：`sessions`、`messages`、`mail`、`compactions`、`session_holds`（新）、`retired_agents`（新，4.4 的投影）。

#### 占用（hold）

别的领域为某个会话开始一项异步工作时，在会话上登记一个占用（不透明的 `holder`，如 `job:<id>`、`delegation:<owner>:<key>`）。占用行**永久保留**，带状态：

| 状态 | 含义 |
|---|---|
| `active` | 工作还在进行 |
| `released` | 工作的结局已经交回（和一封信件同一事务） |
| `cleared` | 会话被停止，工作被放弃，之后的结局不再投 |

占用可以带 `call_id`：这项工作的结局要作为那次挂起调用的结果交回（`ask_agent`）。**「这次调用已经交出去」= 本会话有带这个 `call_id` 的占用，不论状态。** 它是永久事实，替代 `pending.delegated_in` 对 `Delegation` 表的查询；`pending_calls`、`waiting`、`resumable` 都按它区分「已交出、在等回答」与「回复已保存、工具还没执行」。

「会话还有工作在外面」= 有 `active` 占用，替代 `SessionWork.active_in` 与 `finish._work_done` 对后台任务、委派的查询。

#### 命令

| 命令 | 说明 |
|---|---|
| `receive(Inbound)` | 外部来信的唯一入口（用户消息、对 `ask_user` 的回答、触发器），现有规则不变 |
| `post(session, mail, key, *, coalesce=False, release=None)` | 代码里的生产者投信（事件、检查）。`coalesce`：同一来源已有信件排着就不投。`release=h`：同一事务里把占用 `h` 记为 released。占用 `h` 已是 `cleared` 时什么都不投，返回 `Dropped`（记 info 日志，带 holder）；会话已不存在返回 `Gone`；会话存在而占用从未登记过则报错（bug） |
| `hold(session, holder)` | 登记一个占用（后台任务用），幂等 |
| `dispatch(owner, call_id?, thread, content, awaited, key)` | **委派投递，一个事务**：校验并打开或找到 thread（同名续聊，agent 或种类不符返回 `ThreadMismatch`，什么都不写）；在发起方登记占用，`holder` 由幂等键确定性派生（`delegation:<owner>:<key>`）；往 thread 投一封以同一个 `holder` 为关联 id 的信件；发布 `DelegationDispatched`。返回 holder 与子会话 id |
| `send_to_thread(parent, thread, content, awaited, key)` | 不交回结局的 `delegate_task`：打开或找到公开 thread 并投信，不登记占用 |
| `reply(owner, holder, result, key)` | 交回占用的结局：发起方还停在 `holder` 对应的挂起调用上 → 回答信件；否则 → 事件信件。同一事务把占用记为 released。占用已 `cleared` → `Dropped`；会话已不存在 → `Gone`。「迟到的回答改为事件」只在这里 |
| `stop(session)` | 有运行在进行时只取消它的取消作用域；运行保存完被停止的那一步、作用域退出之后，由会话 actor 执行停止事务（与现状一致）。没有运行时直接执行。停止事务：关闭历史、丢弃信件、lane 上排队的任务记为完成、全部 `active` 占用记为 `cleared`、未结清的关联以 `stopped` 结清。因为停止事务在运行完全退出之后，运行退出途中登记的占用也会被清掉；万一仍有遗漏（bug），再停止一次即可清掉 |
| `delete(session)` | 会话或它的私有 thread 忙时拒绝（409）。**忙** = lane 上有排队或运行中的任务（运行、压缩），或有 `active` 占用。不忙则一个事务删除会话与私有 thread，未结清的关联以 `deleted` 结清 |

`dispatch` 是会话领域里唯一同时写两个会话的事务（发起方的占用 + thread 的信件），两个表都属于会话领域，所以仍是 L1（原则 4 的唯一例外）。

#### 查询

`detail(session)`（会话页读模型）、`find_thread(parent, name)`、`threads_of(parent)`（含每个 thread 的运行状态与最新回答，供 `list_threads` 和检查信件）、`waiting_on(owner, holder)`（发起方是否停在这项占用对应的调用上，委派检查用）、`hold_state(session, holder)`（对账用）、`is_busy(session)`（触发器跳过用）、`agent_busy(agent)`、`transcript(agent, after_id, through_id)` 与 `responses_since(agent, after_id)`（反思用）。

#### 关联与结清

信件可以带一个不透明的关联 id（委派来的信件以它的 holder `delegation:<owner>:<key>` 为关联 id；holder 同时是占用 id、关联 id 与委派表主键）。会话判断关联什么时候「结束」，发布 `SessionSettled`。**结清只由会话自己在以下时机判断**，不由占用释放触发：

- **完成**：运行结束的事务里，运行正常结束、没有停在挂起的调用上、信箱里没有排队的信件、没有 `active` 占用 → 会话内已处理、还未结清的全部关联以 `finished` 结清，带回答与文件。占用释放总是伴随一封信件（`reply`、`post(release=…)`），信件会唤醒下一次运行，结清由那次运行结束时判断。
- **最终失败**：队列记失败的事务里（`failed_in`），会话内已处理、还未结清的全部关联，加上交给过这次运行、还排着的（现有 `handed_to` 规则，这些信件随之丢弃），以 `failed` 结清。按会话范围而不是按 `consumed_by` 归属，所以早先停在 `ask_user` 上的运行处理过的委派、被续跑接管（现有 `_adopted`）的信件都包括在内。
- **停止 / 删除**：未结清的关联以 `stopped` / `deleted` 结清。

`mail.correlation_settled` 是会话侧的记录，保证会话对每个关联只发布一次结清；「结局只交回一次」以委派状态为准（4.2）。

`failed_in` 只写会话自己的表，按契约不应失败；它若失败，worker 不带钩子记下失败（现有机制），由会话的信箱对账（`MailReconciler`）补做：最近一次运行已失败、仍有已处理未结清的关联且没有更新的运行时，以 `failed` 结清。这是同一规则的第二条执行路径，两处调用同一个函数，不写两份。

#### 发布的事件

| 事件 | 时机 | 主要数据 |
|---|---|---|
| `RunEnded` | 运行结束（完成、挂起、停止；没有可交给模型的空闲收尾不发） | 会话、agent、运行 id、结局、回答、是否问了用户、处理过的信件里是否有用户在等、本次保存的模型回复数 |
| `SessionSettled` | 关联结束 | 会话、结局（finished / failed / stopped / deleted）、回答、文件、关联 id 列表 |
| `DelegationDispatched` | `dispatch` 的同一事务 | holder、发起方、子会话、`call_id`、用户是否在等发起方 |
| `SessionStopped` | 停止后 | 会话、被清掉的占用列表 |
| `SessionDeleted` | 删除后 | 会话（含私有 thread）、agent |
| `AgentSessionsCleared` | 4.4 的删除 agent 流程里，该 agent 的会话已全部删除 | agent |

### 4.2 委派（`muliao/delegations/`）——流程管理者

**职责**：要交回结局的委派（`ask_agent`、`delegate_task(report_back)`）的生命周期；运行中检查；委派工具组（`delegate_task`、`ask_agent`、`list_threads`、`stop_thread`、`list_agents`）。

**拥有的表**：`delegations`，主键是 `holder`（`owner_session_id`、`child_session_id` 是不透明 id，无外键）。状态：`running` → `settled` / `abandoned`。委派行**只由 `sub:delegations` 这一条 lane 创建和修改**，没有第二个写者。

**发起（工具里）**：`Sessions.dispatch(owner, call_id, thread, ..., key=工具调用 id)`，一个事务完成占用、投信与发布 `DelegationDispatched`。返回 `ThreadMismatch` 时什么都没写，把原因作为可纠正的错误交还模型；否则 `ask_agent` 抛出 `CallDeferred`，`delegate_task(report_back)` 返回。续跑重新执行这次调用时 `dispatch` 按键去重。发起没有中间状态，因此不需要补偿，也不需要对账。

普通 `delegate_task`（不交回）只调用 `Sessions.send_to_thread`，不产生委派。

**订阅**（全部在 `sub:delegations` 一条 lane 上）：

| 事件 | 处理 |
|---|---|
| `DelegationDispatched` | Commit 里插入 `running` 行（按 holder 去重）。委派行永久保留（只进入终态），会话删除时也不删：发起方要被删除，必须先不忙，此时它发起的委派都已结清或被放弃；之后迟到的 `SessionSettled` 看到终态行按规则忽略，「没有这一行」只剩真正的 bug 一种情况 |
| `SessionSettled`（子会话） | 对其中每个关联 holder：状态是 running → `Sessions.reply(owner, holder, 结局, key=事件 id + holder)`，Commit 里记为 settled；`reply` 返回 `Dropped` 或 `Gone` 同样记为 settled，并记日志。已 settled 或 abandoned 的忽略。**没有这一行是契约违约，投递直接失败**（见下） |
| `SessionStopped`（发起方） | 被清掉的占用里的委派：running → Commit 里记为 abandoned，`execute` 里对子会话 `Sessions.stop(child)`（逐层由子会话自己的 `SessionStopped` 继续） |

**为什么 `DelegationDispatched` 一定先于对应的 `SessionSettled` 到达**：子会话的信件与 `DelegationDispatched` 的投递任务在同一个事务里提交；子会话只有在之后的事务里处理了这封信件，才可能因为它结清并发布 `SessionSettled`，那条投递任务的 id 一定更大。两者在同一条 lane 上按 id FIFO 处理。`SessionStopped`（发起方）同理：停止事务晚于发起时的 `dispatch`。

**运行中检查**：定期给 running 且 `Sessions.waiting_on(owner, holder)` 为假的委派，`Sessions.post(owner, 检查信件, coalesce=True)`。

### 4.3 后台任务（`muliao/jobs/`）

**拥有的表**：`jobs`。状态：`starting` → `running` → `exited` / `killed` / `interrupted`。

**发起**（在屏蔽作用域里完成，加时限，运行被停止也不会停在中间，见第 9 节）：插入 job 行（`starting`，键为工具调用 id）→ `Sessions.hold(session, "job:<id>")` → 启动进程 → 记为 `running`。**没有走到 `running` 的 `starting` 行只有一条收敛规则**，进程内的启动失败（spawn 报错、写 pid 失败、超出时限，此时占用是否已提交不确定）与崩溃后的启动对账都调用它：`Sessions.hold_state(session, holder)` 显示占用存在 → 按 `interrupted` 结束（同下文「结束」，走正常的投信加释放）；不存在 → 删除该行。进程内失败时工具同时把错误交还模型。

**结束**：记下状态与退出码，同一事务 `send(Sessions.post, session, 完成事件信件, key="job:<id>:end", release="job:<id>")`；投信返回 `Dropped` 或 `Gone` 时只记日志。

**订阅**：`SessionStopped` → 结束被清掉的占用对应的后台任务（进程组），不投信。job 行永久保留（终态即历史），会话删除时不删：停止后进程的结局可能晚于删除写入，它照常写进自己的行，投信得到 `Gone` 只记日志。

### 4.4 Agent 目录与资源（`muliao/agents/`）

**职责**：agent 的注册（名称、模型、工具组、反思间隔）、资源仓库（指令、记忆、skill、MCP 配置）、自我改进工具、管理 agent 的工具。

**拥有的表与资源**：`agents`；`data/resources/`。

**删除 agent**：

1. `Agents.delete(agent)`：`Sessions.agent_busy(agent)` 为真时拒绝（409，与删除会话的规则一致）；否则把 agent 标为 `deleting`（不出现在列表与名称解析里），同一事务发布 `AgentDeleting`。
2. 订阅者：
   - 会话：记入 `retired_agents`（之后 `receive`、`dispatch` 在本地事务里拒绝这个 agent，无先查后写竞态）；删除它的全部会话。期间若有会话忙（检查与标记之间的极小窗口），这次投递按契约内的暂时失败重试；超过重试上限时投递记为 failed，agent 停在 `deleting`，在「任务」页可见，人工重试即可继续。全部删除后发布 `AgentSessionsCleared`（没有会话时立即发布）。
   - 反思：取消该 agent 排队的反思任务，删除检查点与记录；正在执行的反思任务结束后不再推进。
   - 触发器、vault：删除它的触发器与私有 secret。
3. agents 订阅 `AgentSessionsCleared`：删除行与资源目录。

### 4.5 Vault（`muliao/vault/`）、文件（`muliao/files/`）

现状基本已是独立领域。把对外接口收进 `api.py`，删掉与 agent 表之间的外键。文件写入后不可变、不删除，`receive` 校验附件存在可以先查后写。

### 4.6 反思（`muliao/reflection/`）

**拥有的表**：`reflections`、`reflection_checkpoints`（从 `agents.reflection_checkpoint` 移来）。

**订阅** `RunEnded`：用 `Sessions.responses_since(agent, checkpoint)` 数步数（事实仍只在 messages 表里），达到 `reflect_every`（`Agents` 查询）就推进检查点并投递 reflection 任务；执行时用 `Sessions.transcript(...)` 取记录。

### 4.7 通知（`muliao/notifications/`）

**拥有的表**：`notifications`、`push_subscriptions`；Web Push 投递。

**命令**：`notify(..., key)`（`notify_user`、前置检查告警、推送测试）。

**订阅**：`RunEnded`（处理过的信件有用户在等，且做完了或问了用户 → 一条通知 + 投递，键为运行 id）；`SessionDeleted` → 删除指向它的通知。

### 4.8 触发器（`muliao/triggers/`）

**拥有的表**：`triggers`。触发 = `Sessions.receive(Inbound)`；「上一次触发的会话还在运行就跳过」用 `Sessions.is_busy`（先查后写的竞态最坏多触发一次，与现状相同）。订阅 `AgentDeleting` 删除该 agent 的触发器。

### 4.9 agent 运行时（`muliao/agent_runtime/`）

不是业务领域，是会话与反思共用的端口：模型注册表、指令组装（读 `Agents`、`Vault` 的接口）、工具组合（`tool_catalog` + 各领域提供的工具集）、pydantic-ai 的逐步驱动适配（现在的 `runner.py`）。工具一律通过领域接口做事，不碰表。

## 5. 关键流程

### 5.1 用户消息 → 运行 → 收尾

1. `Sessions.receive`：L1 事务投信，判断唤醒，排运行任务。
2. 运行在会话 lane 上逐步执行、逐步保存（L1，现状不变）。
3. 运行结束的事务里：记 task 完成、更新已读与时间、判断结清（可能发布 `SessionSettled`）、发布 `RunEnded`、判断是否唤醒下一次运行。全部是会话自己的数据。
4. 通知领域推送；反思领域数步数。

### 5.2 `ask_agent`

1. 发起方的工具调用 `dispatch`（占用发起方 + 投信给 thread + 发布 `DelegationDispatched`，一个事务）后挂起。占用带 `call_id`，这次调用从此永久算作「已交出」。委派领域随后创建委派行。
2. 子会话正常运行；结清条件满足时发布 `SessionSettled(finished)`。
3. 委派领域 `Sessions.reply(owner, ...)`：发起方还停在这次调用上 → 回答信件，唤醒它续跑，这次调用以回答为结果，不会被重新执行；用户已经跳过（调用已记为「还在处理」）→ 事件信件。占用记为 released。
4. 用户在等发起方时，回答信件随它的 `awaited` 推送。

### 5.3 停止会话

有运行在进行时先取消它、等它保存完被停止的那一步并退出。随后 `Sessions.stop` 的停止事务在一个 L1 事务里完成会话自己的全部状态迁移（包括把 `active` 占用记为 `cleared`），然后发布 `SessionStopped` 与 `SessionSettled(stopped)`。后台任务领域结束进程；委派领域放弃发起方的委派并逐层停止子会话，同时把子会话的「被停止」交回给发起方。停止之后才到的结局（后台任务结束的投信、子会话的 `reply`）看到占用已 `cleared`，一律不投，所以不会唤醒刚停掉的会话。这条规则在会话内部，不依赖投递顺序。

### 5.4 删除会话

会话或它的私有 thread 忙时拒绝（L1，会话领域内部判断，无异步窗口；界面上「停止」与「删除」是两步）。不忙则一个事务删除会话与私有 thread，未结清的关联以 `deleted` 结清，发布 `SessionDeleted`。公开 thread 保留（父会话 id 置空）。其他领域各自清理引用它的数据。

### 5.5 运行最终失败

队列记失败的事务里，会话按 4.1 的规则以会话范围结清关联（`failed`），丢弃交给过它还排着的委派信件，发布 `SessionSettled(failed)`。

## 6. 与现有行为的对照

| 现有行为 | 新方案 |
|---|---|
| `finish._work_done` | 没有 `active` 占用 + 信箱为空 + 没有停在挂起调用上（会话内部） |
| `finish` 里的反思、推送 | 订阅 `RunEnded` |
| `halt` / `halt_all` | `stop` 一个 L1 事务 + `SessionStopped`；迟到结局按占用 `cleared` 不投 |
| `failed_in` / `handed_to` / `_adopted` | 4.1「最终失败」，会话范围 |
| `delegated_in` / `waiting` / `deferred_results` / `STILL_WORKING` | 带 `call_id` 的占用（永久）；规则不变 |
| `Threads.send`（单事务） | `dispatch`（单事务，同时发布 `DelegationDispatched`），委派行由委派 lane 创建 |
| `delete_sessions` | 忙时拒绝（判断在会话内部）+ `SessionDeleted`；委派行与 job 行保留终态 |
| `agent_lifecycle` | 忙时拒绝 + 删除 agent 流程（4.4） |
| 反思检查点在 `agents` 表 | `reflection_checkpoints` |
| 触发器跳过（`busy_in`） | `Sessions.is_busy` |

## 7. 最终一致的三条判据

| 跨领域效果 | 失败可见 | 可收敛 | 代价 |
|---|---|---|---|
| 委派结局交回发起方 | 投递任务失败可见、可重试 | 委派状态单写者 + `reply` 按键去重；乱序时按占用状态判断 | 交回晚几百毫秒 |
| 推送 | 同上；Web Push 失败语义不变 | 通知按运行 id 去重 | 推送晚几百毫秒 |
| 反思调度 | 同上 | 步数从 messages 推导，检查点推进幂等 | 无 |
| 停止后结束后台任务、放弃委派 | 同上 | 结束进程与放弃委派幂等；迟到结局不投 | 停止后进程可能多跑几百毫秒 |
| 删除后的清理（通知） | 同上 | 删除幂等 | 指向已删除会话的通知短暂存在 |
| 删除 agent | `deleting` 状态可见 | 每步幂等；会话全部删除后完成 | agent 从列表消失与目录删除之间有窗口 |
| 后台任务的发起中途崩溃 | `starting` 行可查 | 启动时对账按占用是否存在收敛 | 重启前多占一行 |
| 委派行的创建 | 投递任务失败可见 | 按 holder 去重；先于结清到达（4.2） | 委派行晚于占用几百毫秒出现 |

## 8. 会话种类与暂缓的项

### 8.1 会话种类

`sessions.private` 改为 `kind`（`conversation` 顶层会话、`thread` 公开 thread、`private_thread`），一个策略对象一处定义：用户能否写、是否进收件箱、是否标未读、能否单独删除、回答交给谁（`Audience`）、续聊用哪个工具。现在散在 6 处的 `if session.private` 改为读策略。

### 8.2 回答信件的判别

`mail.answers` 的值存成带工具名的信封（`{"tool": "ask_user" | "ask_agent", "result": ...}`），接口与前端按判别字段区分，删掉按形状区分。

### 8.3 临时 agent（暂缓）

方向是改为用完即删的 thread（运行只剩一种模型，失败只重做自己），但放到占用与结清稳定之后（第 11 节阶段 1 之后）再定，不和本次捆在一起。

### 8.4 agent loop 的归属

本次不换。把 pydantic-ai 的 loop 收在 `agent_runtime` 的一个端口后面（「从这段历史推进一步，告诉我发生了什么」），会话领域只依赖这个端口与消息格式。以后自己写 loop（框架只做模型客户端）时，改动限于这个端口的实现。

## 9. 并发模型（anyio）

现状有 8 处手动管理 asyncio 任务（裸 `create_task` 加逐个 `cancel` 再 `gather`），最脆弱的是 `stopping.py` 靠 `task.cancelling()` 计数区分「用户停止」与「关停、超时」。改为 anyio 的结构化并发（它已是 FastAPI、pydantic-ai 的依赖，改为直接依赖，运行在 asyncio 后端上）。

**取消**：

- 用户停止 = 不带消息地取消这次运行自己的原生任务；关停或总时限取消的是外层，宿主收到后同样取消运行的任务再照原语义抛出。用 `asyncio.wait` 等运行结束（运行被取消时不抛出、宿主被取消时才抛出），`stopping.py` 的计数判断删除。停止事务在运行退出之后执行（4.1）。
- **唯一的例外**是上面这一处用原生任务（`runtime/stopping.py`，带 noqa 与说明）。实测原因：pydantic-ai 把每个工具调用放进原生任务并 await 它，被取消的任务会把取消连同消息传给它正在 await 的任务；如果用 anyio 作用域停止运行，anyio 的取消消息就传进工具任务，工具里的 anyio 任务组（如 shell）把它当成自己的取消吞掉，工具照常返回结果（被停止的命令得到「超时被杀」的结果）。不带消息的原生取消不会被误认。`tests/test_stopping.py` 固定了三种取消下工具都如实收到取消。
- anyio 的取消是电平触发的：作用域被取消后，里面每一次 await 都会再抛出取消，直到退出作用域。因此**被取消后还要 await 的清理一律放进屏蔽作用域并加时限**（`CancelScope(shield=True)` + `move_on_after`）。

**需要屏蔽的清理点**与超时后的收敛：

| 清理 | 超时或失败后 |
|---|---|
| **每个数据库事务**（`db.Database.transaction` 整体屏蔽 anyio 的取消，取消在事务结束后生效） | 事务按契约很短；不屏蔽时取消会打断语句，SQLAlchemy 作废连接后的后台关闭又被再次取消，留下永远等不到的任务，关停时卡住（实测） |
| 保存被停止的那一步（`steps.save_stopped`）：用户停止后在宿主里执行，每个事务都已屏蔽 | 两个事务之间赶上关停时，剩下的由队列恢复后续跑；停止事务照样关闭历史 |
| 后台任务启动失败后的清理（删行、取消监督者） | 记 error 日志；行留在 running，检查的对账记为被中断（4.3 的 `starting` 规则在第 2 阶段落地） |
| 结束进程组并回收（后台任务、同步 shell、前置检查） | 记 error 日志；后台任务由对账或启动时记为 `interrupted` |
| git 退出与失败写入后的撤销暂存 | git 被强杀；锁由下次启动的 recover 清理 |
| 临时 agent 工作目录删除 | 记 error 日志；启动时清空 `data/tmp` |

**异常的传播边界**（三类，每类一个任务组或作用域）：

1. **根任务组**（容器启动时开，关停 = 取消它）：只放「意外异常即进程退出」的循环——worker 调度循环、定时触发循环、信箱对账循环、后台任务的检查循环。它们意外退出是 bug，进程退出交给进程管理器重启（现有语义）。
2. **每个任务一个边界**：worker 执行的每个 task（运行、压缩、反思、投递、事件投递）在自己的作用域里执行，边界上捕获异常并按队列规则记账（重试或失败），不向上传播，一个运行出错不影响别的运行。
3. **每个后台任务的监督者一个边界**：意外异常只记 error 日志，由后台任务的对账收敛（「监督意外结束」是契约内的情况），不取消别的监督者或运行。

**运行开始与停止的竞态**：运行任务已被领取、但还没登记取消作用域时，`stop` 看不到正在进行的运行，会直接执行停止事务（现状也有这个竞态）。修正：运行开始的第一个事务里检查自己的 task 是否已被停止事务记为完成，是就不执行。

**规则用 lint 固定**：ruff banned-api 禁止 `asyncio.create_task`、`asyncio.ensure_future`、`asyncio.gather`、`asyncio.wait`、`asyncio.wait_for`、`asyncio.shield`、`asyncio.timeout`、`asyncio.TaskGroup`（测试除外）。停止、关停、超时三种取消各写测试。测试用 anyio 的 pytest 插件（`anyio_mode = auto`）：异步 fixture 与测试在同一个任务里，任务组可以跨 fixture 的 yield。

## 10. 模块与依赖规则

```
muliao/
  platform/        数据库、任务队列（actor 调度）、事件发布与订阅、实时展示总线、并发根、时钟、id
  agent_runtime/   模型注册表、指令组装、工具组合、loop 适配
  sessions/        会话（信箱、历史、运行、挂起调用、占用、结清、压缩、停止、删除、种类）
  delegations/     委派流程管理者与委派工具
  jobs/            后台任务
  agents/          agent 目录、资源仓库、自我改进工具
  vault/  files/  reflection/  notifications/  triggers/
  api/             HTTP 适配（每个领域一个路由模块，只调领域的 api.py）
  app.py           组合根：构造领域、注册事件订阅、组装工具组
```

依赖规则用 import-linter 检查，进测试流程：领域包之间只能 import 对方的 `api` 模块；任何领域不得 import 别的领域的表；`platform` 不 import 任何领域；领域不 import `api/` 与 `app.py`。

表定义放在各领域包内（`<domain>/tables.py`），Alembic 汇总全部 metadata。跨领域外键全部删除，领域内的外键保留。开发期断言：一个写事务写入的表属于同一个领域（平台任务表除外）。

## 11. 实施阶段

每个阶段独立提交、审查，测试全过；全部完成后重建生产库上线。

0. **平台**：anyio 结构化并发（第 9 节）；事件即投递任务（发布、订阅注册、处理器两段语义）；命令幂等键；import-linter 接入，现有违规记成基线。
1. **占用与结清**：`session_holds`（带状态、永久）、`dispatch` 与 `DelegationDispatched`、关联与 `SessionSettled`、`reply`；委派改为流程管理者（委派行只由它的 lane 创建）；删掉 `finish` 对后台任务与委派的依赖、`pending` 对 `Delegation` 表的依赖。
2. **停止、删除、失败经事件**：`SessionStopped` / `SessionDeleted` 的订阅者；后台任务的 `starting` 与对账；删除 agent 流程与 `retired_agents`；删除跨领域外键。
3. **其余订阅者**：推送、反思订阅 `RunEnded`；`reflection_checkpoints`。
4. **按领域分包**：每个领域一个包与 `api.py`，表随领域；import-linter 去掉基线；事务单领域断言生效。
5. **会话种类策略、回答信封。**
6. **文档并入 architecture.md、上线。** 之后再评估临时 agent（8.3）。

必须有测试固定的行为（另加各阶段的常规测试）：回答到达后不会重新执行 `ask_agent`；发起时 thread 校验失败什么都不写；子会话在 `dispatch` 之后立刻结清，结局仍然交回；后台任务启动失败释放占用；`starting` 中途崩溃由对账收敛；停止在运行退出之后执行，运行退出途中登记的占用被清掉；停止后迟到的后台任务结束投信与 `SessionSettled` 不唤醒会话；停止后立即删除，迟到的 `SessionSettled` 与后台任务结束投信不产生失败投递；运行刚被领取时停止，这次运行不执行；持锁期间停止一次运行（保存被停止的一步超时，停止事务照样关闭历史）；最终失败结清早先运行处理过的关联；删除带私有 thread 的会话（忙时拒绝、不忙时一并删除）；删除没有会话的 agent。领域测试只经对方的 `api.py`；跨领域流程测试在 `drain`（同时处理运行与事件投递）之后断言。

## 12. 行为变化

- 委派结局、推送、反思调度从运行结束的同一事务改为随后的事件投递，晚几百毫秒。
- 删除 agent 改为异步完成（先从列表消失，会话与资源随后删除）。
- 删除会话与 agent 保留忙时拒绝（用户已决定）；「忙」现在包括会话 lane 上排队的压缩。

## 13. 已决定

- 删除会话与 agent 保留忙时拒绝。
- 每个订阅者一条 lane。
- 平台增加定向命令 `send`。
