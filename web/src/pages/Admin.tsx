import { useEffect, useState } from 'react'
import { Button, Card, Chip, Form, toast } from '@heroui/react'
import { Activity, BellRing, ChevronRight, KeyRound, LogOut, Plug, RotateCcw, ScrollText, Sparkles, Trash2 } from 'lucide-react'
import { Link, useNavigate } from 'react-router-dom'
import { api, unauthorizedEvent, type SecretInfo, type TaskStatus } from '../api'
import { enablePush, pushState, unsubscribePush, type PushState } from '../push'
import { ActionButton,
  EmptyHint,
  ErrorAlert,
  Loading,
  PUBLIC_OWNER,
  Page,
  Segmented,
  SelectInput,
  TextInput,
  agentLabel,
  formatTime,
  ownerLabel,
  ownerOf,
  ownerOptions,
  useAction,
  useAgents,
  useConfirm,
  useLoad,
  errorMessage,
} from '../ui'

export function Vault() {
  const confirm = useConfirm()
  const secrets = useLoad(() => api.secrets(), [])
  const agents = useAgents()
  const [owner, setOwner] = useState(PUBLIC_OWNER)
  const [key, setKey] = useState('')
  const [value, setValue] = useState('')
  const [description, setDescription] = useState('')
  const action = useAction()

  async function save() {
    const ok = await action.run(async () => {
      await api.setSecret(ownerOf(owner), key, value, description)
      toast.success(`已保存 ${key}`)
    })
    if (ok) {
      setKey('')
      setValue('')
      setDescription('')
      await secrets.reload()
    }
  }

  async function remove(s: SecretInfo) {
    const ok = await confirm({
      title: `删除${ownerLabel(agents.data, s.owner)} secret「${s.key}」？`,
      body: '引用它的 MCP 与命令会失败（私有 secret 被删后，同名的公共 secret 会生效）。',
      confirmLabel: '删除',
      danger: true,
    })
    if (ok) await action.run(() => api.deleteSecret(s.owner, s.key).then(secrets.reload))
  }

  return (
    <Page title="Vault" back="/settings">
      <p className="text-sm text-muted">
        Secret 加密保存，值不会再显示。agent 通过 key 引用：MCP 配置里写{' '}
        <code className="font-mono">{'${vault:KEY}'}</code>，shell 里用{' '}
        <code className="font-mono">run_shell(secrets=[KEY])</code> 注入同名环境变量。私有 secret 只有该 agent 可用，同名时遮蔽公共
        secret；公共 MCP server 只能引用公共 secret。
      </p>
      <Card>
        <Card.Content className="flex flex-col gap-4">
          <SelectInput label="归属" value={owner} onChange={setOwner} options={ownerOptions(agents.data)} />
          <TextInput label="Key" value={key} onChange={setKey} placeholder="SLACK_TOKEN" mono isRequired />
          <TextInput label="值" type="password" value={value} onChange={setValue} isRequired />
          <TextInput label="说明" value={description} onChange={setDescription} />
          <ErrorAlert error={action.error} />
          <ActionButton isDisabled={!key || !value} isPending={action.busy} onPress={() => void save()}>
            保存（同名覆盖）
          </ActionButton>
        </Card.Content>
      </Card>
      <Loading state={secrets}>
        {(list) =>
          list.length === 0 ? (
            <EmptyHint>还没有 secret</EmptyHint>
          ) : (
            <div className="flex flex-col gap-2">
              {list.map((s) => (
                <Card key={`${s.owner}/${s.key}`}>
                  <Card.Content className="flex flex-row items-center gap-3">
                    <div className="min-w-0 flex-1">
                      <div className="font-mono font-medium">{s.key}</div>
                      <div className="truncate text-xs text-muted">
                        {ownerLabel(agents.data, s.owner)} · {s.description || '无说明'} · {formatTime(s.updated_at)}
                      </div>
                    </div>
                    <Button isIconOnly size="sm" variant="ghost" aria-label={`删除 ${s.key}`} onPress={() => void remove(s)}>
                      <Trash2 className="size-4" />
                    </Button>
                  </Card.Content>
                </Card>
              ))}
            </div>
          )
        }
      </Loading>
    </Page>
  )
}

const STATUS_FILTERS = {
  failed: { label: '失败', status: 'failed' as TaskStatus },
  pending: { label: '等待', status: 'pending' as TaskStatus },
  running: { label: '运行中', status: 'running' as TaskStatus },
  all: { label: '全部', status: undefined },
}
type StatusFilter = keyof typeof STATUS_FILTERS

const STATUS_COLOR: Record<TaskStatus, 'danger' | 'success' | 'warning' | 'accent'> = {
  failed: 'danger',
  done: 'success',
  pending: 'warning',
  running: 'accent',
}

export function Tasks() {
  const [filter, setFilter] = useState<StatusFilter>('failed')
  const tasks = useLoad(() => api.tasks(STATUS_FILTERS[filter].status), [filter])
  const agents = useAgents()
  const action = useAction()
  return (
    <Page title="任务" back="/settings">
      <Segmented
        label="任务状态"
        value={filter}
        onChange={setFilter}
        options={Object.entries(STATUS_FILTERS).map(([id, f]) => ({ id: id as StatusFilter, label: f.label }))}
      />
      <ErrorAlert error={action.error} />
      <Loading state={tasks}>
        {(list) =>
          list.length === 0 ? (
            <EmptyHint>没有任务</EmptyHint>
          ) : (
            <div className="flex flex-col gap-2">
              {list.map((t) => (
                <Card key={t.id}>
                  <Card.Header>
                    <div className="flex flex-wrap items-center gap-1.5">
                      <span className="font-mono text-sm font-medium">#{t.id}</span>
                      <Chip size="sm" variant="secondary">
                        {t.kind}
                      </Chip>
                      {t.agent_id && (
                        <Chip size="sm" color="accent" variant="soft">
                          {agentLabel(agents.data, t.agent_id)}
                        </Chip>
                      )}
                      <Chip size="sm" color={STATUS_COLOR[t.status]} variant="soft">
                        {t.status}
                      </Chip>
                    </div>
                    <Card.Description>
                      {formatTime(t.created_at)} · 尝试 {t.attempts} 次
                      {t.session_id && (
                        <>
                          {' · '}
                          <Link className="text-accent" to={`/sessions/${t.session_id}`}>
                            会话
                          </Link>
                        </>
                      )}
                    </Card.Description>
                  </Card.Header>
                  {t.error && (
                    <Card.Content>
                      <pre className="max-h-48 overflow-auto whitespace-pre-wrap break-all rounded-lg bg-danger-soft p-2 font-mono text-xs text-danger-soft-foreground">
                        {t.error}
                      </pre>
                    </Card.Content>
                  )}
                  {t.status === 'failed' && (
                    <Card.Footer>
                      <Button size="sm" variant="secondary" onPress={() => void action.run(() => api.retryTask(t.id).then(tasks.reload))}>
                        <RotateCcw className="size-4" />
                        重试
                      </Button>
                    </Card.Footer>
                  )}
                </Card>
              ))}
            </div>
          )
        }
      </Loading>
    </Page>
  )
}

const PUSH_TEXT: Record<PushState, string> = {
  unsupported: '此浏览器不支持推送通知。',
  'needs-install': 'iOS 需要先用 Safari「分享 → 添加到主屏幕」，再从主屏幕图标打开本应用，才能开启推送。',
  default: '尚未开启推送。',
  denied: '通知权限已被拒绝，请在系统设置中为本应用开启通知。',
  unsubscribed: '本设备没有订阅推送（退出登录时会取消），重新开启即可。',
  subscribed: '推送已开启。',
}

const LINKS = [
  { to: '/prompt', label: '公共指令', Icon: ScrollText },
  { to: '/skills', label: '公共 Skills', Icon: Sparkles },
  { to: '/mcp', label: 'MCP servers', Icon: Plug },
  { to: '/vault', label: 'Vault', Icon: KeyRound },
  { to: '/tasks', label: '任务与失败记录', Icon: Activity },
] as const

export function Settings() {
  const confirm = useConfirm()
  // null：还在查询本设备的订阅
  const [push, setPush] = useState<PushState | null>(null)
  const action = useAction()
  const refreshPush = () =>
    pushState().then(setPush, (e) => action.setError(`读取推送状态失败：${errorMessage(e)}`))
  useEffect(() => {
    void refreshPush()
    // 只在打开设置页时读一次
    // oxlint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  async function logout() {
    if (!(await confirm({ title: '退出登录？', confirmLabel: '退出' }))) return
    await action.run(async () => {
      // 服务端会话失效才是退出的核心，先做；之后不再需要登录的步骤失败了也不能挡住退出
      await api.logout()
      // 退出后锁屏上不该再出现 agent 的回复：取消本设备的订阅（服务端下次推送收到 410 即删除），重新登录后再开启
      await unsubscribePush().catch((e) => toast.danger(`取消本设备的推送失败：${errorMessage(e)}`))
      // 与登录过期走同一个出口：App 关闭事件流、回到登录页
      unauthorizedEvent.dispatchEvent(new Event('unauthorized'))
    })
  }

  return (
    <Page title="设置">
      <Card>
        <Card.Header>
          <Card.Title className="flex items-center gap-2">
            <BellRing className="size-4" />
            推送通知
          </Card.Title>
          <Card.Description>{push === null ? '正在读取推送状态…' : PUSH_TEXT[push]}</Card.Description>
        </Card.Header>
        {(push === 'default' || push === 'unsubscribed' || push === 'subscribed') && (
          <Card.Footer className="flex gap-2">
            <ActionButton
              size="sm"
              isPending={action.busy}
              onPress={() => void action.run(enablePush).then(refreshPush)}
            >
              {push === 'subscribed' ? '重新订阅本设备' : '开启推送'}
            </ActionButton>
            {push === 'subscribed' && (
              <Button
                size="sm"
                variant="secondary"
                isDisabled={action.busy}
                onPress={() =>
                  void action.run(async () => {
                    await api.testPush()
                    toast.success('测试通知已发出')
                  })
                }
              >
                发送测试通知
              </Button>
            )}
          </Card.Footer>
        )}
      </Card>
      <ErrorAlert error={action.error} />
      <Card className="p-0">
        <div className="flex flex-col divide-y divide-separator">
          {LINKS.map(({ to, label, Icon }) => (
            <Link key={to} to={to} className="flex items-center gap-3 px-4 py-3 transition-colors hover:bg-surface-hover">
              <Icon className="size-4 text-muted" />
              <span className="flex-1">{label}</span>
              <ChevronRight className="size-4 text-muted" />
            </Link>
          ))}
        </div>
      </Card>
      <Button variant="danger-soft" onPress={() => void logout()}>
        <LogOut className="size-4" />
        退出登录
      </Button>
    </Page>
  )
}

export function Login({ onLogin }: { onLogin: () => void }) {
  const [password, setPassword] = useState('')
  const action = useAction()
  const navigate = useNavigate()

  async function submit() {
    if (await action.run(() => api.login(password))) {
      onLogin()
      navigate('/', { replace: true })
    }
  }

  return (
    <div className="flex h-full items-center justify-center p-6">
      <Card className="w-full max-w-sm">
        <Card.Header className="items-center text-center">
          <img src="/icon-192.png" alt="" width={64} height={64} className="rounded-2xl" />
          <Card.Title className="mt-2 text-xl">Muliao</Card.Title>
        </Card.Header>
        <Card.Content>
          <Form
            className="flex flex-col gap-4"
            onSubmit={(e) => {
              e.preventDefault()
              void submit()
            }}
          >
            <TextInput label="密码" type="password" value={password} onChange={setPassword} autoFocus />
            <ErrorAlert error={action.error} />
            <ActionButton type="submit" isDisabled={!password} isPending={action.busy} fullWidth>
              登录
            </ActionButton>
          </Form>
        </Card.Content>
      </Card>
    </div>
  )
}
