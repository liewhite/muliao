import { useState } from 'react'
import { Card } from '@heroui/react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { selectableAgents } from '../agentGroups'
import { api, asAgentId, sessionRoute } from '../api'
import { AttachButton, PendingUploads, useUploads } from '../attachments'
import { appendTranscript, useVoiceInput } from '../voice'
import { VoiceButton } from '../VoiceButton'
import { ActionButton, EmptyHint, ErrorAlert, LinkButton, Loading, Page, SelectInput, TextAreaInput, useAction, useAgents } from '../ui'

export default function NewSession() {
  const [params] = useSearchParams()
  const navigate = useNavigate()
  const agents = useAgents()
  // 显式选的（URL ?agent= 或用户选择）优先，否则是系统默认 agent；都没有就留空让用户选
  const [chosen, setAgent] = useState<string | null>(params.get('agent'))
  const agent = chosen ?? agents.data?.find((a) => a.is_default)?.id ?? null
  const [prompt, setPrompt] = useState('')
  const uploads = useUploads()
  const action = useAction()
  // 新会话还要选 agent：转写结果先填进输入框，由用户点「开始」
  const voice = useVoiceInput((text) => setPrompt((p) => appendTranscript(p, text)))
  const canStart = agent !== null && (prompt.trim() !== '' || uploads.ids.length > 0) && !uploads.blocking

  async function start() {
    // 快捷键不经过按钮的禁用状态
    if (action.busy || !canStart) return
    await action.run(async () => {
      if (agent === null) throw new Error('请选择 agent')
      const session = await api.startSession(asAgentId(agent), prompt.trim(), uploads.ids)
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
                <SelectInput
                  label="Agent"
                  value={agent}
                  onChange={setAgent}
                  placeholder="选择 agent"
                  // 归档的不能对话；从它的详情页带 ?agent= 进来时留着显示，开始时由服务端拒绝并说明
                  options={selectableAgents(list, agent).map((a) => ({ id: a.id, label: a.name, description: a.description }))}
                />
                <div onPaste={uploads.onPaste}>
                  <TextAreaInput label="消息" value={prompt} onChange={setPrompt} rows={6} autoFocus onSubmit={() => void start()} />
                </div>
                <PendingUploads uploads={uploads} />
                <ErrorAlert error={action.error ?? voice.error} />
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
