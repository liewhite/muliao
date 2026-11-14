import { useEffect, useState } from 'react'
import { Button, Card } from '@heroui/react'
import { MessageSquare, Pencil, Play, Trash2 } from 'lucide-react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import {
  api,
  asAgentId,
  asIssueId,
  sessionRoute,
  type Actor,
  type Agent,
  type IssueDetail,
  type IssueId,
  type SessionId,
} from '../api'
import { useReturnState, useRouteParam } from '../nav'
import {
  PRIORITIES,
  PRIORITY_LABEL,
  STATUSES,
  STATUS_LABEL,
  activityText,
  assigneeOf,
  assigneeOptions,
  assigneeValue,
  issueRefs,
  issueRoute,
  parseIssueRefs,
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
  formatTime,
  useAction,
  useAgents,
  useConfirm,
} from '../ui'
import { agentLabel } from '../agentGroups'
import { useLoad } from '../load'
import { useDraft } from '../drafts'
import { OwnerSelect, StatusDot } from './Issues'

function actorName(agents: Agent[] | undefined, actor: Actor) {
  switch (actor.kind) {
    case 'user':
      return '我'
    case 'system':
      return '系统'
    case 'agent':
      return agentLabel(agents, actor.agent_id)
  }
}

/** 这条动态发生在哪个会话（agent 动手时所在的会话）：点开看过程；用户的操作、会话已删除时没有 */
function SourceSession({ sessionId }: { sessionId: SessionId | null }) {
  const from = useReturnState()
  if (sessionId === null) return null
  return (
    <Link
      to={sessionRoute(sessionId)}
      state={from}
      className="flex shrink-0 items-center text-muted hover:text-accent"
      aria-label="来源会话"
      title="打开做这件事的会话"
    >
      <MessageSquare className="size-3.5" />
    </Link>
  )
}

function Timeline({ detail }: { detail: IssueDetail }) {
  const agents = useAgents().data
  return (
    <ol className="flex flex-col gap-3">
      {detail.activities.map((a) =>
        a.kind === 'comment' ? (
          <li key={a.id}>
            <Card>
              <Card.Content className="flex flex-col gap-1 p-3">
                <div className="flex items-center gap-2 text-xs text-muted">
                  <span className="font-medium text-foreground">{actorName(agents, a.actor)}</span>
                  <span>{formatTime(a.created_at)}</span>
                  <SourceSession sessionId={a.session_id} />
                </div>
                <Markdown text={a.body ?? ''} />
              </Card.Content>
            </Card>
          </li>
        ) : (
          <li key={a.id} className="flex items-center gap-2 px-1 text-xs text-muted">
            <span className="size-1.5 shrink-0 rounded-full bg-muted" aria-hidden />
            <span className="font-medium text-foreground">{actorName(agents, a.actor)}</span>
            <span className="min-w-0 break-words">{activityText(agents, a)}</span>
            <span className="ml-auto shrink-0">{formatTime(a.created_at)}</span>
            <SourceSession sessionId={a.session_id} />
          </li>
        ),
      )}
    </ol>
  )
}

function IssueLinks({ ids }: { ids: IssueId[] }) {
  const from = useReturnState()
  return ids.map((i) => (
    <Link key={i} to={issueRoute(i)} state={from} className="text-accent">
      #{i}
    </Link>
  ))
}

/** 编辑标题、正文与依赖：只提交改过的字段（编辑期间 agent 可能改了另一个；drafts.useDraft） */
function Editor({ detail, onDone }: { detail: IssueDetail; onDone: () => void }) {
  const { issue } = detail
  const form = useDraft({
    title: issue.title,
    description: issue.description,
    depends_on: issue.depends_on.map((i) => `#${i}`).join(', '),
  })
  const parsed = parseIssueRefs(form.draft.depends_on)
  const save = useAction()
  return (
    <Card>
      <Card.Content className="flex flex-col gap-3">
        <TextInput label="标题" value={form.draft.title} onChange={(title) => form.set({ title })} isRequired />
        <TextAreaInput
          label="描述（Markdown）"
          value={form.draft.description}
          onChange={(description) => form.set({ description })}
          rows={8}
        />
        <TextInput
          label="依赖的 issue（编号，逗号分隔；全部完成或取消后才交给被指派的 agent）"
          value={form.draft.depends_on}
          onChange={(depends_on) => form.set({ depends_on })}
        />
        {parsed === null && <p className="text-xs text-danger">只能填 issue 编号，如 #12, #15</p>}
        <ErrorAlert error={save.error} />
        <div className="flex gap-2">
          <ActionButton
            isPending={save.busy}
            isDisabled={!form.draft.title.trim() || parsed === null}
            onPress={() =>
              void save.run(async () => {
                // 依赖按解析后的编号提交、比较：只改了空格不算改过；基线是由编号拼出来的，解析回去就是原来的编号
                const { depends_on: _text, ...text } = form.changes
                const baseDeps = parseIssueRefs(form.base.depends_on) ?? []
                const changedDeps = parsed !== null && parsed.join() !== baseDeps.join()
                if (Object.keys(text).length > 0 || changedDeps) {
                  await api.updateIssue(issue.id, {
                    ...text,
                    ...(changedDeps && { depends_on: parsed }),
                  })
                }
                onDone()
              })
            }
          >
            保存
          </ActionButton>
          <Button variant="ghost" onPress={onDone}>
            取消
          </Button>
        </div>
      </Card.Content>
    </Card>
  )
}

// 指派给用户的 issue 在这里审核（推送通知点开就是这一页；Chromium 通知上的「打回」带 ?review=reject 打开，聚焦理由）
function Review({ issueId }: { issueId: IssueId }) {
  const [params] = useSearchParams()
  const [reason, setReason] = useState('')
  const review = useAction()
  const reject = () => {
    if (review.busy || !reason.trim()) return
    void review.run(async () => {
      await api.rejectIssue(issueId, reason)
      setReason('')
    })
  }
  return (
    <Card>
      <Card.Content className="flex flex-col gap-2">
        <p className="text-sm font-medium">待你审核</p>
        <TextAreaInput
          label="打回理由（记成评论，回到待办并指派回交给你的 agent）"
          value={reason}
          onChange={setReason}
          rows={2}
          autoFocus={params.get('review') === 'reject'}
          onSubmit={reject}
        />
        <ErrorAlert error={review.error} />
        <div className="flex gap-2">
          <ActionButton
            isPending={review.busy}
            onPress={() =>
              void review.run(() => api.approveIssue(issueId))
            }
          >
            通过
          </ActionButton>
          <ActionButton variant="danger-soft" isPending={review.busy} isDisabled={!reason.trim()} onPress={reject}>
            打回
          </ActionButton>
        </div>
      </Card.Content>
    </Card>
  )
}

function Issue({ detail }: { detail: IssueDetail }) {
  const agents = useAgents()
  const navigate = useNavigate()
  const from = useReturnState()
  const confirm = useConfirm()
  const [editing, setEditing] = useState(false)
  const [comment, setComment] = useState('')
  const change = useAction()
  const send = useAction()
  const { issue } = detail
  const patch = (body: Parameters<typeof api.updateIssue>[1]) =>
    void change.run(() => api.updateIssue(issue.id, body))
  const remove = async () => {
    if (!(await confirm({ title: `删除 #${issue.id}？`, body: '评论与时间线一起删除，agent 的会话保留。', danger: true }))) return
    await change.run(() => api.deleteIssue(issue.id), { leave: () => navigate('/issues') })
  }
  const postComment = () => {
    // 快捷键不经过按钮的禁用状态：请求进行中再按一次不能重复发表
    if (send.busy || !comment.trim()) return
    void send.run(async () => {
      await api.commentIssue(issue.id, comment)
      setComment('')
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
      {/* 标题下面先放属性、审核与关联会话（打开页面最常看、最常改的），再放正文；编辑时标题和正文一起在编辑器里 */}
      {editing ? (
        <Editor
          detail={detail}
          onDone={() => setEditing(false)}
        />
      ) : (
        <h2 className="flex items-center gap-2 text-xl font-semibold">
          <StatusDot status={issue.status} />
          {issue.title}
        </h2>
      )}

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <OwnerSelect
          assignable
          label="负责项目（owner）"
          value={issue.owner}
          placeholder="无负责项目"
          onChange={(owner) => patch({ owner })}
          isDisabled={change.busy}
        />
        <SelectInput
          label="状态"
          value={issue.status}
          onChange={(status) => patch({ status })}
          options={STATUSES.map((s) => ({ id: s, label: STATUS_LABEL[s] }))}
          isDisabled={change.busy}
        />
        <SelectInput
          label="优先级"
          value={issue.priority}
          onChange={(priority) => patch({ priority })}
          options={PRIORITIES.map((p) => ({ id: p, label: PRIORITY_LABEL[p] }))}
          isDisabled={change.busy}
        />
        <SelectInput
          label="指派给"
          value={assigneeValue(issue.assignee)}
          onChange={(v) => patch({ assignee: assigneeOf(v) })}
          options={assigneeOptions(agents.data, issue.assignee)}
          isDisabled={change.busy}
        />
      </div>
      {issue.startable && (
        <div className="flex items-center gap-2">
          <ActionButton
            size="sm"
            isPending={change.busy}
            onPress={() =>
              void change.run(() => api.startIssue(issue.id))
            }
          >
            <Play className="size-4" />
            开工
          </ActionButton>
          <span className="text-xs text-muted">指派给负责项目的 agent，并进入进行中</span>
        </div>
      )}
      <ErrorAlert error={change.error} />

      {(issue.depends_on.length > 0 || issue.blocks.length > 0) && (
        <div className="flex flex-col gap-1 text-sm">
          {issue.depends_on.length > 0 && (
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-muted">依赖</span>
              <IssueLinks ids={issue.depends_on} />
              <span className="text-muted">
                {issue.waiting_on.length > 0 ? `等待 ${issueRefs(issue.waiting_on)} 完成` : '已全部完成'}
              </span>
            </div>
          )}
          {issue.blocks.length > 0 && (
            <div className="flex flex-wrap items-center gap-2">
              <span className="text-muted">被依赖</span>
              <IssueLinks ids={issue.blocks} />
            </div>
          )}
        </div>
      )}

      {sessions.length > 0 && (
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-sm">
          <span className="text-muted">关联会话</span>
          {sessions.map(([agentId, sessionId]) => (
            <Link
              key={sessionId}
              to={sessionRoute(sessionId)}
              state={from}
              className="flex items-center gap-1 text-accent"
            >
              <MessageSquare className="size-4" />
              {/* JSON 对象的键只能是字符串：会话表按 agent id 为键 */}
              {agentLabel(agents.data, asAgentId(agentId))} 的会话
            </Link>
          ))}
        </div>
      )}

      {issue.reviewable && <Review issueId={issue.id} />}

      {!editing && (
        <div className="border-t border-separator pt-3">
          {issue.description.trim() ? <Markdown text={issue.description} /> : <p className="text-sm text-muted">没有描述</p>}
        </div>
      )}

      <h3 className="mt-2 text-sm font-semibold text-muted">动态</h3>
      <Timeline detail={detail} />

      <Card>
        <Card.Content className="flex flex-col gap-2">
          <TextAreaInput label="评论" value={comment} onChange={setComment} rows={3} onSubmit={postComment} />
          <ErrorAlert error={send.error} />
          <ActionButton isPending={send.busy} isDisabled={!comment.trim()} onPress={postComment}>
            发表评论
          </ActionButton>
        </Card.Content>
      </Card>
    </Page>
  )
}

export default function IssueView() {
  const id = useRouteParam('id')
  const issueId = asIssueId(Number(id))
  const detail = useLoad(() => api.issue(issueId), [issueId])
  // 看到了 issue 就看到了请你审核它的通知（与打开会话一样）
  useEffect(() => {
    void api
      .markLinkRead(issueRoute(issueId))
      .catch((e) => console.error('marking issue notifications read failed', e))
  }, [issueId])
  return (
    <Loading state={detail}>{(d) => <Issue detail={d} />}</Loading>
  )
}
