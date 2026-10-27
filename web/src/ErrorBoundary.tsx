import { Component, type ReactNode } from 'react'

/**
 * 渲染出错时在原地显示错误和重新加载按钮，而不是让 React 卸载整棵树、整个应用白屏。
 * 这里不吞错：错误原样展示并打到 console，修复要回到出错的组件。
 * resetKey 变了（如换了页面）就清掉错误、重新渲染子树；没出错时它变了什么都不做（子树不重新挂载）。
 */
export class ErrorBoundary extends Component<
  { children: ReactNode; resetKey?: string },
  { error: Error | null; errorKey: string | undefined }
> {
  state = { error: null as Error | null, errorKey: undefined as string | undefined }

  static getDerivedStateFromError(error: Error) {
    return { error }
  }

  static getDerivedStateFromProps(
    props: { resetKey?: string },
    state: { error: Error | null; errorKey: string | undefined },
  ) {
    // 出错时记下当时的 resetKey；之后它变了就清掉错误
    if (state.error === null) return { errorKey: props.resetKey }
    return props.resetKey !== state.errorKey ? { error: null, errorKey: props.resetKey } : null
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
