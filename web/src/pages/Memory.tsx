import { useEffect, useState } from 'react'
import { toast } from '@heroui/react'
import { api, type AgentId } from '../api'
import { ResourceHistory } from '../ResourceHistory'
import { ActionButton, ErrorAlert, Loading, Page, TextAreaInput, useAction, useLoad } from '../ui'

/** 一份记忆（agent 为 null 即全局记忆）：编辑、保存，以及它的历史版本 */
export function MemoryEditor({ agent, hint }: { agent: AgentId | null; hint: string }) {
  const memory = useLoad(() => api.memory(agent), [agent])
  const [content, setContent] = useState('')
  const action = useAction()
  useEffect(() => {
    if (memory.data) setContent(memory.data.content)
  }, [memory.data])

  return (
    <Loading state={memory}>
      {(m) => (
        <div className="flex flex-col gap-3">
          <TextAreaInput
            label={`记忆（${content.length}/${m.max_chars} 字符）`}
            hint={hint}
            value={content}
            onChange={setContent}
            rows={14}
            mono
          />
          <ErrorAlert error={action.error} />
          <ActionButton
            isDisabled={content === m.content}
            isPending={action.busy}
            onPress={() =>
              void action.run(async () => {
                await api.setMemory(agent, content)
                toast.success('记忆已保存')
                await memory.reload()
              })
            }
          >
            保存记忆
          </ActionButton>
          <ResourceHistory target={{ kind: 'memory', agent }} current={m.content} onRestored={() => void memory.reload()} />
        </div>
      )}
    </Loading>
  )
}

/** 全局记忆：所有 agent 共享，放在各自的记忆之前；各 agent 的自进化也会修改它 */
export default function GlobalMemory() {
  return (
    <Page title="全局记忆">
      <MemoryEditor agent={null} hint="所有 agent 每次运行都会看到，放在各自的记忆之前；agent 也会用工具修改它。每次保存都会留下一个版本" />
    </Page>
  )
}
