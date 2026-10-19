import { useEffect, useState } from 'react'
import { Button, Card, Chip, Disclosure, toast } from '@heroui/react'
import { History, RotateCcw } from 'lucide-react'
import { api, type AgentId } from '../api'
import {
  ActionButton,
  EmptyHint,
  ErrorAlert,
  Loading,
  Markdown,
  Page,
  TextAreaInput,
  agentLabel,
  formatTime,
  useAction,
  useAgents,
  useConfirm,
  useLoad,
} from '../ui'

/** prompt 的版本历史（新的在前，第一条即当前生效），可以把旧版恢复为新的一版。current 变化时重新加载 */
export function PromptHistory({ owner, current, onRestored }: { owner: AgentId | null; current: string; onRestored: () => void }) {
  const history = useLoad(() => api.promptHistory(owner), [owner, current])
  const agents = useAgents()
  const confirm = useConfirm()
  const action = useAction()

  async function restore(id: number, at: string) {
    const ok = await confirm({ title: `恢复 ${formatTime(at)} 的版本？`, body: '它会作为新的一版生效，现有历史保留。', confirmLabel: '恢复' })
    if (ok && (await action.run(() => api.restorePrompt(id)))) {
      toast.success('已恢复')
      onRestored()
    }
  }

  return (
    <Disclosure className="rounded-xl border border-separator px-3 py-2">
      <Disclosure.Heading>
        <Disclosure.Trigger className="flex w-full items-center gap-2 text-sm text-muted">
          <History className="size-4" />
          历史版本
          <Disclosure.Indicator />
        </Disclosure.Trigger>
      </Disclosure.Heading>
      <Disclosure.Content>
        <Disclosure.Body className="flex flex-col gap-2 pt-2">
          <ErrorAlert error={action.error} />
          <Loading state={history}>
            {(list) =>
              list.length === 0 ? (
                <EmptyHint>还没有历史</EmptyHint>
              ) : (
                list.map((r, i) => (
                  <Card key={r.id} variant="secondary">
                    <Card.Header className="flex flex-row items-center gap-2">
                      <Card.Description className="flex-1">
                        {formatTime(r.created_at)} · {r.author === 'user' ? '用户' : agentLabel(agents.data, r.author as AgentId)}
                      </Card.Description>
                      {i === 0 ? (
                        <Chip size="sm" color="success" variant="soft">
                          当前
                        </Chip>
                      ) : (
                        <Button size="sm" variant="ghost" isDisabled={action.busy} onPress={() => void restore(r.id, r.created_at)}>
                          <RotateCcw className="size-4" />
                          恢复
                        </Button>
                      )}
                    </Card.Header>
                    <Card.Content className="max-h-60 overflow-auto text-sm">
                      {r.content.trim() ? <Markdown text={r.content} /> : <span className="text-muted">（空）</span>}
                    </Card.Content>
                  </Card>
                ))
              )
            }
          </Loading>
        </Disclosure.Body>
      </Disclosure.Content>
    </Disclosure>
  )
}

/** 公共 prompt：合并进所有 agent 的指令，位于各 agent 自己的指令之前 */
export default function PublicPrompt() {
  const history = useLoad(() => api.promptHistory(null), [])
  const [content, setContent] = useState('')
  const action = useAction()
  // 从未设置过公共指令时没有任何版本，即为空
  const current = history.data?.[0]?.content ?? ''
  useEffect(() => setContent(current), [current])
  const reload = () => void history.reload()

  return (
    <Page title="公共指令" back="/settings">
      <Loading state={history}>
        {() => (
          <div className="flex flex-col gap-3">
            <TextAreaInput
              label="公共指令"
              hint="所有 agent 都会看到，放在各自指令之前。每次保存都会留下一个版本"
              value={content}
              onChange={setContent}
              rows={14}
            />
            <ErrorAlert error={action.error} />
            <ActionButton
              isDisabled={content === current}
              isPending={action.busy}
              onPress={() =>
                void action.run(async () => {
                  await api.setPublicPrompt(content)
                  toast.success('已保存')
                  reload()
                })
              }
            >
              保存
            </ActionButton>
            <PromptHistory owner={null} current={current} onRestored={reload} />
          </div>
        )}
      </Loading>
    </Page>
  )
}
