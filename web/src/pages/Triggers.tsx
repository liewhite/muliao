import { useState, type ReactNode } from 'react'
import { Alert, Button, Card, Chip, Disclosure, Modal, toast } from '@heroui/react'
import { Archive, Copy, History, KeyRound, Pencil, Play, Trash2 } from 'lucide-react'
import { Link } from 'react-router-dom'
import { useReturnState } from '../nav'
import {
  api,
  asAgentId,
  type Agent,
  type Precheck,
  type Schemas,
  type Trigger,
  type TriggerKind,
  type TriggerKindName,
  sessionRoute,
} from '../api'
import { selectableAgents } from '../agentGroups'
import { groupTriggersByAgent } from '../triggerGroups'
import { inAnHour, wallClock } from '../wallClock'
import {
  ActionButton,
  CODE_BLOCK,
  CollapsibleForm,
  EmptyHint,
  ErrorAlert,
  GroupSection,
  Loading,
  Page,
  SelectInput,
  TextAreaInput,
  TextInput,
  ToggleSwitch,
  agentLabel,
  copyText,
  errorMessage,
  formatTime,
  useAction,
  useAgents,
  useConfirm,
  useLoad,
} from '../ui'

/** 提交给接口的配置（由后端按 kind 校验）；读到的配置按 kind 有各自的类型，见 KindConfig */
type Config = Record<string, unknown>

/** 某种方式的触发器：收窄后 config 与 status 都是这种方式的类型 */
type TriggerOf<K extends TriggerKindName> = Extract<Trigger, { kind: K }>

/** 有服务端默认值的配置字段（StreamConfig 的 secrets、debounce_seconds）：读到的配置里总有，新建时可以不填（由服务端补上
 * 默认值，前端不另写一份值）。生成的类型只有读到的形状（这两个字段必填），所以在这里列出字段名 */
type Defaulted = 'secrets' | 'debounce_seconds'

/** 编辑中的配置：有默认值的字段可省 */
type EditableConfig<C> = Omit<C, Defaulted> & Partial<Pick<C, Extract<keyof C, Defaulted>>>

/** kind 与它的配置成对出现，编辑器按 kind 收窄 */
type KindConfig = { [K in TriggerKindName]: { kind: K; config: EditableConfig<TriggerOf<K>['config']> } }[TriggerKindName]

/** 配置编辑器以 config 为初始值、自己保存编辑中的内容；内容不合法时回调 null：保存按钮随之禁用，不会提交上一次的合法值 */
type ConfigChange = (c: Config | null) => void

/** 各触发方式在界面上的名字；summary 是选择方式时的一句话说明（后端的 description 是写给 agent 的，太长） */
const KIND_LABELS: Record<TriggerKindName, { label: string; summary: string }> = {
  schedule: { label: '定时', summary: '按 cron 表达式周期运行' },
  once: { label: '一次性', summary: '到指定时间运行一次，之后移入「已结束」' },
  webhook: { label: 'Webhook', summary: '收到 HTTP POST 时运行' },
  stream: { label: '流式', summary: '托管一条常驻命令，它输出的每一行是一个事件' },
}

/** 新建时的初始配置。在打开表单、切换方式时才算：一次性触发器的默认时间是那时起一小时后 */
function emptyConfig(kind: TriggerKindName): KindConfig {
  switch (kind) {
    case 'schedule':
      return { kind, config: { cron: '*/5 * * * *' } }
    case 'once':
      return { kind, config: { at: inAnHour(new Date()) } }
    case 'webhook':
      return { kind, config: {} }
    case 'stream':
      return { kind, config: { command: '' } }
  }
}

function CronEditor({ config, onChange }: { config: Schemas['ScheduleConfig']; onChange: ConfigChange }) {
  const [cron, setCron] = useState(config.cron)
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

function RunAtEditor({ config, onChange }: { config: Schemas['OnceConfig']; onChange: ConfigChange }) {
  const [at, setAt] = useState(wallClock(config.at))
  return (
    <TextInput
      label="触发时间"
      hint="按服务端配置文件中的 timezone 解释；必须是将来的时间。触发一次之后它就从列表里消失，在「已结束」里还能看到结果"
      type="datetime-local"
      value={at}
      onChange={(value) => {
        setAt(value)
        onChange(value ? { at: value } : null)
      }}
    />
  )
}

function StreamEditor({ config, onChange }: { config: EditableConfig<Schemas['StreamConfig']>; onChange: ConfigChange }) {
  const [command, setCommand] = useState(config.command)
  const [secrets, setSecrets] = useState((config.secrets ?? []).join(', '))
  const [debounce, setDebounce] = useState(config.debounce_seconds === undefined ? '' : String(config.debounce_seconds))

  function emit(next: { command?: string; secrets?: string; debounce?: string }) {
    const v = { command, secrets, debounce, ...next }
    // 合并窗口留空用服务端的默认值；范围由服务端校验（不合法时保存报 422），这里只挡不是数字的输入
    const seconds = v.debounce.trim() === '' ? undefined : Number(v.debounce)
    onChange(
      v.command.trim() && (seconds === undefined || Number.isFinite(seconds))
        ? {
            command: v.command,
            secrets: v.secrets.split(',').map((k) => k.trim()).filter((k) => k !== ''),
            ...(seconds === undefined ? {} : { debounce_seconds: seconds }),
          }
        : null,
    )
  }

  return (
    <>
      <TextAreaInput
        label="常驻命令（bash，在 agent 的工作目录里运行）"
        hint="stdout 每一行是一个事件；退出后按退避自动重启，停用或删除时被杀掉。stderr 写到工作目录的 trigger-logs/<名称>.stderr.log"
        value={command}
        onChange={(value) => {
          setCommand(value)
          emit({ command: value })
        }}
        rows={3}
        mono
      />
      <TextInput
        label="合并窗口（秒）"
        hint="第一行到达后再等这么久，期间的行合并成一次触发；上次的会话还在忙时事件排队，它结束后一起投递。留空用默认值"
        value={debounce}
        onChange={(value) => {
          setDebounce(value)
          emit({ debounce: value })
        }}
        mono
      />
      <TextInput
        label="secret（可选）"
        hint="vault 里的 key，逗号分隔，作为同名环境变量传给命令"
        value={secrets}
        onChange={(value) => {
          setSecrets(value)
          emit({ secrets: value })
        }}
        mono
      />
    </>
  )
}

/** 按方式选配置编辑器；webhook 没有配置项 */
function ConfigEditor({ value, onChange }: { value: KindConfig; onChange: ConfigChange }) {
  switch (value.kind) {
    case 'schedule':
      return <CronEditor config={value.config} onChange={onChange} />
    case 'once':
      return <RunAtEditor config={value.config} onChange={onChange} />
    case 'webhook':
      return null
    case 'stream':
      return <StreamEditor config={value.config} onChange={onChange} />
  }
}

const FIRE_WHEN_LABELS: Record<Precheck['fire_when'], string> = {
  output: '有输出就触发',
  changed: '输出变化才触发',
}

/** 前置检查：先跑一条命令，只有真有事才调用 agent。关闭时回调 null（每次都交给 agent） */
function PrecheckEditor({ precheck, onChange }: { precheck: Precheck | null; onChange: (p: Precheck | null) => void }) {
  const [on, setOn] = useState(precheck !== null)
  const [command, setCommand] = useState(precheck?.command ?? '')
  const [secrets, setSecrets] = useState((precheck?.secrets ?? []).join(', '))
  const [fireWhen, setFireWhen] = useState<Precheck['fire_when']>(precheck?.fire_when ?? 'output')

  function emit(next: { on?: boolean; command?: string; secrets?: string; fireWhen?: Precheck['fire_when'] }) {
    const v = { on, command, secrets, fireWhen, ...next }
    onChange(
      v.on
        ? {
            command: v.command,
            secrets: v.secrets.split(',').map((k) => k.trim()).filter((k) => k !== ''),
            fire_when: v.fireWhen,
          }
        : null,
    )
  }

  return (
    <div className="flex flex-col gap-2 rounded-xl border border-separator p-3">
      <label className="flex items-center gap-2 text-sm">
        <ToggleSwitch
          label="前置检查"
          isSelected={on}
          onChange={(value) => {
            setOn(value)
            emit({ on: value })
          }}
        />
        前置检查：先跑一条命令，有事才调用 agent
      </label>
      {on && (
        <>
          <TextAreaInput
            label="命令（bash，在 agent 的工作目录里执行）"
            hint="stdin 是事件内容（webhook 的请求体）。退出码 0：按下面的条件决定是否触发，输出作为事件交给 agent；非 0 视为检查出错，不触发并推送一次告警"
            value={command}
            onChange={(value) => {
              setCommand(value)
              emit({ command: value })
            }}
            rows={3}
            mono
          />
          <SelectInput
            label="触发条件"
            value={fireWhen}
            onChange={(value) => {
              const next = value as Precheck['fire_when']
              setFireWhen(next)
              emit({ fireWhen: next })
            }}
            options={[
              { id: 'output', label: FIRE_WHEN_LABELS.output, description: '没有输出就跳过；适合脚本自己记游标、只打印新内容' },
              { id: 'changed', label: FIRE_WHEN_LABELS.changed, description: '与上次输出比较，交给 agent 差异和完整输出；第一次只记录基线' },
            ]}
          />
          <TextInput
            label="secret（可选）"
            hint="vault 里的 key，逗号分隔，作为同名环境变量传给命令"
            value={secrets}
            onChange={(value) => {
              setSecrets(value)
              emit({ secrets: value })
            }}
            mono
          />
        </>
      )}
    </div>
  )
}

function PrecheckSummary({ trigger }: { trigger: Trigger }) {
  if (trigger.precheck === null) return null
  return (
    <div className="flex flex-col gap-1 text-xs text-muted">
      <div>
        前置检查 · {FIRE_WHEN_LABELS[trigger.precheck.fire_when]}
        {trigger.last_checked_at && <> · 上次检查 {formatTime(trigger.last_checked_at)}</>}
      </div>
      <pre className={CODE_BLOCK}>{trigger.precheck.command}</pre>
    </div>
  )
}

/** webhook 的 token 只在生成后显示这一次 */
function WebhookToken({ trigger, onChange }: { trigger: TriggerOf<'webhook'>; onChange: () => void }) {
  const confirm = useConfirm()
  const action = useAction()
  const [token, setToken] = useState<string | null>(null)
  const hasToken = trigger.status.has_token

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
              {`curl -X POST ${trigger.status.url} \\\n  -H 'Authorization: Bearer ${token}' \\\n  -d '{"hello": "world"}'`}
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

/** 这种方式自己的运行状态：下次时间、地址等 */
function KindStatus({ trigger }: { trigger: Trigger }) {
  switch (trigger.kind) {
    case 'schedule':
      return (
        <span>
          <code className="rounded bg-surface-secondary px-1.5 py-0.5 font-mono text-xs">{trigger.config.cron}</code>
          {trigger.enabled && <> · 下次 {formatTime(trigger.status.next_run_at)}</>}
          {trigger.status.skipped && (
            <span className="text-warning">
              {' '}
              · 自 {formatTime(trigger.status.skipped.since)} 起已跳过 {trigger.status.skipped.count} 次：上次的会话
              {trigger.status.skipped.previous_state === 'waiting' ? '在等待（后台任务、委派、到点唤醒或你的回答）' : '还在运行'}
            </span>
          )}
        </span>
      )
    case 'once':
      return <span>计划 {formatTime(trigger.status.run_at)}</span>
    case 'webhook':
      return (
        <span className="break-all">
          <code className="rounded bg-surface-secondary px-1.5 py-0.5 font-mono text-xs">POST {trigger.status.url}</code>
          {!trigger.status.has_token && <> · 还没有 token，生成后才能接收事件</>}
        </span>
      )
    case 'stream':
      return (
        <span className="flex flex-col gap-1">
          <code className="break-all rounded bg-surface-secondary px-1.5 py-0.5 font-mono text-xs">{trigger.config.command}</code>
          <span>
            {trigger.status.running_since ? <>运行中，自 {formatTime(trigger.status.running_since)}</> : '未在运行'}
            {' '}· stderr：<code className="font-mono text-xs">{trigger.status.stderr_log}</code>
          </span>
          {trigger.status.error && (
            <span className="text-warning break-all">
              连续失败 {trigger.status.failures} 次，最近一次：{trigger.status.error}
            </span>
          )}
        </span>
      )
  }
}

/** 这个触发器发起过的会话（后端按 origin=trigger 与它的 id 查），展开时才取 */
function TriggerSessions({ trigger }: { trigger: Trigger }) {
  const [open, setOpen] = useState(false)
  return (
    <Disclosure className="rounded-xl border border-separator px-3 py-2" onExpandedChange={setOpen}>
      <Disclosure.Heading>
        <Disclosure.Trigger className="flex w-full items-center gap-2 text-sm text-muted">
          <History className="size-4" />
          历史会话
          <Disclosure.Indicator className="ml-auto" />
        </Disclosure.Trigger>
      </Disclosure.Heading>
      <Disclosure.Content>
        <Disclosure.Body className="pt-2">{open && <SessionList triggerId={trigger.id} />}</Disclosure.Body>
      </Disclosure.Content>
    </Disclosure>
  )
}

function SessionList({ triggerId }: { triggerId: number }) {
  const from = useReturnState()
  const sessions = useLoad(
    () => api.sessions({ origin: ['trigger'], origin_ref: String(triggerId), limit: 50 }),
    [triggerId],
  )
  return (
    <Loading state={sessions}>
      {(list) =>
        list.length === 0 ? (
          <EmptyHint>还没有触发过</EmptyHint>
        ) : (
          <ul className="flex flex-col gap-1.5 text-sm">
            {list.map((s) => (
              <li key={s.id} className="flex flex-wrap items-baseline gap-2">
                <Link className="text-accent" to={sessionRoute(s.id)} state={from}>
                  {s.title}
                </Link>
                <span className="text-xs text-muted">{formatTime(s.created_at)}</span>
              </li>
            ))}
          </ul>
        )
      }
    </Loading>
  )
}

/** 启用开关：卡片和详情里共用；失败用 toast 提示（卡片上没有放错误的位置） */
function EnableSwitch({ trigger, onChange }: { trigger: Trigger; onChange: () => void }) {
  const [busy, setBusy] = useState(false)
  async function toggle(enabled: boolean) {
    setBusy(true)
    try {
      await api.updateTrigger(trigger.id, { enabled })
      onChange()
    } catch (e) {
      toast.danger(`${enabled ? '启用' : '停用'}「${trigger.name}」失败：${errorMessage(e)}`)
    } finally {
      setBusy(false)
    }
  }
  return (
    <ToggleSwitch
      label={`启用触发器「${trigger.name}」`}
      isSelected={trigger.enabled}
      isDisabled={busy}
      onChange={(enabled) => void toggle(enabled)}
    />
  )
}

/** 卡片上的一行配置摘要，完整内容在详情里 */
function kindSummary(trigger: Trigger): string {
  switch (trigger.kind) {
    case 'schedule':
      return trigger.enabled ? `${trigger.config.cron} · 下次 ${formatTime(trigger.status.next_run_at)}` : trigger.config.cron
    case 'once':
      return `计划 ${formatTime(trigger.status.run_at)}`
    case 'webhook':
      return `POST ${trigger.status.url}`
    case 'stream':
      return trigger.config.command
  }
}

/** 需要人注意的状态，卡片上只给一句话，原因在详情里 */
function triggerWarning(trigger: Trigger): string | null {
  if (trigger.disabled_reason) return trigger.finished_at ? '触发失败' : '触发失败，已自动停用'
  if (trigger.precheck_error) return '前置检查出错'
  switch (trigger.kind) {
    case 'schedule':
      return trigger.status.skipped ? `已连续跳过 ${trigger.status.skipped.count} 次` : null
    case 'webhook':
      return trigger.status.has_token ? null : '还没有 token'
    case 'stream':
      return trigger.status.error ? `连续失败 ${trigger.status.failures} 次` : null
    case 'once':
      return null
  }
}

/** 网格里的一张卡片：各项只显示开头，点开看详情。整张卡片可点（铺满的透明按钮），开关浮在它上面 */
function TriggerCard({
  trigger,
  agentName,
  onOpen,
  onChange,
}: {
  trigger: Trigger
  /** 不按 agent 分组的列表（已结束）在卡片上标出 agent；分组里组头已经写了 */
  agentName?: string
  onOpen: () => void
  /** 已结束的触发器不能启停，不传 */
  onChange?: () => void
}) {
  const warning = triggerWarning(trigger)
  return (
    <Card variant={onChange ? 'default' : 'secondary'} className="relative gap-2 transition-shadow hover:shadow-md">
      <button
        type="button"
        aria-label={`查看触发器「${trigger.name}」的详情`}
        className="absolute inset-0 z-0 cursor-pointer rounded-[inherit] outline-none focus-visible:ring-2 focus-visible:ring-accent"
        onClick={onOpen}
      />
      <div className="pointer-events-none flex items-start gap-2">
        <div className="min-w-0 flex-1">
          <div className="truncate font-medium">{trigger.name}</div>
          <div className="mt-1 flex flex-wrap items-center gap-1.5">
            <Chip size="sm" variant="soft">
              {KIND_LABELS[trigger.kind].label}
            </Chip>
            {agentName !== undefined && (
              <Chip size="sm" color="accent" variant="soft">
                {agentName}
              </Chip>
            )}
            {warning && (
              <Chip size="sm" color="warning" variant="soft">
                {warning}
              </Chip>
            )}
          </div>
        </div>
        {onChange && (
          <div className="pointer-events-auto relative z-10">
            <EnableSwitch trigger={trigger} onChange={onChange} />
          </div>
        )}
      </div>
      <div className="pointer-events-none flex min-w-0 flex-col gap-1.5">
        <code className="truncate font-mono text-xs text-muted">{kindSummary(trigger)}</code>
        {trigger.precheck && (
          <div className="truncate text-xs text-muted">
            前置检查：<code className="font-mono">{trigger.precheck.command}</code>
          </div>
        )}
        <p className="line-clamp-3 whitespace-pre-wrap break-words text-sm">{trigger.prompt}</p>
        <div className="text-xs text-muted">
          {trigger.finished_at
            ? `完成 ${formatTime(trigger.finished_at)}`
            : trigger.last_fired_at
              ? `上次触发 ${formatTime(trigger.last_fired_at)}`
              : '还没有触发过'}
        </div>
      </div>
    </Card>
  )
}

/** 卡片网格：按容器宽度自动排列，宽屏一行 3–4 张 */
function CardGrid({ children }: { children: ReactNode }) {
  return <div className="grid grid-cols-[repeat(auto-fill,minmax(17rem,1fr))] gap-3">{children}</div>
}

/**
 * 详情弹窗：trigger 为 undefined 时关闭（包括详情里删掉了它、列表重新加载后不再有它）。
 * 点背景不关：误触会丢掉编辑草稿和只显示一次的 webhook token；只用 Esc 或关闭按钮
 */
function DetailModal({
  trigger,
  onClose,
  children,
}: {
  trigger: Trigger | undefined
  onClose: () => void
  children: (trigger: Trigger) => ReactNode
}) {
  // 关闭动画期间继续显示最后一个，不让弹窗先变空再收起
  const [shown, setShown] = useState(trigger)
  if (trigger !== undefined && trigger !== shown) setShown(trigger)
  return (
    <Modal.Backdrop isOpen={trigger !== undefined} isDismissable={false} onOpenChange={(o) => !o && onClose()}>
      <Modal.Container size="lg" scroll="inside">
        <Modal.Dialog className="sm:max-w-3xl">{shown && children(shown)}</Modal.Dialog>
      </Modal.Container>
    </Modal.Backdrop>
  )
}

function TriggerItem({ trigger, agents, onChange }: { trigger: Trigger; agents: Agent[] | undefined; onChange: () => void }) {
  const from = useReturnState()
  const confirm = useConfirm()
  const [editing, setEditing] = useState(false)
  const [prompt, setPrompt] = useState(trigger.prompt)
  const [config, setConfig] = useState<Config | null>(trigger.config)
  const [precheck, setPrecheck] = useState<Precheck | null>(trigger.precheck)
  const action = useAction()

  // 每次进入编辑都从已保存的值开始：取消过的草稿不会在下一次保存时被提交
  function startEditing() {
    setPrompt(trigger.prompt)
    setConfig(trigger.config)
    setPrecheck(trigger.precheck)
    setEditing(true)
  }

  const precheckChanged = JSON.stringify(precheck) !== JSON.stringify(trigger.precheck)
  const save = () =>
    action.run(async () => {
      if (config === null) throw new Error('配置不合法')
      // 前置检查没改就不提交：改了会重置 changed 模式的基线
      await api.updateTrigger(trigger.id, { prompt, config, ...(precheckChanged ? { precheck } : {}) })
      setEditing(false)
      onChange()
    })

  async function remove() {
    const ok = await confirm({ title: `删除触发器「${trigger.name}」？`, confirmLabel: '删除', danger: true })
    if (ok) await action.run(() => api.deleteTrigger(trigger.id).then(onChange))
  }

  return (
    <>
      <Modal.Header className="flex flex-row items-start gap-3 pr-8">
        <div className="min-w-0 flex-1">
          <Modal.Heading className="break-all">{trigger.name}</Modal.Heading>
          <div className="mt-1 flex flex-wrap items-center gap-1.5 text-xs text-muted">
            <Chip size="sm" variant="soft">
              {KIND_LABELS[trigger.kind].label}
            </Chip>
            <Chip size="sm" color="accent" variant="soft">
              {agentLabel(agents, trigger.agent_id)}
            </Chip>
            <KindStatus trigger={trigger} />
          </div>
        </div>
        <EnableSwitch trigger={trigger} onChange={onChange} />
      </Modal.Header>
      <Modal.Body className="flex flex-col gap-3">
        <div className="text-xs text-muted">
          {trigger.last_fired_at ? <>上次触发 {formatTime(trigger.last_fired_at)}</> : '还没有触发过'}
          {trigger.last_session_id && (
            <>
              {' · '}
              <Link className="text-accent" to={sessionRoute(trigger.last_session_id)} state={from}>
                上次会话
              </Link>
            </>
          )}
        </div>
        {trigger.disabled_reason && <ErrorAlert title="触发失败，已自动停用" error={trigger.disabled_reason} />}
        {trigger.precheck_error && (
          <ErrorAlert title="前置检查出错，本次未触发" error={trigger.precheck_error} />
        )}
        {editing ? (
          <>
            <ConfigEditor value={trigger} onChange={setConfig} />
            <PrecheckEditor precheck={trigger.precheck} onChange={setPrecheck} />
            <TextAreaInput label="处理说明" value={prompt} onChange={setPrompt} rows={5} />
          </>
        ) : (
          <>
            <PrecheckSummary trigger={trigger} />
            <p className="whitespace-pre-wrap text-sm">{trigger.prompt}</p>
            <TriggerSessions trigger={trigger} />
          </>
        )}
        <ErrorAlert error={action.error} />
      </Modal.Body>
      <Modal.Footer className="flex flex-wrap justify-start gap-2">
        {editing ? (
          <>
            <ActionButton
              size="sm"
              isDisabled={config === null || (precheck !== null && !precheck.command.trim())}
              isPending={action.busy}
              onPress={() => void save()}
            >
              保存
            </ActionButton>
            <Button size="sm" variant="tertiary" onPress={() => setEditing(false)}>
              取消
            </Button>
          </>
        ) : (
          <>
            <Button size="sm" variant="secondary" onPress={startEditing}>
              <Pencil className="size-4" />
              编辑
            </Button>
            <Button
              size="sm"
              variant="secondary"
              isDisabled={action.busy || !trigger.enabled}
              onPress={() =>
                void action.run(async () => {
                  const { session_id } = await api.runTrigger(trigger.id)
                  toast.success(session_id === null ? '前置检查没有发现要处理的事，未调用 agent' : '已触发')
                  onChange()
                })
              }
            >
              <Play className="size-4" />
              立即运行
            </Button>
            {trigger.kind === 'webhook' && <WebhookToken trigger={trigger} onChange={onChange} />}
            <Button size="sm" variant="danger-soft" isDisabled={action.busy} onPress={() => void remove()}>
              <Trash2 className="size-4" />
              删除
            </Button>
          </>
        )}
      </Modal.Footer>
      <Modal.CloseTrigger />
    </>
  )
}

function TriggerForm({ kinds, agents, onCreated }: { kinds: TriggerKind[]; agents: Agent[] | undefined; onCreated: () => void }) {
  // 选中方式时的初始配置，也就决定了当前的方式；编辑中的配置在 config 里
  const [initial, setInitial] = useState<KindConfig | null>(kinds[0] ? emptyConfig(kinds[0].name) : null)
  const kind = initial?.kind ?? null
  const [name, setName] = useState('')
  const [agentId, setAgentId] = useState<string | null>(null)
  const [prompt, setPrompt] = useState('')
  const [config, setConfig] = useState<Config | null>(initial?.config ?? null)
  const [precheck, setPrecheck] = useState<Precheck | null>(null)
  const action = useAction()

  async function create() {
    const ok = await action.run(async () => {
      if (kind === null || agentId === null) throw new Error('请选择触发方式和 agent')
      if (config === null) throw new Error('配置不合法')
      await api.createTrigger({ name, agent_id: asAgentId(agentId), kind, prompt, config, precheck })
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
        onChange={(id) => {
          const k = kinds.find((x) => x.name === id)?.name
          const next = k ? emptyConfig(k) : null
          setInitial(next)
          setConfig(next?.config ?? null)
        }}
        options={kinds.map((k) => ({ id: k.name, label: KIND_LABELS[k.name].label, description: KIND_LABELS[k.name].summary }))}
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
        options={selectableAgents(agents, agentId).map((a) => ({ id: a.id, label: a.name }))}
      />
      {initial && <ConfigEditor key={initial.kind} value={initial} onChange={setConfig} />}
      <PrecheckEditor precheck={null} onChange={setPrecheck} />
      <TextAreaInput
        label="处理说明"
        hint="每次触发都在新会话中执行；事件内容（如 webhook 的请求体）附在其后。触发的会话不会自动推送，需要时在说明里要求 agent 调用 notify_user"
        value={prompt}
        onChange={setPrompt}
        rows={5}
      />
      <ErrorAlert error={action.error} />
      <ActionButton
        isDisabled={!name || !agentId || !prompt || !kind || config === null || (precheck !== null && !precheck.command.trim())}
        isPending={action.busy}
        onPress={() => void create()}
      >
        创建
      </ActionButton>
    </CollapsibleForm>
  )
}

/** 已结束的触发器（跑完的一次性触发器）的详情：只能看结果与删除，不能再改、启用或运行 */
function FinishedTrigger({ trigger, agents, onChange }: { trigger: Trigger; agents: Agent[] | undefined; onChange: () => void }) {
  const from = useReturnState()
  const confirm = useConfirm()
  const action = useAction()

  async function remove() {
    const ok = await confirm({ title: `删除「${trigger.name}」的记录？`, body: '它发起的会话不会被删除。', confirmLabel: '删除', danger: true })
    if (ok) await action.run(() => api.deleteTrigger(trigger.id).then(onChange))
  }

  return (
    <>
      <Modal.Header className="pr-8">
        <Modal.Heading className="break-all">{trigger.name}</Modal.Heading>
        <div className="mt-1 flex flex-wrap items-center gap-1.5 text-xs text-muted">
          <Chip size="sm" variant="soft">
            {KIND_LABELS[trigger.kind].label}
          </Chip>
          <Chip size="sm" color="accent" variant="soft">
            {agentLabel(agents, trigger.agent_id)}
          </Chip>
          <KindStatus trigger={trigger} />
          {trigger.finished_at && <span>· 完成 {formatTime(trigger.finished_at)}</span>}
        </div>
      </Modal.Header>
      <Modal.Body className="flex flex-col gap-3">
        <div className="text-xs text-muted">
          {trigger.last_session_id ? (
            <Link className="text-accent" to={sessionRoute(trigger.last_session_id)} state={from}>
              这次触发的会话
            </Link>
          ) : (
            '没有触发会话（前置检查判定没事，或触发失败）'
          )}
        </div>
        {trigger.disabled_reason && <ErrorAlert title="触发失败" error={trigger.disabled_reason} />}
        {trigger.precheck_error && <ErrorAlert title="前置检查出错，未触发" error={trigger.precheck_error} />}
        <p className="whitespace-pre-wrap text-sm">{trigger.prompt}</p>
        <TriggerSessions trigger={trigger} />
        <ErrorAlert error={action.error} />
      </Modal.Body>
      <Modal.Footer className="justify-start">
        <Button size="sm" variant="danger-soft" isDisabled={action.busy} onPress={() => void remove()}>
          <Trash2 className="size-4" />
          删除记录
        </Button>
      </Modal.Footer>
      <Modal.CloseTrigger />
    </>
  )
}

function FinishedTriggers({ agents }: { agents: Agent[] | undefined }) {
  const finished = useLoad(() => api.triggers(true), [])
  const [openId, setOpenId] = useState<number | null>(null)
  return (
    <Loading state={finished}>
      {(list) => {
        if (list.length === 0) return <EmptyHint>还没有跑完的一次性触发器</EmptyHint>
        const open = list.find((t) => t.id === openId)
        return (
          <>
            <CardGrid>
              {list.map((t) => (
                <TriggerCard key={t.id} trigger={t} agentName={agentLabel(agents, t.agent_id)} onOpen={() => setOpenId(t.id)} />
              ))}
            </CardGrid>
            <DetailModal trigger={open} onClose={() => setOpenId(null)}>
              {(t) => <FinishedTrigger key={t.id} trigger={t} agents={agents} onChange={() => void finished.reload()} />}
            </DetailModal>
          </>
        )
      }}
    </Loading>
  )
}

/** 自动发起会话的入口：定时、webhook……方式的差异只在各自的配置与操作。按 agent 分组的卡片网格，点卡片看详情 */
export default function Triggers() {
  const triggers = useLoad(() => api.triggers(), [])
  const kinds = useLoad(() => api.triggerKinds(), [])
  const agents = useAgents()
  // 详情跟着列表走：重新加载后显示最新内容，删掉后自动关闭
  const [openId, setOpenId] = useState<number | null>(null)
  // 展开时才取已结束的列表，顺便每次展开都是最新的
  const [showFinished, setShowFinished] = useState(false)
  const reload = () => void triggers.reload()
  return (
    <Page title="触发器" wide>
      <div className="mx-auto flex w-full max-w-7xl flex-col gap-4 px-4 py-3">
        <Loading state={kinds}>{(list) => <TriggerForm kinds={list} agents={agents.data} onCreated={reload} />}</Loading>
        <Loading state={triggers}>
          {(list) => {
            if (list.length === 0) return <EmptyHint>没有触发器</EmptyHint>
            const open = list.find((t) => t.id === openId)
            return (
              <>
                {groupTriggersByAgent(list, agents.data).map((g) => (
                  <GroupSection key={g.agentId} name={agentLabel(agents.data, g.agentId)} count={g.triggers.length}>
                    <CardGrid>
                      {g.triggers.map((t) => (
                        <TriggerCard key={t.id} trigger={t} onOpen={() => setOpenId(t.id)} onChange={reload} />
                      ))}
                    </CardGrid>
                  </GroupSection>
                ))}
                <DetailModal trigger={open} onClose={() => setOpenId(null)}>
                  {(t) => (
                    <TriggerItem
                      key={`${t.id}-${t.prompt}-${JSON.stringify(t.config)}-${JSON.stringify(t.precheck)}`}
                      trigger={t}
                      agents={agents.data}
                      onChange={reload}
                    />
                  )}
                </DetailModal>
              </>
            )
          }}
        </Loading>
        <Card>
          <Disclosure onExpandedChange={setShowFinished}>
            <Disclosure.Heading>
              <Disclosure.Trigger className="flex w-full items-center gap-2 px-1 font-medium">
                <Archive className="size-4" />
                已结束
                <Disclosure.Indicator className="ml-auto" />
              </Disclosure.Trigger>
            </Disclosure.Heading>
            <Disclosure.Content>
              <Disclosure.Body className="flex flex-col gap-3 pt-4">
                {showFinished && <FinishedTriggers agents={agents.data} />}
              </Disclosure.Body>
            </Disclosure.Content>
          </Disclosure>
        </Card>
      </div>
    </Page>
  )
}
