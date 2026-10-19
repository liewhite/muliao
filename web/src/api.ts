// 与后端 src/muliao/api/*.py 的响应模型一一对应

export type Origin = 'user' | 'trigger' | 'delegation'
export type TaskStatus = 'pending' | 'running' | 'done' | 'failed'
export type TaskKind = 'turn' | 'reflection' | 'deliver' | 'compaction'

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
  turns_since_reflection: number
  created_at: string
}

export interface AgentInput {
  name: string
  description: string
  model: string | null
  instructions: string
  reflect_every: number | null
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

export type ItemType = 'user' | 'assistant' | 'thinking' | 'tool_call' | 'tool_result' | 'tool_error'

export interface Item {
  type: ItemType
  text: string
  tool_name: string | null
  tool_call_id: string | null
  message_id: number | null
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
  prompt: string | null
  not_before: string
  created_at: string
  finished_at: string | null
}

export interface SessionDetail {
  session: Session
  items: Item[]
  compactions: CompactionInfo[]
  open_turns: Task[]
  threads: Session[]
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
  last_fired_at: string | null
  last_session_id: string | null
  last_error: string | null
}

export interface PromptRevision {
  id: number
  content: string
  /** "user"，或做出修改的 agent 的 id */
  author: string
  created_at: string
}

export interface McpServer {
  id: number
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

export type McpInput = Omit<McpServer, 'id' | 'enabled' | 'tools'> & { probe: boolean }

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
  const resp = await fetch(withQuery(path, query), {
    method,
    credentials: 'same-origin',
    headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
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

export interface AgentDefaults {
  model: string
  reflect_every: number
}

export const api = {
  login: (password: string) => post<void>('/api/auth/login', { password }),
  logout: () => post<void>('/api/auth/logout'),
  me: () => get<void>('/api/auth/me'),

  agents: () => get<Agent[]>('/api/agents'),
  agentDefaults: () => get<AgentDefaults>('/api/agent-defaults'),
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
  startSession: (agent_id: AgentId, prompt: string) => post<Session>('/api/sessions', { agent_id, prompt }),
  reply: (id: string, prompt: string) => post<Task>(`/api/sessions/${id}/messages`, { prompt }),
  markRead: (id: string) => post<void>(`/api/sessions/${id}/read`),
  compact: (id: string) => post<{ queued: boolean }>(`/api/sessions/${id}/compact`),
  deleteSession: (id: string) => del(`/api/sessions/${id}`),

  tasks: (status?: TaskStatus) => get<Task[]>('/api/tasks', { status }),
  retryTask: (id: number) => post<Task>(`/api/tasks/${id}/retry`),

  triggerKinds: () => get<TriggerKind[]>('/api/trigger-kinds'),
  triggers: () => get<Trigger[]>('/api/triggers'),
  createTrigger: (t: { name: string; agent_id: AgentId; kind: string; prompt: string; config: Record<string, unknown> }) =>
    post<Trigger>('/api/triggers', t),
  /** 只修改给出的字段 */
  updateTrigger: (id: number, patch: { prompt?: string; config?: Record<string, unknown>; enabled?: boolean }) =>
    request<Trigger>('PATCH', `/api/triggers/${id}`, patch),
  runTrigger: (id: number) => post<{ session_id: string }>(`/api/triggers/${id}/run`),
  /** webhook：生成或重新生成 token，明文只返回这一次 */
  generateTriggerToken: (id: number) => post<{ token: string }>(`/api/triggers/${id}/token`),
  deleteTrigger: (id: number) => del(`/api/triggers/${id}`),
  /** owner 为空时是公共 prompt 的历史；新的在前，第一条即当前生效 */
  promptHistory: (owner: AgentId | null) =>
    get<PromptRevision[]>('/api/prompts', owner === null ? {} : { owner }),
  setPublicPrompt: (content: string) => put<void>('/api/prompts/public', { content }),
  restorePrompt: (revisionId: number) => post<void>(`/api/prompt-revisions/${revisionId}/restore`),
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
