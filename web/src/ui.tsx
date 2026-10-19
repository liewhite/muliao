import { useCallback, useEffect, useState, type ReactNode } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { useNavigate } from 'react-router-dom'

export function errorMessage(e: unknown): string {
  return e instanceof Error ? e.message : String(e)
}

export interface Loaded<T> {
  data: T | undefined
  error: string | null
  loading: boolean
  reload: () => Promise<void>
}

/** 加载数据；deps 变化时重新加载。错误原样展示，不吞掉。 */
export function useLoad<T>(load: () => Promise<T>, deps: unknown[]): Loaded<T> {
  const [data, setData] = useState<T>()
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const run = useCallback(load, deps)
  const reload = useCallback(async () => {
    try {
      setData(await run())
      setError(null)
    } catch (e) {
      setError(errorMessage(e))
    } finally {
      setLoading(false)
    }
  }, [run])
  useEffect(() => {
    setLoading(true)
    void reload()
  }, [reload])
  return { data, error, loading, reload }
}

/** 包装一个用户操作：执行中禁用按钮，失败时展示错误。 */
export function useAction() {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const run = useCallback(async (action: () => Promise<unknown>) => {
    setBusy(true)
    setError(null)
    try {
      await action()
      return true
    } catch (e) {
      setError(errorMessage(e))
      return false
    } finally {
      setBusy(false)
    }
  }, [])
  return { busy, error, run, setError }
}

export function ErrorBanner({ error }: { error: string | null }) {
  if (!error) return null
  return <div className="error">{error}</div>
}

export function Page({
  title,
  back,
  actions,
  children,
}: {
  title: ReactNode
  back?: string
  actions?: ReactNode
  children: ReactNode
}) {
  const navigate = useNavigate()
  return (
    <div className="page">
      <header className="page-header">
        {back !== undefined && (
          <button className="icon-button" onClick={() => navigate(back)} aria-label="返回">
            ‹
          </button>
        )}
        <h1>{title}</h1>
        <div className="header-actions">{actions}</div>
      </header>
      <main className="page-body">{children}</main>
    </div>
  )
}

export function Loading<T>({ state, children }: { state: Loaded<T>; children: (data: T) => ReactNode }) {
  if (state.error) return <ErrorBanner error={state.error} />
  if (state.data === undefined) return <div className="muted center">加载中…</div>
  return <>{children(state.data)}</>
}

export function Markdown({ text }: { text: string }) {
  return (
    <div className="markdown">
      <ReactMarkdown remarkPlugins={[remarkGfm]}>{text}</ReactMarkdown>
    </div>
  )
}

const dateFormat = new Intl.DateTimeFormat(undefined, {
  month: 'numeric',
  day: 'numeric',
  hour: '2-digit',
  minute: '2-digit',
})

export function formatTime(iso: string): string {
  return dateFormat.format(new Date(iso))
}

export function relativeTime(iso: string): string {
  const diff = (Date.now() - new Date(iso).getTime()) / 1000
  if (diff < 60) return '刚刚'
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`
  return formatTime(iso)
}

export function Field({ label, hint, children }: { label: string; hint?: ReactNode; children: ReactNode }) {
  return (
    <label className="field">
      <span className="field-label">{label}</span>
      {children}
      {hint && <span className="field-hint">{hint}</span>}
    </label>
  )
}

export function confirmAction(message: string): boolean {
  return window.confirm(message)
}
