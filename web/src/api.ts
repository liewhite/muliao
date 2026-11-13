// 接口类型取自 schema.d.ts（由后端的 OpenAPI 描述生成，见 scripts/gen-api.mjs），这里只起名字、补上品牌类型

import type { components, paths } from './schema'

export type Schemas = components['schemas']

/** 会话来源的种类：用户在网页发起、触发器、agent 之间委派、issue 看板唤醒 */
export type Origin = Schemas['SessionOrigin']['kind']

/** agent 与会话的编号：生成的接口类型里带品牌（scripts/gen-api.mjs），编译器拦住「把名称或别种编号当它传」 */
export type AgentId = Schemas['AgentId']
export type SessionId = Schemas['SessionId']

/** 仅用于边界：路由参数、取值为 id 的下拉框 */
export const asAgentId = (value: string) => value as AgentId
export const asSessionId = (value: string) => value as SessionId

export type Agent = Schemas['AgentOut']
/** agent 表单的一整份值（新建时预填的缺省、编辑时打开的当前值）；修改只提交改过的字段（Schemas['AgentUpdate']） */
export type AgentInput = Schemas['AgentDefaults']
export type Session = Schemas['SessionOut']
/** 对等批准的调用的决定：批准、拒绝、始终允许 */
export type ApprovalChoice = NonNullable<Schemas['ReplyIn']['approvals']>[string]
export type ScriptGrant = Schemas['ScriptGrantOut']
// 会话列表里的一行：多一个工作状态 state（在运行 / 在等 / 空闲），不空闲的（在运行、在等）排在最前（接口排序）
export type SessionListItem = Schemas['SessionListItem']
/** 一条通知（不叫 Notification：那是浏览器的全局对象） */
export type AppNotification = Schemas['Notification']

// 下面四个值与两个路由（sessionRoute、issues.ts 的 issueRoute）前后端各写一份，server/crates/muliao/tests/frontend_contract.rs 核对
/** 指代用户的那个值（与后端 assets 的 naming::USER 一致）：issue 的指派人、资源修改的作者等 */
export const USER = 'user'
/** issue 时间线上系统自己的动作（删除 agent 时的清理）的 actor（与后端 issues 的 Actor::System 一致） */
export const SYSTEM_ACTOR = 'system'

/** 全局记忆的名字（与后端 assets 的 rules::GLOBAL_MEMORY 一致）：base 固定收录，不能删 */
export const GLOBAL_MEMORY = 'global'

/** 引用文件的地址前缀（与后端 files 的 FILES_PATH 一致）：回答里只有指向这里的图片会被渲染 */
export const FILES_PATH = '/api/files'

export type FileInfo = Schemas['FileOut']
/** 读不出来的资源文件（skill、MCP 配置）：列表接口不含它们，单独列出供删除 */
export type Attachment = Schemas['Attachment']
/** 会话条目：按 type 区分，每种只带自己的字段 */
export type Item = Schemas['Item']
/** 历史里的一条：来源消息 id（定位压缩点）与条目 */
export type SavedItem = Schemas['SavedItem']
/** 工具调用的参数或结果按工具的展示规则画成的一块（后端 platform 的 tool_views.rs） */
export type ViewBlock = Schemas['ViewBlock']
export type CompactionInfo = Schemas['CompactionOut']
/** 会话最近一次还没正常结束的运行（在跑、在等重试，或最终失败） */
export type RunRecord = Schemas['RunRecord']
/** 运行出过暂时性的错、正在自动重试：上次的错误与下次尝试的时间 */
export type Retrying = Schemas['Retrying']

/** 一个分组的用量汇总；没有参与分组的维度为 null。input_tokens 已含缓存读写，不要相加 */
export type Usage = Schemas['UsageBucket']
export type UsageGroup = NonNullable<NonNullable<paths['/api/usage']['get']['parameters']['query']>['group_by']>[number]
/** agent 做过的一件事（只追加，由 agent 在每日自进化时写） */
export type HistoryEntry = Schemas['Record']

export type Question = Schemas['Question']
export type QuestionAnswer = Schemas['QuestionAnswer']
export type AnsweredQuestion = Schemas['AnsweredQuestion']
export type AskView = Schemas['AskView']
export type AskState = Schemas['AskState']

export type MailState = Schemas['MailState']
/** 信箱里的一封信：按 kind 区分的联合（用户消息、事件、回答），事件一定带来源 */
export type Mail = Schemas['SessionDetail']['mail'][number]
/** 事件的来源：后台任务、thread 或到点唤醒（后端解析好的种类） */
export type ItemSource = Schemas['EventSource']
/** 同一来源的事件归在一起用的键 */
export const sourceKey = (s: ItemSource): string =>
  s.kind === 'job' ? `job:${s.job_id}` : s.kind === 'thread' ? `thread:${s.session_id}` : 'wake'
export type SessionDetail = Schemas['SessionDetail']
export type LiveSegment = Schemas['LiveSegment']
/** 会话在等的东西（按 kind 区分）：后台任务（job 字段带详情）、委派、到点唤醒、提问、等批准 */
export type Wait = SessionDetail['waits'][number]

export type Skill = Schemas['SkillDetail']
export type Memory = Schemas['MemoryOut']
/** 界面认识的一种触发方式（接口里 name 是字符串：触发方式由服务端登记） */
export type TriggerKind = Schemas['TriggerKindOut'] & { name: TriggerKindName }
export type Precheck = Schemas['Precheck']
/** 按 kind 区分的联合：收窄 kind 后 config 与 status 就是这种方式的类型 */
export type Trigger = Schemas['TriggerOut']
export type TriggerKindName = Trigger['kind']
/** 新建触发器的请求，同样按 kind 区分：有服务端默认值的配置项可省 */
export type TriggerCreate = Schemas['TriggerCreate']
export type McpServer = Schemas['McpOut']
export type McpInput = Schemas['McpIn']
/** AI 资产池里的一项（内置工具、skill、MCP server、secret、记忆），带收录它的角色与这一种自己的详情（按 kind 收窄）；
 * 后端 assets 的 catalog.rs */
export type CatalogAsset = Schemas['CatalogAssetOut']
export type AssetKind = CatalogAsset['kind']
export type AssetOf<K extends AssetKind> = Extract<CatalogAsset, { kind: K }>
/** 存成文件的资源种类（能改、能转移）：内置工具之外的四种 */
export type StoredKind = Exclude<AssetKind, 'tool'>
/** 角色（内置或自建）、它能用的工具与资产、带它的 agent；后端 assets 的 rules.rs、distribution.rs */
export type Role = Schemas['RoleOut']
export type RoleInput = Schemas['RoleIn']
export type ToolPermission = Schemas['ToolOut']
export type Reach = Schemas['ReachOut']
/** agent 能用的记忆、skill、secret、MCP 与各自来源，加记忆注入预览；后端 assets 的 distribution.rs */
export type AgentAssets = Schemas['AgentAssetsOut']
export type AgentAsset = Schemas['AssetOut']
export type IssueStatus = Schemas['Status']
export type IssuePriority = Schemas['Priority']
/** assignee：agent id、'user'（用户），或 null（未指派） */
export type Issue = Schemas['BoardIssue']
/** 时间线上的一条，按 kind 区分：收窄后就是这种的内容（body）与变化（change） */
export type IssueActivity = IssueDetail['activities'][number]
export type IssueDetail = Schemas['IssueDetail']
/** agent 工作目录里的一项；目录的 size 为 null */
export type WorkspaceEntry = Schemas['EntryOut']

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

// 请求的时限：服务端或网络卡住时报错，而不是让等它的操作（按钮）一直转。要大于服务端单个操作的上限，否则服务端随后
// 成功或报出的真正原因被换成「超时」：最长的是探测 MCP server（连接 30 秒，runtime 的 mcp.rs CONNECT_TIMEOUT，再列工具）。
// 传文件（上传、录音、预览）按大小与网速可能很久
export const REQUEST_TIMEOUT_MS = 120_000
const FILE_TIMEOUT_MS = 600_000

/** 所有请求的唯一出口：统一的时限、401 处理与错误信息 */
async function send<T>(
  method: string,
  path: string,
  query: Query | undefined,
  body?: { type: string; data: BodyInit },
  // 响应体是文件（如预览的文件）：成功时交出响应本身，由调用方读 Blob
  raw = false,
): Promise<T> {
  const timeoutMs = raw || body?.data instanceof Blob ? FILE_TIMEOUT_MS : REQUEST_TIMEOUT_MS
  let resp: Response
  try {
    resp = await fetch(withQuery(path, query), {
      method,
      credentials: 'same-origin',
      headers: body === undefined ? undefined : { 'Content-Type': body.type },
      body: body?.data,
      signal: AbortSignal.timeout(timeoutMs),
    })
  } catch (e) {
    if (e instanceof DOMException && e.name === 'TimeoutError')
      throw new Error(`请求超时（${timeoutMs / 1000} 秒没有响应）：${method} ${path}`, { cause: e })
    throw e
  }
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
  // 204 按 HTTP 语义没有响应体，即使带着 content-type: application/json（有的服务端框架会这样发），所以不能按 Content-Type 判断
  if (resp.status === 204) return undefined as T
  if (raw) return resp as T
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

/** 前端路由：agent 页按 id（名称可改） */
export const agentRoute = (id: AgentId) => `/agents/${id}`
/** 会话页；通知点开的地址由后端 notifications 的 inbox::url_of 生成，与它一致 */
export const sessionRoute = (id: SessionId) => `/sessions/${id}`
export const newSessionRoute = (agentId: AgentId) => `/new?${new URLSearchParams({ agent: agentId })}`
/** agent 自定义页面的界面路由（页面本身在沙箱 iframe 里按接口给的签名地址打开） */
export const sitePageRoute = (agentId: AgentId, name: string) =>
  `/pages/${encodeURIComponent(agentId)}/${encodeURIComponent(name)}`
export type SitePage = Schemas['SitePageOut']
/** agent 工作目录里一个文件的内容（path 相对工作目录）：图片可直接内联，其余作为附件下载 */
export const workspaceFileUrl = (agentId: AgentId, path: string) =>
  `/api/agents/${encodeURIComponent(agentId)}/workspace/file?${new URLSearchParams({ path })}`

/** skill 目录里一个文件的内容（path 相对 skill 目录），同 workspaceFileUrl */
export const skillFileUrl = (name: string, path: string) =>
  `/api/assets/skill/${encodeURIComponent(name)}/file?${new URLSearchParams({ path })}`

export const api = {
  login: (password: string) => call('post', '/api/auth/login', { body: { password } }),
  logout: () => call('post', '/api/auth/logout'),
  me: () => call('get', '/api/auth/me'),

  agents: () => call('get', '/api/agents'),
  agentDefaults: () => call('get', '/api/agent-defaults'),
  /** 全部角色：内置在前（base 每个 agent 隐含），自建的按名称；各带只按它自己的规则能用的工具与资产 */
  roles: () => call('get', '/api/roles'),
  createRole: (name: string, body: RoleInput) => call('post', '/api/roles/{name}', { path: { name }, body }),
  /** 只改给出的字段 */
  updateRole: (name: string, body: Schemas['RoleUpdate']) => call('patch', '/api/roles/{name}', { path: { name }, body }),
  deleteRole: (name: string) => call('delete', '/api/roles/{name}', { path: { name } }),
  /** agent 配置（还没保存的也行）最终能用的工具与资产：与运行时同一套计算；agent_id 为 null 是还没建的 */
  previewAgent: (body: Schemas['AgentPreviewIn']) => call('post', '/api/agent-preview', { body }),
  /** agent 能用的资产按种类分组，各带来源；记忆另附合并后注入指令的样子（与运行时同一渲染） */
  agentAssets: (agentId: AgentId) =>
    call('get', '/api/agents/{agent_id}/assets', { path: { agent_id: agentId } }),
  /** 角色规则（还没保存的也行）能用的工具与资产；name 为 null 是还没建的 */
  previewRole: (body: Schemas['RolePreviewIn']) => call('post', '/api/role-preview', { body }),
  /** 全部 AI 资源：内置工具在前，资源文件按种类、名称 */
  assets: () => call('get', '/api/assets'),
  /** 删掉 skill（整个目录）/ MCP server / secret / 平台记忆的文件；全局记忆不能删，私有记忆随 agent */
  deleteAsset: (kind: StoredKind, name: string) => {
    const path = { path: { name } }
    switch (kind) {
      case 'skill':
        return call('delete', '/api/assets/skill/{name}', path)
      case 'mcp':
        return call('delete', '/api/assets/mcp/{name}', path)
      case 'secret':
        return call('delete', '/api/assets/secret/{name}', path)
      case 'memory':
        return call('delete', '/api/assets/memory/{name}', path)
    }
  },
  /** 把 skill / MCP server / secret 挪到另一个 owner（null 归平台）的目录下；内容与收录它的角色不变。记忆不转移 */
  transferAsset: (kind: StoredKind, name: string, owner: AgentId | null) =>
    call('put', '/api/assets/{kind}/{name}/owner', { path: { kind, name }, body: { owner } }),
  /** agent 的工作历史：不给 query 按日期倒序；给了按关键词检索 */
  agentHistory: (agent_id: AgentId, query?: string) =>
    call('get', '/api/agents/{agent_id}/history', { path: { agent_id }, query: { query, limit: 200 } }),
  /** agent 工作目录里 path（相对工作目录，空为根）这一层：目录在前，各自按名称 */
  workspace: (agent_id: AgentId, path: string) =>
    call('get', '/api/agents/{agent_id}/workspace', { path: { agent_id }, query: { path } }),
  /** 浏览中的一个文件的内容（workspaceFileUrl / skillFileUrl；响应体是文件本身，接口描述里没有 JSON 响应体） */
  fileBlob: (url: string) => send<Response>('GET', url, undefined, undefined, true).then((r) => r.blob()),
  /** 全部 agent 的自定义页面（工作目录 site/<名>/index.html），各带打开它的签名地址 */
  pages: () => call('get', '/api/pages'),
  /** 服务端配置决定的可选能力（如语音输入）与默认值（如压缩阈值） */
  features: () => call('get', '/api/features'),
  /** 服务端配置的 LLM provider 名 */
  providers: () => call('get', '/api/providers'),
  /** 向 provider 实时查询的模型名 */
  providerModels: (name: string) => call('get', '/api/providers/{name}/models', { path: { name } }),
  agent: (agent_id: AgentId) => call('get', '/api/agents/{agent_id}', { path: { agent_id } }),
  createAgent: (body: Schemas['AgentCreate']) => call('post', '/api/agents', { body }),
  /** 只改给出的字段 */
  updateAgent: (agent_id: AgentId, body: Schemas['AgentUpdate']) =>
    call('patch', '/api/agents/{agent_id}', { path: { agent_id }, body }),
  deleteAgent: (agent_id: AgentId) => call('delete', '/api/agents/{agent_id}', { path: { agent_id } }),
  /** 设为（true）或取消（false）系统默认 agent：Web 新会话默认选中它 */
  setDefaultAgent: (agent_id: AgentId, on: boolean) =>
    on
      ? call('put', '/api/agents/{agent_id}/default', { path: { agent_id } })
      : call('delete', '/api/agents/{agent_id}/default', { path: { agent_id } }),
  /** 归档（true）或取消归档（false）：归档的不能对话、委派、指派，它的触发器不触发；它忙或是默认 agent 时 409 */
  setAgentArchived: (agent_id: AgentId, on: boolean) =>
    on
      ? call('put', '/api/agents/{agent_id}/archive', { path: { agent_id } })
      : call('delete', '/api/agents/{agent_id}/archive', { path: { agent_id } }),
  /** 平台的一块记忆（global 是全局记忆，其余是共享记忆） */
  memory: (name: string) => call('get', '/api/assets/memory/{name}', { path: { name } }),
  /** 新建或整体改写平台的一块记忆。version 是打开时的版本（新建为 null），期间被别处改过 409 */
  saveMemory: (name: string, body: Schemas['MemoryIn']) =>
    call('put', '/api/assets/memory/{name}', { path: { name }, body }),
  /** agent 的私有记忆：按所属 agent 认，不占记忆的名字 */
  agentMemory: (agent_id: AgentId) =>
    call('get', '/api/agents/{agent_id}/memory', { path: { agent_id } }),
  /** 整体改写 agent 的私有记忆，version 同 saveMemory */
  saveAgentMemory: (agent_id: AgentId, body: Schemas['MemoryIn']) =>
    call('put', '/api/agents/{agent_id}/memory', { path: { agent_id }, body }),

  skill: (name: string) => call('get', '/api/assets/skill/{name}', { path: { name } }),
  /** 新建的归 owner；已有的整体改写正文与描述，owner 不变。version 同 saveMemory */
  saveSkill: (name: string, body: Schemas['SkillIn']) => call('put', '/api/assets/skill/{name}', { path: { name }, body }),
  /** skill 目录里 path（相对 skill 目录，空为根）这一层，同工作目录浏览 */
  skillFiles: (name: string, path: string) =>
    call('get', '/api/assets/skill/{name}/files', { path: { name }, query: { path } }),

  /** trigger_id：只列这个触发器发起过的会话（不能再给 origin）；q 是关键字（标题或消息含有它，不能为空串） */
  sessions: (query: {
    agent_id?: AgentId
    origin?: Origin[]
    trigger_id?: number
    q?: string
    limit?: number
    offset?: number
  }) =>
    call('get', '/api/sessions', { query }),
  session: (session_id: SessionId) =>
    call('get', '/api/sessions/{session_id}', { path: { session_id } }),
  startSession: (agent_id: AgentId, prompt: string, attachments: string[]) =>
    call('post', '/api/sessions', { body: { agent_id, prompt, attachments } }),
  reply: (session_id: SessionId, prompt: string, attachments: string[]) =>
    call('post', '/api/sessions/{session_id}/messages', { path: { session_id }, body: { prompt, attachments, answers: {}, approvals: {} } }),
  /** 回答挂起的提问：作为这次 ask_user 调用的结果交回模型 */
  answer: (session_id: SessionId, toolCallId: string, answers: QuestionAnswer[]) =>
    call('post', '/api/sessions/{session_id}/messages', {
      path: { session_id },
      body: { prompt: '', attachments: [], answers: { [toolCallId]: answers }, approvals: {} },
    }),
  /**
   * 批准或拒绝一次等批准的工具调用：批准后按原参数执行，结果交回模型；拒绝时模型收到「用户拒绝」；始终允许：批准，
   * 且以后同样的调用不再问（设置页可撤销）
   */
  approve: (session_id: SessionId, toolCallId: string, choice: ApprovalChoice) =>
    call('post', '/api/sessions/{session_id}/messages', {
      path: { session_id },
      body: { prompt: '', attachments: [], answers: {}, approvals: { [toolCallId]: choice } },
    }),
  /** skill 脚本的「始终允许」，新的在前 */
  scriptGrants: () => call('get', '/api/script-grants'),
  /** 撤销一条始终允许：之后同一调用重新要批准 */
  revokeScriptGrant: (grant_id: number) =>
    call('delete', '/api/script-grants/{grant_id}', { path: { grant_id } }),
  /** 请求体就是文件内容，Content-Type 是它的类型（接口描述里没有 JSON 请求体）；浏览器认不出类型时 file.type 为空，按「未知二进制」上传 */
  uploadFile: (file: File) =>
    send<FileInfo>('POST', FILES_PATH, { name: file.name }, { type: file.type || 'application/octet-stream', data: file }),
  /** 请求体是一段录音，Content-Type 是它的类型；返回转写出的文字（听不到人说话时为空） */
  transcribe: (audio: Blob) =>
    send<Schemas['TranscriptOut']>('POST', '/api/transcriptions', undefined, { type: audio.type || 'audio/webm', data: audio }),
  /** 同时把指向这个会话的通知标为已读 */
  markRead: (session_id: SessionId) => call('post', '/api/sessions/{session_id}/read', { path: { session_id } }),
  notifications: () => call('get', '/api/notifications'),
  unreadNotifications: () => call('get', '/api/notifications/unread'),
  /** 标记到 throughId（用户看到的最新一条）为止已读，之后才到的保持未读 */
  markNotificationsRead: (throughId: number) => call('post', '/api/notifications/read', { body: { through_id: throughId } }),
  /** 打开了 issue：请用户审核它的通知标为已读 */
  markIssueNotificationsRead: (issue_id: number) =>
    call('post', '/api/notifications/issues/{issue_id}/read', { path: { issue_id } }),
  /** 停止正在进行的运行；停止前已经产生的部分照常保存 */
  stop: (session_id: SessionId) => call('post', '/api/sessions/{session_id}/stop', { path: { session_id } }),
  /** 压缩在后台做，开始与结束时会话页随 saved 事件刷新 */
  compact: (session_id: SessionId) => call('post', '/api/sessions/{session_id}/compact', { path: { session_id } }),
  /** 重试最近一次最终失败的运行（同一次运行接着跑）；会话在跑或最近一次没失败时 409 */
  retry: (session_id: SessionId) => call('post', '/api/sessions/{session_id}/retry', { path: { session_id } }),
  deleteSession: (session_id: SessionId) => call('delete', '/api/sessions/{session_id}', { path: { session_id } }),
  /** 结束 agent 的一个后台任务（同 job_kill）；结局照常作为事件回到发起它的会话 */
  killJob: (agent_id: AgentId, job_id: number) =>
    call('post', '/api/agents/{agent_id}/jobs/{job_id}/kill', { path: { agent_id, job_id } }),

  /** [since, until) 之间的 token 用量，按给出的维度分组；天按服务端配置的时区划分 */
  usage: (since: Date, until: Date, group_by: UsageGroup[]) =>
    call('get', '/api/usage', { query: { since: since.toISOString(), until: until.toISOString(), group_by } }),

  triggerKinds: () => call('get', '/api/trigger-kinds'),
  /** finished 为 true 时列已结束的（跑完的一次性触发器），它们只能查看与删除 */
  triggers: (finished = false) => call('get', '/api/triggers', { query: { finished } }),
  createTrigger: (body: TriggerCreate & { agent_id: AgentId }) => call('post', '/api/triggers', { body }),
  /** 只修改给出的字段；precheck 不给表示不变，给 null 表示去掉 */
  updateTrigger: (trigger_id: number, body: Schemas['TriggerPatch']) =>
    call('patch', '/api/triggers/{trigger_id}', { path: { trigger_id }, body }),
  /** session_id 为 null：前置检查判定这次不调用 agent */
  runTrigger: (trigger_id: number) => call('post', '/api/triggers/{trigger_id}/run', { path: { trigger_id } }),
  deleteTrigger: (trigger_id: number) => call('delete', '/api/triggers/{trigger_id}', { path: { trigger_id } }),
  /** 新增（启用的）MCP server；同名已存在 409 */
  addMcp: (name: string, body: McpInput) =>
    call('put', '/api/assets/mcp/{name}', { path: { name }, body }),
  probeMcp: (name: string) => call('post', '/api/assets/mcp/{name}/tools', { path: { name } }),
  setMcpEnabled: (name: string, enabled: boolean) =>
    call('put', '/api/assets/mcp/{name}/enabled', { path: { name }, body: { enabled } }),

  /** 新建的归 owner（null 为平台）；已有的改值与描述，owner 不变；allowShell 为 null：已有的保持原值，新建的允许 */
  setSecret: (owner: AgentId | null, key: string, value: string, description: string, allowShell: boolean | null) =>
    call('put', '/api/assets/secret/{name}', {
      path: { name: key },
      body: { owner, value, description, allow_shell: allowShell },
    }),

  /** owner 给出时只列这个项目（agent）负责的 issue */
  issues: (owner?: AgentId) => call('get', '/api/issues', { query: { owner } }),
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
  /** 开工 backlog 里的 issue：指派给 owner 并进入 in_progress */
  startIssue: (issue_id: number) => call('post', '/api/issues/{issue_id}/start', { path: { issue_id } }),
  rejectIssue: (issue_id: number, reason: string) =>
    call('post', '/api/issues/{issue_id}/reject', { path: { issue_id }, body: { reason } }),

  vapidKey: () => call('get', '/api/push/vapid'),
  subscribePush: (body: Schemas['PushSubscriptionIn']) => call('post', '/api/push/subscriptions', { body }),
  testPush: () => call('post', '/api/push/test'),
}
