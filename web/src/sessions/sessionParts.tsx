// 会话页的条目与等待项共用的小部件和会话信息

import type { ReactNode } from 'react'
import { Disclosure } from '@heroui/react'
import type { ApprovalChoice, CallId, QuestionAnswer, SessionId, Thread } from '../api'
import { AgentName } from '../agents/agentList'

/** lead：放在展开按钮之前、自己可以点的内容（如指向 thread 的链接），不能嵌在按钮里 */
export function Collapsible({ title, children, lead }: { title: ReactNode; children: ReactNode; lead?: ReactNode }) {
  return (
    <Disclosure className="max-w-[88%] self-start rounded-xl border border-dashed border-separator px-3 py-1.5 text-sm text-muted">
      <Disclosure.Heading className="flex items-center gap-2">
        {lead}
        <Disclosure.Trigger className="flex min-w-0 flex-1 items-center gap-2 text-left">
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

/** 渲染会话条目要用到的会话信息：回答提问的方式、这个会话开出的 thread、agent 名称 */
export interface SessionContext {
  answer: (callId: CallId, answers: QuestionAnswer[]) => Promise<unknown>
  /** 批准、拒绝或始终允许一次等批准的工具调用 */
  approve: (callId: CallId, choice: ApprovalChoice) => Promise<unknown>
  threads: Map<SessionId, Thread>
  /**
   * 条目来自哪里：
   * - history：数据库里的历史，与会话的 thread 列表来自同一个响应，引用的 thread 缺失就是已被删除；
   * - live：运行中的条目，thread 可能刚开出、还没重新拉取
   */
  view: 'history' | 'live'
}

/** thread 名与做它的 agent */
export function ThreadName({ thread }: { thread: Thread }) {
  return (
    <>
      {thread.thread}（<AgentName id={thread.agent_id} />）
    </>
  )
}
