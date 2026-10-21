import { useRef, useState } from 'react'
import { Card, Chip } from '@heroui/react'
import { DndContext, MouseSensor, TouchSensor, useDraggable, useDroppable, useSensor, useSensors, type DragEndEvent } from '@dnd-kit/core'
import { Link, useNavigate } from 'react-router-dom'
import { api, type Agent, type Issue, type IssuePriority, type IssueStatus } from '../api'
import { useServerEvents } from '../events'
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
  ownerLabel,
  groupByStatus,
  issueRoute,
  moveIssue,
  stalled,
  WORK_LABEL,
} from '../issues'
import {
  ActionButton,
  CollapsibleForm,
  EmptyHint,
  ErrorAlert,
  Loading,
  Page,
  Segmented,
  SelectInput,
  TextAreaInput,
  TextInput,
  relativeTime,
  useAction,
  useAgents,
  useLoad,
} from '../ui'

type View = 'board' | 'list'
// 「所有项目」选项的 id；查询时映射为不过滤
const ALL_OWNERS = '@all'
const VIEW_KEY = 'muliao.issues.view'

export function StatusDot({ status }: { status: IssueStatus }) {
  return <span className={`inline-block size-3 shrink-0 rounded-full border-2 ${STATUS_COLOR[status]}`} aria-hidden />
}

function IssueCard({ issue, agents }: { issue: Issue; agents: Agent[] | undefined }) {
  return (
    <Card className="cursor-pointer transition-colors hover:bg-surface-hover">
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
              <Chip size="sm" variant="secondary">
                {ownerLabel(agents, issue.owner)}
              </Chip>
            )}
            <Chip size="sm" variant="soft">
              → {assigneeLabel(agents, issue.assignee)}
            </Chip>
            {stalled(issue) ? (
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
        if (e.key === 'Enter') onOpen(issue.id)
      }}
    >
      <IssueCard issue={issue} agents={agents} />
    </div>
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
      {issues.map((i) => (
        <DraggableCard key={i.id} issue={i} agents={agents} onOpen={onOpen} />
      ))}
    </section>
  )
}

function Board({ issues, agents, onMove }: { issues: Issue[]; agents: Agent[] | undefined; onMove: (id: number, s: IssueStatus) => void }) {
  // 鼠标拖动一小段才算拖拽，点一下是打开；触屏长按才拖，滑动仍是滚动。不用 PointerSensor：触屏上它与浏览器的滚动
  // 抢同一串指针事件，滑动一开始就被 pointercancel 打断，拖不起来
  const sensors = useSensors(
    useSensor(MouseSensor, { activationConstraint: { distance: 6 } }),
    useSensor(TouchSensor, { activationConstraint: { delay: 250, tolerance: 8 } }),
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
        <section key={s} className="flex flex-col">
          <h2 className="flex items-center gap-2 py-1 text-sm font-semibold text-muted">
            <StatusDot status={s} />
            {STATUS_LABEL[s]}
            <span className="font-normal">{groups[s].length}</span>
          </h2>
          {groups[s].map((i) => (
            <Link
              key={i.id}
              to={issueRoute(i.id)}
              className="flex items-center gap-3 border-b border-separator px-1 py-2 text-sm hover:bg-surface-hover"
            >
              <span className="w-10 shrink-0 text-xs text-muted">#{i.id}</span>
              <StatusDot status={i.status} />
              <span className="min-w-0 flex-1 truncate">{i.title}</span>
              {i.priority !== 'none' && <span className="shrink-0 text-xs text-muted">{PRIORITY_LABEL[i.priority]}</span>}
              <span className="w-20 shrink-0 truncate text-right text-xs text-muted">{assigneeLabel(agents, i.assignee)}</span>
            </Link>
          ))}
        </section>
      ))}
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
  const action = useAction()
  const submit = () =>
    action.run(async () => {
      if (owner === null) throw new Error('请选择负责的项目（owner）')
      await api.createIssue({ title, owner, description, status, priority, assignee: assigneeOf(assignee) })
      setTitle('')
      setDescription('')
      onCreated()
    })
  return (
    <CollapsibleForm title="新建 issue">
      <TextInput label="标题" value={title} onChange={setTitle} isRequired />
      <TextAreaInput label="描述（Markdown）" value={description} onChange={setDescription} rows={4} />
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <SelectInput
          label="负责项目（owner）"
          hint="转派给别人做也不变"
          value={owner}
          onChange={setOwner}
          options={(agents ?? []).map((a) => ({ id: a.id as string, label: a.name }))}
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
      <p className="text-xs text-muted">指派给 agent 且状态为 Todo / 进行中时，会唤醒它来处理。</p>
      <ErrorAlert error={action.error} />
      <ActionButton isPending={action.busy} isDisabled={!title.trim() || owner === null} onPress={submit}>
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
  // 拖拽后先在本地挪动，接口返回、重新拉取之后以服务端为准
  const [moving, setMoving] = useState<{ id: number; status: IssueStatus } | null>(null)
  const issues = loaded.data && moving ? moveIssue(loaded.data, moving.id, moving.status) : loaded.data
  const move = useAction()
  useServerEvents((e) => {
    // agent 的运行里可能改了 issue：运行结束、或重连后重新拉取
    if (e.type === 'reconnected' || (e.type === 'task' && e.kind === 'run')) void loaded.reload()
  })
  const changeView = (v: View) => {
    localStorage.setItem(VIEW_KEY, v)
    setView(v)
  }
  const onMove = (id: number, status: IssueStatus) => {
    setMoving({ id, status })
    void move.run(async () => {
      try {
        await api.updateIssue(id, { status })
      } finally {
        await loaded.reload()
        setMoving(null)
      }
    })
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
        ]}
      />
      <SelectInput
        label="负责项目"
        value={owner}
        onChange={setOwner}
        options={[{ id: ALL_OWNERS, label: '所有项目' }, ...(agents.data ?? []).map((a) => ({ id: a.id as string, label: a.name }))]}
      />
      <NewIssueForm agents={agents.data} onCreated={() => void loaded.reload()} />
      <ErrorAlert error={move.error} />
    </div>
  )
  return (
    <Page title="Issues" wide={view === 'board'}>
      {view === 'board' ? (
        <>
          <div className="mx-auto w-full max-w-3xl px-4 pt-3">{toolbar}</div>
          <Loading state={{ ...loaded, data: issues }}>{(list) => <Board issues={list} agents={agents.data} onMove={onMove} />}</Loading>
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
