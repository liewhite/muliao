import { useCallback, useRef, useState, type ClipboardEvent } from 'react'
import { Button, Spinner } from '@heroui/react'
import { FileText, Paperclip, X } from 'lucide-react'
import { api, type Attachment, type FileInfo } from './api'
import { errorMessage } from './ui'

/** 消息里的附件：图片显示缩略图（点开看原图），其他文件是下载链接 */
export function AttachmentList({ files, align = 'start' }: { files: Attachment[]; align?: 'start' | 'end' }) {
  if (files.length === 0) return null
  const images = files.filter((f) => f.inline)
  const others = files.filter((f) => !f.inline)
  return (
    <div className={`flex max-w-[88%] flex-col gap-1.5 ${align === 'end' ? 'items-end self-end' : 'items-start self-start'}`}>
      {images.length > 0 && (
        <div className={`flex flex-wrap gap-1.5 ${align === 'end' ? 'justify-end' : ''}`}>
          {images.map((f) => (
            <a key={f.url} href={f.url} target="_blank" rel="noreferrer">
              <img src={f.url} alt={f.name} loading="lazy" className="max-h-48 max-w-full rounded-xl border border-separator object-contain" />
            </a>
          ))}
        </div>
      )}
      {others.map((f) => (
        <a
          key={f.url}
          href={f.url}
          className="flex max-w-full items-center gap-2 rounded-xl border border-separator bg-surface px-3 py-2 text-sm hover:bg-surface-hover"
        >
          <FileText className="size-4 shrink-0 text-muted" />
          <span className="truncate">{f.name}</span>
        </a>
      ))}
    </div>
  )
}

type Upload =
  | { key: number; name: string; state: 'uploading' }
  | { key: number; name: string; state: 'done'; attachment: FileInfo }
  | { key: number; name: string; state: 'failed'; error: string }

/** 发送前的附件：选中或粘贴即上传（先上传拿到 id，发消息时引用），失败的留在列表里显示原因 */
export function useUploads() {
  const [uploads, setUploads] = useState<Upload[]>([])
  const next = useRef(0)

  const add = useCallback((files: File[]) => {
    for (const file of files) {
      const key = next.current++
      setUploads((prev) => [...prev, { key, name: file.name, state: 'uploading' }])
      api.uploadFile(file).then(
        (info) => setUploads((prev) => prev.map((u) => (u.key === key ? { key, name: file.name, state: 'done', attachment: info } : u))),
        (e: unknown) => setUploads((prev) => prev.map((u) => (u.key === key ? { key, name: file.name, state: 'failed', error: errorMessage(e) } : u))),
      )
    }
  }, [])
  const remove = useCallback((key: number) => setUploads((prev) => prev.filter((u) => u.key !== key)), [])
  const removeMany = useCallback((keys: number[]) => setUploads((prev) => prev.filter((u) => !keys.includes(u.key))), [])
  const done = uploads.flatMap((u) => (u.state === 'done' ? [u] : []))

  return {
    uploads,
    add,
    remove,
    /** 这次要发出的附件：发送成功后只移除这一批，发送过程中新加的留下 */
    batch: () => ({ ids: done.map((u) => u.attachment.id), keys: done.map((u) => u.key) }),
    removeMany,
    ids: done.map((u) => u.attachment.id),
    /** 有正在上传或失败未移除的：此时不能发送，免得用户以为附件发出去了 */
    blocking: uploads.some((u) => u.state !== 'done'),
    /** 粘贴截图等文件时直接作为附件 */
    onPaste: (e: ClipboardEvent) => {
      const files = Array.from(e.clipboardData.files)
      if (files.length > 0) {
        e.preventDefault()
        add(files)
      }
    },
  }
}

export type Uploads = ReturnType<typeof useUploads>

export function AttachButton({ uploads, isDisabled }: { uploads: Uploads; isDisabled?: boolean }) {
  const input = useRef<HTMLInputElement>(null)
  return (
    <>
      <input
        ref={input}
        type="file"
        multiple
        hidden
        onChange={(e) => {
          uploads.add(Array.from(e.target.files ?? []))
          // 允许再次选择同一个文件
          e.target.value = ''
        }}
      />
      <Button isIconOnly variant="ghost" isDisabled={isDisabled} onPress={() => input.current?.click()} aria-label="添加附件">
        <Paperclip className="size-4" />
      </Button>
    </>
  )
}

export function PendingUploads({ uploads }: { uploads: Uploads }) {
  if (uploads.uploads.length === 0) return null
  return (
    <div className="flex flex-wrap gap-1.5">
      {uploads.uploads.map((u) => (
        <div
          key={u.key}
          className={`flex max-w-full items-center gap-1.5 rounded-lg border px-2 py-1 text-xs ${
            u.state === 'failed' ? 'border-danger text-danger' : 'border-separator'
          }`}
          title={u.state === 'failed' ? u.error : u.name}
        >
          {u.state === 'uploading' ? <Spinner size="sm" /> : u.state === 'done' && u.attachment.inline ? (
            <img src={u.attachment.url} alt="" className="size-6 rounded object-cover" />
          ) : (
            <FileText className="size-3.5 shrink-0" />
          )}
          <span className="truncate">{u.state === 'failed' ? `${u.name}：${u.error}` : u.name}</span>
          <button type="button" className="shrink-0 text-muted hover:text-foreground" onClick={() => uploads.remove(u.key)} aria-label={`移除 ${u.name}`}>
            <X className="size-3.5" />
          </button>
        </div>
      ))}
    </div>
  )
}
