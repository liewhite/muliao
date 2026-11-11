import { useState, type ReactNode } from 'react'
import { Alert, Button, Card, Chip, Disclosure, Modal, toast } from '@heroui/react'
import { Archive, Copy, History, KeyRound, Pencil, Play, Trash2 } from 'lucide-react'
import { Link } from 'react-router-dom'
import { useReturnState } from '../nav'
import {
  api,
  asAgentId,
  type Precheck,
  type Schemas,
  type Trigger,
  type TriggerKind,
  type TriggerCreate,
  type TriggerKindName,
  sessionRoute,
} from '../api'
import { selectableAgents, agentLabel } from '../agentGroups'
import { groupTriggersByAgent } from '../triggerGroups'
import { useDraft } from '../drafts'
import { inAnHour, wallClock } from '../wallClock'
import {
  ActionButton,
  CODE_BLOCK,
  CollapsibleForm,
  EmptyHint,
  ErrorAlert,
  GroupSection,
  DraftConflicts,
  Loading,
  Page,
  SelectInput,
  TextAreaInput,
  TextInput,
  ToggleSwitch,
  CardGrid,
  copyText,
  formatTime,
  useAction,
  useAgentName,
  useAgents,
  useConfirm,
} from '../ui'
import { errorMessage, refreshShown, useLoad } from '../load'

/** 某种方式的触发器：收窄后 config 与 status 都是这种方式的类型 */
type TriggerOf<K extends TriggerKindName> = Extract<Trigger, { kind: K }>

/** kind 与它的配置成对出现，形状同新建请求（有服务端默认值的配置项可省，读到的配置也合这个形状），编辑器按 kind 收窄 */
type KindConfig = { [K in TriggerKindName]: Pick<Extract<TriggerCreate, { kind: K }>, 'kind' | 'config'> }[TriggerKindName]

/** 配置编辑器以 config 为初始值、自己保存编辑中的内容；内容不合法时回调 null：保存按钮随之禁用，不会提交上一次的合法值 */
type ConfigChange = (c: KindConfig | null) => void

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
        onChange({ kind: 'schedule', config: { cron: value } })
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
        onChange(value ? { kind: 'once', config: { at: value } } : null)
      }}
    />
  )
}

function StreamEditor({ config, onChange }: { config: Schemas['StreamConfig-Input']; onChange: ConfigChange }) {
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
            kind: 'stream',
            config: {
              command: v.command,
              secrets: v.secrets.split(',').map((k) => k.trim()).filter((k) => k !== ''),
              ...(seconds === undefined ? {} : { debounce_seconds: seconds }),
            },
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

function RouteInput({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  // 地址格式的说明只在后端一份（与给 agent 的工具说明相同，是英文）
  const help = useLoad(null, () => api.routeHelp(), [])
  return (
    <TextInput
      label="投递目标"
      hint={`可选地址：${help.data?.address_help ?? help.error ?? '…'}。事件里单独一行 @route <目标> 可以把之后的内容投到别处`}
      value={value}
      onChange={onChange}
      placeholder="new"
      mono
    />
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
function WebhookToken({ trigger }: { trigger: TriggerOf<'webhook'> }) {
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
          {trigger.status.failures > 0 && (
            <span className="text-warning">连续很快退出 {trigger.status.failures} 次，按退避重启中</span>
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
  const sessions = useLoad('sessions', () => api.sessions({ trigger_id: triggerId, limit: 50 }), [triggerId])
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
function EnableSwitch({ trigger }: { trigger: Trigger }) {
  const [busy, setBusy] = useState(false)
  async function toggle(enabled: boolean) {
    setBusy(true)
    try {
      await api.updateTrigger(trigger.id, { enabled })
      await refreshShown()
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

type FaultSource = Schemas['FaultSource']

/** 各种脚本出错的标题：卡片上的一句话与详情里的标题 */
const FAULT_TITLES: Record<FaultSource, { short: string; title: string }> = {
  precheck: { short: '前置检查出错', title: '前置检查出错，未触发' },
  route: { short: '投递地址写错，有段被丢弃', title: '投递地址写错，这些地址的段被丢弃（其余照投）' },
  stream: { short: '流式命令反复退出', title: '流式命令反复退出' },
}

/** 脚本出错，按来源各一个提示；改前置检查或配置、重新启用后清除，检查成功或流式命令稳定运行后清除各自的 */
function TriggerFaults({ trigger }: { trigger: Trigger }) {
  const sources = [...new Set(trigger.faults.map((f) => f.source))]
  return sources.map((source) => (
    <ErrorAlert
      key={source}
      title={FAULT_TITLES[source].title}
      error={trigger.faults
        .filter((f) => f.source === source)
        .map((f) => (f.address ? `@route ${f.address}: ${f.error}` : f.error))
        .join('\n')}
    />
  ))
}

/** 需要人注意的状态，卡片上只给一句话，原因在详情里 */
function triggerWarning(trigger: Trigger): string | null {
  if (trigger.disabled_reason) return trigger.finished_at ? '触发失败' : '触发失败，已自动停用'
  if (trigger.faults.length) return FAULT_TITLES[trigger.faults[0].source].short
  switch (trigger.kind) {
    case 'schedule':
      return trigger.status.skipped ? `已连续跳过 ${trigger.status.skipped.count} 次` : null
    case 'webhook':
      return trigger.status.has_token ? null : '还没有 token'
    case 'stream':
      return trigger.status.failures ? `连续失败 ${trigger.status.failures} 次` : null
    case 'once':
      return null
  }
}

/** 网格里的一张卡片：各项只显示开头，点开看详情。整张卡片可点（铺满的透明按钮），开关浮在它上面 */
function TriggerCard({
  trigger,
  agentName,
  onOpen,
}: {
  trigger: Trigger
  /** 不按 agent 分组的列表（已结束）在卡片上标出 agent；分组里组头已经写了 */
  agentName?: string
  onOpen: () => void
}) {
  const warning = triggerWarning(trigger)
  // 已结束的触发器不能启停
  const finished = trigger.finished_at !== null
  return (
    <Card variant={finished ? 'secondary' : 'default'} className="relative gap-2 transition-shadow hover:shadow-md">
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
        {!finished && (
          <div className="pointer-events-auto relative z-10">
            <EnableSwitch trigger={trigger} />
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
        {trigger.route !== 'new' && (
          <div className="truncate text-xs text-muted">
            投递到：<code className="font-mono">{trigger.route}</code>
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

function TriggerItem({ trigger }: { trigger: Trigger }) {
  const from = useReturnState()
  const agentName = useAgentName(trigger.agent_id)
  const [editing, setEditing] = useState(false)
  // 详情正文开头的几行，查看与编辑时都显示
  const facts = (
    <>
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
      <TriggerFaults trigger={trigger} />
    </>
  )
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
              {agentName}
            </Chip>
            <KindStatus trigger={trigger} />
          </div>
        </div>
        <EnableSwitch trigger={trigger} />
      </Modal.Header>
      {/* 编辑器只在编辑时挂载：每次进入编辑都从当前的值开始，取消过的草稿不会在下一次保存时被提交 */}
      {editing ? (
        <TriggerEditor trigger={trigger} facts={facts} onDone={() => setEditing(false)} />
      ) : (
        <TriggerShown trigger={trigger} facts={facts} onEdit={() => setEditing(true)} />
      )}
      <Modal.CloseTrigger />
    </>
  )
}

const TRIGGER_FIELD_LABELS = { prompt: '处理说明', config: '配置', precheck: '前置检查', route: '投递目标' }

/** 字段草稿（drafts.useDraft）：只提交改过的，没动的字段别人可能改过，同一字段两边都改了提示冲突；前置检查改了还会重置
 * changed 模式的基线 */
function TriggerEditor({ trigger, facts, onDone }: { trigger: Trigger; facts: ReactNode; onDone: () => void }) {
  // 配置不合法时为 null
  const form = useDraft<{ prompt: string; config: KindConfig['config'] | null; precheck: Precheck | null; route: string }>({
    prompt: trigger.prompt,
    config: trigger.config,
    precheck: trigger.precheck,
    route: trigger.route,
  })
  const action = useAction()
  const { draft } = form
  const save = () =>
    action.run(async () => {
      const { config, route, base, ...rest } = form.patch
      if (config === null) throw new Error('配置不合法')
      const patch = { ...rest, ...(config && { config }), ...(route !== undefined && { route: route.trim() }), base }
      if (Object.keys(form.changes).length > 0) await api.updateTrigger(trigger.id, patch)
      onDone()
    })
  // 配置与前置检查的编辑器自己保存编辑中的内容、只在挂载时读初值：基线换了（跟上服务端、用对方的）就按 key 重新挂载，
  // 从此刻的草稿开始（草稿不合法时从基线开始）
  const config = { kind: trigger.kind, config: draft.config ?? form.base.config } as KindConfig
  return (
    <>
      <Modal.Body className="flex flex-col gap-3">
        {facts}
        <ConfigEditor
          key={JSON.stringify(form.base.config)}
          value={config}
          onChange={(c) => form.set({ config: c?.config ?? null })}
        />
        <PrecheckEditor
          key={JSON.stringify(form.base.precheck)}
          precheck={draft.precheck}
          onChange={(precheck) => form.set({ precheck })}
        />
        <RouteInput value={draft.route} onChange={(route) => form.set({ route })} />
        <TextAreaInput label="处理说明" value={draft.prompt} onChange={(prompt) => form.set({ prompt })} rows={5} />
        <DraftConflicts draft={form} labels={TRIGGER_FIELD_LABELS} />
        <ErrorAlert error={action.error} />
      </Modal.Body>
      <Modal.Footer className="flex flex-wrap justify-start gap-2">
        <ActionButton
          size="sm"
          // 投递目标必填（回到默认就填说明里的默认地址）；新建时留空才是用默认
          isDisabled={
            draft.config === null ||
            !draft.route.trim() ||
            (draft.precheck !== null && !draft.precheck.command.trim()) ||
            form.conflicts.length > 0
          }
          isPending={action.busy}
          onPress={() => void save()}
        >
          保存
        </ActionButton>
        <Button size="sm" variant="tertiary" onPress={onDone}>
          取消
        </Button>
      </Modal.Footer>
    </>
  )
}

function TriggerShown({ trigger, facts, onEdit }: { trigger: Trigger; facts: ReactNode; onEdit: () => void }) {
  const confirm = useConfirm()
  const action = useAction()

  async function remove() {
    const ok = await confirm({ title: `删除触发器「${trigger.name}」？`, confirmLabel: '删除', danger: true })
    if (ok) await action.run(() => api.deleteTrigger(trigger.id))
  }

  return (
    <>
      <Modal.Body className="flex flex-col gap-3">
        {facts}
        <PrecheckSummary trigger={trigger} />
        <div className="text-xs text-muted">
          投递到：<code className="font-mono">{trigger.route}</code>
        </div>
        <p className="whitespace-pre-wrap text-sm">{trigger.prompt}</p>
        <TriggerSessions trigger={trigger} />
        <ErrorAlert error={action.error} />
      </Modal.Body>
      <Modal.Footer className="flex flex-wrap justify-start gap-2">
        <Button size="sm" variant="secondary" onPress={onEdit}>
          <Pencil className="size-4" />
          编辑
        </Button>
        <Button
          size="sm"
          variant="secondary"
          isDisabled={action.busy || !trigger.enabled}
          onPress={() =>
            void action.run(async () => {
              const { fired } = await api.runTrigger(trigger.id)
              toast.success(fired ? '已触发' : '前置检查没有发现要处理的事，未调用 agent')
            })
          }
        >
          <Play className="size-4" />
          立即运行
        </Button>
        {trigger.kind === 'webhook' && <WebhookToken trigger={trigger} />}
        <Button size="sm" variant="danger-soft" isDisabled={action.busy} onPress={() => void remove()}>
          <Trash2 className="size-4" />
          删除
        </Button>
      </Modal.Footer>
    </>
  )
}

function TriggerForm({ kinds }: { kinds: TriggerKind[] }) {
  // 建好一条就换一份新的表单：各输入（含配置、前置检查自己的状态）都回到初始值，不带到下一条
  const [created, setCreated] = useState(0)
  return (
    <CollapsibleForm title="新建触发器">
      <TriggerFields
        key={created}
        kinds={kinds}
        onCreated={() => setCreated((n) => n + 1)}
      />
    </CollapsibleForm>
  )
}

/** onCreated：建好了（换一份空表单），列表随 changed 更新 */
function TriggerFields({ kinds, onCreated }: { kinds: TriggerKind[]; onCreated: () => void }) {
  const agents = useAgents().data
  // 选中方式时的初始配置，也就决定了当前的方式；编辑中的配置在 config 里
  const [initial, setInitial] = useState<KindConfig | null>(kinds[0] ? emptyConfig(kinds[0].name) : null)
  const kind = initial?.kind ?? null
  const [name, setName] = useState('')
  const [agentId, setAgentId] = useState<string | null>(null)
  const [prompt, setPrompt] = useState('')
  const [draft, setDraft] = useState<KindConfig | null>(initial)
  const [precheck, setPrecheck] = useState<Precheck | null>(null)
  const [route, setRoute] = useState('')
  const action = useAction()

  async function create() {
    const ok = await action.run(async () => {
      if (kind === null || agentId === null) throw new Error('请选择触发方式和 agent')
      if (draft === null) throw new Error('配置不合法')
      await api.createTrigger({
        ...draft,
        name,
        agent_id: asAgentId(agentId),
        prompt,
        precheck,
        route: route.trim() || null,
      })
      toast.success('触发器已创建')
    })
    if (ok) onCreated()
  }

  return (
    <>
      <SelectInput
        label="触发方式"
        value={kind}
        onChange={(id) => {
          const k = kinds.find((x) => x.name === id)?.name
          const next = k ? emptyConfig(k) : null
          setInitial(next)
          setDraft(next)
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
      {initial && <ConfigEditor key={initial.kind} value={initial} onChange={setDraft} />}
      <PrecheckEditor precheck={null} onChange={setPrecheck} />
      <RouteInput value={route} onChange={setRoute} />
      <TextAreaInput
        label="处理说明"
        hint="每次触发投到投递目标（默认新会话）；事件内容（如 webhook 的请求体）附在其后。触发的会话不会自动推送，需要时在说明里要求 agent 调用 notify_user"
        value={prompt}
        onChange={setPrompt}
        rows={5}
      />
      <ErrorAlert error={action.error} />
      <ActionButton
        isDisabled={!name || !agentId || !prompt || !kind || draft === null || (precheck !== null && !precheck.command.trim())}
        isPending={action.busy}
        onPress={() => void create()}
      >
        创建
      </ActionButton>
    </>
  )
}

/** 已结束的触发器（跑完的一次性触发器）的详情：只能看结果与删除，不能再改、启用或运行 */
function FinishedTrigger({ trigger }: { trigger: Trigger }) {
  const from = useReturnState()
  const agentName = useAgentName(trigger.agent_id)
  const confirm = useConfirm()
  const action = useAction()

  async function remove() {
    const ok = await confirm({ title: `删除「${trigger.name}」的记录？`, body: '它发起的会话不会被删除。', confirmLabel: '删除', danger: true })
    if (ok) await action.run(() => api.deleteTrigger(trigger.id))
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
            {agentName}
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
        <TriggerFaults trigger={trigger} />
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

function FinishedTriggers() {
  const finished = useLoad('triggers', () => api.triggers(true), [])
  const agents = useAgents().data
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
              {(t) => <FinishedTrigger key={t.id} trigger={t} />}
            </DetailModal>
          </>
        )
      }}
    </Loading>
  )
}

/** 自动发起会话的入口：定时、webhook……方式的差异只在各自的配置与操作。按 agent 分组的卡片网格，点卡片看详情 */
export default function Triggers() {
  const triggers = useLoad('triggers', () => api.triggers(), [])
  const kinds = useLoad(null, () => api.triggerKinds(), [])
  const agents = useAgents()
  // 详情跟着列表走：重新加载后显示最新内容，删掉后自动关闭
  const [openId, setOpenId] = useState<number | null>(null)
  // 展开时才取已结束的列表，顺便每次展开都是最新的
  const [showFinished, setShowFinished] = useState(false)
  return (
    <Page title="触发器" wide>
      <div className="mx-auto flex w-full max-w-7xl flex-col gap-4 px-4 py-3">
        <Loading state={kinds}>{(list) => <TriggerForm kinds={list} />}</Loading>
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
                        <TriggerCard key={t.id} trigger={t} onOpen={() => setOpenId(t.id)} />
                      ))}
                    </CardGrid>
                  </GroupSection>
                ))}
                <DetailModal trigger={open} onClose={() => setOpenId(null)}>
                  {(t) => <TriggerItem key={t.id} trigger={t} />}
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
                {showFinished && <FinishedTriggers />}
              </Disclosure.Body>
            </Disclosure.Content>
          </Disclosure>
        </Card>
      </div>
    </Page>
  )
}
