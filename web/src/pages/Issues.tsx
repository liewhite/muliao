import { useRef, useState, type ComponentProps, type CSSProperties } from 'react'
import { Card, Chip } from '@heroui/react'
import { DndContext, KeyboardSensor, MouseSensor, TouchSensor, useDraggable, useDroppable, useSensor, useSensors, type DragEndEvent } from '@dnd-kit/core'
import { Link, useNavigate } from 'react-router-dom'
import { api, type Agent, type Issue, type IssuePriority, type IssueStatus } from '../api'
import { useRefetch } from '../events'
import {
  PRIORITIES,
  PRIORITY_LABEL,
  STATUSES,
  STATUS_COLOR,
  STATUS_LABEL,
  UNASSIGNED,
  assigneeLabel,
  assigneeOf,
  assigneeOptions,
  FOLD_LIMIT,
  foldIssues,
  ownerHue,
  ownerLabel,
  groupByStatus,
  issueRoute,
  moveIssues,
  type PendingMove,
  WORK_LABEL,
  issueRefs,
  parseIssueRefs,
} from '../issues'
import { NODE_HEIGHT, NODE_WIDTH, edgePath, graphIssues, layoutIssueGraph, type GraphScope } from '../issueGraph'
import {
  ActionButton,
  CollapsibleForm,
  EmptyHint,
  ErrorAlert,
  Loading,
  type Option,
  Page,
  Segmented,
  SelectInput,
  TextAreaInput,
  TextInput,
  errorMessage,
  relativeTime,
  useAction,
  useAgents,
  useLoad,
} from '../ui'

type View = 'board' | 'list' | 'graph'
// 「所有项目」选项的 id；查询时映射为不过滤
const ALL_OWNERS = '@all'
const VIEW_KEY = 'muliao.issues.view'
const GRAPH_SCOPE_KEY = 'muliao.issues.graphScope'

export function StatusDot({ status }: { status: IssueStatus }) {
  return <span className={`inline-block size-3 shrink-0 rounded-full border-2 ${STATUS_COLOR[status]}`} aria-hidden />
}

/** 带负责项目色相的样式：配合 owner-color 类（styles.css）得到 --owner-fg / --owner-bg；不着色时为 undefined */
function ownerStyle(hue: number | null): CSSProperties | undefined {
  return hue === null ? undefined : ({ '--owner-hue': hue } as CSSProperties)
}

/** 负责项目的色点；不着色时是灰点 */
export function OwnerDot({ agents, owner }: { agents: Agent[] | undefined; owner: string | null }) {
  const hue = ownerHue(agents, owner)
  return (
    <span
      className={`inline-block size-2.5 shrink-0 rounded-full ${hue === null ? 'bg-muted' : 'owner-color bg-(--owner-fg)'}`}
      style={ownerStyle(hue)}
      aria-hidden
    />
  )
}

/** 负责项目的胶囊：浅色底 + owner 色文字 */
function OwnerChip({ agents, owner }: { agents: Agent[] | undefined; owner: string }) {
  const hue = ownerHue(agents, owner)
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded-full px-2 py-0.5 text-xs font-medium ${
        hue === null ? 'bg-default text-muted' : 'owner-color bg-(--owner-bg) text-(--owner-fg)'
      }`}
      style={ownerStyle(hue)}
    >
      <OwnerDot agents={agents} owner={owner} />
      {ownerLabel(agents, owner)}
    </span>
  )
}

/** 选负责项目的下拉框：每个 agent 前带它的色点；leading 是排在 agent 前面的额外选项（如「所有项目」） */
export function OwnerSelect({
  agents,
  leading = [],
  ...props
}: Omit<ComponentProps<typeof SelectInput>, 'options'> & { agents: Agent[] | undefined; leading?: Option[] }) {
  const options = (agents ?? []).map((a) => ({
    id: a.id as string,
    label: a.name,
    prefix: <OwnerDot agents={agents} owner={a.id} />,
  }))
  return <SelectInput {...props} options={[...leading, ...options]} />
}

function IssueCard({ issue, agents }: { issue: Issue; agents: Agent[] | undefined }) {
  const hue = ownerHue(agents, issue.owner)
  // 左侧色条用 owner 色：扫一眼列就能按项目分组
  return (
    <Card
      className={`cursor-pointer border-l-4 transition-colors hover:bg-surface-hover ${
        hue === null ? 'border-l-transparent' : 'owner-color border-l-(--owner-fg)'
      }`}
      style={ownerStyle(hue)}
    >
        <Card.Content className="flex flex-col gap-1.5 p-3">
          <div className="flex items-center gap-2 text-xs text-muted">
            <span>#{issue.id}</span>
            <span className="ml-auto">{relativeTime(issue.updated_at)}</span>
          </div>
          <div className="text-sm font-medium leading-snug">{issue.title}</div>
          <div className="flex flex-wrap gap-1.5">
            {issue.priority !== 'none' && (
              <Chip size="sm" variant="secondary" color={issue.priority === 'urgent' ? 'danger' : 'default'}>
                {PRIORITY_LABEL[issue.priority]}
              </Chip>
            )}
            {issue.owner !== null && (
              <OwnerChip agents={agents} owner={issue.owner} />
            )}
            <Chip size="sm" variant="soft">
              → {assigneeLabel(agents, issue.assignee)}
            </Chip>
            {issue.waiting_on.length > 0 && (
              <Chip size="sm" variant="soft">
                等待 {issueRefs(issue.waiting_on)}
              </Chip>
            )}
            {issue.stalled ? (
              <Chip size="sm" variant="soft" color="warning">
                没人在处理
              </Chip>
            ) : (
              issue.assignee_session && (
                <Chip size="sm" variant="soft" color={issue.assignee_session === 'running' ? 'success' : 'default'}>
                  {WORK_LABEL[issue.assignee_session]}
                </Chip>
              )
            )}
          </div>
        </Card.Content>
    </Card>
  )
}

function DraggableCard({ issue, agents, onOpen }: { issue: Issue; agents: Agent[] | undefined; onOpen: (id: number) => void }) {
  const { attributes, listeners, setNodeRef, transform, isDragging } = useDraggable({ id: issue.id })
  const style = transform ? { transform: `translate3d(${transform.x}px, ${transform.y}px, 0)` } : undefined
  return (
    // 单击打开详情；拖拽结束时浏览器随后发出的那次 click 由 onOpen 忽略。触屏上禁用长按菜单与文字选中，长按才能拖起
    <div
      ref={setNodeRef}
      style={style}
      className={`touch-manipulation select-none [-webkit-touch-callout:none] ${isDragging ? 'relative z-10 opacity-80' : ''}`}
      {...listeners}
      {...attributes}
      onClick={() => onOpen(issue.id)}
      onKeyDown={(e) => {
        // 键盘拖拽也从 onKeyDown 起手（KeyboardSensor），不能被这里盖掉
        listeners?.onKeyDown?.(e)
        if (e.key === 'Enter') onOpen(issue.id)
      }}
    >
      <IssueCard issue={issue} agents={agents} />
    </div>
  )
}

/** 折叠起来的 issue 的展开 / 收起按钮；不超过 FOLD_LIMIT 个时不显示 */
function FoldToggle({ total, expanded, onToggle }: { total: number; expanded: boolean; onToggle: () => void }) {
  if (total <= FOLD_LIMIT) return null
  return (
    <button type="button" className="self-start px-1 py-1 text-xs text-muted hover:text-foreground" onClick={onToggle}>
      {expanded ? '收起' : `展开其余 ${total - FOLD_LIMIT} 个`}
    </button>
  )
}

function Column({
  status,
  issues,
  agents,
  onOpen,
}: {
  status: IssueStatus
  issues: Issue[]
  agents: Agent[] | undefined
  onOpen: (id: number) => void
}) {
  const { setNodeRef, isOver } = useDroppable({ id: status })
  const [expanded, setExpanded] = useState(false)
  return (
    <section
      ref={setNodeRef}
      className={`flex w-72 shrink-0 flex-col gap-2 rounded-xl p-2 ${isOver ? 'bg-surface-hover' : 'bg-surface-secondary'}`}
    >
      <h2 className="flex items-center gap-2 px-1 text-sm font-semibold">
        <StatusDot status={status} />
        {STATUS_LABEL[status]}
        <span className="font-normal text-muted">{issues.length}</span>
      </h2>
      {foldIssues(issues, expanded).map((i) => (
        <DraggableCard key={i.id} issue={i} agents={agents} onOpen={onOpen} />
      ))}
      <FoldToggle total={issues.length} expanded={expanded} onToggle={() => setExpanded(!expanded)} />
    </section>
  )
}

function Board({ issues, agents, onMove }: { issues: Issue[]; agents: Agent[] | undefined; onMove: (id: number, s: IssueStatus) => void }) {
  // 鼠标拖动一小段才算拖拽，点一下是打开；触屏长按才拖，滑动仍是滚动。不用 PointerSensor：触屏上它与浏览器的滚动
  // 抢同一串指针事件，滑动一开始就被 pointercancel 打断，拖不起来
  const sensors = useSensors(
    useSensor(MouseSensor, { activationConstraint: { distance: 6 } }),
    useSensor(TouchSensor, { activationConstraint: { delay: 250, tolerance: 8 } }),
    // 键盘：空格拿起，方向键移动，空格或回车放下、Esc 取消；回车留给「打开」，不用来拿起
    useSensor(KeyboardSensor, { keyboardCodes: { start: ['Space'], cancel: ['Escape'], end: ['Space', 'Enter'] } }),
  )
  const navigate = useNavigate()
  // 拖拽中、以及刚结束的这一刻：松手后浏览器紧接着会派发一次 click，不能把它当成「单击打开」
  const dragging = useRef(false)
  const groups = groupByStatus(issues)
  const settle = () => {
    // click 在松手的同一轮事件里派发，下一轮再清除标记
    setTimeout(() => {
      dragging.current = false
    }, 0)
  }
  const onOpen = (id: number) => {
    if (!dragging.current) navigate(issueRoute(id))
  }
  const onDragEnd = (e: DragEndEvent) => {
    settle()
    if (e.over === null) return
    const status = e.over.id as IssueStatus
    const id = Number(e.active.id)
    if (issues.find((i) => i.id === id)?.status !== status) onMove(id, status)
  }
  return (
    <DndContext
      sensors={sensors}
      onDragStart={() => {
        dragging.current = true
      }}
      onDragEnd={onDragEnd}
      onDragCancel={settle}
    >
      <div className="flex min-h-full gap-3 overflow-x-auto px-4 py-3">
        {STATUSES.map((s) => (
          <Column key={s} status={s} issues={groups[s]} agents={agents} onOpen={onOpen} />
        ))}
      </div>
    </DndContext>
  )
}

function List({ issues, agents }: { issues: Issue[]; agents: Agent[] | undefined }) {
  const groups = groupByStatus(issues)
  const nonEmpty = STATUSES.filter((s) => groups[s].length > 0)
  if (nonEmpty.length === 0) return <EmptyHint>还没有 issue</EmptyHint>
  return (
    <div className="flex flex-col gap-4">
      {nonEmpty.map((s) => (
        <ListSection key={s} status={s} issues={groups[s]} agents={agents} />
      ))}
    </div>
  )
}

function ListSection({ status, issues, agents }: { status: IssueStatus; issues: Issue[]; agents: Agent[] | undefined }) {
  const [expanded, setExpanded] = useState(false)
  return (
    <section className="flex flex-col">
      <h2 className="flex items-center gap-2 py-1 text-sm font-semibold text-muted">
        <StatusDot status={status} />
        {STATUS_LABEL[status]}
        <span className="font-normal">{issues.length}</span>
      </h2>
      {foldIssues(issues, expanded).map((i) => (
        <Link
          key={i.id}
          to={issueRoute(i.id)}
          className="flex items-center gap-3 border-b border-separator px-1 py-2 text-sm hover:bg-surface-hover"
        >
          <span className="w-10 shrink-0 text-xs text-muted">#{i.id}</span>
          <StatusDot status={i.status} />
          <OwnerDot agents={agents} owner={i.owner} />
          <span className="min-w-0 flex-1 truncate">{i.title}</span>
          {i.priority !== 'none' && <span className="shrink-0 text-xs text-muted">{PRIORITY_LABEL[i.priority]}</span>}
          <span className="w-20 shrink-0 truncate text-right text-xs text-muted">{assigneeLabel(agents, i.assignee)}</span>
        </Link>
      ))}
      <FoldToggle total={issues.length} expanded={expanded} onToggle={() => setExpanded(!expanded)} />
    </section>
  )
}

/** 依赖图里边的颜色：悬停相连、依赖已完成/取消、依赖还没完成 */
const EDGE_COLOR = {
  hover: { stroke: 'stroke-accent', fill: 'fill-accent' },
  settled: { stroke: 'stroke-muted opacity-60', fill: 'fill-muted' },
  open: { stroke: 'stroke-warning', fill: 'fill-warning' },
} as const

/** 依赖图：依赖在左、依赖它的在右；卡片按状态打点、owner 色条，边在依赖未完成时是实线（还挡着下游），完成/取消后为虚线 */
function DependencyGraph({ issues, agents, scope }: { issues: Issue[]; agents: Agent[] | undefined; scope: GraphScope }) {
  // 悬停的卡片：高亮与它直接相连的边
  const [hover, setHover] = useState<number | null>(null)
  const shown = graphIssues(issues, scope)
  if (shown.length === 0)
    return <EmptyHint>{scope === 'open' ? '没有未完成的依赖关系' : '还没有带依赖关系的 issue'}</EmptyHint>
  const g = layoutIssueGraph(shown)
  const touches = (e: { from: number; to: number }) => hover !== null && (e.from === hover || e.to === hover)
  return (
    <div className="overflow-auto px-4 py-3">
      <div className="relative" style={{ width: g.width, height: g.height }}>
        <svg className="pointer-events-none absolute inset-0" width={g.width} height={g.height} aria-hidden>
          {/* 箭头按边的颜色各备一个：context-stroke 在旧浏览器上不生效 */}
          <defs>
            {Object.entries(EDGE_COLOR).map(([kind, color]) => (
              <marker key={kind} id={`issue-arrow-${kind}`} viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto">
                <path d="M0,0 L10,5 L0,10 z" className={color.fill} />
              </marker>
            ))}
          </defs>
          {/* 高亮的边最后画，压在别的边上面 */}
          {[...g.edges]
            .sort((a, b) => Number(touches(a)) - Number(touches(b)))
            .map((e) => {
              const kind = touches(e) ? 'hover' : e.settled ? 'settled' : 'open'
              return (
                <path
                  key={`${e.from}-${e.to}`}
                  d={edgePath(e.points)}
                  fill="none"
                  markerEnd={`url(#issue-arrow-${kind})`}
                  strokeWidth={kind === 'hover' ? 2.5 : 1.5}
                  strokeDasharray={e.settled ? '5 4' : undefined}
                  className={EDGE_COLOR[kind].stroke}
                />
              )
            })}
        </svg>
        {g.nodes.map(({ issue, x, y }) => {
          const hue = ownerHue(agents, issue.owner)
          return (
            <Link
              key={issue.id}
              to={issueRoute(issue.id)}
              onMouseEnter={() => setHover(issue.id)}
              onMouseLeave={() => setHover(null)}
              className={`absolute flex flex-col justify-between gap-1 rounded-lg border border-l-4 border-separator bg-surface px-2.5 py-1.5 text-sm shadow-sm transition-colors hover:bg-surface-hover ${
                hue === null ? '' : 'owner-color border-l-(--owner-fg)'
              } ${issue.status === 'done' || issue.status === 'cancelled' ? 'opacity-60' : ''}`}
              style={{ ...ownerStyle(hue), left: x, top: y, width: NODE_WIDTH, height: NODE_HEIGHT }}
              title={`#${issue.id} ${issue.title}`}
            >
              <span className="line-clamp-2 leading-snug font-medium">{issue.title}</span>
              <span className="flex items-center gap-1.5 text-xs text-muted">
                <StatusDot status={issue.status} />
                <span>#{issue.id}</span>
                <span>{STATUS_LABEL[issue.status]}</span>
                <span className="ml-auto truncate">→ {assigneeLabel(agents, issue.assignee)}</span>
              </span>
            </Link>
          )
        })}
      </div>
    </div>
  )
}

function NewIssueForm({ agents, onCreated }: { agents: Agent[] | undefined; onCreated: () => void }) {
  const [title, setTitle] = useState('')
  const [description, setDescription] = useState('')
  const [status, setStatus] = useState<IssueStatus>('todo')
  const [priority, setPriority] = useState<IssuePriority>('none')
  const [assignee, setAssignee] = useState(UNASSIGNED)
  // 负责的项目必填：不猜
  const [owner, setOwner] = useState<string | null>(null)
  const [dependsOn, setDependsOn] = useState('')
  const parsed = parseIssueRefs(dependsOn)
  const action = useAction()
  const submit = () =>
    action.run(async () => {
      if (owner === null) throw new Error('请选择负责的项目（owner）')
      if (parsed === null) throw new Error('依赖只能填 issue 编号，如 #12, #15')
      await api.createIssue({
        title,
        owner,
        description,
        status,
        priority,
        assignee: assigneeOf(assignee),
        depends_on: parsed,
      })
      setTitle('')
      setDescription('')
      setDependsOn('')
      onCreated()
    })
  return (
    <CollapsibleForm title="新建 issue">
      <TextInput label="标题" value={title} onChange={setTitle} isRequired />
      <TextAreaInput label="描述（Markdown）" value={description} onChange={setDescription} rows={4} />
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <OwnerSelect
          agents={agents}
          label="负责项目（owner）"
          hint="转派给别人做也不变"
          value={owner}
          onChange={setOwner}
        />
        <SelectInput
          label="状态"
          value={status}
          onChange={(v) => setStatus(v as IssueStatus)}
          options={STATUSES.map((s) => ({ id: s, label: STATUS_LABEL[s] }))}
        />
        <SelectInput
          label="优先级"
          value={priority}
          onChange={(v) => setPriority(v as IssuePriority)}
          options={PRIORITIES.map((p) => ({ id: p, label: PRIORITY_LABEL[p] }))}
        />
        <SelectInput label="指派给" value={assignee} onChange={setAssignee} options={assigneeOptions(agents)} />
      </div>
      <TextInput label="依赖的 issue（编号，逗号分隔，可不填）" value={dependsOn} onChange={setDependsOn} />
      <p className="text-xs text-muted">
        指派给 agent 且状态为 Todo / 进行中时，会唤醒它来处理；有依赖的要等依赖全部完成或取消后才唤醒。
      </p>
      <ErrorAlert error={action.error} />
      <ActionButton
        isPending={action.busy}
        isDisabled={!title.trim() || owner === null || parsed === null}
        onPress={submit}
      >
        创建
      </ActionButton>
    </CollapsibleForm>
  )
}

export default function Issues() {
  const [view, setView] = useState<View>(() => (localStorage.getItem(VIEW_KEY) as View | null) ?? 'board')
  const agents = useAgents()
  // 按负责项目筛选：看一个项目的全部 issue（含已完成、已取消）
  const [owner, setOwner] = useState(ALL_OWNERS)
  const loaded = useLoad(() => api.issues(owner === ALL_OWNERS ? undefined : owner), [owner])
  // 拖拽后先在本地挪动，接口返回、重新拉取之后以服务端为准；连着拖几张时每张各占一项
  const [moving, setMoving] = useState<ReadonlyMap<number, PendingMove>>(new Map())
  const issues = loaded.data && moveIssues(loaded.data, moving)
  // 拖拽失败的错误：留到下一轮拖拽（之前的都已结束）开始时才清掉，不被同时进行的另一次冲掉
  const [moveError, setMoveError] = useState<string | null>(null)
  // agent 的运行里可能改了 issue：运行结束、或重连后重新拉取
  useRefetch('issues', loaded.reload)
  const changeView = (v: View) => {
    localStorage.setItem(VIEW_KEY, v)
    setView(v)
  }
  const [graphScope, setGraphScope] = useState<GraphScope>(
    () => (localStorage.getItem(GRAPH_SCOPE_KEY) as GraphScope | null) ?? 'open',
  )
  const changeGraphScope = (s: GraphScope) => {
    localStorage.setItem(GRAPH_SCOPE_KEY, s)
    setGraphScope(s)
  }
  const onMove = (id: number, status: IssueStatus) => {
    const pending: PendingMove = { status, at: new Date().toISOString() }
    if (moving.size === 0) setMoveError(null)
    setMoving((prev) => new Map(prev).set(id, pending))
    void (async () => {
      try {
        await api.updateIssue(id, { status })
      } catch (e) {
        setMoveError(errorMessage(e))
      } finally {
        await loaded.reload()
        // 同一张又被拖了一次：那一次还在提交，留着它的
        setMoving((prev) => {
          if (prev.get(id) !== pending) return prev
          const next = new Map(prev)
          next.delete(id)
          return next
        })
      }
    })()
  }
  const toolbar = (
    <div className="flex flex-col gap-3">
      <Segmented
        label="视图"
        value={view}
        onChange={changeView}
        options={[
          { id: 'board', label: '看板' },
          { id: 'list', label: '列表' },
          { id: 'graph', label: '依赖图' },
        ]}
      />
      {view === 'graph' && (
        <Segmented
          label="依赖图范围"
          value={graphScope}
          onChange={changeGraphScope}
          options={[
            { id: 'open', label: '有未完成的依赖链' },
            { id: 'all', label: '全部依赖链' },
          ]}
        />
      )}
      <OwnerSelect
        agents={agents.data}
        label="负责项目"
        value={owner}
        onChange={setOwner}
        leading={[{ id: ALL_OWNERS, label: '所有项目' }]}
      />
      <NewIssueForm agents={agents.data} onCreated={() => void loaded.reload()} />
      <ErrorAlert error={moveError} />
    </div>
  )
  return (
    <Page title="Issues" wide={view !== 'list'}>
      {view === 'board' ? (
        <>
          <div className="mx-auto w-full max-w-3xl px-4 pt-3">{toolbar}</div>
          <Loading state={{ ...loaded, data: issues }}>{(list) => <Board issues={list} agents={agents.data} onMove={onMove} />}</Loading>
        </>
      ) : view === 'graph' ? (
        <>
          <div className="mx-auto w-full max-w-3xl px-4 pt-3">{toolbar}</div>
          <Loading state={{ ...loaded, data: issues }}>
            {(list) => <DependencyGraph issues={list} agents={agents.data} scope={graphScope} />}
          </Loading>
        </>
      ) : (
        <>
          {toolbar}
          <Loading state={{ ...loaded, data: issues }}>{(list) => <List issues={list} agents={agents.data} />}</Loading>
        </>
      )}
    </Page>
  )
}
