import { Component, type ReactNode } from 'react'

/**
 * 渲染出错时在原地显示错误和重新加载按钮，而不是让 React 卸载整棵树、整个应用白屏。
 * 这里不吞错：错误原样展示并打到 console，修复要回到出错的组件。
 */
export class ErrorBoundary extends Component<{ children: ReactNode }, { error: Error | null }> {
  state = { error: null as Error | null }

  static getDerivedStateFromError(error: Error) {
    return { error }
  }

  componentDidCatch(error: Error) {
    console.error('render failed', error)
  }

  render() {
    const { error } = this.state
    if (error === null) return this.props.children
    return (
      <div className="flex flex-col gap-3 p-4 pt-[calc(env(safe-area-inset-top)+1rem)]">
        <div className="font-semibold text-danger">页面出错了</div>
        <pre className="whitespace-pre-wrap break-all rounded-lg bg-surface-secondary p-3 text-xs">{error.message}</pre>
        <button type="button" className="self-start rounded-lg border border-separator px-3 py-1.5" onClick={() => location.reload()}>
          重新加载
        </button>
      </div>
    )
  }
}
