// issue 看板的纯逻辑：状态与优先级的展示顺序、按状态分列、时间线文字

import { agentLabel, selectableAgents } from './agentGroups'
import type { Option } from './ui'
import {
  asIssueId,
  type Agent,
  type AgentId,
  type Assignee,
  type Issue,
  type IssueActivity,
  type IssueId,
  type IssuePriority,
  type IssueStatus,
  type Schemas,
} from './api'

/** 状态的标签；键的顺序就是看板的列顺序（也是列表视图的分组顺序）：受阻排在进行中和待审核之间 */
export const STATUS_LABEL: Record<IssueStatus, string> = {
  backlog: 'Backlog',
  in_progress: '进行中',
  blocked: '受阻',
  in_review: '待审核',
  done: '完成',
  cancelled: '已取消',
}

/** 看板的列顺序：从 STATUS_LABEL 的键推导，Record 保证每个状态都在 */
export const STATUSES = Object.keys(STATUS_LABEL) as readonly IssueStatus[]

/** 状态圆点的颜色（Tailwind 类） */
export const STATUS_COLOR: Record<IssueStatus, string> = {
  backlog: 'border-muted',
  in_progress: 'border-warning',
  in_review: 'border-accent',
  blocked: 'border-danger',
  done: 'border-success bg-success',
  cancelled: 'border-muted bg-muted',
}

/** 优先级的标签；键的顺序就是展示顺序 */
export const PRIORITY_LABEL: Record<IssuePriority, string> = {
  urgent: '紧急',
  high: '高',
  medium: '中',
  low: '低',
  none: '无优先级',
}

/** 优先级的展示顺序：从 PRIORITY_LABEL 的键推导 */
export const PRIORITIES = Object.keys(PRIORITY_LABEL) as readonly IssuePriority[]

/** issue 页；与后端 muliao_issues::route 一致（交给用户审核的通知点开它，server/crates/muliao/tests/frontend_contract.rs 核对）。Service Worker（public/sw.js）按这个形状认出待审核 issue 的通知 */
export const issueRoute = (id: IssueId) => `/issues/${id}`

/** 指派人下拉框里「未指派」「我」的选项 id（选项 id 必须非空；agent id 是 12 位十六进制，不会与它们相同） */
export const UNASSIGNED = '@none'
const ME = '@me'
/** 指派人下拉框的选项 id：未指派、我，或 agent id */
export type AssigneeChoice = typeof UNASSIGNED | typeof ME | AgentId

/** 指派人选项：未指派、我、各个 agent（id 为 agent id）。归档的 agent 不能被指派，只在它就是当前指派人（current）时留着以便显示 */
export function assigneeOptions(agents: Agent[] | undefined, current?: Assignee | null): Option<AssigneeChoice>[] {
  const keep = current?.kind === 'agent' ? current.agent_id : null
  return [
    { id: UNASSIGNED, label: '未指派' },
    { id: ME, label: '我' },
    ...selectableAgents(agents, keep).map((a) => ({ id: a.id, label: a.name })),
  ]
}

/** 指派人与下拉框选项 id 互换 */
export function assigneeValue(assignee: Assignee | null): AssigneeChoice {
  if (assignee === null) return UNASSIGNED
  return assignee.kind === 'user' ? ME : assignee.agent_id
}
export function assigneeOf(value: AssigneeChoice): Assignee | null {
  if (value === UNASSIGNED) return null
  return value === ME ? { kind: 'user' } : { kind: 'agent', agent_id: value }
}

/** 负责项目的显示名；负责的 agent 被删除后为 null */
export function ownerLabel(agents: Agent[] | undefined, owner: AgentId | null): string {
  if (owner === null) return '无负责项目'
  return agentLabel(agents, owner)
}

/** 相邻两个 agent 的色相间隔：黄金角，每加一个 agent 都落在已有色相间较大的空隙里（十个以上会出现约 20° 的相近色） */
const GOLDEN_ANGLE = 137.508

/**
 * 负责项目的色相（0–360），看板卡片、筛选、详情页都从这里取色，同一 agent 处处同色。按 agent 的创建顺序依次取黄金角：
 * 新建的 agent 排在最后，已有的颜色不变；不用名字/id 的哈希，是因为十来个 agent 时哈希必然有几个撞成几乎同色。
 * 负责的 agent 已被删除、或 agent 列表还没加载完时为 null（不着色）。
 */
export function ownerHue(agents: Agent[] | undefined, owner: AgentId | null): number | null {
  if (owner === null || agents === undefined) return null
  const order = [...agents].sort((a, b) => Date.parse(a.created_at) - Date.parse(b.created_at) || a.id.localeCompare(b.id))
  const rank = order.findIndex((a) => a.id === owner)
  return rank === -1 ? null : Math.round((rank * GOLDEN_ANGLE) % 360)
}

export function assigneeLabel(agents: Agent[] | undefined, assignee: Assignee | null): string {
  if (assignee === null) return '未指派'
  return assignee.kind === 'user' ? '我' : agentLabel(agents, assignee.agent_id)
}

/** 同一列里：最近更新的在前，刚拖进来的 issue 就排在最上面 */
export function compareIssues(a: Issue, b: Issue): number {
  return b.updated_at.localeCompare(a.updated_at)
}

export function groupByStatus(issues: Issue[]): Record<IssueStatus, Issue[]> {
  const groups = Object.fromEntries(STATUSES.map((s) => [s, [] as Issue[]])) as Record<IssueStatus, Issue[]>
  for (const issue of issues) groups[issue.status].push(issue)
  for (const s of STATUSES) groups[s].sort(compareIssues)
  return groups
}

/** 每个状态默认显示的 issue 数，其余折叠：「完成」一栏会越堆越多 */
export const FOLD_LIMIT = 10

/** 一栏里要渲染的 issue：折叠时只取前 FOLD_LIMIT 个（已按最近更新排好），展开时全部 */
export function foldIssues(issues: Issue[], expanded: boolean): Issue[] {
  return expanded ? issues : issues.slice(0, FOLD_LIMIT)
}

/** 一次还没被服务端确认的拖拽：移到的状态与拖拽的时刻 */
export interface PendingMove {
  status: IssueStatus
  at: string
}

/** 拖拽、本地乐观更新：把每个还在提交的 issue 移到新状态，并按服务端的做法刷新更新时间 */
export function moveIssues(issues: Issue[], moves: ReadonlyMap<IssueId, PendingMove>): Issue[] {
  return issues.map((i) => {
    const m = moves.get(i.id)
    return m ? { ...i, status: m.status, updated_at: m.at } : i
  })
}

/** 时间线里的状态还可能是 #258 去掉的旧状态（历史不改），没有中文名，原样显示 */
const RECORDED_LABEL: Record<Schemas['Recorded'], string> = { ...STATUS_LABEL, todo: 'todo', waiting: 'waiting' }
const statusLabel = (v: Schemas['Recorded']) => RECORDED_LABEL[v]

/** issue 编号列表的文字：「#1, #2」，空为「无」 */
export function issueRefs(ids: readonly IssueId[]): string {
  return ids.map((i) => `#${i}`).join(', ') || '无'
}

/** 解析用户输入的依赖列表（「#1, 2 #3」）；有不是编号的内容返回 null */
export function parseIssueRefs(text: string): IssueId[] | null {
  const parts = text.split(/[\s,，]+/).filter(Boolean)
  const ids = parts.map((p) => (/^#?\d+$/.test(p) ? Number(p.replace('#', '')) : NaN))
  return ids.some(Number.isNaN) ? null : ids.map(asIssueId)
}

/** 时间线上一条属性变化的文字（评论另行渲染） */
export function activityText(agents: Agent[] | undefined, a: IssueActivity): string {
  switch (a.kind) {
    case 'created':
      return '创建了 issue'
    case 'description':
      return '修改了描述'
    case 'comment':
      return '评论了'
    case 'depends_on':
      return `把依赖从「${issueRefs(a.change.from)}」改为「${issueRefs(a.change.to)}」`
    case 'dependency':
      return `把依赖 #${a.change.issue} 的状态从「${statusLabel(a.change.from)}」改为「${statusLabel(a.change.to)}」`
    case 'run_failed':
      return `在 issue 会话里的运行失败了：${a.body}`
    case 'stopped':
      return `停止了 ${agentLabel(agents, a.change.to)} 在这个 issue 上的会话`
    case 'unsettled':
      return '在 issue 会话里的运行结束时 issue 仍是进行中、会话也没在等什么：已请它表态'
    case 'status':
      return `把状态从「${statusLabel(a.change.from)}」改为「${statusLabel(a.change.to)}」`
    case 'assignee':
      return `把指派从「${assigneeLabel(agents, a.change.from)}」改为「${assigneeLabel(agents, a.change.to)}」`
    case 'owner':
      return `把负责项目从「${ownerLabel(agents, a.change.from)}」改为「${ownerLabel(agents, a.change.to)}」`
    case 'priority':
      return `把优先级从「${PRIORITY_LABEL[a.change.from]}」改为「${PRIORITY_LABEL[a.change.to]}」`
    case 'title':
      return `把标题从「${a.change.from}」改为「${a.change.to}」`
  }
}

/**
 * 被指派 agent 在 issue 会话里的状态；该有人做却没人在做（issue.stalled，后端判定）时要提醒。会话在等后台任务、委派、
 * 到点唤醒或用户回答（waiting）对看板来说也是有人在处理，同样显示「处理中」（#258、#261）
 */
export const WORK_LABEL: Record<NonNullable<Issue['assignee_state']>, string> = {
  running: '处理中',
  waiting: '处理中',
  idle: '空闲',
}
