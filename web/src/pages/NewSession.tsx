import { useState } from 'react'
import { Card } from '@heroui/react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { AgentPicker } from '../AgentPicker'
import { api, asAgentId, sessionRoute } from '../api'
import { loadFeatures } from '../features'
import { AttachButton, PendingUploads, useUploads } from '../attachments'
import { appendTranscript, useVoiceInput } from '../voice'
import { VoiceButton } from '../VoiceButton'
import {
  ActionButton,
  EmptyHint,
  ErrorAlert,
  LinkButton,
  Loading,
  Page,
  TextAreaInput,
  TextInput,
  useAction,
  useAgents,
  useLoad,
} from '../ui'

/** 压缩阈值输入框的值：留空为 null（用全局默认），不是不小于下限的整数时为 'invalid' */
function parseThreshold(text: string, min: number): number | null | 'invalid' {
  if (text.trim() === '') return null
  const n = Number(text)
  return Number.isInteger(n) && n >= min ? n : 'invalid'
}

export default function NewSession() {
  const [params] = useSearchParams()
  const navigate = useNavigate()
  const agents = useAgents()
  // 显式选的（URL ?agent= 或用户选择）优先，否则是系统默认 agent；都没有就留空让用户选
  const [chosen, setAgent] = useState<string | null>(params.get('agent'))
  const agent = chosen ?? agents.data?.find((a) => a.is_default)?.id ?? null
  const [prompt, setPrompt] = useState('')
  const [thresholdText, setThresholdText] = useState('')
  const features = useLoad(loadFeatures, [])
  // 默认值与下限没取到时输入框禁用、只能留空（用默认）
  const threshold = features.data ? parseThreshold(thresholdText, features.data.min_compaction_threshold_tokens) : null
  const uploads = useUploads()
  const action = useAction()
  // 新会话还要选 agent：转写结果先填进输入框，由用户点「开始」
  const voice = useVoiceInput((text) => setPrompt((p) => appendTranscript(p, text)))
  const canStart =
    agent !== null &&
    (prompt.trim() !== '' || uploads.ids.length > 0) &&
    !uploads.blocking &&
    threshold !== 'invalid'

  async function start() {
    // 快捷键不经过按钮的禁用状态
    if (action.busy || !canStart) return
    await action.run(async () => {
      if (agent === null) throw new Error('请选择 agent')
      const session = await api.startSession(asAgentId(agent), prompt.trim(), uploads.ids, threshold)
      navigate(sessionRoute(session.id), { replace: true })
    })
  }

  return (
    <Page title="新会话" back="/">
      <Loading state={agents}>
        {(list) =>
          list.length === 0 ? (
            <div className="flex flex-col items-center gap-3">
              <EmptyHint>还没有 agent。</EmptyHint>
              <LinkButton to="/agents/new">创建 agent</LinkButton>
            </div>
          ) : (
            <Card>
              <Card.Content className="flex flex-col gap-4">
                <AgentPicker
                  label="Agent"
                  value={agent}
                  onChange={setAgent}
                  agents={list}
                />
                <div onPaste={uploads.onPaste}>
                  <TextAreaInput label="消息" value={prompt} onChange={setPrompt} rows={6} autoFocus onSubmit={() => void start()} />
                </div>
                <PendingUploads uploads={uploads} />
                <TextInput
                  label="压缩阈值（token，可选）"
                  type="number"
                  value={features.data ? thresholdText : ''}
                  onChange={setThresholdText}
                  isDisabled={!features.data}
                  placeholder={
                    features.data ? `留空用默认 ${features.data.compaction_threshold_tokens.toLocaleString()}` : '留空用默认'
                  }
                  hint={
                    features.data && threshold === 'invalid'
                      ? `要是不小于 ${features.data.min_compaction_threshold_tokens.toLocaleString()} 的整数`
                      : '发给模型的上下文超过它就压缩早期内容'
                  }
                />
                <ErrorAlert error={action.error ?? voice.error ?? features.error} />
                <div className="flex items-center gap-2">
                  <AttachButton uploads={uploads} isDisabled={action.busy} />
                  <VoiceButton voice={voice} isDisabled={action.busy} />
                  <ActionButton isDisabled={!canStart} isPending={action.busy} onPress={() => void start()}>
                    开始
                  </ActionButton>
                </div>
              </Card.Content>
            </Card>
          )
        }
      </Loading>
    </Page>
  )
}
