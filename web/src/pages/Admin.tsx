import { useEffect, useState } from 'react'
import { Button, Card, Chip, Form, toast } from '@heroui/react'
import { BellRing, LogOut, ShieldCheck, Trash2 } from 'lucide-react'
import { Link, useNavigate } from 'react-router-dom'
import { api, unauthorizedEvent, sessionRoute, type GrantId } from '../api'
import { useReturnState } from '../nav'
import { enablePush, pushState, unsubscribePush, type PushState } from '../push'
import {
  ActionButton,
  ErrorAlert,
  Loading,
  Page,
  TextInput,
  formatTime,
  useAction,
  useAgents,
  useConfirm,
} from '../ui'
import { agentLabel } from '../agentGroups'
import { useLoad, errorMessage } from '../load'

const PUSH_TEXT: Record<PushState, string> = {
  unsupported: '此浏览器不支持推送通知。',
  'needs-install': 'iOS 需要先用 Safari「分享 → 添加到主屏幕」，再从主屏幕图标打开本应用，才能开启推送。',
  default: '尚未开启推送。',
  denied: '通知权限已被拒绝，请在系统设置中为本应用开启通知。',
  unsubscribed: '本设备没有订阅推送（退出登录时会取消），重新开启即可。',
  subscribed: '推送已开启。',
}

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
      <ScriptGrants />
      <ErrorAlert error={action.error} />
      <Button variant="danger-soft" onPress={() => void logout()}>
        <LogOut className="size-4" />
        退出登录
      </Button>
    </Page>
  )
}

/** skill 脚本的「始终允许」：在审批处点「始终允许」记下的，逐条可撤销（撤销后同一调用重新要批准） */
function ScriptGrants() {
  const from = useReturnState()
  const grants = useLoad(() => api.scriptGrants(), [])
  const agents = useAgents()
  const confirm = useConfirm()
  const action = useAction()
  async function revoke(id: GrantId) {
    if (!(await confirm({ title: '撤销这条始终允许？', confirmLabel: '撤销' }))) return
    await action.run(() => api.revokeScriptGrant(id))
  }
  return (
    <Card>
      <Card.Header>
        <Card.Title className="flex items-center gap-2">
          <ShieldCheck className="size-4" />
          始终允许的脚本
        </Card.Title>
        <Card.Description>
          同一个 agent 以同样的参数和 secret 运行同一个脚本、且 skill 里除 SKILL.md 外的文件没变时，不再问你。
        </Card.Description>
      </Card.Header>
      <Card.Content>
        <ErrorAlert error={action.error} />
        <Loading state={grants}>
          {(list) =>
            list.length === 0 ? (
              <p className="text-sm text-muted">没有</p>
            ) : (
              <div className="flex flex-col divide-y divide-border">
                {list.map((g) => (
                  <div key={g.id} className="flex items-start gap-2 py-2">
                    <div className="flex min-w-0 flex-1 flex-col gap-0.5 text-sm">
                      <div className="flex flex-wrap items-center gap-1.5">
                        <Chip size="sm" color="accent" variant="soft">
                          {agentLabel(agents.data, g.agent_id)}
                        </Chip>
                        <span className="break-all font-mono">
                          {g.skill}/{g.script}
                        </span>
                      </div>
                      <span className="break-all font-mono text-xs">
                        {g.args.length === 0 ? '（无参数）' : g.args.map((a) => JSON.stringify(a)).join(' ')}
                        {g.secrets.length > 0 && ` · secrets: ${g.secrets.join(', ')}`}
                      </span>
                      <span className="text-xs text-muted">
                        {formatTime(g.created_at)} 允许（
                        <Link className="text-accent" to={sessionRoute(g.session_id)} state={from}>
                          会话
                        </Link>
                        ）· 已放行 {g.hits} 次
                        {g.last_used_at && `，最近 ${formatTime(g.last_used_at)}`}
                      </span>
                    </div>
                    <Button
                      size="sm"
                      variant="ghost"
                      isIconOnly
                      aria-label="撤销"
                      isDisabled={action.busy}
                      onPress={() => void revoke(g.id)}
                    >
                      <Trash2 className="size-4" />
                    </Button>
                  </div>
                ))}
              </div>
            )
          }
        </Loading>
      </Card.Content>
    </Card>
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
