import { useState } from 'react'
import { Card } from '@heroui/react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { api, asAgentId } from '../api'
import { ActionButton, EmptyHint, ErrorAlert, LinkButton, Loading, Page, SelectInput, TextAreaInput, useAction, useAgents } from '../ui'

export default function NewSession() {
  const [params] = useSearchParams()
  const navigate = useNavigate()
  const agents = useAgents()
  const [agent, setAgent] = useState<string | null>(params.get('agent'))
  const [prompt, setPrompt] = useState('')
  const action = useAction()

  async function start() {
    await action.run(async () => {
      if (agent === null) throw new Error('请选择 agent')
      const session = await api.startSession(asAgentId(agent), prompt.trim())
      navigate(`/sessions/${session.id}`, { replace: true })
    })
  }

  return (
    <Page title="新对话" back="/">
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
                  options={list.map((a) => ({ id: a.id, label: a.name, description: a.description }))}
                />
                <TextAreaInput label="消息" value={prompt} onChange={setPrompt} rows={6} autoFocus />
                <ErrorAlert error={action.error} />
                <ActionButton isDisabled={!agent || !prompt.trim()} isPending={action.busy} onPress={() => void start()}>
                  开始
                </ActionButton>
              </Card.Content>
            </Card>
          )
        }
      </Loading>
    </Page>
  )
}
