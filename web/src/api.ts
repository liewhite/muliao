// 接口类型取自 schema.d.ts（由后端的 OpenAPI 描述生成，见 scripts/gen-api.mjs），这里只起名字、补上品牌类型

import type { components, paths } from './schema'

export type Schemas = components['schemas']

export type Origin = Schemas['Origin']
export type TaskStatus = Schemas['TaskStatus']

/** agent 的 id 与名称都是字符串；品牌类型让编译器拦住「把名称当 id 传」 */
export type AgentId = string & { readonly __brand: 'AgentId' }

/** 仅用于边界：路由参数、取值为 id 的下拉框 */
export const asAgentId = (value: string) => value as AgentId

/** 把接口类型里是 agent id 的字段换成 AgentId（可为空的保持可为空）；对联合类型逐个成员替换，保留区分字段 */
type WithAgentId<T, K extends keyof T> = T extends unknown
  ? Omit<T, K> & { [P in K]: null extends T[P] ? AgentId | null : AgentId }
  : never

export type Agent = WithAgentId<Schemas['AgentOut'], 'id'>
/** 修改 agent 的请求体：整体替换，字段全部必填（新建用 Schemas['AgentCreate']） */
export type AgentInput = Schemas['AgentUpdate']
export type Session = WithAgentId<Schemas['SessionOut'], 'agent_id'>
// 会话列表里的一行：多一个工作状态 state（在运行 / 在等 / 空闲），不空闲的（在运行、在等）排在最前（接口排序）
export type SessionListItem = WithAgentId<Schemas['SessionListItem'], 'agent_id'>
/** 一条通知（不叫 Notification：那是浏览器的全局对象） */
export type AppNotification = Schemas['NotificationOut']

// 下面三个值与两个路由（sessionRoute、issues.ts 的 issueRoute）前后端各写一份，tests/test_frontend_contract.py 核对
/** 指代用户的那个值（与后端 agents.api.USER 一致）：issue 的指派人、资源修改的作者等 */
export const USER = 'user'
/** issue 时间线上系统自己的动作（删除 agent 时的清理）的 actor（与后端 issues.rules.SYSTEM_ACTOR 一致） */
export const SYSTEM_ACTOR = 'system'

/** 引用文件的地址前缀（与后端 muliao.files.FILES_PATH 一致）：回答里只有指向这里的图片会被渲染 */
export const FILES_PATH = '/api/files'

export type FileInfo = Schemas['FileOut']
/** 读不出来的资源文件（skill、MCP 配置）：列表接口不含它们，单独列出供删除 */
export type Attachment = Schemas['Attachment']
export type Item = Schemas['Item']
export type CompactionInfo = Schemas['CompactionOut']
export type Task = WithAgentId<Schemas['TaskOut'], 'agent_id'>

/** 一个分组的用量汇总；没有参与分组的维度为 null。input_tokens 已含缓存读写，不要相加 */
export type Usage = WithAgentId<Schemas['UsageOut'], 'agent_id'>
export type UsageGroup = Schemas['UsageGroup']
/** agent 做过的一件事（只追加，由 agent 在每日自进化时写） */
export type HistoryEntry = WithAgentId<Schemas['HistoryOut'], 'agent_id'>

export type Question = Schemas['Question']
export type QuestionAnswer = Schemas['QuestionAnswer']
export type AnsweredQuestion = Schemas['AnsweredQuestion']
export type AskView = Schemas['AskView']

export type MailKind = Schemas['MailKind']
export type MailState = Schemas['MailState']
export type Mail = Schemas['MailOut']
/** 事件的来源：后端解析好的 thread、后台任务或其他（raw 是原文，同一来源按它归在一起） */
export type EventSource = NonNullable<Item['source']>
export type SessionDetail = Omit<Schemas['SessionDetail'], 'session' | 'run' | 'threads'> & {
  session: Session
  run: Task | null
  threads: Session[]
}
export type LiveSegment = Schemas['LiveSegmentOut']

export type Skill = Schemas['SkillDetail']
export type Memory = Schemas['MemoryOut']
export type TriggerKind = Schemas['TriggerKindOut']
export type Precheck = Schemas['Precheck']
/** 按 kind 区分的联合：收窄 kind 后 config 与 status 就是这种方式的类型 */
export type Trigger = WithAgentId<
  Schemas['ScheduleTriggerOut'] | Schemas['OnceTriggerOut'] | Schemas['WebhookTriggerOut'] | Schemas['StreamTriggerOut'],
  'agent_id'
>
export type TriggerKindName = Trigger['kind']
export type McpServer = WithAgentId<Schemas['McpOut'], 'owner'>
export type McpInput = WithAgentId<Schemas['McpIn'], 'owner'>
/** 条目池里的一项（内置工具、skill、MCP server、secret），带收录它的角色与这一种自己的详情（按 kind 收窄）；
 * 后端 agents/catalog.py、api/items.py */
export type CatalogItem = WithAgentId<
  Schemas['ToolItemOut'] | Schemas['SkillItemOut'] | Schemas['McpItemOut'] | Schemas['SecretItemOut'],
  'owner'
>
export type ItemKind = CatalogItem['kind']
export type ItemOf<K extends ItemKind> = Extract<CatalogItem, { kind: K }>
/** 存在库里的条目种类（能改、能转移、有版本）：内置工具之外的三种 */
export type StoredKind = Schemas['StoredKind']
/** 版本历史的对象：记忆按 agent（null 为全局记忆），条目按种类与名字 */
export type RevisionTarget = { kind: 'memory'; agent: AgentId | null } | { kind: StoredKind; name: string }
export type Revision = Schemas['RevisionOut']
/** 角色（内置或自建）、它能用的工具与条目、带它的 agent；后端 agents/permissions.py、agents/roles.py */
export type Role = Omit<Schemas['RoleOut'], 'agents'> & { agents: AgentId[] }
export type RoleInput = Schemas['RoleIn']
export type ToolPermission = Schemas['ToolOut']
export type Reach = Schemas['ReachOut']
export type IssueStatus = Schemas['IssueStatus']
export type IssuePriority = Schemas['IssuePriority']
/** assignee：agent id、'user'（用户），或 null（未指派） */
export type Issue = Schemas['IssueOut']
export type IssueActivity = Schemas['ActivityOut']
export type IssueDetail = Schemas['IssueDetailOut']
export type AgentDefaults = Schemas['AgentDefaultsOut']

export class ApiError extends Error {
  readonly status: number

  constructor(status: number, message: string) {
    super(message)
    this.status = status
  }
}

type Query = Record<string, string | number | boolean | null | undefined | string[]>

function withQuery(path: string, query?: Query): string {
  if (!query) return path
  const params = new URLSearchParams()
  for (const [k, v] of Object.entries(query)) {
    if (v === null || v === undefined) continue
    if (Array.isArray(v)) v.forEach((x) => params.append(k, x))
    else params.set(k, String(v))
  }
  const qs = params.toString()
  return qs ? `${path}?${qs}` : path
}

// 401 时通知应用跳转登录页
export const unauthorizedEvent = new EventTarget()

/** 所有请求的唯一出口：统一的 401 处理与错误信息 */
async function send<T>(
  method: string,
  path: string,
  query: Query | undefined,
  body?: { type: string; data: BodyInit },
  // 响应体是文件（如 .skill 包）：成功时原样交出 Blob
  blob = false,
): Promise<T> {
  const resp = await fetch(withQuery(path, query), {
    method,
    credentials: 'same-origin',
    headers: body === undefined ? undefined : { 'Content-Type': body.type },
    body: body?.data,
  })
  if (resp.status === 401 && path !== '/api/auth/login') {
    unauthorizedEvent.dispatchEvent(new Event('unauthorized'))
  }
  if (!resp.ok) {
    const text = await resp.text()
    let message = text
    try {
      const detail = JSON.parse(text).detail
      message = typeof detail === 'string' ? detail : JSON.stringify(detail)
    } catch {
      // 非 JSON 错误体（如代理返回的 HTML）原样展示
    }
    throw new ApiError(resp.status, `${resp.status}: ${message}`)
  }
  // 204 按 HTTP 语义没有响应体。FastAPI 的 204 仍带着 JSONResponse 的 content-type: application/json
  // （fastapi/routing.py 先建 JSONResponse 再清空响应体），所以不能按 Content-Type 判断
  if (resp.status === 204) return undefined as T
  if (blob) return (await resp.blob()) as T
  // 其余按响应的类型解码：JSON 响应体缺失时 resp.json() 直接报错；没有响应体的接口（stop 这类 202，接口描述里
  // 没有 content，类型为 void）必须真的没有响应体
  if (resp.headers.get('Content-Type')?.startsWith('application/json')) return (await resp.json()) as T
  const text = await resp.text()
  if (text !== '') throw new Error(`${method} ${path} answered ${resp.status} with an unexpected non-JSON body`)
  return undefined as T
}

// ---- 按接口描述（schema.d.ts 的 paths）类型化的调用：路径模板、参数、请求体、响应各只对应一次 ----

type Method = 'get' | 'post' | 'put' | 'patch' | 'delete'
type Path = keyof paths
type MethodOf<P extends Path> = { [M in Method]: [NonNullable<paths[P][M]>] extends [never] ? never : M }[Method]
type Operation<P extends Path, M extends Method> = NonNullable<paths[P][M]>

type Responses<O> = O extends { responses: infer R } ? R : never
type JsonOf<R> = R extends { content: { 'application/json': infer T } } ? T : void
/** 成功响应的 JSON 体；204、202 没有响应体时为 void */
type Result<O> = JsonOf<Responses<O>[Extract<keyof Responses<O>, 200 | 201 | 202 | 204>]>

type Parameters_<O> = O extends { parameters: infer X } ? X : never
type PathParams<O> = Parameters_<O> extends { path: infer X } ? X : never
type QueryParams<O> = Parameters_<O> extends { query?: infer X } ? NonNullable<X> : never
type Body<O> = O extends { requestBody: { content: { 'application/json': infer B } } } ? B : never
type WithoutNever<T> = { [K in keyof T as [T[K]] extends [never] ? never : K]: T[K] }
/** 查询参数都可选时，query 本身也可以不给 */
type QueryArg<Q> = [Q] extends [never] ? object : object extends Q ? { query?: Q } : { query: Q }
type Args<O> = WithoutNever<{ path: PathParams<O>; body: Body<O> }> & QueryArg<QueryParams<O>>

export function call<P extends Path, M extends MethodOf<P>>(
  method: M,
  path: P,
  ...[args]: object extends Args<Operation<P, M>> ? [Args<Operation<P, M>>?] : [Args<Operation<P, M>>]
): Promise<Result<Operation<P, M>>> {
  const given = (args ?? {}) as { path?: Record<string, string | number>; query?: Query; body?: unknown }
  const url = path.replace(/\{(\w+)\}/g, (_, name: string) => {
    const value = given.path?.[name]
    if (value === undefined) throw new Error(`missing path parameter ${name} for ${path}`)
    return encodeURIComponent(String(value))
  })
  // 只有接口描述里有 JSON 请求体的调用才发请求体
  const body = given.body === undefined ? undefined : { type: 'application/json', data: JSON.stringify(given.body) }
  return send(method.toUpperCase(), url, given.query, body)
}

/** 把 AgentId 换回接口里的 string：品牌类型去掉品牌后必须就是接口推出的类型 */
type Unbranded<T> = T extends AgentId
  ? string
  : T extends readonly (infer U)[]
    ? Unbranded<U>[]
    : T extends object
      ? { [K in keyof T]: Unbranded<T[K]> }
      : T

/** 接口返回的 agent id 都是字符串；这里是给它们打上 AgentId 品牌的边界（字段由 WithAgentId 决定）。
 * 入参必须是 T 去掉品牌后的类型，所以 call 推出的响应类型仍然被检查，这里只收窄 agent id */
export const branded = <T>(p: Promise<Unbranded<T>>) => p as Promise<unknown> as Promise<T>

/** 前端路由：agent 页按 id（名称可改） */
export const agentRoute = (id: AgentId) => `/agents/${id}`
/** 会话页；通知点开的地址由后端 notifications.tables.Notification.url 生成，与它一致 */
export const sessionRoute = (id: string) => `/sessions/${id}`
export const newSessionRoute = (agentId: AgentId) => `/new?${new URLSearchParams({ agent: agentId })}`

export const api = {
  login: (password: string) => call('post', '/api/auth/login', { body: { password } }),
  logout: () => call('post', '/api/auth/logout'),
  me: () => call('get', '/api/auth/me'),

  agents: () => branded<Agent[]>(call('get', '/api/agents')),
  agentDefaults: () => call('get', '/api/agent-defaults'),
  /** 全部角色：内置在前（base 每个 agent 隐含），自建的按名称；各带只按它自己的规则能用的工具与条目 */
  roles: () => branded<Role[]>(call('get', '/api/roles')),
  createRole: (name: string, body: RoleInput) => call('post', '/api/roles/{name}', { path: { name }, body }),
  updateRole: (name: string, body: RoleInput) => call('put', '/api/roles/{name}', { path: { name }, body }),
  deleteRole: (name: string) => call('delete', '/api/roles/{name}', { path: { name } }),
  /** agent 配置（还没保存的也行）最终能用的工具与条目：与运行时同一套计算；agent_id 为 null 是还没建的 */
  previewAgent: (body: Schemas['AgentPreviewIn']) => call('post', '/api/agent-preview', { body }),
  /** 角色规则（还没保存的也行）能用的工具与条目；name 为 null 是还没建的 */
  previewRole: (body: Schemas['RolePreviewIn']) => call('post', '/api/role-preview', { body }),
  /** 全部条目：内置工具在前，skill、MCP server、secret 按种类、名称 */
  items: () => branded<CatalogItem[]>(call('get', '/api/items')),
  /** 把 skill / MCP server / secret 转给另一个 owner（null 归平台），记一版；内容与收录它的角色不变 */
  transferItem: (kind: StoredKind, name: string, owner: AgentId | null) =>
    call('put', '/api/items/{kind}/{name}/owner', { path: { kind, name }, body: { owner } }),
  /** 版本历史：新的在前，第一条即当前生效 */
  history: (target: RevisionTarget): Promise<Revision[]> =>
    target.kind === 'memory'
      ? call('get', '/api/memory/history', { query: { agent: target.agent } })
      : call('get', '/api/items/{kind}/{name}/history', { path: { kind: target.kind, name: target.name } }),
  /** 把某一版恢复为新的一版（secret 的版本不含值，不能恢复） */
  restore: (target: RevisionTarget, rev: number) =>
    target.kind === 'memory'
      ? call('post', '/api/memory/restore', { body: { agent: target.agent, rev } })
      : call('post', '/api/items/{kind}/{name}/restore', { path: { kind: target.kind, name: target.name }, body: { rev } }),
  /** agent 的工作历史：不给 query 按日期倒序；给了按关键词检索 */
  agentHistory: (agent_id: AgentId, query?: string) =>
    branded<HistoryEntry[]>(
      call('get', '/api/agents/{agent_id}/history', { path: { agent_id }, query: { query, limit: 200 } }),
    ),
  /** 服务端配置决定的可选能力（如语音输入） */
  features: () => call('get', '/api/features'),
  /** 服务端配置的 LLM provider 名 */
  providers: () => call('get', '/api/providers'),
  /** 向 provider 实时查询的模型名 */
  providerModels: (name: string) => call('get', '/api/providers/{name}/models', { path: { name } }),
  agent: (agent_id: AgentId) => branded<Agent>(call('get', '/api/agents/{agent_id}', { path: { agent_id } })),
  createAgent: (body: Schemas['AgentCreate']) => branded<Agent>(call('post', '/api/agents', { body })),
  updateAgent: (agent_id: AgentId, body: AgentInput) =>
    branded<Agent>(call('put', '/api/agents/{agent_id}', { path: { agent_id }, body })),
  deleteAgent: (agent_id: AgentId) => call('delete', '/api/agents/{agent_id}', { path: { agent_id } }),
  /** 设为（true）或取消（false）系统默认 agent：Web 新对话默认选中它 */
  setDefaultAgent: (agent_id: AgentId, on: boolean) =>
    branded<Agent>(
      on
        ? call('put', '/api/agents/{agent_id}/default', { path: { agent_id } })
        : call('delete', '/api/agents/{agent_id}/default', { path: { agent_id } }),
    ),
  /** 归档（true）或取消归档（false）：归档的不能对话、委派、指派，它的触发器不触发；它忙或是默认 agent 时 409 */
  setAgentArchived: (agent_id: AgentId, on: boolean) =>
    branded<Agent>(
      on
        ? call('put', '/api/agents/{agent_id}/archive', { path: { agent_id } })
        : call('delete', '/api/agents/{agent_id}/archive', { path: { agent_id } }),
    ),
  /** agent 为 null 时是全局记忆（所有 agent 共享） */
  memory: (agent: AgentId | null) => call('get', '/api/memory', { query: { agent } }),
  setMemory: (agent: AgentId | null, content: string) => call('put', '/api/memory', { body: { agent, content } }),

  skill: (name: string) => call('get', '/api/items/skill/{name}', { path: { name } }),
  /** 新建的归 owner；已有的整体改写正文与描述，owner 不变 */
  saveSkill: (name: string, body: Schemas['SkillIn']) => call('put', '/api/items/skill/{name}', { path: { name }, body }),
  deleteSkill: (name: string) => call('delete', '/api/items/skill/{name}', { path: { name } }),
  /** 标准 .skill 文件（zip）；失败时与其他调用一样抛 ApiError，不会把错误体当成文件存下 */
  exportSkill: (name: string) =>
    send<Blob>('GET', `/api/items/skill/${encodeURIComponent(name)}/package`, undefined, undefined, true),
  /** 导入标准 .skill 文件（名字取自包）；同名 skill 已存在时 409，replace 为 true 时覆盖 */
  importSkill: (owner: string | null, file: Blob, replace: boolean) =>
    send<Schemas['SkillImportOut']>('POST', '/api/items/skill', { owner, replace }, { type: 'application/zip', data: file }),

  /** origin_ref 是发起方在它那个 origin 里的身份：触发器的 id，用来列出某个触发器发起过的会话；q 是关键字（标题或消息含有它，不能为空串） */
  sessions: (query: {
    agent_id?: AgentId
    origin?: Origin[]
    origin_ref?: string
    q?: string
    limit?: number
    offset?: number
  }) =>
    branded<SessionListItem[]>(call('get', '/api/sessions', { query })),
  session: (session_id: string) =>
    branded<SessionDetail>(call('get', '/api/sessions/{session_id}', { path: { session_id } })),
  startSession: (agent_id: AgentId, prompt: string, attachments: string[]) =>
    branded<Session>(call('post', '/api/sessions', { body: { agent_id, prompt, attachments } })),
  reply: (session_id: string, prompt: string, attachments: string[]) =>
    call('post', '/api/sessions/{session_id}/messages', { path: { session_id }, body: { prompt, attachments, answers: {} } }),
  /** 回答挂起的提问：作为这次 ask_user 调用的结果交回模型 */
  answer: (session_id: string, toolCallId: string, answers: QuestionAnswer[]) =>
    call('post', '/api/sessions/{session_id}/messages', {
      path: { session_id },
      body: { prompt: '', attachments: [], answers: { [toolCallId]: answers } },
    }),
  /** 请求体就是文件内容，Content-Type 是它的类型（接口描述里没有 JSON 请求体）；浏览器认不出类型时 file.type 为空，按「未知二进制」上传 */
  uploadFile: (file: File) =>
    send<FileInfo>('POST', FILES_PATH, { name: file.name }, { type: file.type || 'application/octet-stream', data: file }),
  /** 请求体是一段录音，Content-Type 是它的类型；返回转写出的文字（听不到人说话时为空） */
  transcribe: (audio: Blob) =>
    send<Schemas['TranscriptOut']>('POST', '/api/transcriptions', undefined, { type: audio.type || 'audio/webm', data: audio }),
  /** 同时把指向这个会话的通知标为已读 */
  markRead: (session_id: string) => call('post', '/api/sessions/{session_id}/read', { path: { session_id } }),
  notifications: () => call('get', '/api/notifications'),
  unreadNotifications: () => call('get', '/api/notifications/unread'),
  /** 标记到 throughId（用户看到的最新一条）为止已读，之后才到的保持未读 */
  markNotificationsRead: (throughId: number) => call('post', '/api/notifications/read', { body: { through_id: throughId } }),
  /** 打开了 issue：请用户审核它的通知标为已读 */
  markIssueNotificationsRead: (issue_id: number) =>
    call('post', '/api/notifications/issues/{issue_id}/read', { path: { issue_id } }),
  /** 停止正在进行的运行；停止前已经产生的部分照常保存 */
  stop: (session_id: string) => call('post', '/api/sessions/{session_id}/stop', { path: { session_id } }),
  compact: (session_id: string) => call('post', '/api/sessions/{session_id}/compact', { path: { session_id } }),
  deleteSession: (session_id: string) => call('delete', '/api/sessions/{session_id}', { path: { session_id } }),

  tasks: (status?: TaskStatus) => branded<Task[]>(call('get', '/api/tasks', { query: { status } })),
  /** [since, until) 之间的 token 用量，按给出的维度分组；天按服务端配置的时区划分 */
  usage: (since: Date, until: Date, group_by: UsageGroup[]) =>
    branded<Usage[]>(
      call('get', '/api/usage', { query: { since: since.toISOString(), until: until.toISOString(), group_by } }),
    ),
  retryTask: (task_id: number) => branded<Task>(call('post', '/api/tasks/{task_id}/retry', { path: { task_id } })),

  triggerKinds: () => call('get', '/api/trigger-kinds'),
  /** finished 为 true 时列已结束的（跑完的一次性触发器），它们只能查看与删除 */
  triggers: (finished = false) => branded<Trigger[]>(call('get', '/api/triggers', { query: { finished } })),
  createTrigger: (body: Schemas['TriggerCreate'] & { agent_id: AgentId }) => branded<Trigger>(call('post', '/api/triggers', { body })),
  /** 只修改给出的字段；precheck 不给表示不变，给 null 表示去掉 */
  updateTrigger: (trigger_id: number, body: Schemas['TriggerPatch']) =>
    branded<Trigger>(call('patch', '/api/triggers/{trigger_id}', { path: { trigger_id }, body })),
  /** session_id 为 null：前置检查判定这次不调用 agent */
  runTrigger: (trigger_id: number) => call('post', '/api/triggers/{trigger_id}/run', { path: { trigger_id } }),
  /** webhook：生成或重新生成 token，明文只返回这一次 */
  generateTriggerToken: (trigger_id: number) =>
    call('post', '/api/triggers/{trigger_id}/webhook/token', { path: { trigger_id } }),
  deleteTrigger: (trigger_id: number) => call('delete', '/api/triggers/{trigger_id}', { path: { trigger_id } }),
  /** 新增（启用的）MCP server；同名已存在 409 */
  addMcp: (name: string, body: McpInput) =>
    branded<McpServer>(call('put', '/api/items/mcp/{name}', { path: { name }, body })),
  probeMcp: (name: string) => call('post', '/api/items/mcp/{name}/tools', { path: { name } }),
  setMcpEnabled: (name: string, enabled: boolean) =>
    branded<McpServer>(call('put', '/api/items/mcp/{name}/enabled', { path: { name }, body: { enabled } })),
  deleteMcp: (name: string) => call('delete', '/api/items/mcp/{name}', { path: { name } }),

  /** 新建的归 owner（null 为平台）；已有的改值与描述，owner 不变 */
  setSecret: (owner: AgentId | null, key: string, value: string, description: string) =>
    call('put', '/api/items/secret/{name}', { path: { name: key }, body: { owner, value, description } }),
  deleteSecret: (key: string) => call('delete', '/api/items/secret/{name}', { path: { name: key } }),

  /** owner 给出时只列这个项目（agent）负责的 issue */
  issues: (owner?: string) => call('get', '/api/issues', { query: { owner } }),
  issue: (issue_id: number) => call('get', '/api/issues/{issue_id}', { path: { issue_id } }),
  createIssue: (body: Schemas['IssueCreate']) => call('post', '/api/issues', { body }),
  /** 只修改给出的字段；assignee 给 null 表示取消指派 */
  updateIssue: (issue_id: number, body: Schemas['IssuePatch']) =>
    call('patch', '/api/issues/{issue_id}', { path: { issue_id }, body }),
  commentIssue: (issue_id: number, body: string) =>
    call('post', '/api/issues/{issue_id}/comments', { path: { issue_id }, body: { body } }),
  deleteIssue: (issue_id: number) => call('delete', '/api/issues/{issue_id}', { path: { issue_id } }),
  /** 审核指派给用户的 issue：通过 → done；打回 → 理由记成评论、回到 in_progress、指派回交给用户的 agent */
  approveIssue: (issue_id: number) => call('post', '/api/issues/{issue_id}/approve', { path: { issue_id } }),
  rejectIssue: (issue_id: number, reason: string) =>
    call('post', '/api/issues/{issue_id}/reject', { path: { issue_id }, body: { reason } }),

  vapidKey: () => call('get', '/api/push/vapid'),
  subscribePush: (body: Schemas['PushSubscriptionIn']) => call('post', '/api/push/subscriptions', { body }),
  testPush: () => call('post', '/api/push/test'),
}
