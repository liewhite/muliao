// 接口类型取自 schema.d.ts（由后端的 OpenAPI 描述生成，见 scripts/gen-api.mjs），这里只起名字、补上品牌类型

import type { components } from './schema'

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
/** agent 表单总是给出全部字段：新建与修改共用 */
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

export type QuestionOption = Schemas['Option']
export type Question = Schemas['Question']
export type QuestionAnswer = Schemas['QuestionAnswer']
export type AnsweredQuestion = Schemas['AnsweredQuestion']
export type AskResult = Schemas['AskResult']
export type AgentAnswer = Schemas['AgentAnswer']
export type AskView = Schemas['AskView']

/** 回答信件里一次调用的结果：ask_user 是用户的回答，ask_agent 是对方的结局。两者字段不重叠，按形状区分（后端 runtime.pending.CallResult） */
export const isAskResult = (result: AskResult | AgentAnswer): result is AskResult => 'answered' in result

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

async function request<T>(method: string, path: string, body?: unknown, query?: Query): Promise<T> {
  return send<T>(method, path, query, body === undefined ? undefined : { type: 'application/json', data: JSON.stringify(body) })
}

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
  if (resp.status === 204) return undefined as T
  return (await resp.json()) as T
}

const get = <T>(path: string, query?: Query) => request<T>('GET', path, undefined, query)
const post = <T>(path: string, body?: unknown, query?: Query) => request<T>('POST', path, body ?? {}, query)
const put = <T>(path: string, body: unknown, query?: Query) => request<T>('PUT', path, body, query)
const del = (path: string, query?: Query) => request<void>('DELETE', path, undefined, query)

/** 前端路由：agent 名称可含中文，必须编码 */
export const agentRoute = (id: AgentId) => `/agents/${id}`
/** 会话页；推送通知点开的地址由后端 delivery/webpush.py 生成，与它保持一致 */
export const sessionRoute = (id: string) => `/sessions/${id}`
export const newSessionRoute = (agentId: AgentId) => `/new?${new URLSearchParams({ agent: agentId })}`

export const api = {
  login: (password: string) => post<void>('/api/auth/login', { password }),
  logout: () => post<void>('/api/auth/logout'),
  me: () => get<void>('/api/auth/me'),

  agents: () => get<Agent[]>('/api/agents'),
  agentDefaults: () => get<AgentDefaults>('/api/agent-defaults'),
  /** 服务端配置的 LLM provider 名 */
  providers: () => get<string[]>('/api/providers'),
  /** 向 provider 实时查询的模型名 */
  providerModels: (provider: string) => get<string[]>(`/api/providers/${encodeURIComponent(provider)}/models`),
  agent: (id: AgentId) => get<Agent>(`/api/agents/${id}`),
  createAgent: (input: Schemas['AgentCreate']) => post<Agent>('/api/agents', input),
  updateAgent: (id: AgentId, input: AgentInput) => put<Agent>(`/api/agents/${id}`, input),
  deleteAgent: (id: AgentId) => del(`/api/agents/${id}`),
  memory: (id: AgentId) => get<Memory>(`/api/agents/${id}/memory`),
  setMemory: (id: AgentId, content: string) => put<Memory>(`/api/agents/${id}/memory`, { content }),
  reflections: (id: AgentId) => get<Reflection[]>(`/api/agents/${id}/reflections`),

  skills: (owner: string | null) => get<SkillMeta[]>('/api/skills', { owner }),
  skill: (owner: string | null, name: string) => get<Skill>('/api/skill', { owner, name }),
  saveSkill: (skill: Skill) => put<SkillMeta>('/api/skill', skill),
  deleteSkill: (owner: string | null, name: string) => del('/api/skill', { owner, name }),

  sessions: (query: { agent_id?: AgentId; origin?: Origin[]; limit?: number; offset?: number }) =>
    get<Session[]>('/api/sessions', query),
  session: (id: string) => get<SessionDetail>(`/api/sessions/${id}`),
  startSession: (agent_id: AgentId, prompt: string, attachments: string[]) =>
    post<Session>('/api/sessions', { agent_id, prompt, attachments }),
  reply: (id: string, prompt: string, attachments: string[]) =>
    post<Mail[]>(`/api/sessions/${id}/messages`, { prompt, attachments }),
  /** 回答挂起的提问：作为这次 ask_user 调用的结果交回模型 */
  answer: (id: string, toolCallId: string, answers: QuestionAnswer[]) =>
    post<Mail[]>(`/api/sessions/${id}/messages`, { prompt: '', answers: { [toolCallId]: answers } }),
  /** 请求体就是文件内容，Content-Type 是它的类型；浏览器认不出类型时 file.type 为空，按「未知二进制」上传 */
  uploadFile: (file: File) =>
    send<FileInfo>('POST', FILES_PATH, { name: file.name }, { type: file.type || 'application/octet-stream', data: file }),
  /** 同时把指向这个会话的通知标为已读 */
  markRead: (id: string) => post<void>(`/api/sessions/${id}/read`),
  notifications: () => get<AppNotification[]>('/api/notifications'),
  unreadNotifications: () => get<{ count: number }>('/api/notifications/unread'),
  /** 标记到 throughId（用户看到的最新一条）为止已读，之后才到的保持未读 */
  markNotificationsRead: (throughId: number) => post<void>('/api/notifications/read', { through_id: throughId }),
  /** 停止正在运行的一轮；停止前已经产生的部分照常保存 */
  stop: (id: string) => post<void>(`/api/sessions/${id}/stop`),
  compact: (id: string) => post<{ queued: boolean }>(`/api/sessions/${id}/compact`),
  deleteSession: (id: string) => del(`/api/sessions/${id}`),

  tasks: (status?: TaskStatus) => get<Task[]>('/api/tasks', { status }),
  retryTask: (id: number) => post<Task>(`/api/tasks/${id}/retry`),

  triggerKinds: () => get<TriggerKind[]>('/api/trigger-kinds'),
  triggers: () => get<Trigger[]>('/api/triggers'),
  createTrigger: (t: {
    name: string
    agent_id: AgentId
    kind: string
    prompt: string
    config: Record<string, unknown>
    precheck: Precheck | null
  }) =>
    post<Trigger>('/api/triggers', t),
  /** 只修改给出的字段 */
  /** precheck 不给表示不变，给 null 表示去掉 */
  updateTrigger: (
    id: number,
    patch: { prompt?: string; config?: Record<string, unknown>; enabled?: boolean; precheck?: Precheck | null },
  ) =>
    request<Trigger>('PATCH', `/api/triggers/${id}`, patch),
  /** session_id 为 null：前置检查判定这次不调用 agent */
  runTrigger: (id: number) => post<{ session_id: string | null }>(`/api/triggers/${id}/run`),
  /** webhook：生成或重新生成 token，明文只返回这一次 */
  generateTriggerToken: (id: number) => post<{ token: string }>(`/api/triggers/${id}/webhook/token`),
  deleteTrigger: (id: number) => del(`/api/triggers/${id}`),
  /** owner 为空时是公共 prompt 的历史；新的在前，第一条即当前生效 */
  /** owner 为空时是公共资源；新的在前，第一条即当前生效 */
  history: (kind: ResourceKind, owner: AgentId | null) =>
    get<Revision[]>(`/api/resources/${kind}/history`, owner === null ? {} : { owner }),
  restore: (kind: ResourceKind, owner: AgentId | null, rev: string) =>
    post<void>(`/api/resources/${kind}/restore`, { owner, rev }),
  setPublicPrompt: (content: string) => put<void>('/api/prompts/public', { content }),
  mcp: () => get<McpServer[]>('/api/mcp'),
  addMcp: (input: McpInput) => post<McpServer>('/api/mcp', input),
  probeMcp: (owner: AgentId | null, name: string) => post<string[]>('/api/mcp/tools', undefined, { owner, name }),
  setMcpEnabled: (owner: AgentId | null, name: string, enabled: boolean) =>
    put<McpServer>('/api/mcp/enabled', { enabled }, { owner, name }),
  deleteMcp: (owner: AgentId | null, name: string) => del('/api/mcp', { owner, name }),

  secrets: () => get<SecretInfo[]>('/api/vault'),
  setSecret: (owner: AgentId | null, key: string, value: string, description: string) =>
    put<void>(`/api/vault/${encodeURIComponent(key)}`, { value, description }, { owner }),
  deleteSecret: (owner: AgentId | null, key: string) => del(`/api/vault/${encodeURIComponent(key)}`, { owner }),

  vapidKey: () => get<{ public_key: string }>('/api/push/vapid'),
  subscribePush: (sub: PushSubscriptionJSON) => post<void>('/api/push/subscriptions', sub),
  testPush: () => post<void>('/api/push/test'),
}
