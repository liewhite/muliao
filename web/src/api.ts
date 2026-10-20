// 与后端 src/muliao/api/*.py 的响应模型一一对应

import type { LiveRun } from './events'

export type Origin = 'user' | 'trigger' | 'delegation'
export type TaskStatus = 'pending' | 'running' | 'done' | 'failed'
export type TaskKind = 'run' | 'reflection' | 'deliver' | 'compaction'

/** agent 的 id 与名称都是字符串；品牌类型让编译器拦住「把名称当 id 传」 */
export type AgentId = string & { readonly __brand: 'AgentId' }

/** 仅用于边界：路由参数、取值为 id 的下拉框 */
export const asAgentId = (value: string) => value as AgentId

export interface Agent {
  /** 系统生成、不可变，用于所有引用；name 只用于展示 */
  id: AgentId
  name: string
  description: string
  model: string
  instructions: string
  reflect_every: number
  /** 选用的工具组；自我改进（记忆、skill）总是开放 */
  tool_groups: string[]
  /** 自上次反思以来的步数（模型回复数） */
  steps_since_reflection: number
  created_at: string
}

export interface AgentInput {
  name: string
  description: string
  model: string | null
  instructions: string
  reflect_every: number | null
  tool_groups: string[]
}

export interface Session {
  id: string
  agent_id: AgentId
  title: string
  origin: Origin
  origin_ref: string | null
  /** 委派产生的子会话：父会话与 thread 名 */
  parent_session_id: string | null
  thread: string | null
  /** ask_agent 开出的 thread：只属于发起方 agent，用户只读 */
  private: boolean
  unread: boolean
  created_at: string
  updated_at: string
}

/** 一条通知（不叫 Notification：那是浏览器的全局对象） */
export interface AppNotification {
  id: number
  title: string
  body: string
  /** 点开打开的会话；系统通知（前置检查告警、推送测试）为 null */
  session_id: string | null
  unread: boolean
  created_at: string
}

/** 引用文件的地址前缀（与后端 muliao.files.FILES_PATH 一致）：回答里只有指向这里的图片会被渲染 */
export const FILES_PATH = '/api/files'

/** 对话里的一个文件（后端 api/files.py 的 FileOut） */
export interface FileInfo {
  id: string
  name: string
  media_type: string
  size: number
  url: string
  /** 能内联显示的图片；其余是下载链接 */
  inline: boolean
  created_at: string
}

/** 消息里的附件（后端 conventions.Attachment） */
export interface Attachment {
  url: string
  name: string
  media_type: string
  inline: boolean
}

/** stopped：用户停止了这一轮（后端 conventions.TURN_STOPPED），出现在那一轮保存下来的内容之后 */
/** event：会话事件（后台任务完成、运行中的检查……），tool_name 是来源（如 job:12），text 是内容 */
export type ItemType = 'user' | 'event' | 'assistant' | 'thinking' | 'tool_call' | 'tool_result' | 'tool_error' | 'stopped'

export interface Item {
  type: ItemType
  text: string
  tool_name: string | null
  tool_call_id: string | null
  message_id: number | null
  /** 用户消息的附件，或工具结果挂上的文件；其他条目为空 */
  attachments: Attachment[]
  /** 工具结果关联的子会话 id（委派开出或续写的 thread）；其他条目为空 */
  threads: string[]
}

export interface CompactionInfo {
  through_message_id: number
  summary: string
  created_at: string
}

export interface Task {
  id: number
  kind: TaskKind
  agent_id: AgentId | null
  session_id: string | null
  status: TaskStatus
  attempts: number
  error: string | null
  not_before: string
  created_at: string
  finished_at: string | null
}

/** ask_user 提问（后端 runtime/questions.py 的 Question）；工具名与后端 ASK_USER 一致 */
export const ASK_USER = 'ask_user'

export interface QuestionOption {
  label: string
  description: string
}

export interface Question {
  question: string
  header: string
  options: QuestionOption[]
  multi_select: boolean
}

/** 用户对一题的回答：选中的选项和/或「其他」里写的文字 */
export interface QuestionAnswer {
  selected: string[]
  other: string | null
}

export interface AnsweredQuestion {
  question: string
  selected: string[]
  other: string | null
}

/** 一次提问交给模型的结果（后端 runtime/questions.py 的 AskResult）：用户回答了，或没回答就发了新消息 */
export interface AskResult {
  answered: boolean
  answers: AnsweredQuestion[]
  note: string | null
}

export interface PendingQuestions {
  tool_call_id: string
  questions: Question[]
}

export type MailKind = 'message' | 'answer' | 'event'
export type MailState = 'queued' | 'consumed' | 'discarded'

/** 会话信箱里的一封信件（后端 runtime/mailbox.py） */
export interface Mail {
  id: number
  kind: MailKind
  /** 事件的发送方（如 job:12）；用户消息与回答为 null */
  source: string | null
  content: string
  attachments: FileInfo[]
  /** 回答信件：tool_call_id -> 交给模型的结果 */
  answers: Record<string, AskResult>
  state: MailState
  created_at: string
}

export interface SessionDetail {
  session: Session
  items: Item[]
  compactions: CompactionInfo[]
  /** 显示在历史之后的信件：排队中的，以及最近一次停止丢弃的 */
  mail: Mail[]
  /** 最近一次运行，还没正常结束时（排队、运行中、等重试或失败） */
  run: Task | null
  threads: Session[]
  /** 还在等回答、也还没提交回答的提问 */
  pending_questions: PendingQuestions[]
  /** 正在进行的运行当前这一段的事件；没有则为 null */
  live: LiveRun | null
}

export interface SkillMeta {
  name: string
  description: string
  owner: string | null
}

export interface Skill extends SkillMeta {
  body: string
}

export interface Memory {
  content: string
  max_chars: number
}

export interface Reflection {
  id: number
  from_message_id: number
  to_message_id: number
  output: string
  created_at: string
}

export interface TriggerKind {
  name: string
  /** 什么时候触发、config 的格式 */
  description: string
}

/** 触发前的前置检查（后端 triggers/precheck.py 的 Precheck） */
export interface Precheck {
  command: string
  secrets: string[]
  /** output：有输出就触发；changed：输出与上次不同才触发（首次只记基线） */
  fire_when: 'output' | 'changed'
}

export interface Trigger {
  id: number
  name: string
  agent_id: AgentId
  /** 触发方式，如 schedule、webhook；方式的差异只在 config 与 status */
  kind: string
  prompt: string
  config: Record<string, unknown>
  /** 这种方式自己给出的运行状态，如下次触发时间、webhook 地址；不含凭证 */
  status: Record<string, unknown>
  enabled: boolean
  /** 前置检查；null 表示每次都交给 agent */
  precheck: Precheck | null
  last_checked_at: string | null
  last_fired_at: string | null
  last_session_id: string | null
  last_error: string | null
}

/** agent 能改的、有版本历史的文本资源 */
export type ResourceKind = 'prompt' | 'memory'

export interface Revision {
  /** git 提交哈希 */
  rev: string
  content: string
  /** "user"，或做出修改的 agent 的 id */
  author: string
  created_at: string
}

export interface McpServer {
  owner: AgentId | null
  name: string
  transport: 'stdio' | 'http'
  command: string | null
  args: string[]
  url: string | null
  env: Record<string, string>
  headers: Record<string, string>
  enabled: boolean
  tools: string[]
}

export type McpInput = Omit<McpServer, 'enabled' | 'tools'> & { probe: boolean }

export interface SecretInfo {
  /** null 为公共 */
  owner: AgentId | null
  key: string
  description: string
  updated_at: string
}

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
export const newSessionRoute = (agentId: AgentId) => `/new?${new URLSearchParams({ agent: agentId })}`

/** 一个可选的工具组（后端 tool_catalog） */
export interface ToolGroup {
  name: string
  label: string
  /** 开了它就能间接做任何事：给处理不可信输入的 agent 开要谨慎 */
  full_access: boolean
  /** 新建 agent 时默认开启 */
  default: boolean
}

export interface AgentDefaults {
  model: string
  reflect_every: number
  /** 当前配置下可选的工具组，按目录顺序 */
  groups: ToolGroup[]
}

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
  createAgent: (input: AgentInput) => post<Agent>('/api/agents', input),
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
