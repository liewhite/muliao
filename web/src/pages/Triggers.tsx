import { useState, type ReactNode } from 'react'
import { Alert, Button, Card, Chip, toast } from '@heroui/react'
import { Copy, KeyRound, Pencil, Play, Trash2 } from 'lucide-react'
import { Link } from 'react-router-dom'
import { api, asAgentId, type Agent, type Trigger, type TriggerKind } from '../api'
import {
  ActionButton,
  CODE_BLOCK,
  CollapsibleForm,
  EmptyHint,
  ErrorAlert,
  Loading,
  Page,
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

type Config = Record<string, unknown>

/** 各触发方式在界面上的差异：配置编辑、状态展示、额外操作。未登记的方式用通用的 JSON 编辑与原样展示。 */
/** 配置编辑器以 config 为初始值、自己保存编辑中的内容；内容不合法时回调 null：保存按钮随之禁用，不会提交上一次的合法值 */
type ConfigChange = (c: Config | null) => void

interface KindView {
  label: string
  emptyConfig: Config
  ConfigEditor: (props: { config: Config; onChange: ConfigChange }) => ReactNode
  Status: (props: { trigger: Trigger }) => ReactNode
  Actions?: (props: { trigger: Trigger; onChange: () => void }) => ReactNode
}

function JsonConfigEditor({ config, onChange }: { config: Config; onChange: ConfigChange }) {
  const [text, setText] = useState(JSON.stringify(config, null, 2))
  const [error, setError] = useState<string | null>(null)
  return (
    <TextAreaInput
      label="config（JSON）"
      hint={error ?? '这种触发方式的配置'}
      value={text}
      onChange={(value) => {
        setText(value)
        try {
          onChange(JSON.parse(value) as Config)
          setError(null)
        } catch {
          setError('不是合法的 JSON')
          onChange(null)
        }
      }}
      rows={4}
      mono
    />
  )
}

function CronEditor({ config, onChange }: { config: Config; onChange: ConfigChange }) {
  const [cron, setCron] = useState(String(config.cron ?? ''))
  return (
    <TextInput
      label="cron 表达式"
      hint="5 段式 cron 表达式，按服务端配置文件中的 timezone 解释，如 */5 * * * *"
      value={cron}
      onChange={(value) => {
        setCron(value)
        onChange({ cron: value })
      }}
      mono
    />
  )
}

function RawStatus({ trigger }: { trigger: Trigger }) {
  return Object.keys(trigger.status).length ? <pre className={CODE_BLOCK}>{JSON.stringify(trigger.status, null, 2)}</pre> : null
}

/** webhook 的 token 只在生成后显示这一次 */
function WebhookToken({ trigger, onChange }: { trigger: Trigger; onChange: () => void }) {
  const confirm = useConfirm()
  const action = useAction()
  const [token, setToken] = useState<string | null>(null)
  const hasToken = trigger.status.has_token === true

  async function generate() {
    if (hasToken) {
      const ok = await confirm({
        title: `重新生成「${trigger.name}」的 token？`,
        body: '旧 token 立即失效，外部系统需要换成新 token。',
        confirmLabel: '重新生成',
        danger: true,
      })
      if (!ok) return
    }
    await action.run(async () => {
      setToken((await api.generateTriggerToken(trigger.id)).token)
      onChange()
    })
  }

  return (
    <>
      <Button size="sm" variant="secondary" isDisabled={action.busy} onPress={() => void generate()}>
        <KeyRound className="size-4" />
        {hasToken ? '重新生成 token' : '生成 token'}
      </Button>
      <ErrorAlert error={action.error} />
      {token && (
        <Alert status="warning" className="basis-full">
          <Alert.Content className="flex min-w-0 flex-col gap-2">
            <Alert.Title>请保存 token，离开后无法再次查看</Alert.Title>
            <pre className={CODE_BLOCK}>
              {`curl -X POST ${String(trigger.status.url)} \\\n  -H 'Authorization: Bearer ${token}' \\\n  -d '{"hello": "world"}'`}
            </pre>
            <div className="text-xs">响应里的 session_id 可以在后续请求中以 ?session_id=… 传回，在同一个会话里继续。</div>
            <div className="flex gap-2">
              <Button size="sm" variant="secondary" onPress={() => void action.run(() => copyText(token, ' token'))}>
                <Copy className="size-4" />
                复制 token
              </Button>
              <Button size="sm" variant="tertiary" onPress={() => setToken(null)}>
                已保存
              </Button>
            </div>
          </Alert.Content>
        </Alert>
      )}
    </>
  )
}

const KIND_VIEWS: Record<string, KindView> = {
  schedule: {
    label: '定时',
    emptyConfig: { cron: '*/5 * * * *' },
    ConfigEditor: CronEditor,
    Status: ({ trigger }) => (
      <span>
        <code className="rounded bg-surface-secondary px-1.5 py-0.5 font-mono text-xs">{String(trigger.config.cron)}</code>
        {trigger.enabled && <> · 下次 {formatTime(String(trigger.status.next_run_at))}</>}
      </span>
    ),
  },
  webhook: {
    label: 'Webhook',
    emptyConfig: {},
    ConfigEditor: () => null,
    Status: ({ trigger }) => (
      <span className="break-all">
        <code className="rounded bg-surface-secondary px-1.5 py-0.5 font-mono text-xs">POST {String(trigger.status.url)}</code>
        {trigger.status.has_token !== true && <> · 还没有 token，生成后才能接收事件</>}
      </span>
    ),
    Actions: WebhookToken,
  },
}

function viewOf(kind: string): KindView {
  return KIND_VIEWS[kind] ?? { label: kind, emptyConfig: {}, ConfigEditor: JsonConfigEditor, Status: RawStatus }
}

function TriggerItem({ trigger, agents, onChange }: { trigger: Trigger; agents: Agent[] | undefined; onChange: () => void }) {
  const confirm = useConfirm()
  const view = viewOf(trigger.kind)
  const [editing, setEditing] = useState(false)
  const [prompt, setPrompt] = useState(trigger.prompt)
  const [config, setConfig] = useState<Config | null>(trigger.config)
  const action = useAction()

  const save = () =>
    action.run(async () => {
      if (config === null) throw new Error('配置不合法')
      await api.updateTrigger(trigger.id, { prompt, config })
      setEditing(false)
      onChange()
    })

  async function remove() {
    const ok = await confirm({ title: `删除触发器「${trigger.name}」？`, confirmLabel: '删除', danger: true })
    if (ok) await action.run(() => api.deleteTrigger(trigger.id).then(onChange))
  }

  return (
    <Card>
      <Card.Header className="flex flex-row items-start gap-3">
        <div className="min-w-0 flex-1">
          <Card.Title>{trigger.name}</Card.Title>
          <div className="mt-1 flex flex-wrap items-center gap-1.5 text-xs text-muted">
            <Chip size="sm" variant="soft">
              {view.label}
            </Chip>
            <Chip size="sm" color="accent" variant="soft">
              {agentLabel(agents, trigger.agent_id)}
            </Chip>
            <view.Status trigger={trigger} />
          </div>
        </div>
        <ToggleSwitch
          label={`启用触发器「${trigger.name}」`}
          isSelected={trigger.enabled}
          isDisabled={action.busy}
          onChange={(enabled) => void action.run(() => api.updateTrigger(trigger.id, { enabled }).then(onChange))}
        />
      </Card.Header>
      <Card.Content className="flex flex-col gap-3">
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
        {trigger.last_error && <ErrorAlert title="触发失败，已自动停用" error={trigger.last_error} />}
        {editing ? (
          <>
            <view.ConfigEditor config={trigger.config} onChange={setConfig} />
            <TextAreaInput label="处理说明" value={prompt} onChange={setPrompt} rows={5} />
          </>
        ) : (
          <p className="whitespace-pre-wrap text-sm">{trigger.prompt}</p>
        )}
        <ErrorAlert error={action.error} />
      </Card.Content>
      <Card.Footer className="flex flex-wrap gap-2">
        {editing ? (
          <>
            <ActionButton size="sm" isDisabled={config === null} isPending={action.busy} onPress={() => void save()}>
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
            <Button
              size="sm"
              variant="secondary"
              isDisabled={action.busy || !trigger.enabled}
              onPress={() =>
                void action.run(async () => {
                  await api.runTrigger(trigger.id)
                  toast.success('已触发')
                  onChange()
                })
              }
            >
              <Play className="size-4" />
              立即运行
            </Button>
            {view.Actions && <view.Actions trigger={trigger} onChange={onChange} />}
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

function TriggerForm({ kinds, agents, onCreated }: { kinds: TriggerKind[]; agents: Agent[] | undefined; onCreated: () => void }) {
  const [kind, setKind] = useState<string | null>(kinds[0]?.name ?? null)
  const [name, setName] = useState('')
  const [agentId, setAgentId] = useState<string | null>(null)
  const [prompt, setPrompt] = useState('')
  const [config, setConfig] = useState<Config | null>(kind ? viewOf(kind).emptyConfig : {})
  const action = useAction()
  const view = kind ? viewOf(kind) : null

  async function create() {
    const ok = await action.run(async () => {
      if (kind === null || agentId === null) throw new Error('请选择触发方式和 agent')
      if (config === null) throw new Error('配置不合法')
      await api.createTrigger({ name, agent_id: asAgentId(agentId), kind, prompt, config })
      toast.success('触发器已创建')
    })
    if (ok) {
      setName('')
      setPrompt('')
      onCreated()
    }
  }

  return (
    <CollapsibleForm title="新建触发器">
      <SelectInput
        label="触发方式"
        value={kind}
        onChange={(k) => {
          setKind(k)
          if (k) setConfig(viewOf(k).emptyConfig)
        }}
        options={kinds.map((k) => ({ id: k.name, label: viewOf(k.name).label, description: k.description }))}
      />
      <TextInput
        label="名称"
        hint="小写字母、数字和 -，webhook 的地址里会用到，如 github-issues"
        value={name}
        onChange={setName}
        isRequired
        mono
      />
      <SelectInput
        label="Agent"
        value={agentId}
        onChange={setAgentId}
        placeholder="选择 agent"
        options={(agents ?? []).map((a) => ({ id: a.id, label: a.name }))}
      />
      {view && kind && <view.ConfigEditor key={kind} config={viewOf(kind).emptyConfig} onChange={setConfig} />}
      <TextAreaInput
        label="处理说明"
        hint="每次触发都在新会话中执行；事件内容（如 webhook 的请求体）附在其后。触发的会话不会自动推送，需要时在说明里要求 agent 调用 notify_user"
        value={prompt}
        onChange={setPrompt}
        rows={5}
      />
      <ErrorAlert error={action.error} />
      <ActionButton isDisabled={!name || !agentId || !prompt || !kind || config === null} isPending={action.busy} onPress={() => void create()}>
        创建
      </ActionButton>
    </CollapsibleForm>
  )
}

/** 自动发起会话的入口：定时、webhook……方式的差异只在各自的配置与操作 */
export default function Triggers() {
  const triggers = useLoad(() => api.triggers(), [])
  const kinds = useLoad(() => api.triggerKinds(), [])
  const agents = useAgents()
  return (
    <Page title="触发">
      <div className="flex flex-col gap-3">
        <Loading state={kinds}>
          {(list) => <TriggerForm kinds={list} agents={agents.data} onCreated={() => void triggers.reload()} />}
        </Loading>
        <Loading state={triggers}>
          {(list) =>
            list.length === 0 ? (
              <EmptyHint>没有触发器</EmptyHint>
            ) : (
              <div className="flex flex-col gap-3">
                {list.map((t) => (
                  <TriggerItem
                    key={`${t.id}-${t.prompt}-${JSON.stringify(t.config)}`}
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
    </Page>
  )
}
