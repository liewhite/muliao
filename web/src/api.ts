// 接口类型取自 schema.d.ts（由后端的 OpenAPI 描述生成，见 scripts/gen-api.mjs），这里只起名字、补上品牌类型

import type { components, paths } from './schema'

export type Schemas = components['schemas']

export type Origin = Schemas['Origin']
export type TaskStatus = Schemas['TaskStatus']
export type TaskKind = Schemas['TaskKind']

/** agent 的 id 与名称都是字符串；品牌类型让编译器拦住「把名称当 id 传」 */
export type AgentId = string & { readonly __brand: 'AgentId' }

/** 仅用于边界：路由参数、取值为 id 的下拉框 */
export const asAgentId = (value: string) => value as AgentId

/** 把接口类型里是 agent id 的字段换成 AgentId（可为空的保持可为空） */
type WithAgentId<T, K extends keyof T> = Omit<T, K> & { [P in K]: null extends T[P] ? AgentId | null : AgentId }

export type Agent = WithAgentId<Schemas['AgentOut'], 'id'>
/** 修改 agent 的请求体：整体替换，字段全部必填（新建用 Schemas['AgentCreate']） */
export type AgentInput = Schemas['AgentUpdate']
export type Session = WithAgentId<Schemas['SessionOut'], 'agent_id'>
/** 一条通知（不叫 Notification：那是浏览器的全局对象） */
export type AppNotification = Schemas['NotificationOut']

/** 引用文件的地址前缀（与后端 muliao.files.FILES_PATH 一致）：回答里只有指向这里的图片会被渲染 */
export const FILES_PATH = '/api/files'

export type FileInfo = Schemas['FileOut']
export type Attachment = Schemas['Attachment']
export type Item = Schemas['Item']
export type CompactionInfo = Schemas['CompactionOut']
export type Task = WithAgentId<Schemas['TaskOut'], 'agent_id'>

/** 一个分组的用量汇总；没有参与分组的维度为 null。input_tokens 已含缓存读写，不要相加 */
export type Usage = WithAgentId<Schemas['UsageOut'], 'agent_id'>
export type UsageGroup = Schemas['UsageGroup']

export type QuestionOption = Schemas['Option']
export type Question = Schemas['Question']
export type QuestionAnswer = Schemas['QuestionAnswer']
export type AnsweredQuestion = Schemas['AnsweredQuestion']
export type AskView = Schemas['AskView']

export type MailKind = Schemas['MailKind']
export type MailState = Schemas['MailState']
export type Mail = Schemas['MailOut']
export type SessionDetail = Omit<Schemas['SessionDetail'], 'session' | 'run' | 'threads'> & {
  session: Session
  run: Task | null
  threads: Session[]
}
export type LiveRun = Schemas['LiveRunOut']

export type SkillMeta = Schemas['SkillOut']
export type Skill = Schemas['SkillDetail']
export type Memory = Schemas['MemoryOut']
export type Reflection = Schemas['ReflectionOut']
export type TriggerKind = Schemas['TriggerKindOut']
export type Precheck = Schemas['Precheck']
export type Trigger = WithAgentId<Schemas['TriggerOut'], 'agent_id'>
export type ResourceKind = Schemas['ResourceKind']
export type Revision = Schemas['RevisionOut']
export type McpServer = WithAgentId<Schemas['McpOut'], 'owner'>
export type McpInput = WithAgentId<Schemas['McpIn'], 'owner'>
export type SecretInfo = WithAgentId<Schemas['SecretOut'], 'owner'>
/** 一个可选的工具组（后端 tool_catalog） */
export type ToolGroup = Schemas['ToolGroupOut']
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
async function send<T>(method: string, path: string, query: Query | undefined, body?: { type: string; data: BodyInit }): Promise<T> {
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

/** 前端路由：agent 名称可含中文，必须编码 */
export const agentRoute = (id: AgentId) => `/agents/${id}`
/** 会话页；推送通知点开的地址由后端 delivery/webpush.py 生成，与它保持一致 */
export const sessionRoute = (id: string) => `/sessions/${id}`
export const newSessionRoute = (agentId: AgentId) => `/new?${new URLSearchParams({ agent: agentId })}`

export const api = {
  login: (password: string) => call('post', '/api/auth/login', { body: { password } }),
  logout: () => call('post', '/api/auth/logout'),
  me: () => call('get', '/api/auth/me'),

  agents: () => branded<Agent[]>(call('get', '/api/agents')),
  agentDefaults: () => call('get', '/api/agent-defaults'),
  /** 服务端配置的 LLM provider 名 */
  providers: () => call('get', '/api/providers'),
  /** 向 provider 实时查询的模型名 */
  providerModels: (name: string) => call('get', '/api/providers/{name}/models', { path: { name } }),
  agent: (agent_id: AgentId) => branded<Agent>(call('get', '/api/agents/{agent_id}', { path: { agent_id } })),
  createAgent: (body: Schemas['AgentCreate']) => branded<Agent>(call('post', '/api/agents', { body })),
  updateAgent: (agent_id: AgentId, body: AgentInput) =>
    branded<Agent>(call('put', '/api/agents/{agent_id}', { path: { agent_id }, body })),
  deleteAgent: (agent_id: AgentId) => call('delete', '/api/agents/{agent_id}', { path: { agent_id } }),
  memory: (agent_id: AgentId) => call('get', '/api/agents/{agent_id}/memory', { path: { agent_id } }),
  setMemory: (agent_id: AgentId, content: string) =>
    call('put', '/api/agents/{agent_id}/memory', { path: { agent_id }, body: { content } }),
  reflections: (agent_id: AgentId) => call('get', '/api/agents/{agent_id}/reflections', { path: { agent_id } }),

  skills: (owner: string | null) => call('get', '/api/skills', { query: { owner } }),
  skill: (owner: string | null, name: string) => call('get', '/api/skill', { query: { owner, name } }),
  saveSkill: (body: Schemas['SkillIn']) => call('put', '/api/skill', { body }),
  deleteSkill: (owner: string | null, name: string) => call('delete', '/api/skill', { query: { owner, name } }),

  sessions: (query: { agent_id?: AgentId; origin?: Origin[]; limit?: number; offset?: number }) =>
    branded<Session[]>(call('get', '/api/sessions', { query })),
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
  /** 同时把指向这个会话的通知标为已读 */
  markRead: (session_id: string) => call('post', '/api/sessions/{session_id}/read', { path: { session_id } }),
  notifications: () => call('get', '/api/notifications'),
  unreadNotifications: () => call('get', '/api/notifications/unread'),
  /** 标记到 throughId（用户看到的最新一条）为止已读，之后才到的保持未读 */
  markNotificationsRead: (throughId: number) => call('post', '/api/notifications/read', { body: { through_id: throughId } }),
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
  triggers: () => branded<Trigger[]>(call('get', '/api/triggers')),
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
  /** owner 为空时是公共资源；新的在前，第一条即当前生效 */
  history: (kind: ResourceKind, owner: AgentId | null) =>
    call('get', '/api/resources/{kind}/history', { path: { kind }, query: { owner } }),
  restore: (kind: ResourceKind, owner: AgentId | null, rev: string) =>
    call('post', '/api/resources/{kind}/restore', { path: { kind }, body: { owner, rev } }),
  setPublicPrompt: (content: string) => call('put', '/api/prompts/public', { body: { content } }),
  mcp: () => branded<McpServer[]>(call('get', '/api/mcp')),
  addMcp: (body: McpInput) => branded<McpServer>(call('post', '/api/mcp', { body })),
  probeMcp: (owner: AgentId | null, name: string) => call('post', '/api/mcp/tools', { query: { owner, name } }),
  setMcpEnabled: (owner: AgentId | null, name: string, enabled: boolean) =>
    branded<McpServer>(call('put', '/api/mcp/enabled', { query: { owner, name }, body: { enabled } })),
  deleteMcp: (owner: AgentId | null, name: string) => call('delete', '/api/mcp', { query: { owner, name } }),

  secrets: () => branded<SecretInfo[]>(call('get', '/api/vault')),
  setSecret: (owner: AgentId | null, key: string, value: string, description: string) =>
    call('put', '/api/vault/{key}', { path: { key }, query: { owner }, body: { value, description } }),
  deleteSecret: (owner: AgentId | null, key: string) => call('delete', '/api/vault/{key}', { path: { key }, query: { owner } }),

  vapidKey: () => call('get', '/api/push/vapid'),
  subscribePush: (body: Schemas['PushSubscriptionIn']) => call('post', '/api/push/subscriptions', { body }),
  testPush: () => call('post', '/api/push/test'),
}
