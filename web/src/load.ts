// 从接口加载数据，并在它变了时重新拉取。哪类数据变了只有一个来源：服务端的 changed 事件（schema.ChangedEvent，由别的
// 数据派生的类别后端也一并发出），加上重连（之前可能丢了事件）。页面声明自己展示的是哪类数据，订阅在这里一处完成；
// 保存之后不用手动重新拉取：操作成功后正在显示的数据一起重新拉取（refreshShown），别处的改动由 changed 带来。

import { useMemo, useSyncExternalStore } from 'react'
import { coalesced, listenServerEvents, type PageEvent, type Resource } from './events'

export function errorMessage(e: unknown): string {
  return e instanceof Error ? e.message : String(e)
}

export interface LoadState<T> {
  data: T | undefined
  error: string | null
  loading: boolean
}

export interface Loaded<T> extends LoadState<T> {
  /** 立即重新拉取（不随 changed 自动刷新的数据才需要，见 useLoad 的 resource 为 null） */
  reload: () => Promise<void>
}

/** 展示 resource 的数据是否要重新拉取：连上了（之前可能丢了事件），或它变了 */
export const refetches = (e: PageEvent, resource: Resource) =>
  e.type === 'connected' || (e.type === 'changed' && e.resource === resource)

/**
 * 一份数据：只让最新一次请求的结果落下（晚到的旧响应不能盖掉新值）；有人在看时订阅 resource，变了就重新拉取（合并密集的
 * 事件，见 coalesced），没人看了就退订。重新拉取失败时保留已有数据、记下错误（Loading 照常显示已有内容）。
 */
// 有人在看、随服务端数据变化的 Loader：用户的操作成功后立即一起重新拉取（refreshShown）
const shown = new Set<Loader<unknown>>()

/**
 * 用户的操作（ui.useAction）成功后调用：正在显示的数据立即重新拉取、等它落下，操作的「进行中」盖住刷新，不靠 SSE 的
 * changed 送到（连接半开时可能要等几十秒）。resource 为 null 的页面自己刷新，不在其中。
 */
export async function refreshShown(): Promise<void> {
  await Promise.all([...shown].map((l) => l.reload()))
}

export class Loader<T> {
  private state: LoadState<T> = { data: undefined, error: null, loading: true }
  private latest = 0
  private readonly subscribers = new Set<() => void>()
  private unlisten: (() => void) | null = null
  private readonly refetch: () => void
  private readonly resource: Resource | null
  private readonly load: () => Promise<T>

  constructor(resource: Resource | null, load: () => Promise<T>) {
    this.resource = resource
    this.load = load
    this.refetch = coalesced(this.reload)
  }

  reload = async (): Promise<void> => {
    const id = ++this.latest
    let next: LoadState<T>
    try {
      next = { data: await this.load(), error: null, loading: false }
    } catch (e) {
      next = { ...this.state, error: errorMessage(e), loading: false }
    }
    if (id !== this.latest) return
    this.state = next
    this.subscribers.forEach((s) => s())
  }

  /** 第一个订阅者到来时开始订阅事件并拉取（之前拉到过的先照常显示），最后一个走了就退订 */
  subscribe = (onChange: () => void): (() => void) => {
    this.subscribers.add(onChange)
    if (this.subscribers.size === 1) {
      const resource = this.resource
      if (resource !== null) {
        this.unlisten = listenServerEvents((e) => refetches(e, resource) && this.refetch())
        shown.add(this as Loader<unknown>)
      }
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
 * 加载展示 resource 这类数据的内容，它变了（不管谁改的）就重新拉取；deps 变化时换一份新的（不显示旧参数的数据）。
 * resource 为 null：不随服务端的数据变化（如可选的模型列表），或页面自己按更细的事件刷新（会话页）。错误原样展示，不吞掉。
 */
export function useLoad<T>(resource: Resource | null, load: () => Promise<T>, deps: unknown[]): Loaded<T> {
  // deps 决定这是哪一份数据：load 闭包随 deps 一起换
  // oxlint-disable-next-line react-hooks/exhaustive-deps
  const loader = useMemo(() => new Loader(resource, load), [resource, ...deps])
  return useLoader(loader)
}
