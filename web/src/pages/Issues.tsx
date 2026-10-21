import { useState } from 'react'
import { Card, Chip } from '@heroui/react'
import { DndContext, PointerSensor, TouchSensor, useDraggable, useDroppable, useSensor, useSensors, type DragEndEvent } from '@dnd-kit/core'
import { Link } from 'react-router-dom'
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
const VIEW_KEY = 'muliao.issues.view'

export function StatusDot({ status }: { status: IssueStatus }) {
  return <span className={`inline-block size-3 shrink-0 rounded-full border-2 ${STATUS_COLOR[status]}`} aria-hidden />
}

function IssueCard({ issue, agents }: { issue: Issue; agents: Agent[] | undefined }) {
  return (
    <Link to={issueRoute(issue.id)}>
      <Card className="transition-colors hover:bg-surface-hover">
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
            <Chip size="sm" variant="soft">
              {assigneeLabel(agents, issue.assignee)}
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
    </Link>
  )
}

function DraggableCard({ issue, agents }: { issue: Issue; agents: Agent[] | undefined }) {
  const { attributes, listeners, setNodeRef, transform, isDragging } = useDraggable({ id: issue.id })
  const style = transform ? { transform: `translate3d(${transform.x}px, ${transform.y}px, 0)` } : undefined
  return (
    <div ref={setNodeRef} style={style} className={isDragging ? 'relative z-10 opacity-80' : ''} {...listeners} {...attributes}>
      <IssueCard issue={issue} agents={agents} />
    </div>
  )
}

function Column({ status, issues, agents }: { status: IssueStatus; issues: Issue[]; agents: Agent[] | undefined }) {
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
        <DraggableCard key={i.id} issue={i} agents={agents} />
      ))}
    </section>
  )
}

function Board({ issues, agents, onMove }: { issues: Issue[]; agents: Agent[] | undefined; onMove: (id: number, s: IssueStatus) => void }) {
  // 拖动一小段才算拖拽，点一下仍是打开 issue；触屏长按才拖，不挡横向滚动
  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 6 } }),
    useSensor(TouchSensor, { activationConstraint: { delay: 250, tolerance: 5 } }),
  )
  const groups = groupByStatus(issues)
  const onDragEnd = (e: DragEndEvent) => {
    if (e.over === null) return
    const status = e.over.id as IssueStatus
    const id = Number(e.active.id)
    if (issues.find((i) => i.id === id)?.status !== status) onMove(id, status)
  }
  return (
    <DndContext sensors={sensors} onDragEnd={onDragEnd}>
      <div className="flex min-h-full gap-3 overflow-x-auto px-4 py-3">
        {STATUSES.map((s) => (
          <Column key={s} status={s} issues={groups[s]} agents={agents} />
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
  const action = useAction()
  const submit = () =>
    action.run(async () => {
      await api.createIssue({ title, description, status, priority, assignee: assigneeOf(assignee) })
      setTitle('')
      setDescription('')
      onCreated()
    })
  return (
    <CollapsibleForm title="新建 issue">
      <TextInput label="标题" value={title} onChange={setTitle} isRequired />
      <TextAreaInput label="描述（Markdown）" value={description} onChange={setDescription} rows={4} />
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
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
      <ActionButton isPending={action.busy} isDisabled={!title.trim()} onPress={submit}>
        创建
      </ActionButton>
    </CollapsibleForm>
  )
}

export default function Issues() {
  const [view, setView] = useState<View>(() => (localStorage.getItem(VIEW_KEY) as View | null) ?? 'board')
  const agents = useAgents()
  const loaded = useLoad(() => api.issues(), [])
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
