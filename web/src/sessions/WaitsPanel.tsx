// 等待面板：会话在等的后台任务、委派、到点唤醒、提问与批准

import { Button } from '@heroui/react'
import {
  AlarmClock,
  GitBranch,
  Hourglass,
  MessageCircleQuestion,
  ShieldQuestion,
  Square,
  SquareTerminal,
} from 'lucide-react'
import { Link } from 'react-router-dom'
import { api, type AgentId, type Wait } from '../api'
import { sessionRoute } from '../routes'
import { CODE_BLOCK } from '../ui'
import { useNow } from './stepTime'
import { approvalAnchor, askAnchor, waitDetail, waitKey } from './waits'
import { Collapsible, ThreadName, type SessionContext } from './sessionParts'

/** 等待项上的操作：结束后台任务、停止 thread（经页面的 action，出错显示在输入框上方） */
export interface WaitActions {
  agentId: AgentId
  run: (fn: () => Promise<unknown>) => void
  busy: boolean
}

function WaitRow({ wait, now, ctx, actions }: { wait: Wait; now: number; ctx: SessionContext; actions: WaitActions }) {
  const detail = <span className="text-muted">{waitDetail(wait, now)}</span>
  switch (wait.kind) {
    case 'job': {
      const job = wait.job
      return (
        <Collapsible
          lead={
            <>
              <SquareTerminal className="size-3.5 shrink-0" />
              <span className="shrink-0 text-foreground">后台任务 #{wait.job_id}</span>
            </>
          }
          title={
            <span className="flex min-w-0 gap-2">
              <span className="truncate font-mono">{job.command.split('\n', 1)[0]}</span>
              <span className="shrink-0">{waitDetail(wait, now)}</span>
            </span>
          }
        >
          <div className="flex flex-col gap-2">
            <pre className={CODE_BLOCK}>{job.command}</pre>
            <div className="text-xs text-muted">日志 {job.log_path}（在 agent 工作目录里）最后几行：</div>
            <pre className={CODE_BLOCK}>{job.tail || '（还没有输出）'}</pre>
            {job.status !== 'running' ? (
              <div className="text-xs text-muted">已结束，等结局交回会话</div>
            ) : (
              <Button
                size="sm"
                variant="danger-soft"
                className="self-start"
                isDisabled={actions.busy}
                onPress={() => actions.run(() => api.killJob(actions.agentId, wait.job_id))}
              >
                <Square className="size-3.5" />
                结束任务
              </Button>
            )}
          </div>
        </Collapsible>
      )
    }
    case 'delegation': {
      const id = wait.thread_session_id
      const thread = ctx.threads.get(id)
      return (
        <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-0.5">
          <GitBranch className="size-3.5 shrink-0 text-muted" />
          <Link to={sessionRoute(id)} className="max-w-full truncate text-accent hover:underline">
            {thread ? <ThreadName thread={thread} /> : 'thread'}
          </Link>
          {detail}
          <Button
            size="sm"
            variant="ghost"
            className="ml-auto shrink-0"
            isDisabled={actions.busy}
            onPress={() => actions.run(() => api.stop(id))}
            aria-description="停止做这项委派的 thread，结局以「被停止」交回"
          >
            <Square className="size-3.5" />
            停止 thread
          </Button>
        </div>
      )
    }
    case 'wake':
      return (
        <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-0.5" title={wait.note}>
          <AlarmClock className="size-3.5 shrink-0 text-muted" />
          <span className="shrink-0">到点唤醒 {detail}</span>
          <span className="max-w-full truncate text-muted">{wait.note}</span>
        </div>
      )
    case 'ask':
      return (
        <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-0.5">
          <MessageCircleQuestion className="size-3.5 shrink-0 text-muted" />
          <span className="shrink-0">等你回答</span>
          {detail}
          <Button
            size="sm"
            variant="ghost"
            className="ml-auto shrink-0"
            onPress={() => document.getElementById(askAnchor(wait.call_id))?.scrollIntoView({ behavior: 'smooth' })}
          >
            跳到提问
          </Button>
        </div>
      )
    case 'approval':
      return (
        <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-0.5">
          <ShieldQuestion className="size-3.5 shrink-0 text-muted" />
          <span className="shrink-0">等你批准</span>
          <span className="font-mono">{detail}</span>
          <Button
            size="sm"
            variant="ghost"
            className="ml-auto shrink-0"
            onPress={() =>
              document.getElementById(approvalAnchor(wait.call_id))?.scrollIntoView({ behavior: 'smooth' })
            }
          >
            去批准
          </Button>
        </div>
      )
  }
}

/** 会话在等的东西（与工作状态同一条规则）：让人看得出它为什么挂起、在等谁 */
export function WaitsPanel({ waits, ctx, actions }: { waits: Wait[]; ctx: SessionContext; actions: WaitActions }) {
  const now = useNow(waits.length > 0)
  if (waits.length === 0) return null
  return (
    <div className="flex flex-col gap-1.5 rounded-xl border border-dashed border-separator p-3 text-sm">
      <div className="flex items-center gap-1.5 text-muted">
        <Hourglass className="size-3.5" />
        等待中
      </div>
      {waits.map((w) => (
        <WaitRow key={waitKey(w)} wait={w} now={now} ctx={ctx} actions={actions} />
      ))}
    </div>
  )
}
