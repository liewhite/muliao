import { useEffect, useState } from 'react'
import { Button, Card, Chip, Form, toast } from '@heroui/react'
import { Activity, BarChart3, BellRing, ChevronRight, KeyRound, LogOut, Plug, RotateCcw, ScrollText, Sparkles } from 'lucide-react'
import { Link, useNavigate } from 'react-router-dom'
import { api, unauthorizedEvent, type TaskStatus, sessionRoute } from '../api'
import { enablePush, pushState, unsubscribePush, type PushState } from '../push'
import { ActionButton,
  EmptyHint,
  ErrorAlert,
  Loading,
  Page,
  Segmented,
  TextInput,
  agentLabel,
  formatTime,
  useAction,
  useAgents,
  useConfirm,
  useLoad,
  errorMessage,
} from '../ui'

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
                          <Link className="text-accent" to={sessionRoute(t.session_id)}>
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
  { to: '/mcp', label: '公共 MCP servers', Icon: Plug },
  { to: '/vault', label: '公共 Vault', Icon: KeyRound },
  { to: '/tasks', label: '任务与失败记录', Icon: Activity },
  { to: '/usage', label: 'Token 用量', Icon: BarChart3 },
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
