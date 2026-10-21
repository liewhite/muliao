import { useState } from 'react'
import { Button, Card } from '@heroui/react'
import { MessageSquare, Pencil, Trash2 } from 'lucide-react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { api, asAgentId, sessionRoute, type Agent, type IssueDetail, type IssuePriority, type IssueStatus } from '../api'
import { useServerEvents } from '../events'
import {
  PRIORITIES,
  PRIORITY_LABEL,
  STATUSES,
  STATUS_LABEL,
  activityText,
  assigneeOf,
  assigneeOptions,
  assigneeValue,
} from '../issues'
import {
  ActionButton,
  ErrorAlert,
  Loading,
  Markdown,
  Page,
  SelectInput,
  TextAreaInput,
  TextInput,
  agentLabel,
  formatTime,
  useAction,
  useAgents,
  useConfirm,
  useLoad,
} from '../ui'
import { StatusDot } from './Issues'

function actorName(agents: Agent[] | undefined, actor: string | null) {
  return actor === null ? '我' : agentLabel(agents, asAgentId(actor))
}

function Timeline({ detail, agents }: { detail: IssueDetail; agents: Agent[] | undefined }) {
  return (
    <ol className="flex flex-col gap-3">
      {detail.activities.map((a) =>
        a.kind === 'comment' ? (
          <li key={a.id}>
            <Card>
              <Card.Content className="flex flex-col gap-1 p-3">
                <div className="flex items-center gap-2 text-xs text-muted">
                  <span className="font-medium text-foreground">{actorName(agents, a.actor_agent_id)}</span>
                  <span>{formatTime(a.created_at)}</span>
                </div>
                <Markdown text={a.body ?? ''} />
              </Card.Content>
            </Card>
          </li>
        ) : (
          <li key={a.id} className="flex items-center gap-2 px-1 text-xs text-muted">
            <span className="size-1.5 shrink-0 rounded-full bg-muted" aria-hidden />
            <span className="font-medium text-foreground">{actorName(agents, a.actor_agent_id)}</span>
            <span className="min-w-0">{activityText(agents, a)}</span>
            <span className="ml-auto shrink-0">{formatTime(a.created_at)}</span>
          </li>
        ),
      )}
    </ol>
  )
}

function Editor({ detail, onSaved, onCancel }: { detail: IssueDetail; onSaved: () => void; onCancel: () => void }) {
  const [title, setTitle] = useState(detail.issue.title)
  const [description, setDescription] = useState(detail.issue.description)
  const save = useAction()
  return (
    <Card>
      <Card.Content className="flex flex-col gap-3">
        <TextInput label="标题" value={title} onChange={setTitle} isRequired />
        <TextAreaInput label="描述（Markdown）" value={description} onChange={setDescription} rows={8} />
        <ErrorAlert error={save.error} />
        <div className="flex gap-2">
          <ActionButton
            isPending={save.busy}
            isDisabled={!title.trim()}
            onPress={() =>
              void save.run(async () => {
                await api.updateIssue(detail.issue.id, { title, description })
                onSaved()
              })
            }
          >
            保存
          </ActionButton>
          <Button variant="ghost" onPress={onCancel}>
            取消
          </Button>
        </div>
      </Card.Content>
    </Card>
  )
}

function Issue({ detail, reload }: { detail: IssueDetail; reload: () => Promise<void> }) {
  const agents = useAgents()
  const navigate = useNavigate()
  const confirm = useConfirm()
  const [editing, setEditing] = useState(false)
  const [comment, setComment] = useState('')
  const change = useAction()
  const send = useAction()
  const { issue } = detail
  const patch = (body: Parameters<typeof api.updateIssue>[1]) =>
    void change.run(async () => {
      await api.updateIssue(issue.id, body)
      await reload()
    })
  const remove = async () => {
    if (!(await confirm({ title: `删除 #${issue.id}？`, body: '评论与时间线一起删除，agent 的会话保留。', danger: true }))) return
    await change.run(async () => {
      await api.deleteIssue(issue.id)
      navigate('/issues')
    })
  }
  const sessions = Object.entries(issue.sessions)

  return (
    <Page
      title={`#${issue.id}`}
      back="/issues"
      actions={
        <>
          <Button isIconOnly variant="ghost" size="sm" aria-label="编辑" onPress={() => setEditing(true)}>
            <Pencil className="size-4" />
          </Button>
          <Button isIconOnly variant="ghost" size="sm" aria-label="删除" onPress={() => void remove()}>
            <Trash2 className="size-4" />
          </Button>
        </>
      }
    >
      {editing ? (
        <Editor
          detail={detail}
          onCancel={() => setEditing(false)}
          onSaved={() => {
            setEditing(false)
            void reload()
          }}
        />
      ) : (
        <div className="flex flex-col gap-2">
          <h2 className="flex items-center gap-2 text-xl font-semibold">
            <StatusDot status={issue.status} />
            {issue.title}
          </h2>
          {issue.description.trim() ? <Markdown text={issue.description} /> : <p className="text-sm text-muted">没有描述</p>}
        </div>
      )}

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
        <SelectInput
          label="状态"
          value={issue.status}
          onChange={(v) => patch({ status: v as IssueStatus })}
          options={STATUSES.map((s) => ({ id: s, label: STATUS_LABEL[s] }))}
          isDisabled={change.busy}
        />
        <SelectInput
          label="优先级"
          value={issue.priority}
          onChange={(v) => patch({ priority: v as IssuePriority })}
          options={PRIORITIES.map((p) => ({ id: p, label: PRIORITY_LABEL[p] }))}
          isDisabled={change.busy}
        />
        <SelectInput
          label="指派给"
          value={assigneeValue(issue.assignee)}
          onChange={(v) => patch({ assignee: assigneeOf(v) })}
          options={assigneeOptions(agents.data)}
          isDisabled={change.busy}
        />
      </div>
      <ErrorAlert error={change.error} />

      {sessions.length > 0 && (
        <div className="flex flex-wrap gap-2">
          {sessions.map(([agentId, sessionId]) => (
            <Link key={sessionId} to={sessionRoute(sessionId)} className="flex items-center gap-1 text-sm text-accent">
              <MessageSquare className="size-4" />
              {agentLabel(agents.data, asAgentId(agentId))} 的会话
            </Link>
          ))}
        </div>
      )}

      <h3 className="mt-2 text-sm font-semibold text-muted">动态</h3>
      <Timeline detail={detail} agents={agents.data} />

      <Card>
        <Card.Content className="flex flex-col gap-2">
          <TextAreaInput label="评论" value={comment} onChange={setComment} rows={3} />
          <ErrorAlert error={send.error} />
          <ActionButton
            isPending={send.busy}
            isDisabled={!comment.trim()}
            onPress={() =>
              void send.run(async () => {
                await api.commentIssue(issue.id, comment)
                setComment('')
                await reload()
              })
            }
          >
            发表评论
          </ActionButton>
        </Card.Content>
      </Card>
    </Page>
  )
}

export default function IssueView() {
  const { id = '' } = useParams()
  const issueId = Number(id)
  const detail = useLoad(() => api.issue(issueId), [issueId])
  useServerEvents((e) => {
    if (e.type === 'reconnected' || (e.type === 'task' && e.kind === 'run')) void detail.reload()
  })
  return (
    <Loading state={detail}>{(d) => <Issue detail={d} reload={detail.reload} />}</Loading>
  )
}
