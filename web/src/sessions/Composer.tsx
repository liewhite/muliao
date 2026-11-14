// 会话页底部的输入框：附件、语音、文字，停止与发送

import type { KeyboardEvent } from 'react'
import { Button, TextArea, TextField } from '@heroui/react'
import { SendHorizontal, Square } from 'lucide-react'
import { isSendShortcut, SEND_HINT } from '../sendKey'
import { AttachButton, PendingUploads, type Uploads } from './attachments'
import { VoiceButton } from './VoiceButton'
import type { VoiceInput } from './voice'

export function Composer({
  draft,
  setDraft,
  uploads,
  voice,
  busy,
  sendable,
  active,
  onSend,
  onStop,
}: {
  draft: string
  setDraft: (v: string) => void
  uploads: Uploads
  voice: VoiceInput
  busy: boolean
  /** 有东西可发（见会话页的 canSend） */
  sendable: boolean
  /** 这个会话有事在进行（工作状态不是 idle：运行、排队、等后台任务或委派；或在手动压缩）：显示停止按钮（进行中也可以继续发消息，会排在后面） */
  active: boolean
  onSend: () => void
  onStop: () => void
}) {
  function onKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (isSendShortcut(e)) {
      e.preventDefault()
      onSend()
    }
  }
  return (
    <div className="flex flex-col gap-2">
      <PendingUploads uploads={uploads} />
      <div className="flex items-end gap-2">
        <AttachButton uploads={uploads} isDisabled={busy} />
        <VoiceButton voice={voice} isDisabled={busy} />
        <TextField value={draft} onChange={setDraft} aria-label="回复" variant="secondary" className="flex-1">
          <TextArea
            placeholder={`回复…（${SEND_HINT}）`}
            rows={Math.min(6, Math.max(1, draft.split('\n').length))}
            onKeyDown={onKeyDown}
            onPaste={uploads.onPaste}
            className="resize-none"
          />
        </TextField>
        {active && (
          <Button isIconOnly variant="danger-soft" isDisabled={busy} onPress={onStop} aria-label="停止">
            <Square className="size-4" />
          </Button>
        )}
        <Button isIconOnly isDisabled={busy || !sendable} onPress={onSend} aria-label="发送">
          <SendHorizontal className="size-4" />
        </Button>
      </div>
    </div>
  )
}
