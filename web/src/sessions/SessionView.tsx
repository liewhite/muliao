import { useEffect, useRef, useState } from 'react'
import { Button, Chip, toast } from '@heroui/react'
import { Archive, ArrowDown, CornerLeftUp, Trash2 } from 'lucide-react'
import { Link, useNavigate } from 'react-router-dom'
import { api, asSessionId, type IssueId, type SessionDetail, type SessionId } from '../api'
import { agentRoute, issueRoute, sessionRoute } from '../routes'
import { AgentName } from '../agents/agentList'
import { StatusDot } from '../issues/issueParts'
import { STATUS_LABEL } from '../issues/model'
import { useServerEvents, type PageEvent } from '../events'
import { useReturnState, useRouteParam } from '../nav'
import { cacheHitPercent, contextText, formatTokens } from '../usage'
import { ActionButton, ErrorAlert, Loading, Page, formatTime, useAction, useConfirm } from '../ui'
import { useLoad } from '../load'
import { unlinkedThreads } from './blocks'
import { useUploads, type Uploads } from './attachments'
import { appendTranscript, useVoiceInput } from './voice'
import { useLiveRun } from './liveRun'
import { useFollowBottom } from './followBottom'
import { Transcript, UnlinkedThreads } from './Transcript'
import { WaitsPanel } from './WaitsPanel'
import { Composer } from './Composer'
import type { SessionContext } from './sessionParts'

/**
 * issue 会话的页头标题：指回它的 issue，显示编号、标题、当前状态（随 issue 变化刷新）；在 issue 页返回回到这个会话。
 * 取不到 issue（如已删除，会话保留）时显示会话自己的标题和原因
 */
function IssueTitle({ issueId, fallback }: { issueId: IssueId; fallback: string }) {
  const from = useReturnState()
  const detail = useLoad(() => api.issue(issueId), [issueId])
  const issue = detail.data?.issue
  return (
    <Link
      to={issueRoute(issueId)}
      state={from}
      className="flex min-w-0 items-center gap-1.5 hover:text-accent"
    >
      {issue && <StatusDot status={issue.status} />}
      <span className="min-w-0 truncate">{issue ? `#${issueId} ${issue.title}` : fallback}</span>
      {issue && <span className="shrink-0 text-xs font-normal text-muted">{STATUS_LABEL[issue.status]}</span>}
      {detail.error && <span className="shrink-0 text-xs font-normal text-danger">{detail.error}</span>}
    </Link>
  )
}

/** 有文字或附件即可发送；附件还在上传或有失败的未移除时不能发送 */
const canSend = (draft: string, uploads: Uploads) => (draft.trim() !== '' || uploads.ids.length > 0) && !uploads.blocking

/**
 * 输入框上方的一行小字（不随内容滚动：会话从底部看起，运行中也看得到）：发给模型的上下文现在多大、到多大会压缩，
 * 以及这个会话累计花掉的 token（输入已含缓存读写）。两者都随每一步保存更新：每次模型请求完成就记账，
 * 运行中只差正在进行的那一次请求
 */
function UsageLine({ context, usage }: Pick<SessionDetail, 'context' | 'usage'>) {
  return (
    <div className="flex flex-wrap items-center gap-x-2 px-1 text-xs text-muted">
      <span title="发给模型的上下文：现在的大小 / 压缩阈值（超过就压缩）">
        上下文 {contextText(context.tokens, context.threshold_tokens)}
      </span>
      <span title="这个会话累计花掉的 token（每次模型请求完成就记上）">
        累计 输入 {formatTokens(usage.input_tokens)} · 输出 {formatTokens(usage.output_tokens)} · 缓存命中{' '}
        {cacheHitPercent(usage.input_tokens, usage.cache_read_tokens)}% · {usage.requests} 次请求
      </span>
    </div>
  )
}

/** 会话页按哪些事件重新拉取：连上了（之前可能丢了事件），或这个会话的历史或运行状态变了（saved：又保存了一步，
 * 或运行、压缩开始、结束、失败、停止、重试） */
function refetchesSession(e: PageEvent, id: SessionId): boolean {
  if (e.type === 'connected') return true
  return e.type === 'saved' && e.session_id === id
}

/** 用户看过了这个会话：指向它的通知标为已读（会话的未读由通知派生），标记本身幂等 */
function markSessionRead(id: SessionId) {
  void api.markRead(id).catch((e) => console.error('marking session read failed', e))
}

export default function SessionView() {
  const id = asSessionId(useRouteParam('id'))
  const navigate = useNavigate()
  const confirm = useConfirm()
  // 整份会话（含全部历史，大的有几 MB）不随任何会话的 changed 重新拉取：只按这个会话自己的事件刷新（连上了、saved），
  // 用户的操作之后照常一起刷新（load.refreshShown）
  const detail = useLoad(() => api.session(id), [id], (e) => refetchesSession(e, id))
  const [draft, setDraft] = useState('')
  const uploads = useUploads()
  const action = useAction()
  // 压缩单独一个：它要做完才返回（几秒到几分钟），期间停止等其他操作照常可用
  const compaction = useAction()
  // 压缩中用户点了停止：压缩请求随后以「已停止」结束，那是用户要的结果，不当错误显示
  const stoppedCompaction = useRef(false)
  const bottom = useFollowBottom()
  // 说完就发：转写出的文字接在已输入的内容后面，连同附件一起发出；暂时发不了（附件还在上传、上一条还在发）
  // 或发送失败时留在输入框里，不丢
  const voice = useVoiceInput((text) => {
    const content = appendTranscript(draft, text)
    if (action.busy || !canSend(content, uploads)) setDraft(content)
    else void send(content).then((ok) => ok || setDraft(content))
  })

  const live = useLiveRun(id, detail)
  // 看着它时又来了指向它的通知（会话的未读由通知派生）：用户已经在看，直接标记已读
  useServerEvents((e) => {
    if (e.type === 'notification' && e.session_id === id) markSessionRead(id)
  })

  // 打开会话即标记已读（看着它时新来的通知见上面的事件处理）
  useEffect(() => markSessionRead(id), [id])

  // 打开会话时从底部看起（每个会话一个页面实例，只在挂载时）
  const { pin } = bottom
  useEffect(() => pin(), [pin])

  /** content：要发的文字，默认是输入框里的；语音输入时是输入框内容接上转写结果 */
  async function send(content: string = draft) {
    // 快捷键不经过按钮的禁用状态：请求进行中再按一次不能重复发送
    if (action.busy || !canSend(content, uploads)) return false
    const sent = uploads.batch()
    const shown = draft
    const ok = await action.run(() => api.reply(id, content.trim(), sent.ids))
    if (ok) {
      // 自己发的消息总要看到
      pin()
      // 发送过程中又改了输入框就保留新内容
      setDraft((current) => (current === shown ? '' : current))
      uploads.removeMany(sent.keys)
    }
    return ok
  }

  async function remove() {
    const ok = await confirm({
      title: '删除这个会话？',
      body: '会话的全部消息都会被删除；它开出的 thread 保留。',
      confirmLabel: '删除',
      danger: true,
    })
    if (ok) await action.run(() => api.deleteSession(id), { leave: () => navigate(parentRoute) })
  }

  const session = detail.data?.session
  const parent = session?.origin.kind === 'delegation' ? session.origin.parent_session_id : null
  const parentRoute = parent ? sessionRoute(parent) : '/'
  return (
    <Page
      scrollRef={bottom.scrollRef}
      back={parentRoute}
      title={
        session?.origin.kind === 'issue' ? (
          <IssueTitle issueId={session.origin.issue_id} fallback={session.title} />
        ) : (
          (session?.title ?? '会话')
        )
      }
      actions={
        <>
          <ActionButton
            size="sm"
            variant="secondary"
            isDisabled={action.busy}
            isPending={compaction.busy}
            onPress={() => {
              stoppedCompaction.current = false
              void compaction.run(() =>
                api.compact(id).catch((e: unknown) => {
                  if (stoppedCompaction.current) toast('压缩已停止')
                  else throw e
                }),
              )
            }}
            aria-description="把早期对话压缩成摘要，减少发给模型的上下文"
          >
            {!compaction.busy && <Archive className="size-4" />}
            压缩
          </ActionButton>
          <Button isIconOnly size="sm" variant="ghost" onPress={() => void remove()} aria-label="删除会话">
            <Trash2 className="size-4" />
          </Button>
        </>
      }
      footer={
        // 底部安全区由下面的导航栏留出，这里不重复
        <div className="border-t border-separator bg-surface px-3 pt-2 pb-2">
          <div className="relative mx-auto flex w-full max-w-3xl flex-col gap-2">
            {bottom.atBottom === false && (
              <Button
                isIconOnly
                size="sm"
                variant="secondary"
                className="absolute -top-14 right-0 rounded-full shadow"
                aria-label="回到底部"
                onPress={pin}
              >
                <ArrowDown className="size-4" />
              </Button>
            )}
            <ErrorAlert error={action.error ?? compaction.error ?? voice.error} />
            {detail.data && <UsageLine context={detail.data.context} usage={detail.data.usage} />}
            <Composer
              draft={draft}
              setDraft={setDraft}
              uploads={uploads}
              voice={voice}
              busy={action.busy}
              sendable={canSend(draft, uploads)}
              // 压缩中也能停止（会话此时是空闲的）
              active={compaction.busy || (detail.data !== undefined && detail.data.state !== 'idle')}
              onSend={() => void send()}
              onStop={() => {
                if (compaction.busy) stoppedCompaction.current = true
                void action.run(() => api.stop(id))
              }}
            />
          </div>
        </div>
      }
    >
      <Loading state={detail}>
        {(d) => {
          const ctx: SessionContext = {
            answer: (callId, answers) => api.answer(id, callId, answers),
            approve: (callId, choice) => api.approve(id, callId, choice),
            threads: new Map(d.threads.map((t) => [t.id, t])),
            view: 'history',
          }
          return (
            <div className="flex flex-col gap-3">
              <div className="flex items-center justify-center gap-2">
                <Link to={agentRoute(d.session.agent_id)}>
                  <Chip size="sm" color="accent" variant="soft">
                    <AgentName id={d.session.agent_id} />
                  </Chip>
                </Link>
                <span className="text-xs text-muted">{formatTime(d.session.created_at)}</span>
              </div>
              {d.session.origin.kind === 'delegation' && parent && (
                <Link to={parentRoute} className="flex items-center justify-center gap-1 text-xs text-accent">
                  <CornerLeftUp className="size-3.5" />
                  来自上级会话的 thread「{d.session.origin.thread}」
                </Link>
              )}
              <Transcript detail={d} live={live} ctx={ctx} onRetry={() => void action.run(() => api.retry(id))} />
              <WaitsPanel
                waits={d.waits}
                ctx={ctx}
                actions={{
                  agentId: d.session.agent_id,
                  busy: action.busy,
                  // 结束、停止之后占用要等结局交回才释放，那时的运行会让页面重新拉取；操作之后先拉一次显示即时的状态
                  run: (fn) => void action.run(fn),
                }}
              />
              <UnlinkedThreads threads={unlinkedThreads([...d.items.map((i) => i.item), ...live], d.threads)} />
              <div ref={bottom.contentEndRef} aria-hidden />
            </div>
          )
        }}
      </Loading>
    </Page>
  )
}
