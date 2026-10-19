import { useState } from 'react'
import { Alert, Button, Card, Chip, toast } from '@heroui/react'
import { Copy, KeyRound, Pencil, Trash2 } from 'lucide-react'
import { Link } from 'react-router-dom'
import { api, asAgentId, type Agent, type EventTrigger } from '../api'
import {
  ActionButton,
  CODE_BLOCK,
  CollapsibleForm,
  EmptyHint,
  ErrorAlert,
  Loading,
  SelectInput,
  TextAreaInput,
  TextInput,
  ToggleSwitch,
  agentLabel,
  copyText,
  formatTime,
  useAction,
  useAgents,
  useConfirm,
  useLoad,
} from '../ui'

const PROMPT_HINT = '告诉 agent 这类事件是什么、该怎么处理。事件内容会附在其后；事件会话不会自动推送，需要时要求 agent 调用 notify_user'

/** token 明文只在创建或重置后显示这一次 */
function TokenOnce({ trigger, token, onDone }: { trigger: EventTrigger; token: string; onDone: () => void }) {
  const action = useAction()
  const curl = `curl -X POST ${trigger.url} \\\n  -H 'Authorization: Bearer ${token}' \\\n  -d '{"hello": "world"}'`
  return (
    <Alert status="warning">
      <Alert.Content className="flex min-w-0 flex-col gap-2">
        <Alert.Title>请保存 token，离开后无法再次查看</Alert.Title>
        <pre className={CODE_BLOCK}>{curl}</pre>
        <div className="text-xs">响应里的 session_id 可以在后续请求中以 ?session_id=… 传回，在同一个会话里继续。</div>
        <ErrorAlert error={action.error} />
        <div className="flex gap-2">
          <Button size="sm" variant="secondary" onPress={() => void action.run(() => copyText(token, ' token'))}>
            <Copy className="size-4" />
            复制 token
          </Button>
          <Button size="sm" variant="tertiary" onPress={onDone}>
            已保存
          </Button>
        </div>
      </Alert.Content>
    </Alert>
  )
}

function EventTriggerItem({
  trigger,
  agents,
  onChange,
}: {
  trigger: EventTrigger
  agents: Agent[] | undefined
  onChange: () => void
}) {
  const confirm = useConfirm()
  const [editing, setEditing] = useState(false)
  const [prompt, setPrompt] = useState(trigger.prompt)
  const [token, setToken] = useState<string | null>(null)
  const action = useAction()

  // 开关只改启用状态，提交的是已保存的值；编辑中的草稿只由「保存」提交
  const toggle = (enabled: boolean) =>
    action.run(async () => {
      await api.updateEventTrigger(trigger.id, { prompt: trigger.prompt, enabled })
      onChange()
    })

  const save = () =>
    action.run(async () => {
      await api.updateEventTrigger(trigger.id, { prompt, enabled: trigger.enabled })
      setEditing(false)
      onChange()
    })

  async function rotate() {
    const ok = await confirm({
      title: `重置「${trigger.name}」的 token？`,
      body: '旧 token 立即失效，外部系统需要换成新 token。',
      confirmLabel: '重置',
      danger: true,
    })
    if (ok) await action.run(async () => setToken((await api.rotateEventTriggerToken(trigger.id)).token))
  }

  async function remove() {
    const ok = await confirm({ title: `删除事件触发器「${trigger.name}」？`, confirmLabel: '删除', danger: true })
    if (ok) await action.run(() => api.deleteEventTrigger(trigger.id).then(onChange))
  }

  return (
    <Card>
      <Card.Header className="flex flex-row items-start gap-3">
        <div className="min-w-0 flex-1">
          <Card.Title>{trigger.name}</Card.Title>
          <div className="mt-1 flex flex-wrap items-center gap-1.5">
            <Chip size="sm" color="accent" variant="soft">
              {agentLabel(agents, trigger.agent_id)}
            </Chip>
            <code className="break-all rounded bg-surface-secondary px-1.5 py-0.5 font-mono text-xs">POST {trigger.url}</code>
          </div>
        </div>
        <ToggleSwitch
          label={`启用事件触发器「${trigger.name}」`}
          isSelected={trigger.enabled}
          isDisabled={action.busy}
          onChange={(enabled) => void toggle(enabled)}
        />
      </Card.Header>
      <Card.Content className="flex flex-col gap-3">
        {token && <TokenOnce trigger={trigger} token={token} onDone={() => setToken(null)} />}
        <div className="text-xs text-muted">
          {trigger.last_fired_at ? <>上次触发 {formatTime(trigger.last_fired_at)}</> : '还没有触发过'}
          {trigger.last_session_id && (
            <>
              {' · '}
              <Link className="text-accent" to={`/sessions/${trigger.last_session_id}`}>
                上次会话
              </Link>
            </>
          )}
        </div>
        {editing ? (
          <TextAreaInput label="处理说明" hint={PROMPT_HINT} value={prompt} onChange={setPrompt} rows={5} />
        ) : (
          <p className="whitespace-pre-wrap text-sm">{trigger.prompt}</p>
        )}
        <ErrorAlert error={action.error} />
      </Card.Content>
      <Card.Footer className="flex flex-wrap gap-2">
        {editing ? (
          <>
            <ActionButton size="sm" isPending={action.busy} onPress={() => void save()}>
              保存
            </ActionButton>
            <Button size="sm" variant="tertiary" onPress={() => setEditing(false)}>
              取消
            </Button>
          </>
        ) : (
          <>
            <Button size="sm" variant="secondary" onPress={() => setEditing(true)}>
              <Pencil className="size-4" />
              编辑
            </Button>
            <Button size="sm" variant="secondary" isDisabled={action.busy} onPress={() => void rotate()}>
              <KeyRound className="size-4" />
              重置 token
            </Button>
            <Button size="sm" variant="danger-soft" isDisabled={action.busy} onPress={() => void remove()}>
              <Trash2 className="size-4" />
              删除
            </Button>
          </>
        )}
      </Card.Footer>
    </Card>
  )
}

const EMPTY = { name: '', agent_id: null as string | null, prompt: '' }

function EventTriggerForm({ agents, onCreated }: { agents: Agent[] | undefined; onCreated: () => void }) {
  const [draft, setDraft] = useState(EMPTY)
  const [created, setCreated] = useState<{ trigger: EventTrigger; token: string } | null>(null)
  const action = useAction()
  const set = (patch: Partial<typeof EMPTY>) => setDraft((prev) => ({ ...prev, ...patch }))

  async function create() {
    await action.run(async () => {
      if (draft.agent_id === null) throw new Error('请选择 agent')
      setCreated(await api.createEventTrigger({ ...draft, agent_id: asAgentId(draft.agent_id) }))
      setDraft(EMPTY)
      toast.success('事件触发器已创建')
      onCreated()
    })
  }

  return (
    <>
      {created && <TokenOnce trigger={created.trigger} token={created.token} onDone={() => setCreated(null)} />}
      <CollapsibleForm title="新建事件触发器">
        <TextInput
          label="名称"
          hint="出现在地址里：小写字母、数字、- 和 _，如 github-issues"
          value={draft.name}
          onChange={(name) => set({ name })}
          isRequired
          mono
        />
        <SelectInput
          label="Agent"
          value={draft.agent_id}
          onChange={(agent_id) => set({ agent_id })}
          placeholder="选择 agent"
          options={(agents ?? []).map((a) => ({ id: a.id, label: a.name }))}
        />
        <TextAreaInput label="处理说明" hint={PROMPT_HINT} value={draft.prompt} onChange={(prompt) => set({ prompt })} rows={5} />
        <ErrorAlert error={action.error} />
        <ActionButton
          isDisabled={!draft.name || !draft.agent_id || !draft.prompt}
          isPending={action.busy}
          onPress={() => void create()}
        >
          创建
        </ActionButton>
      </CollapsibleForm>
    </>
  )
}

export function EventTriggerPanel() {
  const triggers = useLoad(() => api.eventTriggers(), [])
  const agents = useAgents()
  return (
    <div className="flex flex-col gap-3">
      <EventTriggerForm agents={agents.data} onCreated={() => void triggers.reload()} />
      <Loading state={triggers}>
        {(list) =>
          list.length === 0 ? (
            <EmptyHint>没有事件触发器</EmptyHint>
          ) : (
            <div className="flex flex-col gap-3">
              {list.map((t) => (
                <EventTriggerItem
                  key={`${t.id}-${t.prompt}`}
                  trigger={t}
                  agents={agents.data}
                  onChange={() => void triggers.reload()}
                />
              ))}
            </div>
          )
        }
      </Loading>
    </div>
  )
}
