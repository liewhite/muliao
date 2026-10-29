// agent 页「文件」：只读浏览 agent 的工作目录。左边目录树（展开时才加载那一层），右边预览（Markdown 渲染、代码高亮、图片）。
// 树用 react-aria-components 的 Tree（键盘导航、展开收起、无障碍），高亮用按需加载的 highlight.js。

import { useState } from 'react'
import { Button, Card } from '@heroui/react'
import { ChevronRight, Download, File, Folder, RefreshCw } from 'lucide-react'
import { Button as AriaButton, Tree, TreeItem, TreeItemContent, type Key } from 'react-aria-components'
import { api, workspaceFileUrl, type AgentId, type WorkspaceEntry } from '../api'
import { CODE_BLOCK, EmptyHint, ErrorAlert, Loading, Markdown, errorMessage, formatTime, useLoad } from '../ui'
import {
  MAX_HIGHLIGHT_BYTES,
  MAX_PREVIEW_BYTES,
  formatSize,
  isBinary,
  joinPath,
  languageOf,
  previewKind,
  splitPath,
} from '../workspaceFiles'

export function WorkspaceBrowser({ agent }: { agent: AgentId }) {
  const root = useLoad(() => api.workspace(agent, ''), [agent])
  // 展开过的子目录的内容（键是相对路径）；收起时保留，再展开不重新请求，点刷新才重新读
  const [dirs, setDirs] = useState<Record<string, WorkspaceEntry[]>>({})
  const [expanded, setExpanded] = useState<Set<Key>>(new Set())
  // 打开的文件的路径；它的大小、类型取自所在目录当前的列表，刷新后跟着更新
  const [opened, setOpened] = useState<string | null>(null)
  // 刷新时递增：预览跟着重新读
  const [version, setVersion] = useState(0)
  const [error, setError] = useState<string | null>(null)

  async function loadDir(path: string) {
    try {
      const entries = await api.workspace(agent, path)
      setDirs((d) => ({ ...d, [path]: entries }))
    } catch (e) {
      setError(errorMessage(e))
    }
  }

  function changeExpanded(keys: Set<Key>) {
    setExpanded(keys)
    for (const k of keys) if (!(String(k) in dirs)) void loadDir(String(k))
  }

  function toggle(path: string) {
    const next = new Set(expanded)
    if (next.has(path)) next.delete(path)
    else next.add(path)
    changeExpanded(next)
  }

  function refresh() {
    setError(null)
    void root.reload()
    // 展开着的重新读；收起的丢掉，下次展开时再读
    setDirs({})
    const again = new Set([...expanded].map(String))
    // 打开的文件所在的目录可能已收起，也要读，预览才有新的元信息
    if (opened) again.add(splitPath(opened)[0])
    again.delete('')
    for (const k of again) void loadDir(k)
    setVersion((v) => v + 1)
  }

  function renderLevel(dir: string, entries: WorkspaceEntry[] | undefined) {
    return entries?.map((entry) => {
      const path = joinPath(dir, entry.name)
      return (
        <TreeItem
          key={path}
          id={path}
          textValue={entry.name}
          hasChildItems={entry.is_dir}
          onAction={() => (entry.is_dir ? toggle(path) : setOpened(path))}
          className={({ isFocusVisible }) =>
            `cursor-pointer rounded-md text-sm outline-none ${isFocusVisible ? 'ring-2 ring-accent' : ''} ${
              opened === path ? 'bg-accent-soft text-accent-soft-foreground' : 'hover:bg-surface-secondary'
            }`
          }
        >
          <TreeItemContent>
            {({ level, isExpanded }) => (
              <div className="flex items-center gap-1 py-1 pe-2" style={{ paddingInlineStart: (level - 1) * 14 + 4 }}>
                {entry.is_dir ? (
                  <AriaButton slot="chevron" className="shrink-0 text-muted outline-none">
                    <ChevronRight className={`size-3.5 transition-transform ${isExpanded ? 'rotate-90' : ''}`} />
                  </AriaButton>
                ) : (
                  <span className="w-3.5 shrink-0" />
                )}
                {entry.is_dir ? (
                  <Folder className="size-4 shrink-0 text-muted" />
                ) : (
                  <File className="size-4 shrink-0 text-muted" />
                )}
                <span className="truncate">{entry.name}</span>
              </div>
            )}
          </TreeItemContent>
          {entry.is_dir && renderLevel(path, dirs[path])}
        </TreeItem>
      )
    })
  }

  function openedPreview(path: string) {
    const [parent, name] = splitPath(path)
    const listing = parent === '' ? root.data : dirs[parent]
    // 所在目录还在读（刷新中）
    if (listing === undefined) return null
    const entry = listing.find((e) => e.name === name)
    if (entry === undefined) return <EmptyHint>{path} 已不在工作目录里</EmptyHint>
    return <FilePreview key={`${path}-${version}`} agent={agent} path={path} entry={entry} />
  }

  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-center justify-between gap-2">
        <span className="text-sm text-muted">只读；展开目录时才读取那一层</span>
        <Button size="sm" variant="ghost" onPress={refresh}>
          <RefreshCw className="size-4" />
          刷新
        </Button>
      </div>
      <ErrorAlert error={error} />
      <div className="grid gap-3 md:grid-cols-[minmax(14rem,1fr)_3fr]">
        <Card className="max-h-[75vh] overflow-auto p-1">
          <Loading state={root}>
            {(entries) =>
              entries.length === 0 ? (
                <EmptyHint>工作目录是空的</EmptyHint>
              ) : (
                <Tree
                  aria-label="工作目录"
                  expandedKeys={expanded}
                  onExpandedChange={changeExpanded}
                  className="outline-none"
                >
                  {renderLevel('', entries)}
                </Tree>
              )
            }
          </Loading>
        </Card>
        <Card className="min-w-0 p-3">{opened ? openedPreview(opened) : <EmptyHint>选一个文件预览</EmptyHint>}</Card>
      </div>
    </div>
  )
}

function FilePreview({ agent, path, entry }: { agent: AgentId; path: string; entry: WorkspaceEntry }) {
  const size = entry.size ?? 0
  const kind = previewKind(entry)
  const url = workspaceFileUrl(agent, path)
  const text = useLoad(
    async () => (kind === 'markdown' || kind === 'text' ? (await api.workspaceFile(agent, path)).text() : ''),
    [agent, path, kind],
  )

  return (
    <div className="flex flex-col gap-2">
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <code className="min-w-0 break-all font-mono">{path}</code>
        <span className="text-muted">
          {formatSize(size)} · {formatTime(entry.modified)}
        </span>
        <a href={url} download={entry.name} className="ms-auto inline-flex items-center gap-1 text-accent">
          <Download className="size-4" />
          下载
        </a>
      </div>
      {kind === 'image' ? (
        <img src={url} alt={entry.name} className="max-h-[70vh] max-w-full self-start rounded-lg" />
      ) : kind === 'too-big' ? (
        <EmptyHint>文件超过 {formatSize(MAX_PREVIEW_BYTES)}，不预览，请下载查看</EmptyHint>
      ) : (
        <Loading state={text}>
          {(t) =>
            isBinary(t) ? (
              <EmptyHint>二进制文件，不预览，请下载查看</EmptyHint>
            ) : kind === 'markdown' ? (
              <Markdown text={t} />
            ) : (
              <Highlighted text={t} language={languageOf(entry.name)} />
            )
          }
        </Loading>
      )}
    </div>
  )
}

// 预览比对话里的代码块高
const CODE = `${CODE_BLOCK} max-h-[70vh]!`

/** 代码高亮：highlight.js 按需加载（单独一个 chunk）；认不出语言或太大时按纯文本显示。它的输出已转义，可以直接插入 */
function Highlighted({ text, language }: { text: string; language: string | null }) {
  const html = useLoad(async () => {
    if (language === null || text.length > MAX_HIGHLIGHT_BYTES) return null
    const hljs = (await import('highlight.js/lib/common')).default
    if (!hljs.getLanguage(language)) return null
    return hljs.highlight(text, { language, ignoreIllegals: true }).value
  }, [text, language])
  if (html.data) return <pre className={`${CODE} hljs`} dangerouslySetInnerHTML={{ __html: html.data }} />
  return <pre className={CODE}>{text}</pre>
}
