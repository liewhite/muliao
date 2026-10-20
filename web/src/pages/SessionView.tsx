import { useCallback, useEffect, useRef, useState, type KeyboardEvent, type ReactNode } from 'react'
import { Button, Chip, Disclosure, Spinner, TextArea, TextField } from '@heroui/react'
import { Archive, ArrowDown, BellDot, CircleStop, CornerLeftUp, GitBranch, Lock, RotateCcw, SendHorizontal, Square, Trash2 } from 'lucide-react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import {
  agentRoute,
  api,
  type Agent,
  type Attachment,
  type CompactionInfo,
  type Item,
  type QuestionAnswer,
  type Mail,
  type Task,
  type Session,
  sessionRoute,
} from '../api'
import {
  groupSteps,
  stepAttachments,
  stepThreads,
  unlinkedThreads,
  summarizeSteps,
  toBlocks,
  toolStatus,
  type AskBlock as AskBlockData,
  type Step,
  type ToolStatus,
  type ToolView,
} from '../blocks'
import { AnsweredQuestions, QuestionForm, QuestionNote } from '../questions'
import { AttachButton, AttachmentList, PendingUploads, useUploads, type Uploads } from '../attachments'
import { liveItems, mergeLive, useServerEvents, type RunEvent } from '../events'
import { refreshUnread, refreshUnreadLogged } from '../unread'
import { CODE_BLOCK, ErrorAlert, Loading, Markdown, Page, agentLabel, formatTime, useAction, useAgents, useConfirm, useLoad } from '../ui'

function Collapsible({ title, children }: { title: ReactNode; children: ReactNode }) {
  return (
    <Disclosure
      className="max-w-[88%] self-start rounded-xl border border-dashed border-separator px-3 py-1.5 text-sm text-muted"
    >
      <Disclosure.Heading>
        <Disclosure.Trigger className="flex w-full items-center gap-2 text-left">
          {title}
          <Disclosure.Indicator />
        </Disclosure.Trigger>
      </Disclosure.Heading>
      <Disclosure.Content>
        <Disclosure.Body className="pt-1">{children}</Disclosure.Body>
      </Disclosure.Content>
    </Disclosure>
  )
}

// 被退回不是故障：参数不合法或工具报了可纠正的错误，模型会据此修正后重试，所以不用醒目的报错样式
const TOOL_STATUS_LABELS: Record<ToolStatus, string> = { running: '运行中', rejected: '被退回', done: '完成' }

function ToolBlock({ tool }: { tool: ToolView }) {
  const status = toolStatus(tool)
  return (
    <Collapsible
      title={
        <>
          <span className="font-mono text-foreground">{tool.name}</span>
          <span className="ml-auto whitespace-nowrap text-xs">{TOOL_STATUS_LABELS[status]}</span>
        </>
      }
    >
      {tool.args && tool.args !== '{}' && <pre className={CODE_BLOCK}>{tool.args}</pre>}
      {tool.result !== null && <pre className={CODE_BLOCK}>{tool.result}</pre>}
    </Collapsible>
  )
}

function UserBubble({ text, attachments, pending }: { text: string; attachments: Attachment[]; pending?: boolean }) {
  return (
    <div className={`flex flex-col items-end gap-1.5 self-stretch ${pending ? 'opacity-70' : ''}`}>
      <AttachmentList files={attachments} align="end" />
      {text && (
        <div className="max-w-[88%] whitespace-pre-wrap break-words rounded-2xl rounded-br-md bg-accent px-3.5 py-2.5 text-accent-foreground">
          {text}
        </div>
      )}
    </div>
  )
}

function StepBlock({ step }: { step: Step }) {
  return step.kind === 'tool' ? (
    <ToolBlock tool={step.tool} />
  ) : (
    <Collapsible title="思考">
      <div className="whitespace-pre-wrap">{step.text}</div>
    </Collapsible>
  )
}

/** 连续的思考与工具调用收成一组，默认折叠；标题汇总步骤数与状态，展开后每一步仍可单独展开。工具发出的文件显示在组下方 */
function StepsGroup({ steps, ctx }: { steps: Step[]; ctx: SessionContext }) {
  const { label, running, rejected } = summarizeSteps(steps)
  const status = running !== null ? `运行中：${running}` : rejected > 0 ? `完成 · ${rejected} 次被退回` : '完成'
  return (
    <>
      <Collapsible
        title={
          <>
            {running !== null && <Spinner size="sm" />}
            <span className="text-foreground">{label}</span>
            <span className="ml-auto whitespace-nowrap text-xs">{status}</span>
          </>
        }
      >
        <div className="flex flex-col gap-2">
          {steps.map((step, i) => (
            <StepBlock key={i} step={step} />
          ))}
        </div>
      </Collapsible>
      {/* 工具发给用户的文件、委派开出的 thread 是成果，不随过程一起折叠，就显示在开它们的那次调用处 */}
      <AttachmentList files={stepAttachments(steps)} />
      <ThreadLinks ids={stepThreads(steps)} ctx={ctx} />
    </>
  )
}

/** 渲染会话条目要用到的会话信息：回答提问的方式、这个会话开出的 thread、agent 名称 */
interface SessionContext {
  answer: (callId: string, answers: QuestionAnswer[]) => Promise<unknown>
  threads: Map<string, Session>
  agents: Agent[] | undefined
  /**
   * 条目来自哪里：
   * - history：数据库里的历史，与会话的 thread 列表来自同一个响应，引用的 thread 缺失就是已被删除；
   * - live：运行中的条目，thread 可能刚开出、还没重新拉取
   */
  view: 'history' | 'live'
}

function AskBlock({ block, ctx }: { block: AskBlockData; ctx: SessionContext }) {
  const { ask } = block
  switch (ask.status) {
    case 'pending':
      return <QuestionForm questions={ask.questions} onSubmit={(answers) => ctx.answer(block.callId, answers)} />
    case 'asking':
      return <QuestionNote text="提问中…" />
    // 回答显示在信箱里
    case 'submitted':
      return null
    case 'answered':
      return <AnsweredQuestions answers={ask.answers} />
    case 'not_answered':
      return <QuestionNote text="没有回答这些问题" />
  }
}

/** 会话事件（后台任务完成、运行中的检查……）：不是用户说的话，单独显示，默认收起内容 */
function EventNote({ source, text }: { source: string; text: string }) {
  return (
    <Collapsible
      title={
        <>
          <BellDot className="size-3.5 shrink-0" />
          <span className="font-mono text-foreground">{source}</span>
          <span className="truncate">{text.split('\n', 1)[0]}</span>
        </>
      }
    >
      <pre className={CODE_BLOCK}>{text}</pre>
    </Collapsible>
  )
}

function Blocks({ items, ctx }: { items: Item[]; ctx: SessionContext }) {
  return (
    <>
      {groupSteps(toBlocks(items)).map((v, i) => {
        switch (v.kind) {
          case 'ask':
            return <AskBlock key={i} block={v.ask} ctx={ctx} />
          case 'stopped':
            return (
              <div key={i} className="flex items-center gap-1.5 self-start text-xs text-muted">
                <CircleStop className="size-3.5" />
                已停止
              </div>
            )
          case 'user':
            return <UserBubble key={i} text={v.text} attachments={v.attachments} />
          case 'event':
            return <EventNote key={i} source={v.source} text={v.text} />
          case 'assistant':
            return (
              <div key={i} className="max-w-[88%] self-start rounded-2xl rounded-bl-md bg-surface px-3.5 py-2.5 shadow-sm">
                <Markdown text={v.text} />
              </div>
            )
          case 'process':
            return <StepsGroup key={i} steps={v.steps} ctx={ctx} />
        }
      })}
    </>
  )
}

/** 按压缩点切分历史：每个压缩点之前的消息已由摘要代替发给模型（切点总在两轮之间）。 */
function History({ items, compactions, ctx }: { items: Item[]; compactions: CompactionInfo[]; ctx: SessionContext }) {
  const parts: ReactNode[] = []
  let rest = items
  for (const c of compactions) {
    const cut = rest.findIndex((i) => i.message_id !== null && i.message_id > c.through_message_id)
    const before = cut === -1 ? rest : rest.slice(0, cut)
    rest = cut === -1 ? [] : rest.slice(cut)
    parts.push(<Blocks key={`b${c.through_message_id}`} items={before} ctx={ctx} />)
    parts.push(
      <Disclosure key={`c${c.through_message_id}`} className="border-y border-dashed border-separator py-1.5 text-sm text-muted">
        <Disclosure.Heading>
          <Disclosure.Trigger className="flex w-full items-center justify-center gap-2">
            以上内容已压缩 · {formatTime(c.created_at)}
            <Disclosure.Indicator />
          </Disclosure.Trigger>
        </Disclosure.Heading>
        <Disclosure.Content>
          <Disclosure.Body className="pt-2">
            <Markdown text={c.summary} />
          </Disclosure.Body>
        </Disclosure.Content>
      </Disclosure>,
    )
  }
  parts.push(<Blocks key="rest" items={rest} ctx={ctx} />)
  return <>{parts}</>
}

/** 显示在历史之后的信件：排队中的显示为待处理，最近一次停止丢弃的划掉 */
function MailNote({ mail }: { mail: Mail }) {
  const discarded = mail.state === 'discarded'
  return (
    <div className={`flex flex-col gap-1.5 ${discarded ? 'line-through opacity-50' : ''}`}>
      {mail.kind === 'event' ? (
        <EventNote source={mail.source ?? '?'} text={mail.content} />
      ) : mail.kind === 'answer' ? (
        Object.values(mail.answers).map((result, i) =>
          result.tool === 'ask_user' ? (
            <AnsweredQuestions key={i} answers={result.result.answers} />
          ) : (
            <EventNote key={i} source="agent 的回答" text={result.result.text} />
          ),
        )
      ) : (
        <UserBubble text={mail.content} attachments={mail.attachments} pending={!discarded} />
      )}
    </div>
  )
}

/** 最近一次运行还没正常结束：排队、运行中、等重试，或失败（可重试） */
function RunStatus({ run, onRetry }: { run: Task; onRetry: (id: number) => void }) {
  if (run.status === 'failed') {
    return (
      <div className="flex flex-col gap-2 rounded-xl bg-danger-soft p-3 text-sm text-danger-soft-foreground">
        <div>运行失败了（尝试 {run.attempts} 次）</div>
        <pre className={CODE_BLOCK}>{run.error}</pre>
        <div className="text-xs">如果是上下文过长，先点右上角「压缩」，完成后再重试。重试从失败的那一步继续。</div>
        <Button size="sm" variant="secondary" className="self-start" onPress={() => onRetry(run.id)}>
          <RotateCcw className="size-4" />
          重试
        </Button>
      </div>
    )
  }
  return (
    <div className="flex items-center gap-2 self-end text-xs text-muted">
      {run.status === 'running' ? (
        <>
          <Spinner size="sm" /> 处理中…
        </>
      ) : run.attempts > 0 ? (
        `等待重试（${formatTime(run.not_before)}）`
      ) : (
        '排队中…'
      )}
      {run.error && <span>上次错误：{run.error}</span>}
    </div>
  )
}

function ThreadRow({ thread, agents }: { thread: Session; agents: Agent[] | undefined }) {
  return (
    <Link to={sessionRoute(thread.id)} className="flex items-center gap-2 rounded-lg px-2 py-1 hover:bg-surface-hover">
      <GitBranch className="size-3.5 shrink-0 text-muted" />
      <span className="truncate font-medium">{thread.thread}</span>
      <span className="truncate text-muted">{agentLabel(agents, thread.agent_id)}</span>
      {thread.kind === 'private_thread' && (
        <Chip size="sm" variant="soft">
          <Lock className="size-3" />
          私有
        </Chip>
      )}
      {thread.unread && <span className="size-2 shrink-0 rounded-full bg-accent" aria-label="未读" />}
      <span className="ml-auto shrink-0 text-xs text-muted">{formatTime(thread.updated_at)}</span>
    </Link>
  )
}

/** 一组步骤里委派开出或续写的 thread */
function ThreadLinks({ ids, ctx }: { ids: string[]; ctx: SessionContext }) {
  if (ids.length === 0) return null
  return (
    <div className="flex w-full max-w-[88%] flex-col self-start rounded-xl border border-separator px-1.5 py-1 text-sm">
      {ids.map((id) => {
        const thread = ctx.threads.get(id)
        if (thread) return <ThreadRow key={id} thread={thread} agents={ctx.agents} />
        return ctx.view === 'history' ? (
          <div key={id} className="flex items-center gap-2 px-2 py-1 text-muted">
            <GitBranch className="size-3.5" />
            thread 已删除
          </div>
        ) : (
          <Link key={id} to={sessionRoute(id)} className="flex items-center gap-2 rounded-lg px-2 py-1 text-accent hover:bg-surface-hover">
            <GitBranch className="size-3.5" />
            打开 thread
          </Link>
        )
      })}
    </div>
  )
}

/** 没有被任何条目引用的 thread（见 blocks.unlinkedThreads）：只能统一列在这里 */
function UnlinkedThreads({ threads, agents }: { threads: Session[]; agents: Agent[] | undefined }) {
  if (threads.length === 0) return null
  return (
    <div className="flex flex-col gap-1.5 rounded-xl border border-separator p-3 text-sm">
      <div className="text-muted">未关联到调用的 thread</div>
      {threads.map((t) => (
        <ThreadRow key={t.id} thread={t} agents={agents} />
      ))}
    </div>
  )
}

/** 有文字或附件即可发送；附件还在上传或有失败的未移除时不能发送 */
const canSend = (draft: string, uploads: Uploads) => (draft.trim() !== '' || uploads.ids.length > 0) && !uploads.blocking

function Composer({
  draft,
  setDraft,
  uploads,
  busy,
  running,
  onSend,
  onStop,
}: {
  draft: string
  setDraft: (v: string) => void
  uploads: Uploads
  busy: boolean
  /** 这个会话有一轮正在运行：显示停止按钮（运行中也可以继续发消息，会排在后面） */
  running: boolean
  onSend: () => void
  onStop: () => void
}) {
  function onKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    // 桌面端 Enter 发送、Shift+Enter 换行；输入法组字时不触发
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing && window.matchMedia('(pointer: fine)').matches) {
      e.preventDefault()
      onSend()
    }
  }
  return (
    <div className="flex flex-col gap-2">
      <PendingUploads uploads={uploads} />
      <div className="flex items-end gap-2">
        <AttachButton uploads={uploads} isDisabled={busy} />
        <TextField value={draft} onChange={setDraft} aria-label="回复" variant="secondary" className="flex-1">
          <TextArea
            placeholder="回复…"
            rows={Math.min(6, Math.max(1, draft.split('\n').length))}
            onKeyDown={onKeyDown}
            onPaste={uploads.onPaste}
            className="resize-none"
          />
        </TextField>
        {running && (
          <Button isIconOnly variant="danger-soft" isDisabled={busy} onPress={onStop} aria-label="停止">
            <Square className="size-4" />
          </Button>
        )}
        <Button isIconOnly isDisabled={busy || !canSend(draft, uploads)} onPress={onSend} aria-label="发送">
          <SendHorizontal className="size-4" />
        </Button>
      </div>
    </div>
  )
}

// 离底部这么近仍算「在底部」
const PINNED_SLACK_PX = 50

/**
 * 新内容到来时只在用户本来就在底部时跟随；往上翻看历史时不把人拽下来。
 *
 * 「在不在底部」只有一个来源 pinned，除 pin() 外只由用户的滚动改变：滚到离底部 50px 以内就钉住，向上滚离开就解除。不能每次
 * scroll 事件都按距底距离重算——程序滚到底产生的 scroll 事件要到下一帧才派发，那时内容可能又长了，会被误判成
 * 「离开了底部」；而内容变长时 scrollTop 只会不变或变大，不会被当成向上滚。高度变化（ResizeObserver：新数据、
 * 流式输出、图片加载完、输入框变高使容器变矮）时 pinned 为真就滚到底；内容变短时 scrollTop 被钳到最底，距底为 0，
 * 也不会被当成向上滚。打开会话、发送消息就是重新钉住。
 * 代价：在底部展开最后一个折叠块时，页面会跟着滚到它的末尾。
 */
function useFollowBottom() {
  const scroller = useRef<HTMLElement | null>(null)
  const pinned = useRef(true)
  // null：内容没挂载（加载中），不显示「回到底部」
  const [atBottom, setAtBottom] = useState<boolean | null>(null)
  const content = useRef<HTMLElement | null>(null)
  // 上一次看到的 scrollTop：比它小才是向上滚
  const lastTop = useRef(0)

  const toEnd = useCallback(() => {
    const el = scroller.current
    if (el === null) return
    el.scrollTop = el.scrollHeight
    lastTop.current = el.scrollTop
  }, [])
  const onScroll = useCallback(() => {
    const el = scroller.current
    if (el === null) return
    if (el.scrollHeight - el.scrollTop - el.clientHeight <= PINNED_SLACK_PX) pinned.current = true
    else if (el.scrollTop < lastTop.current) pinned.current = false
    lastTop.current = el.scrollTop
    setAtBottom(pinned.current)
  }, [])
  // 观察滚动容器（输入框变高时它变矮）与内容（新数据、流式输出、图片加载）的尺寸；两个 ref 各自登记，不依赖挂载先后
  const observer = useRef<ResizeObserver | null>(null)
  const resize = useCallback(
    () =>
      (observer.current ??= new ResizeObserver(() => {
        if (pinned.current) toEnd()
      })),
    [toEnd],
  )
  useEffect(() => () => observer.current?.disconnect(), [])

  const scrollRef = useCallback(
    (el: HTMLElement | null) => {
      if (scroller.current !== null) {
        scroller.current.removeEventListener('scroll', onScroll)
        resize().unobserve(scroller.current)
      }
      scroller.current = el
      if (el !== null) {
        el.addEventListener('scroll', onScroll, { passive: true })
        resize().observe(el)
      }
    },
    [onScroll, resize],
  )
  // 内容末尾的锚点：数据加载后才出现，观察它所在的内容容器
  const contentEndRef = useCallback(
    (el: HTMLDivElement | null) => {
      if (content.current !== null) resize().unobserve(content.current)
      content.current = el?.parentElement ?? null
      if (content.current === null) {
        setAtBottom(null)
        return
      }
      resize().observe(content.current)
      setAtBottom(pinned.current)
    },
    [resize],
  )
  /** 回到底部并跟随之后的新内容（打开会话、发送消息、点「回到底部」） */
  const pin = useCallback(() => {
    pinned.current = true
    setAtBottom(true)
    toEnd()
  }, [toEnd])
  return { scrollRef, contentEndRef, atBottom, pin }
}

export default function SessionView() {
  const { id = '' } = useParams()
  const navigate = useNavigate()
  const confirm = useConfirm()
  const detail = useLoad(() => api.session(id), [id])
  const agents = useAgents()
  const [live, setLive] = useState<RunEvent[]>([])
  const [draft, setDraft] = useState('')
  const uploads = useUploads()
  const action = useAction()
  const bottom = useFollowBottom()

  useServerEvents((e) => {
    if (e.type === 'reconnected') {
      setLive([])
      void detail.reload()
    } else if (e.session_id === id) {
      if (e.type === 'run') setLive((prev) => [...prev, e])
      // 又保存了一步：重新拉取历史；新快照换成新的一段，合并时丢掉上一段的事件
      else if (e.type === 'saved') void detail.reload()
      else if (e.kind === 'run' || e.kind === 'compaction') {
        // 运行状态变化后以数据库为准重新拉取，清空临时流
        if (e.status !== 'running') setLive([])
        void detail.reload()
      }
    }
  })

  // 中途打开或重新拉取时以快照为准，从这一段的开头显示，再接上经 SSE 收到、快照里还没有的事件
  const merged = mergeLive(detail.data?.live ?? null, live)
  const shownLive = merged.events
  const reload = detail.reload
  useEffect(() => {
    if (merged.gap) void reload()
  }, [merged.gap, reload])

  // 打开会话、以及看着它时又有了新回答，都标记已读。不按 session.unread 判断要不要调用：指向这个会话的通知
  // 是另一个事实（notify_user 写的通知可能出现在一轮失败、会话没变未读时），标记本身幂等
  const unread = detail.data?.session.unread
  useEffect(() => {
    void api
      .markRead(id)
      .then(refreshUnread)
      .catch((e) => console.error('marking session read failed', e))
  }, [id, unread])

  // 打开会话时从底部看起（每个会话一个页面实例，只在挂载时）
  const { pin } = bottom
  useEffect(() => pin(), [pin])

  async function send() {
    // Enter 键不经过按钮的禁用状态：请求进行中再按一次不能重复发送
    if (action.busy || !canSend(draft, uploads)) return
    const sent = uploads.batch()
    const text = draft
    const ok = await action.run(() => api.reply(id, text.trim(), sent.ids))
    if (ok) {
      // 自己发的消息总要看到
      pin()
      // 发送过程中又改了输入框就保留新内容
      setDraft((current) => (current === text ? '' : current))
      uploads.removeMany(sent.keys)
      await detail.reload()
    }
  }

  async function remove() {
    const ok = await confirm({
      title: '删除这个会话？',
      body: '会话的全部消息，以及它开出的私有 thread 都会被删除。',
      confirmLabel: '删除',
      danger: true,
    })
    if (ok && (await action.run(() => api.deleteSession(id)))) {
      // 指向它的通知随会话删除
      refreshUnreadLogged()
      navigate(parentRoute)
    }
  }

  const session = detail.data?.session
  const parentRoute = session?.parent_session_id ? sessionRoute(session.parent_session_id) : '/'
  return (
    <Page
      scrollRef={bottom.scrollRef}
      back={parentRoute}
      title={session?.title ?? '会话'}
      actions={
        <>
          <Button
            size="sm"
            variant="secondary"
            isDisabled={action.busy}
            onPress={() => void action.run(() => api.compact(id).then(() => detail.reload()))}
            aria-description="把早期对话压缩成摘要，减少发给模型的上下文"
          >
            <Archive className="size-4" />
            压缩
          </Button>
          {session?.deletable && (
            <Button isIconOnly size="sm" variant="ghost" onPress={() => void remove()} aria-label="删除会话">
              <Trash2 className="size-4" />
            </Button>
          )}
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
            <ErrorAlert error={action.error} />
            {session && !session.user_writable ? (
              <div className="flex items-center justify-center gap-1.5 py-1 text-sm text-muted">
                <Lock className="size-4" />
                这是另一个 agent 向它发起的私有 thread，只能查看
              </div>
            ) : (
              <Composer
                draft={draft}
                setDraft={setDraft}
                uploads={uploads}
                busy={action.busy}
                running={detail.data?.run?.status === 'running'}
                onSend={() => void send()}
                onStop={() => void action.run(() => api.stop(id))}
              />
            )}
          </div>
        </div>
      }
    >
      <Loading state={detail}>
        {(d) => {
          const ctx: SessionContext = {
            answer: (callId, answers) => api.answer(id, callId, answers).then(() => detail.reload()),
            threads: new Map(d.threads.map((t) => [t.id, t])),
            agents: agents.data,
            view: 'history',
          }
          return (
            <div className="flex flex-col gap-3">
              <div className="flex items-center justify-center gap-2">
                <Link to={agentRoute(d.session.agent_id)}>
                  <Chip size="sm" color="accent" variant="soft">
                    {agentLabel(agents.data, d.session.agent_id)}
                  </Chip>
                </Link>
                <span className="text-xs text-muted">{formatTime(d.session.created_at)}</span>
              </div>
              {d.session.parent_session_id && (
                <Link to={parentRoute} className="flex items-center justify-center gap-1 text-xs text-accent">
                  <CornerLeftUp className="size-3.5" />
                  来自上级会话的 thread「{d.session.thread}」
                </Link>
              )}
              <History items={d.items} compactions={d.compactions} ctx={ctx} />
              {d.mail.map((m) => (
                <MailNote key={m.id} mail={m} />
              ))}
              {d.run && <RunStatus run={d.run} onRetry={(tid) => void action.run(() => api.retryTask(tid)).then(() => detail.reload())} />}
              {shownLive.length > 0 && (
                <div className="flex flex-col gap-3 opacity-90">
                  <Blocks items={liveItems(shownLive)} ctx={{ ...ctx, view: 'live' }} />
                </div>
              )}
              <UnlinkedThreads threads={unlinkedThreads(d.items, d.threads)} agents={agents.data} />
              <div ref={bottom.contentEndRef} aria-hidden />
            </div>
          )
        }}
      </Loading>
    </Page>
  )
}
