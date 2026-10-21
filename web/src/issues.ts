// issue 看板的纯逻辑：状态与优先级的展示顺序、按状态分列、时间线文字

import type { Agent, Issue, IssueActivity, IssuePriority, IssueStatus } from './api'

/** 看板的列顺序（也是列表视图的分组顺序） */
export const STATUSES: readonly IssueStatus[] = ['backlog', 'todo', 'in_progress', 'in_review', 'blocked', 'done', 'cancelled']

export const STATUS_LABEL: Record<IssueStatus, string> = {
  backlog: 'Backlog',
  todo: 'Todo',
  in_progress: '进行中',
  in_review: '待审核',
  blocked: '受阻',
  done: '完成',
  cancelled: '已取消',
}

/** 状态圆点的颜色（Tailwind 类） */
export const STATUS_COLOR: Record<IssueStatus, string> = {
  backlog: 'border-muted',
  todo: 'border-foreground',
  in_progress: 'border-warning',
  in_review: 'border-accent',
  blocked: 'border-danger',
  done: 'border-success bg-success',
  cancelled: 'border-muted bg-muted',
}

export const PRIORITIES: readonly IssuePriority[] = ['urgent', 'high', 'medium', 'low', 'none']

export const PRIORITY_LABEL: Record<IssuePriority, string> = {
  urgent: '紧急',
  high: '高',
  medium: '中',
  low: '低',
  none: '无优先级',
}

/** 指派人取值里表示用户的那个（与后端 issues.rules.USER 一致） */
export const USER = 'user'

export const issueRoute = (id: number) => `/issues/${id}`

/** 指派人下拉框里「未指派」的 id（选项 id 必须非空） */
export const UNASSIGNED = '@none'

/** 指派人选项：未指派、我、各个 agent（id 为 agent id） */
export function assigneeOptions(agents: Agent[] | undefined) {
  return [
    { id: UNASSIGNED, label: '未指派' },
    { id: USER, label: '我' },
    ...(agents ?? []).map((a) => ({ id: a.id as string, label: a.name })),
  ]
}

export const assigneeValue = (assignee: string | null) => assignee ?? UNASSIGNED
export const assigneeOf = (value: string) => (value === UNASSIGNED ? null : value)

/** 负责项目的显示名；负责的 agent 被删除后为 null */
export function ownerLabel(agents: Agent[] | undefined, owner: string | null): string {
  if (owner === null) return '无负责项目'
  return agents?.find((a) => a.id === owner)?.name ?? owner
}

export function assigneeLabel(agents: Agent[] | undefined, assignee: string | null): string {
  if (assignee === null) return '未指派'
  if (assignee === USER) return '我'
  return agents?.find((a) => a.id === assignee)?.name ?? assignee
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
export function moveIssues(issues: Issue[], moves: ReadonlyMap<number, PendingMove>): Issue[] {
  return issues.map((i) => {
    const m = moves.get(i.id)
    return m ? { ...i, status: m.status, updated_at: m.at } : i
  })
}

function valueLabel(agents: Agent[] | undefined, a: IssueActivity, v: unknown): string {
  if (a.kind === 'assignee') return assigneeLabel(agents, (v as string | null) ?? null)
  if (a.kind === 'owner') return ownerLabel(agents, (v as string | null) ?? null)
  if (a.kind === 'status') return STATUS_LABEL[v as IssueStatus] ?? String(v)
  if (a.kind === 'priority') return PRIORITY_LABEL[v as IssuePriority] ?? String(v)
  return String(v)
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
    default: {
      const what = { status: '状态', assignee: '指派', owner: '负责项目', priority: '优先级', title: '标题' }[a.kind]
      return `把${what}从「${valueLabel(agents, a, a.change?.from)}」改为「${valueLabel(agents, a, a.change?.to)}」`
    }
  }
}

/** 被指派 agent 在 issue 会话里的状态；该有人做却没人在做（issue.stalled，后端判定）时要提醒 */
export const WORK_LABEL: Record<NonNullable<Issue['assignee_session']>, string> = {
  running: '处理中',
  waiting: '等待中',
  idle: '空闲',
}
