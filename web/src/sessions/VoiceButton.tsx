import { useEffect, useRef, useState } from 'react'
import type { KeyboardEvent, PointerEvent } from 'react'
import { LoaderCircle, Mic } from 'lucide-react'
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

function inside(el: HTMLElement, e: PointerEvent) {
  const r = el.getBoundingClientRect()
  return e.clientX >= r.left && e.clientX <= r.right && e.clientY >= r.top && e.clientY <= r.bottom
}

/**
 * 按住说话的麦克风按钮：按下开始录音，松手转成文字；移出按钮再松手是取消。键盘上按住空格/回车同理。
 * 录音中变红并显示时长，转写中转圈。服务端没配置语音转写时不显示。
 */
export function VoiceButton({ voice, isDisabled }: { voice: VoiceInput; isDisabled?: boolean }) {
  const ref = useRef<HTMLButtonElement>(null)
  const [outside, setOutside] = useState(false)
  if (!voice.available) return null
  const recording = voice.state === 'recording'
  const disabled = isDisabled || voice.state === 'transcribing'

  const onPointerDown = (e: PointerEvent<HTMLButtonElement>) => {
    if (disabled || e.button !== 0) return
    e.preventDefault()
    // 松手、移出时事件仍发给按钮
    e.currentTarget.setPointerCapture(e.pointerId)
    setOutside(false)
    voice.press()
  }
  const onPointerMove = (e: PointerEvent<HTMLButtonElement>) => {
    if (recording && ref.current) setOutside(!inside(ref.current, e))
  }
  const onPointerUp = (e: PointerEvent<HTMLButtonElement>) => {
    voice.release(ref.current ? !inside(ref.current, e) : false)
    setOutside(false)
  }
  const onKey = (e: KeyboardEvent<HTMLButtonElement>, down: boolean) => {
    if (e.key !== ' ' && e.key !== 'Enter') return
    e.preventDefault()
    if (disabled || e.repeat) return
    if (down) voice.press()
    else voice.release()
  }

  const label = recording
    ? outside
      ? '松手取消'
      : '松手转成文字，移出按钮取消'
    : voice.state === 'transcribing'
      ? '正在转成文字'
      : '按住说话'
  const tone = recording
    ? outside
      ? 'bg-default text-muted'
      : 'bg-danger text-danger-foreground'
    : 'text-foreground hover:bg-default'
  return (
    <button
      ref={ref}
      type="button"
      disabled={disabled}
      aria-label={label}
      title={label}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onPointerCancel={() => voice.release(true)}
      onKeyDown={(e) => onKey(e, true)}
      onKeyUp={(e) => onKey(e, false)}
      onBlur={() => voice.release(true)}
      onContextMenu={(e) => e.preventDefault()}
      style={{ touchAction: 'none', WebkitTouchCallout: 'none' }}
      className={`inline-flex h-10 min-w-10 shrink-0 select-none items-center justify-center gap-1.5 rounded-xl px-2.5 text-sm transition-colors disabled:opacity-50 ${tone}`}
    >
      {voice.state === 'transcribing' ? (
        <LoaderCircle className="size-4 animate-spin" />
      ) : (
        <Mic className={`size-4 ${recording && !outside ? 'animate-pulse' : ''}`} />
      )}
      {recording && voice.startedAt !== null && <Elapsed since={voice.startedAt} />}
      {recording && <span className="hidden sm:inline">{outside ? '松手取消' : '松手发送'}</span>}
    </button>
  )
}
