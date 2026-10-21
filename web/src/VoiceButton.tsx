import { useEffect, useState } from 'react'
import { Button } from '@heroui/react'
import { LoaderCircle, Mic, Square } from 'lucide-react'
import type { VoiceInput } from './voice'

function Elapsed({ since }: { since: number }) {
  const [now, setNow] = useState(() => Date.now())
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 500)
    return () => window.clearInterval(id)
  }, [])
  const s = Math.max(0, Math.floor((now - since) / 1000))
  return <span className="tabular-nums">{`${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`}</span>
}

/** 麦克风按钮：录音中变成红色的停止键并显示时长，转写中转圈。服务端没配置语音转写时不显示。 */
export function VoiceButton({ voice, isDisabled }: { voice: VoiceInput; isDisabled?: boolean }) {
  if (!voice.available) return null
  if (voice.state === 'recording') {
    return (
      <Button variant="danger-soft" onPress={voice.toggle} aria-label="停止录音并转成文字" className="gap-1.5 px-3">
        <Square className="size-3.5 animate-pulse fill-current" />
        {voice.startedAt !== null && <Elapsed since={voice.startedAt} />}
      </Button>
    )
  }
  return (
    <Button
      isIconOnly
      variant="ghost"
      isDisabled={isDisabled || voice.state === 'transcribing'}
      onPress={voice.toggle}
      aria-label={voice.state === 'transcribing' ? '正在转成文字' : '语音输入'}
    >
      {voice.state === 'transcribing' ? <LoaderCircle className="size-4 animate-spin" /> : <Mic className="size-4" />}
    </Button>
  )
}
