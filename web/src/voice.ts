import { useCallback, useEffect, useRef, useState } from 'react'
import { api } from './api'
import { errorMessage as apiErrorMessage } from './ui'

// 录音的容器格式：Chrome/Firefox 录 webm/ogg（opus），iOS/Safari 只能录 mp4（aac）；服务端都认
const RECORDING_TYPES = ['audio/webm;codecs=opus', 'audio/webm', 'audio/ogg;codecs=opus', 'audio/mp4']
// 一段录音最长这么久（opus 约 4 KB/s，远低于服务端的大小上限）；到点自动停止并转写
const MAX_RECORDING_MS = 5 * 60 * 1000
// 短于这个的录音当作误触，不转写
const MIN_RECORDING_MS = 500

/** 浏览器能录的第一种格式；都不认时交给浏览器默认（undefined）。 */
export function pickRecordingType(isSupported: (type: string) => boolean): string | undefined {
  return RECORDING_TYPES.find(isSupported)
}

/** 把转写出的文字接到已经输入的内容后面。 */
export function appendTranscript(draft: string, text: string): string {
  const head = draft.trimEnd()
  if (text === '') return draft
  return head === '' ? text : `${head} ${text}`
}

let featuresOnce: Promise<boolean> | null = null

/** 服务端配置了语音转写、且浏览器能录音（需要 HTTPS 或 localhost）时才显示语音输入。 */
function useVoiceAvailable(): boolean {
  const [available, setAvailable] = useState(false)
  useEffect(() => {
    if (typeof MediaRecorder === 'undefined' || !navigator.mediaDevices?.getUserMedia) return
    featuresOnce ??= api.features().then(
      (f) => f.transcription,
      (e) => {
        console.error('loading server features failed', e)
        featuresOnce = null
        return false
      },
    )
    let live = true
    void featuresOnce.then((ok) => live && setAvailable(ok))
    return () => {
      live = false
    }
  }, [])
  return available
}

export type VoiceState = 'idle' | 'recording' | 'transcribing'

/**
 * 按住说话：press 开始录音，release() 结束并交给服务端转写，拿到文字后调用 onText（空录音、误触不调用）；
 * release(true) 是取消，录音丢弃。麦克风授权是异步的，授权完成前就松手同样作废。
 * 离开页面时停止录音并释放麦克风。
 */
export function useVoiceInput(onText: (text: string) => void) {
  const available = useVoiceAvailable()
  const [state, setState] = useState<VoiceState>('idle')
  const [error, setError] = useState<string | null>(null)
  const [startedAt, setStartedAt] = useState<number | null>(null)
  const recorder = useRef<MediaRecorder | null>(null)
  // 用户是否还按着；拿到麦克风时已经松手就不开始录音
  const held = useRef(false)
  const cancelled = useRef(false)
  const onTextRef = useRef(onText)
  useEffect(() => {
    onTextRef.current = onText
  })

  const start = useCallback(async () => {
    setError(null)
    cancelled.current = false
    setState('recording')
    let stream: MediaStream
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true })
    } catch (e) {
      setState('idle')
      setError(`无法使用麦克风：${apiErrorMessage(e)}`)
      return
    }
    if (!held.current) {
      stream.getTracks().forEach((t) => t.stop())
      setState('idle')
      return
    }
    const mimeType = pickRecordingType((t) => MediaRecorder.isTypeSupported(t))
    const rec = new MediaRecorder(stream, mimeType ? { mimeType } : undefined)
    const chunks: Blob[] = []
    const began = Date.now()
    const timer = window.setTimeout(() => rec.state === 'recording' && rec.stop(), MAX_RECORDING_MS)
    rec.ondataavailable = (e) => {
      if (e.data.size > 0) chunks.push(e.data)
    }
    rec.onstop = () => {
      window.clearTimeout(timer)
      stream.getTracks().forEach((t) => t.stop())
      recorder.current = null
      setStartedAt(null)
      const audio = new Blob(chunks, { type: rec.mimeType || mimeType || 'audio/webm' })
      if (cancelled.current || Date.now() - began < MIN_RECORDING_MS || audio.size === 0) {
        setState('idle')
        return
      }
      setState('transcribing')
      api
        .transcribe(audio)
        .then(({ text }) => {
          if (text === '') setError('没有听到说话内容')
          else onTextRef.current(text)
        })
        .catch((e) => setError(`语音转文字失败：${apiErrorMessage(e)}`))
        .finally(() => setState('idle'))
    }
    recorder.current = rec
    rec.start()
    setStartedAt(began)
  }, [])

  // 离开页面：停止录音（onstop 里释放麦克风），不再转写
  useEffect(
    () => () => {
      const rec = recorder.current
      if (rec?.state === 'recording') {
        rec.onstop = null
        rec.stream.getTracks().forEach((t) => t.stop())
        rec.stop()
      }
    },
    [],
  )

  const press = useCallback(() => {
    if (state !== 'idle' || held.current) return
    held.current = true
    void start()
  }, [state, start])

  const release = useCallback((cancel = false) => {
    if (!held.current) return
    held.current = false
    cancelled.current = cancel
    if (recorder.current?.state === 'recording') recorder.current.stop()
  }, [])

  return { available, state, error, startedAt, press, release }
}

export type VoiceInput = ReturnType<typeof useVoiceInput>
