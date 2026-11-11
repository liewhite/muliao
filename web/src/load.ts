// 从接口加载数据，并在它变了时重新拉取（归属见 docs/architecture.md 2.4 第 16、17 行）。一份数据读了哪些类别由它请求的
// 接口声明（api.collectReads，后端路由上的 x-reads），页面不手写；哪类数据变了只有一个来源：服务端的 changed 事件，加上
// 重连（之前可能丢了事件）。什么时候重新拉取只由 Loader 决定：保存之后不手动重新拉取，操作成功后正在显示的数据一起
// 重新拉取（refreshShown），别处的改动由 changed 带来。

import { useMemo, useSyncExternalStore } from 'react'
import { collectReads, type Resource } from './api'
import { coalesced, listenServerEvents, type PageEvent } from './events'

export function errorMessage(e: unknown): string {
  return e instanceof Error ? e.message : String(e)
}

export interface LoadState<T> {
  data: T | undefined
  error: string | null
  loading: boolean
}

export interface Loaded<T> extends LoadState<T> {
  /** 立即重新拉取，等最新一次请求的结果落下（只在 Loader 之外的原因要刷新时用，如文件浏览的「刷新」按钮） */
  reload: () => Promise<void>
}

/**
 * 哪些事件要重新拉取。默认按读过的类别：连上了（之前可能丢了事件），或读过的某类数据变了；什么都没读的（可选模型、配置、
 * 文件预览）不随服务端变化，连上了也不重拉。按更细的事件刷新的页面自己给
 * （会话页：整份历史几 MB，只按这个会话的事件；通知页：只在新通知时，留住这次的未读标记）。
 */
export type RefetchOn = (e: PageEvent, reads: ReadonlySet<Resource>) => boolean

export const byReads: RefetchOn = (e, reads) =>
  reads.size > 0 && (e.type === 'connected' || (e.type === 'changed' && reads.has(e.resource)))

// 有人在看、随服务端数据变化的 Loader：用户的操作成功后立即一起重新拉取（refreshShown）
const shown = new Set<Loader<unknown>>()

/**
 * 用户的操作（ui.useAction）成功后调用：正在显示、随服务端变化的数据立即重新拉取、等它落下，操作的「进行中」盖住刷新，
 * 不靠 SSE 的 changed 送到（连接半开时可能要等几十秒）。每个请求都有时限（api.REQUEST_TIMEOUT_MS），不会一直等。
 */
export async function refreshShown(): Promise<void> {
  await Promise.all([...shown].filter((l) => l.live()).map((l) => l.reload()))
}

/**
 * 一份数据：只让最新一次请求的结果落下（晚到的旧响应不能盖掉新值）；有人在看时听服务端事件，按 refetchOn 重新拉取（合并
 * 密集的事件，见 coalesced），没人看了就退订。重新拉取失败时保留已有数据、记下错误（Loading 照常显示已有内容）。
 */
export class Loader<T> {
  private state: LoadState<T> = { data: undefined, error: null, loading: true }
  private latest = 0
  private current: Promise<void> = Promise.resolve()
  // 最近落下的那次请求读过的类别，加上还在进行中的各次已经读到的（进行中读过的变了，它拿到的可能已是旧的）
  private reads: ReadonlySet<Resource> = new Set()
  private readonly inflight = new Set<Set<Resource>>()
  private readonly subscribers = new Set<() => void>()
  private unlisten: (() => void) | null = null
  private readonly refetch: () => void
  private readonly load: () => Promise<T>
  private readonly refetchOn: RefetchOn | null

  /** refetchOn 不给时按读过的类别（byReads） */
  constructor(load: () => Promise<T>, refetchOn: RefetchOn | null = null) {
    this.load = load
    this.refetchOn = refetchOn
    this.refetch = coalesced(this.reload)
  }

  /** 发一次请求；被更新的一次盖过时也等到最新那次落下才返回 */
  reload = async (): Promise<void> => {
    let mine = this.fetch(++this.latest)
    this.current = mine
    await mine
    while (this.current !== mine) {
      mine = this.current
      await mine
    }
  }

  private async fetch(id: number): Promise<void> {
    const reads = new Set<Resource>()
    this.inflight.add(reads)
    let next: LoadState<T>
    try {
      next = { data: await collectReads(this.load, reads), error: null, loading: false }
    } catch (e) {
      next = { ...this.state, error: errorMessage(e), loading: false }
    } finally {
      this.inflight.delete(reads)
    }
    if (id !== this.latest) return
    this.state = next
    // 失败时显示的还是上次的数据：订阅照上次的，加上这次已经发出的（第一次就失败的也能在连上、变了时重试）
    this.reads = next.error === null ? reads : new Set([...this.reads, ...reads])
    this.subscribers.forEach((s) => s())
  }

  private readsNow(): Set<Resource> {
    return new Set([this.reads, ...this.inflight].flatMap((r) => [...r]))
  }

  /** 随服务端数据变化（读过某类数据，或按自己的事件刷新）：操作之后要一起重新拉取。与 byReads 同一个判断 */
  live(): boolean {
    return this.refetchOn !== null || this.readsNow().size > 0
  }

  /** 第一个订阅者到来时开始听事件并拉取（之前拉到过的先照常显示），最后一个走了就退订 */
  subscribe = (onChange: () => void): (() => void) => {
    this.subscribers.add(onChange)
    if (this.subscribers.size === 1) {
      const refetchOn = this.refetchOn ?? byReads
      this.unlisten = listenServerEvents((e) => refetchOn(e, this.readsNow()) && this.refetch())
      shown.add(this as Loader<unknown>)
      void this.reload()
    }
    return () => {
      this.subscribers.delete(onChange)
      if (this.subscribers.size === 0) {
        this.unlisten?.()
        this.unlisten = null
        shown.delete(this as Loader<unknown>)
      }
    }
  }

  snapshot = (): LoadState<T> => this.state
}

/** 订阅一份 Loader：组件外建好的共享数据（agent 列表、未读数）也经它在组件里用 */
export function useLoader<T>(loader: Loader<T>): Loaded<T> {
  const state = useSyncExternalStore(loader.subscribe, loader.snapshot)
  return { ...state, reload: loader.reload }
}

/**
 * 加载一份数据，它读的数据变了（不管谁改的）就重新拉取；deps 变化时换一份新的（不显示旧参数的数据）。错误原样展示，不吞掉。
 * refetchOn 只给按更细的事件刷新的页面（见 RefetchOn）。
 */
export function useLoad<T>(load: () => Promise<T>, deps: unknown[], refetchOn: RefetchOn | null = null): Loaded<T> {
  // deps 决定这是哪一份数据：load 闭包随 deps 一起换；refetchOn 是页面固定的规则，换的时候也跟着 deps
  // oxlint-disable-next-line react-hooks/exhaustive-deps
  const loader = useMemo(() => new Loader(load, refetchOn), deps)
  return useLoader(loader)
}
