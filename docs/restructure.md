# 设计：按领域重新收敛（2026-10-20）

状态：用户已决定全部按此重构，不做数据迁移（重建数据库）。完成后并入 architecture.md 并删除本文。

## 1. 方案级调整

### 1.1 挂起调用：`ask_user` 与 `ask_agent` 是同一件事

原来的前提「发问方要等回答，所以子 thread 在发问方那一步里同步跑完」是错的模型，它带来了内联通道、`start_inline`、`run_inline` 自己记账与「直到信箱空」、`ReplyTo.AGENT` 与 `inline` 参数、私有 thread 不能停止、私有 thread 看不到运行状态、等待时占着 worker 与总时限、重启后只能记为失败。

新模型：两者都是**挂起的工具调用，等别人回信**。

- `ask_agent` 往私有 thread 的信箱投一封带委派的信件，然后 `CallDeferred`：发问方的运行停在这里结束，不占 worker。同一个调用续跑时再执行，不重复投信（按调用 id 幂等）。
- 私有 thread 和其他会话一样由自己的消费者经 worker 运行。处理完委派信件时，结局投回发问方：发问方还停在这次调用上，就投一封**回答信件**（作为这次调用的结果交回，带上子 thread 与它挂给用户的文件）；否则（发问方已经不等了）投一封事件信件。
- 挂起的调用（`questions.pending_calls`，从历史推导）有两种：`ask_user` 等用户，`ask_agent` 等 agent。**唤醒**：等待中，用户的新消息唤醒（跳过）；挂起的调用都有回答信件时唤醒；事件留着等它们。一条规则（`waiting(history, queued)`），`wake_in` 与消费者开头共用。
- **跳过**：用户发了新消息时，没回答的 `ask_user` 记为没回答（现有语义），还没回答的 `ask_agent` 记为「它还在处理，回答会作为事件送达」，之后它的结局按事件投回。
- 私有 thread 的含义只剩「用户只读、只收发问方的信」。它有自己的信箱和消费者，可以开后台任务、`report_back`、被停止（发问方那次调用拿到「被停止」）。不能单独删除（随父会话删除）。`ask_user` 在私有 thread 里不提供（没有用户在等）。

### 1.2 会话拥有的异步工作：一个接口，两种实现

后台任务与要交回结果的委派都是「会话发起的一项异步工作」：结束时给发起方投一封结局信件、运行中定期检查（同一来源已有信件排队就不再发）、会话被停止时一起停下、重启时收敛。

- `runtime/work.py` 定义接口 `SessionWork`：`checks_in(s, now)`（还在跑的工作给出检查信件）、`halt(owner_session_id)`（随会话停止，不再投信）。`WorkWatcher` 统一跑检查循环。
- 实现：`jobs.JobRunner`（`jobs` 表）、`delegations.Delegations`（新表 `delegations`：发起会话、子会话、调用 id（`ask_agent` 时有）、用户是否在等、状态 running / settled）。委派信件引用它（`mail.delegation_id`）。
- 委派的结局只交回一次：`delegations.settle_in` 把状态从 running 改为 settled 并投信，是唯一出口（完成、被停止、最终失败、子会话被删除）。原来的 `mail.report_to` JSON、`ReportTo`、`open_reports` 的 JSON 查询消失。
- 普通 `delegate_task`（不交回）不建委派行：它只是往公开 thread 投一封用户在等的信。

### 1.3 步级压缩

「只在两次运行之间压缩」是按轮建模时的约束，每一步都保存之后不再成立。

- 消费者在每次请求模型之前看上一次请求的上下文大小，超过阈值就：保存这次请求（照常一步）→ 结束这次驱动 → 压缩（切点在步与步之间：之前没有悬空调用的请求处，保留最近 `compaction_keep_steps` 步）→ 从库里重新加载上下文（摘要 + 切点之后）续跑，pydantic-ai 重发末尾那条请求。运行开始时同样检查一次。
- 压缩逻辑只在消费者一处；手动压缩仍是同 lane 的一个 task，处理器只调用同一个压缩方法。

### 1.4 反思按步数，由消息推导

`turns_since_reflection` 计数列删除。运行结束时数该 agent 自检查点以来的模型回复条数（每条回复就是一步），达到 `reflect_every` 就投递反思并推进检查点。事实只在 `messages` 表里。

## 2. 结构收敛（三份架构审查的结论）

- **turn → run**：`TaskKind.RUN`、`session_lane`、`RUN_STOPPED`、`RUN_EVENTS`、`compaction_keep_steps` 等统一改名；`ReplyTo` 移出 `models.py`，改为运行期的 `Audience`（user / agent / none），由会话与信件在运行开始时推导一次。
- **运行种类与工具组合**：有会话的运行、反思、临时 agent 各用自己的工具组合；后台任务工具（`run_shell` 的 background、`job_*`）是独立工具集，只在有会话的运行里组合进来；工具里不再做运行种类的运行时检查（删 `_require_session` 等）。`ask_user` 只在 `audience = user` 时提供。没有会话的运行不转发运行事件。
- **工具组注册表是唯一来源**：组名、界面标签、是否等同全部权限、是否默认开启都登记在注册表；`AgentService` 用它校验；`/api/agent-defaults` 下发标签与风险；反思的限制用注册表导出的常量；删 `TOOL_GROUPS` 元组与相等断言。
- **Consumer 拆分**：逐步执行与保存（`runtime/steps.py`）、停止会话（`runtime/halt.py`）、收尾（`runtime/finish.py`：推送、反思、委派结局、唤醒）、编排（`consumer.py`）。
- **规则只写一遍**：`conventions.is_stopped(message)`、`conventions.answer_text(messages)`、`questions.pending_calls(history)`、`questions.waiting(history, queued)`；`pending_in_session` 改为从完整历史推导。
- **「之后才到」按信件 id**：运行投递时在 payload 记下当时最大的信件 id（`RunPayload.through_mail_id`，唯一读写处在 mailbox），不再比较两张表的时间。
- **错误分类**：存储的数据坏了或缺了统一抛 `errors.CorruptState`（不是 `DomainError`，不交还模型、不映射成 4xx）；`UpstreamFailed` 移出 `DomainError`（API 单独映射 502）。`PrecheckFailed` 留在 `DomainError`：检查脚本坏了是 agent 能改的，`trigger_run` 要把原因交还模型。
- **会话详情读模型**收进会话服务（`SessionService.detail`），路由只做 DTO 映射。
- **触发器**：`last_error` 拆成 `disabled_reason` 与 `precheck_error`。
- **通知**：读模型从 `delivery/` 移到 `notifications.py`，`delivery/` 只留投递。
- **MCP**：配置只有一个类型（`McpConfig`，自带校验）；`mcp` 组用 pydantic-ai 的 `DynamicToolset` 按运行构建，`ToolGroup.uses_mcp` 特例删除。
- **编辑规则**：`text_edit.edit(current, old, new)` 是「替换恰好一次或追加」的唯一入口，skill、指令、`edit_file` 共用。
- **配置一致性**：`shell_default_timeout <= shell_max_timeout`、`memory_max_chars` 下限等在配置类型上校验。

## 3. 前后端契约

- 运行事件、任务事件、`saved`、心跳都声明为 pydantic 模型；SSE 只序列化这些模型（删 `default=str`）。
- 实时事件按会话条目（`transcript.Item`）的形状发出（文本是「追加到最后一条」的增量），前端只合并，删 `liveToItems` 与 `view: 'live'` 特判。
- `ask_user` / `ask_agent` 条目由后端给出结构化状态（待回答 / 已提交 / 已回答 / 没回答 / 等待 agent），前端不再解析工具结果文本、不再拼三个来源。
- 前端类型从 OpenAPI 生成（`openapi-typescript`），测试检查提交的生成结果与当前接口一致。
- 前端路由与 webhook 地址各自只有一个构造函数。

## 4. 不做的

- 事件段在请求 metadata 里按下标标记（`RUN_EVENTS`）保留：pydantic-ai 的 `UserPromptPart` 没有 metadata，无法逐段标记；改成由信件表推导时间线需要会话条目依赖数据库，收益不抵复杂度。下标在构造内容时就记下，不从文字反推。

## 5. 数据

不写迁移：`migrations/versions/` 重新生成唯一的初始版本。上线时生产库需要重建（会话、agent、触发器、secret、推送订阅都会清空，资源仓库里按旧 agent id 存的目录随之成为孤儿）——执行前单独确认，并先备份。

## 6. 阶段

每个阶段独立提交、审查；全部完成后一次上线。

0. turn → run 改名，迁移重置为唯一初始版本。
1. 委派表 + 挂起调用（`ask_agent`），私有 thread 走 worker，删除内联执行。
2. `SessionWork` 接口（后台任务、委派）、统一检查与停止。
3. 步级压缩、反思按步数。
4. Consumer 拆分与其余结构收敛（第 2 节）。
5. 前后端契约（第 3 节）。
6. 文档合并、上线。
