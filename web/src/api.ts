// 与后端 src/muliao/api/*.py 的响应模型一一对应

export type Origin = 'user' | 'cron' | 'webhook' | 'telegram' | 'delegation'
export type TaskStatus = 'pending' | 'running' | 'done' | 'failed'
export type TaskKind = 'turn' | 'reflection' | 'deliver'

export interface Agent {
  name: string
  description: string
  model: string
  instructions: string
  reflect_every: number
  turns_since_reflection: number
  created_at: string
}

export interface AgentInput {
  description: string
  model: string | null
  instructions: string
  reflect_every: number | null
}

export interface Session {
  id: string
  agent_name: string
  title: string
  origin: Origin
  origin_ref: string | null
  delivery: 'none' | 'webpush' | 'telegram'
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
}

export interface Task {
  id: number
  kind: TaskKind
  agent_name: string | null
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
  open_turns: Task[]
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

export interface CronJob {
  id: number
  name: string
  agent_name: string
  schedule: string
  prompt: string
  enabled: boolean
  next_run_at: string
  last_run_at: string | null
  last_session_id: string | null
  last_error: string | null
}

export interface Webhook {
  id: string
  name: string
  agent_name: string
  prompt: string
  url: string
  token: string
}

export interface McpServer {
  id: number
  owner: string | null
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

export const api = {
  login: (password: string) => post<void>('/api/auth/login', { password }),
  logout: () => post<void>('/api/auth/logout'),
  me: () => get<void>('/api/auth/me'),

  agents: () => get<Agent[]>('/api/agents'),
  agent: (name: string) => get<Agent>(`/api/agents/${name}`),
  createAgent: (name: string, input: AgentInput) => post<Agent>('/api/agents', { name, ...input }),
  updateAgent: (name: string, input: AgentInput) => put<Agent>(`/api/agents/${name}`, input),
  deleteAgent: (name: string) => del(`/api/agents/${name}`),
  memory: (name: string) => get<Memory>(`/api/agents/${name}/memory`),
  setMemory: (name: string, content: string) => put<Memory>(`/api/agents/${name}/memory`, { content }),
  reflections: (name: string) => get<Reflection[]>(`/api/agents/${name}/reflections`),

  skills: (owner: string | null) => get<SkillMeta[]>('/api/skills', { owner }),
  skill: (owner: string | null, name: string) => get<Skill>('/api/skill', { owner, name }),
  saveSkill: (skill: Skill) => put<SkillMeta>('/api/skill', skill),
  deleteSkill: (owner: string | null, name: string) => del('/api/skill', { owner, name }),

  sessions: (query: { agent?: string; origin?: Origin[]; limit?: number; offset?: number }) =>
    get<Session[]>('/api/sessions', query),
  session: (id: string) => get<SessionDetail>(`/api/sessions/${id}`),
  startSession: (agent_name: string, prompt: string) => post<Session>('/api/sessions', { agent_name, prompt }),
  reply: (id: string, prompt: string) => post<Task>(`/api/sessions/${id}/messages`, { prompt }),
  markRead: (id: string) => post<void>(`/api/sessions/${id}/read`),
  deleteSession: (id: string) => del(`/api/sessions/${id}`),

  tasks: (status?: TaskStatus) => get<Task[]>('/api/tasks', { status }),
  retryTask: (id: number) => post<Task>(`/api/tasks/${id}/retry`),

  cron: () => get<CronJob[]>('/api/cron'),
  createCron: (job: { name: string; agent_name: string; schedule: string; prompt: string }) =>
    post<CronJob>('/api/cron', job),
  updateCron: (id: number, job: { schedule: string; prompt: string; enabled: boolean }) =>
    put<CronJob>(`/api/cron/${id}`, job),
  deleteCron: (id: number) => del(`/api/cron/${id}`),

  webhooks: () => get<Webhook[]>('/api/webhooks'),
  createWebhook: (hook: { name: string; agent_name: string; prompt: string }) => post<Webhook>('/api/webhooks', hook),
  deleteWebhook: (id: string) => del(`/api/webhooks/${id}`),

  mcp: () => get<McpServer[]>('/api/mcp'),
  addMcp: (input: McpInput) => post<McpServer>('/api/mcp', input),
  probeMcp: (owner: string | null, name: string) => post<string[]>('/api/mcp/tools', undefined, { owner, name }),
  setMcpEnabled: (owner: string | null, name: string, enabled: boolean) =>
    put<McpServer>('/api/mcp/enabled', { enabled }, { owner, name }),
  deleteMcp: (owner: string | null, name: string) => del('/api/mcp', { owner, name }),

  secrets: () => get<SecretInfo[]>('/api/vault'),
  setSecret: (key: string, value: string, description: string) =>
    put<void>(`/api/vault/${encodeURIComponent(key)}`, { value, description }),
  deleteSecret: (key: string) => del(`/api/vault/${encodeURIComponent(key)}`),

  vapidKey: () => get<{ public_key: string }>('/api/push/vapid'),
  subscribePush: (sub: PushSubscriptionJSON) => post<void>('/api/push/subscriptions', sub),
  testPush: () => post<void>('/api/push/test'),
}
