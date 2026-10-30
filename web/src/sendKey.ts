/**
 * 多行输入框（会话输入框、issue 评论框、新会话）统一的发送快捷键：Enter 换行，⌘+Enter（Windows/Linux 上 Ctrl+Enter）发送。
 * 输入法组字时的 Enter 是确认候选词，不算。
 */
export function isSendShortcut(e: {
  key: string
  metaKey: boolean
  ctrlKey: boolean
  nativeEvent: { isComposing: boolean }
}): boolean {
  return e.key === 'Enter' && (e.metaKey || e.ctrlKey) && !e.nativeEvent.isComposing
}

/** 输入框占位提示里的快捷键说明 */
export const SEND_HINT = '⌘↩ 发送'
