import { useState } from 'react'
import { Button, Card, Chip, Tabs, toast, type Key } from '@heroui/react'
import { Copy, MessageSquarePlus, Plus, Star, Trash2 } from 'lucide-react'
import { NavLink, useNavigate, useParams } from 'react-router-dom'
import {
  agentRoute,
  api,
  asAgentId,
  newSessionRoute,
  type Agent,
  type AgentDefaults,
  type AgentInput,
  type ToolGroup,
} from '../api'
import { ActionButton,
  copyText,
  EmptyHint,
  ErrorAlert,
  LinkButton,
  Loading,
  Page,
  SelectInput,
  TextInput,
  ToggleSwitch,
  formatTime,
  useAction,
  useAgents,
  useConfirm,
  useLoad,
} from '../ui'
import { McpManager } from './Mcp'
import { MemoryEditor } from './Memory'
import { SkillList } from './Skills'
import { SecretManager } from './Vault'
import { refreshUnreadLogged } from '../unread'
import { announceChange } from '../events'

export function AgentList() {
  const agents = useAgents()
  return (
    <Page
      title="Agents"
      actions={
        <LinkButton to="/agents/new">
          <Plus className="size-4" />
          新建
        </LinkButton>
      }
    >
      <Loading state={agents}>
        {(list) =>
          list.length === 0 ? (
            <EmptyHint>还没有 agent</EmptyHint>
          ) : (
            <div className="flex flex-col gap-2">
              {list.map((a) => (
                <NavLink key={a.id} to={agentRoute(a.id)}>
                  {({ isActive }) => (
                    <Card className={`transition-colors hover:bg-surface-hover ${isActive ? 'ring-2 ring-accent' : ''}`}>
                      <Card.Header>
                        <Card.Title>{a.name}</Card.Title>
                        <Card.Description>{a.description}</Card.Description>
                      </Card.Header>
                      <Card.Footer className="flex flex-wrap gap-2">
                        {a.is_default && (
                          <Chip size="sm" color="accent" variant="soft">
                            默认
                          </Chip>
                        )}
                        <Chip size="sm" variant="secondary">
                          {a.model}
                        </Chip>
                      </Card.Footer>
                    </Card>
                  )}
                </NavLink>
              ))}
            </div>
          )
        }
      </Loading>
    </Page>
  )
}

function initialInput(agent: Agent | undefined, defaults: AgentDefaults): AgentInput {
  if (agent) {
    const { name, description, model, tool_groups } = agent
    return { name, description, model, tool_groups }
  }
  return {
    name: '',
    description: '',
    model: defaults.model,
    tool_groups: defaults.default_tool_groups,
  }
}

function ToolGroupsInput({ all, value, onChange }: { all: ToolGroup[]; value: string[]; onChange: (v: string[]) => void }) {
  const names = all.map((g) => g.name)
  return (
    <div className="flex flex-col gap-2">
      <div className="text-sm font-medium">可用工具</div>
      <div className="text-xs text-muted">记忆（含全局记忆）和私有 skill 总是开放。处理不可信内容（外部事件、网页）的 agent 只开放它需要的</div>
      <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
        {all.map((group) => (
          <label key={group.name} className="flex items-center gap-2 text-sm">
            <ToggleSwitch
              label={group.label}
              isSelected={value.includes(group.name)}
              onChange={(on) =>
                onChange(on ? names.filter((g) => g === group.name || value.includes(g)) : value.filter((g) => g !== group.name))
              }
            />
            {group.label}
          </label>
        ))}
      </div>
    </div>
  )
}

/** agent 的模型 "<provider 名>:<模型名>"：provider 来自服务端配置，模型名向该 provider 实时查询 */
function ModelInput({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  const sep = value.indexOf(':')
  const provider = sep > 0 ? value.slice(0, sep) : null
  const model = sep > 0 ? value.slice(sep + 1) : ''
  const providers = useLoad(() => api.providers(), [])
  const models = useLoad(() => (provider ? api.providerModels(provider) : Promise.resolve([])), [provider])
  const listed = models.data ?? []
  return (
    <div className="flex flex-col gap-2">
      <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
        <SelectInput
          label="Provider"
          hint="来自服务端配置的 [llm.providers]"
          value={provider}
          // 换 provider 后模型需重新选择；未选就保存会被服务端拒绝
          onChange={(p) => onChange(`${p}:`)}
          options={(providers.data ?? []).map((p) => ({ id: p, label: p }))}
          isDisabled={providers.loading}
        />
        <SelectInput
          label="模型"
          hint={
            model && !models.loading && !listed.includes(model)
              ? `当前为 ${model}，不在 provider 返回的列表中`
              : '向 provider 实时查询'
          }
          value={model || null}
          onChange={(m) => onChange(`${provider}:${m}`)}
          options={listed.map((m) => ({ id: m, label: m }))}
          placeholder={models.loading ? '查询中…' : '请选择'}
          isDisabled={!provider || models.loading}
        />
      </div>
      <ErrorAlert error={providers.error} title="读取 provider 失败" />
      <ErrorAlert error={models.error} title="查询模型失败" />
    </div>
  )
}

function AgentForm({ agent, defaults, onSaved }: { agent?: Agent; defaults: AgentDefaults; onSaved: (a: Agent) => void }) {
  const [input, setInput] = useState<AgentInput>(() => initialInput(agent, defaults))
  const action = useAction()
  const set = (patch: Partial<AgentInput>) => setInput((prev) => ({ ...prev, ...patch }))

  async function save() {
    await action.run(async () => {
      const saved = agent ? await api.updateAgent(agent.id, input) : await api.createAgent(input)
      toast.success(agent ? '已保存' : '已创建')
      // 分屏时旁边的 agent 列表（以及各处的 agent 名称）跟着更新
      announceChange('agents')
      onSaved(saved)
    })
  }

  return (
    <div className="flex flex-col gap-4">
      <TextInput
        label="名称"
        hint="只用于显示，可随时修改，不能与其他 agent 重名"
        value={input.name}
        onChange={(name) => set({ name })}
        isRequired
      />
      <TextInput
        label="职责描述"
        hint="它的角色：agent 自己每次运行都会看到，其他 agent 委派任务时据此选择"
        value={input.description}
        onChange={(description) => set({ description })}
        isRequired
      />
      <ModelInput value={input.model ?? ''} onChange={(model) => set({ model })} />
      <ToolGroupsInput all={defaults.groups} value={input.tool_groups} onChange={(tool_groups) => set({ tool_groups })} />
      <ErrorAlert error={action.error} />
      <ActionButton isPending={action.busy} onPress={() => void save()}>
        保存
      </ActionButton>
    </div>
  )
}

export function AgentCreate() {
  const navigate = useNavigate()
  const defaults = useLoad(() => api.agentDefaults(), [])
  return (
    <Page title="新建 agent" back="/agents">
      <Card>
        <Card.Content>
          <Loading state={defaults}>
            {(d) => <AgentForm defaults={d} onSaved={(a) => navigate(agentRoute(a.id), { replace: true })} />}
          </Loading>
        </Card.Content>
      </Card>
    </Page>
  )
}

const DETAIL_TABS = [
  ['config', '配置'],
  ['memory', '记忆'],
  ['skills', 'Skills'],
  ['mcp', 'MCP'],
  ['secrets', 'Secrets'],
] as const

export function AgentDetail() {
  const id = asAgentId(useParams().id ?? '')
  const navigate = useNavigate()
  const confirm = useConfirm()
  const agent = useLoad(() => api.agent(id), [id])
  // 可选的工具组列表只来自服务端
  const defaults = useLoad(() => api.agentDefaults(), [])
  const [tab, setTab] = useState<Key>('config')
  const action = useAction()
  const name = agent.data?.name ?? id

  async function remove() {
    const ok = await confirm({
      title: `删除 agent「${name}」？`,
      body: '它的会话、记忆、私有 skill 和工作目录都会被删除，无法恢复。',
      confirmLabel: '删除',
      danger: true,
    })
    if (ok && (await action.run(() => api.deleteAgent(id)))) {
      // 它的会话连同指向它们的通知一起删除
      refreshUnreadLogged()
      announceChange('agents')
      navigate('/agents')
    }
  }

  async function toggleDefault(on: boolean) {
    if (await action.run(() => api.setDefaultAgent(id, on))) {
      toast.success(on ? '已设为默认 agent' : '已取消默认')
      // 列表里的「默认」标记与新对话的默认选择跟着更新
      announceChange('agents')
      void agent.reload()
    }
  }

  return (
    <Page
      title={name}
      back="/agents"
      actions={
        <LinkButton to={newSessionRoute(id)} variant="secondary">
          <MessageSquarePlus className="size-4" />
          对话
        </LinkButton>
      }
    >
      <Loading state={agent}>
        {(a) => (
          <Tabs selectedKey={tab} onSelectionChange={setTab} variant="secondary">
            <Tabs.ListContainer>
              <Tabs.List aria-label="agent 详情">
                {DETAIL_TABS.map(([key, label]) => (
                  <Tabs.Tab key={key} id={key}>
                    {label}
                    <Tabs.Indicator />
                  </Tabs.Tab>
                ))}
              </Tabs.List>
            </Tabs.ListContainer>
            <Tabs.Panel id="config" className="flex flex-col gap-3 pt-3">
              <Card>
                <Card.Content className="flex flex-col gap-2 text-sm">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="text-muted">id</span>
                    <code className="select-all rounded bg-surface-secondary px-1.5 py-0.5 font-mono">{a.id}</code>
                    <Button size="sm" variant="ghost" onPress={() => void action.run(() => copyText(a.id, 'id'))}>
                      <Copy className="size-4" />
                      复制
                    </Button>
                  </div>
                  <div className="text-muted">
                    创建于 {formatTime(a.created_at)}
                  </div>
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="text-muted">
                      {a.is_default ? '系统默认 agent：新对话默认选中它' : '不是默认 agent'}
                    </span>
                    <Button size="sm" variant="ghost" isDisabled={action.busy} onPress={() => void toggleDefault(!a.is_default)}>
                      <Star className="size-4" />
                      {a.is_default ? '取消默认' : '设为默认'}
                    </Button>
                  </div>
                </Card.Content>
              </Card>
              <Card>
                <Card.Content className="flex flex-col gap-3">
                  <Loading state={defaults}>
                    {(d) => (
                      <AgentForm
                        key={`${a.id}-${a.name}-${a.description}`}
                        agent={a}
                        defaults={d}
                        onSaved={() => void agent.reload()}
                      />
                    )}
                  </Loading>
                </Card.Content>
              </Card>
              <ErrorAlert error={action.error} />
              <Button variant="danger-soft" onPress={() => void remove()}>
                <Trash2 className="size-4" />
                删除 agent
              </Button>
            </Tabs.Panel>
            <Tabs.Panel id="memory" className="pt-3">
              <MemoryEditor owner={a.id} hint="agent 每次运行都会看到全部记忆，也会自行修改" />
            </Tabs.Panel>
            <Tabs.Panel id="skills" className="pt-3">
              <SkillList owner={a.id} />
            </Tabs.Panel>
            <Tabs.Panel id="mcp" className="flex flex-col gap-3 pt-3">
              <p className="text-sm text-muted">私有 MCP server 只有这个 agent 能用，可以引用它的私有 secret；同名时遮蔽公共的。</p>
              <McpManager owner={a.id} />
            </Tabs.Panel>
            <Tabs.Panel id="secrets" className="flex flex-col gap-3 pt-3">
              <p className="text-sm text-muted">私有 secret 只有这个 agent 能用；同名时遮蔽公共的。</p>
              <SecretManager owner={a.id} />
            </Tabs.Panel>
          </Tabs>
        )}
      </Loading>
    </Page>
  )
}
