import { describe, expect, it } from 'vitest'
import { appendTranscript, pickRecordingType } from './voice'

describe('pickRecordingType', () => {
  it('prefers opus in webm and falls back to mp4 on Safari', () => {
    expect(pickRecordingType(() => true)).toBe('audio/webm;codecs=opus')
    expect(pickRecordingType((t) => t === 'audio/mp4')).toBe('audio/mp4')
    expect(pickRecordingType(() => false)).toBeUndefined()
  })
})

describe('appendTranscript', () => {
  it('appends after typed text with one space, or stands alone', () => {
    expect(appendTranscript('', '帮我 deploy')).toBe('帮我 deploy')
    expect(appendTranscript('  \n', '帮我 deploy')).toBe('帮我 deploy')
    expect(appendTranscript('先看 CI\n', '再 deploy')).toBe('先看 CI 再 deploy')
    expect(appendTranscript('先看 CI', '')).toBe('先看 CI')
  })
})
