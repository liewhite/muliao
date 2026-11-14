import { useState } from 'react'
import { Button, Card, Chip, Tabs, toast, type Key } from '@heroui/react'
import { Archive, ArchiveRestore, Copy, MessageSquarePlus, Plus, Star, Trash2 } from 'lucide-react'
import { Link, NavLink, useNavigate } from 'react-router-dom'
import { useRouteParam } from '../nav'
import {
  agentRoute,
  api,
  asAgentId,
  newSessionRoute,
  workspaceFileUrl,
  type Agent,
  type AgentId,
  type AgentInput,
} from '../api'
import {
  ActionButton,
  copyText,
  EmptyHint,
  ErrorAlert,
  LinkButton,
  Loading,
  Page,
  SelectInput,
  TextInput,
  formatTime,
  GroupSection,
  useAction,
  useAgents,
  useConfirm,
} from '../ui'
import { never, useLoad } from '../load'
import { PermissionsInput } from '../assets/Permissions'
import { AgentMemories } from '../assets/Memory'
import { AgentAssetsView } from './AgentAssets'
import { HistoryList } from './History'
import { FileBrowser } from '../FileBrowser'
import { SitePageList } from '../sites/SitePages'
import { useDraft } from '../drafts'
import { groupAgents } from './agentGroups'

/** 目录没加载（agent.toml 坏了、目录名对不上等）：不能运行 */
const CONFIG_NOT_LOADED = '配置未加载，不能运行（原因见资源问题）'

function AgentCard({ a }: { a: Agent }) {
  const { config } = a
  return (
    <NavLink to={agentRoute(a.id)}>
      {({ isActive }) => (
        <Card className={`transition-colors hover:bg-surface-hover ${isActive ? 'ring-2 ring-accent' : ''}`}>
          <Card.Header>
            <Card.Title>{a.name}</Card.Title>
            <Card.Description className="truncate" title={config?.description}>
              {config?.description ?? CONFIG_NOT_LOADED}
            </Card.Description>
          </Card.Header>
          <Card.Footer className="flex flex-wrap gap-2">
            {a.is_default && (
              <Chip size="sm" color="accent" variant="soft">
                默认
              </Chip>
            )}
            {a.archived_at !== null && (
              <Chip size="sm" color="warning" variant="soft">
                已归档
              </Chip>
            )}
            {config === null ? (
              <Chip size="sm" color="danger" variant="soft">
                配置未加载
              </Chip>
            ) : (
              <>
                <Chip size="sm" variant="secondary">
                  {config.model.provider} · {config.model.model}
                </Chip>
                {config.thinking !== 'default' && (
                  <Chip size="sm" variant="secondary">
                    思考 {THINKING_LABEL[config.thinking]}
                  </Chip>
                )}
              </>
            )}
          </Card.Footer>
        </Card>
      )}
    </NavLink>
  )
}

function AgentCards({ agents }: { agents: Agent[] }) {
  return (
    <div className="flex flex-col gap-2">
      {agents.map((a) => (
        <AgentCard key={a.id} a={a} />
      ))}
    </div>
  )
}

/** 不分组的平铺在前，每个分组一段（默认展开），归档的放最后一段（默认折叠） */
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
        {(list) => {
          if (list.length === 0) return <EmptyHint>还没有 agent</EmptyHint>
          const { ungrouped, groups, archived } = groupAgents(list)
          return (
            <div className="flex flex-col gap-4">
              {ungrouped.length > 0 && <AgentCards agents={ungrouped} />}
              {groups.map((g) => (
                <GroupSection key={g.name} name={g.name} count={g.agents.length}>
                  <AgentCards agents={g.agents} />
                </GroupSection>
              ))}
              {archived.length > 0 && (
                <GroupSection name="已归档" count={archived.length} defaultExpanded={false}>
                  <AgentCards agents={archived} />
                </GroupSection>
              )}
            </div>
          )
        }}
      </Loading>
    </Page>
  )
}

/** 已加载配置的 agent 的当前值（配置没加载的不显示表单，见调用处） */
function currentInput(agent: Agent, config: NonNullable<Agent['config']>): AgentInput {
  const { name, roles, allow, deny } = agent
  return { ...config, name, roles, allow, deny }
}

type Thinking = AgentInput['thinking']

/** 思考档位：「默认」什么都不传（模型与服务端的默认）；其余交给 agenter 换算成各 provider 的参数 */
const THINKING_LABEL: Record<Thinking, string> = {
  default: '默认',
  off: '关闭',
  low: 'low',
  medium: 'medium',
  high: 'high',
  xhigh: 'xhigh',
}
// THINKING_LABEL 的类型要求每种档位都有显示名，所以它的键恰好是全部档位（唯一一份）
const THINKINGS = Object.keys(THINKING_LABEL) as Thinking[]

type Model = AgentInput['model']

/** agent 的模型：provider 来自服务端配置，模型名向该 provider 实时查询；旁边是它的思考档位 */
function ModelInput({
  value,
  onChange,
  thinking,
  onThinkingChange,
}: {
  value: Model
  onChange: (v: Model) => void
  thinking: Thinking
  onThinkingChange: (v: Thinking) => void
}) {
  const provider = value.provider || null
  const model = value.model
  const providers = useLoad(() => api.providers(), [], never)
  const models = useLoad(() => (provider ? api.providerModels(provider) : Promise.resolve([])), [provider], never)
  const listed = models.data ?? []
  return (
    <div className="flex flex-col gap-2">
      <div className="grid grid-cols-1 gap-2 sm:grid-cols-3">
        <SelectInput
          label="Provider"
          hint="来自服务端配置的 [llm.providers]"
          value={provider}
          // 换 provider 后模型需重新选择；未选就保存会被服务端拒绝
          onChange={(p) => onChange({ provider: p, model: '' })}
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
          onChange={(m) => onChange({ provider: value.provider, model: m })}
          options={listed.map((m) => ({ id: m, label: m }))}
          placeholder={models.loading ? '查询中…' : '请选择'}
          isDisabled={!provider || models.loading}
        />
        <SelectInput
          label="思考"
          hint="默认＝不传，按模型自己的默认"
          value={thinking}
          onChange={onThinkingChange}
          options={THINKINGS.map((id) => ({ id, label: THINKING_LABEL[id] }))}
        />
      </div>
      <ErrorAlert error={providers.error} title="读取 provider 失败" />
      <ErrorAlert error={models.error} title="查询模型失败" />
    </div>
  )
}

/** agent 的各项配置（新建与编辑共用），值由调用方管 */
function AgentFields({
  agentId,
  input,
  set,
}: {
  agentId: AgentId | null
  input: AgentInput
  set: (patch: Partial<AgentInput>) => void
}) {
  return (
    <>
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
      <TextInput
        label="分组"
        hint="可不填；同一类的 agent（如一批接单的）归到一组，列表里按组折叠"
        value={input.group ?? ''}
        onChange={(group) => set({ group })}
      />
      <ModelInput
        value={input.model}
        onChange={(model) => set({ model })}
        thinking={input.thinking}
        onThinkingChange={(thinking) => set({ thinking })}
      />
      <PermissionsInput
        agentId={agentId}
        value={{ roles: input.roles, allow: input.allow, deny: input.deny }}
        onChange={(p) => set(p)}
      />
    </>
  )
}

function AgentCreateForm({ defaults, onSaved }: { defaults: AgentInput; onSaved: (a: Agent) => void }) {
  const [input, setInput] = useState(defaults)
  const action = useAction()
  return (
    <div className="flex flex-col gap-4">
      <AgentFields agentId={null} input={input} set={(patch) => setInput((prev) => ({ ...prev, ...patch }))} />
      <ErrorAlert error={action.error} />
      <ActionButton
        isPending={action.busy}
        onPress={() =>
          void action.run(async () => {
            const created = await api.createAgent(input)
            toast.success('已创建')
            onSaved(created)
          })
        }
      >
        保存
      </ActionButton>
    </div>
  )
}

/** 编辑：只提交改过的字段，编辑期间 agent 自己改了别的字段（改它的 agent.toml）不会被旧值盖掉；没改过时跟着刷新
 * （drafts.useDraft） */
function AgentEditForm({ agent, current }: { agent: AgentId; current: AgentInput }) {
  const form = useDraft(current)
  const action = useAction()
  return (
    <div className="flex flex-col gap-4">
      <AgentFields agentId={agent} input={form.draft} set={form.set} />
      <ErrorAlert error={action.error} />
      <ActionButton
        isDisabled={Object.keys(form.changes).length === 0}
        isPending={action.busy}
        onPress={() =>
          void action.run(async () => {
            const saved = await api.updateAgent(agent, form.changes)
            if (saved.config !== null) form.saved(currentInput(saved, saved.config))
            toast.success('已保存')
          })
        }
      >
        保存
      </ActionButton>
    </div>
  )
}

export function AgentCreate() {
  const navigate = useNavigate()
  const defaults = useLoad(() => api.agentDefaults(), [], never)
  return (
    <Page title="新建 agent" back="/agents">
      <Card>
        <Card.Content>
          <Loading state={defaults}>
            {(d) => <AgentCreateForm defaults={d} onSaved={(a) => navigate(agentRoute(a.id), { replace: true })} />}
          </Loading>
        </Card.Content>
      </Card>
    </Page>
  )
}

const DETAIL_TABS = [
  ['config', '配置'],
  ['assets', '资源'],
  ['memory', '记忆'],
  ['history', '历史'],
  ['files', '文件'],
  ['pages', '页面'],
] as const

export function AgentDetail() {
  const id = asAgentId(useRouteParam('id'))
  const navigate = useNavigate()
  const confirm = useConfirm()
  const agent = useLoad(() => api.agent(id), [id])
  const [tab, setTab] = useState<Key>('config')
  const action = useAction()
  const name = agent.data?.name ?? id

  async function remove() {
    const ok = await confirm({
      title: `删除 agent「${name}」？`,
      body: '它的会话、工作目录和它名下的 AI 资产（含私有记忆）都会被删除（被分给别的 agent 的转给平台），无法恢复。',
      confirmLabel: '删除',
      danger: true,
    })
    if (ok) await action.run(() => api.deleteAgent(id), { leave: () => navigate('/agents') })
  }

  async function toggleArchived(on: boolean) {
    if (on) {
      const ok = await confirm({
        title: `归档 agent「${name}」？`,
        body: '归档后不能对话、委派、指派 issue，它的触发器不触发；会话、记忆等数据都保留，取消归档即恢复。',
        confirmLabel: '归档',
      })
      if (!ok) return
    }
    if (await action.run(() => api.setAgentArchived(id, on))) {
      toast.success(on ? '已归档' : '已取消归档')
    }
  }

  async function toggleDefault(on: boolean) {
    if (await action.run(() => api.setDefaultAgent(id, on))) {
      toast.success(on ? '已设为默认 agent' : '已取消默认')
    }
  }

  return (
    <Page
      title={name}
      back="/agents"
      actions={
        agent.data?.archived_at == null && (
          <LinkButton to={newSessionRoute(id)} variant="secondary">
            <MessageSquarePlus className="size-4" />
            对话
          </LinkButton>
        )
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
                    创建于 {formatTime(a.created_at)} ·{' '}
                    <Link to={`/ai-assets?owner=${a.id}`} className="text-accent">
                      它名下的 AI 资产
                    </Link>
                  </div>
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="text-muted">
                      {a.is_default ? '系统默认 agent：新会话默认选中它' : '不是默认 agent'}
                    </span>
                    <Button size="sm" variant="ghost" isDisabled={action.busy} onPress={() => void toggleDefault(!a.is_default)}>
                      <Star className="size-4" />
                      {a.is_default ? '取消默认' : '设为默认'}
                    </Button>
                  </div>
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="text-muted">
                      {a.archived_at === null
                        ? '未归档'
                        : `已归档（${formatTime(a.archived_at)}）：不能对话、委派、指派，触发器不触发`}
                    </span>
                    <Button
                      size="sm"
                      variant="ghost"
                      isDisabled={action.busy}
                      onPress={() => void toggleArchived(a.archived_at === null)}
                    >
                      {a.archived_at === null ? <Archive className="size-4" /> : <ArchiveRestore className="size-4" />}
                      {a.archived_at === null ? '归档' : '取消归档'}
                    </Button>
                  </div>
                </Card.Content>
              </Card>
              <Card>
                <Card.Content className="flex flex-col gap-3">
                  {a.config === null ? (
                    <p className="text-sm text-danger">{CONFIG_NOT_LOADED}；修好 {a.name}/agent.toml 后才能在这里改</p>
                  ) : (
                    <AgentEditForm agent={a.id} current={currentInput(a, a.config)} />
                  )}
                </Card.Content>
              </Card>
              <ErrorAlert error={action.error} />
              <Button variant="danger-soft" onPress={() => void remove()}>
                <Trash2 className="size-4" />
                删除 agent
              </Button>
            </Tabs.Panel>
            <Tabs.Panel id="assets" className="pt-3">
              <AgentAssetsView agent={a.id} />
            </Tabs.Panel>
            <Tabs.Panel id="memory" className="pt-3">
              <AgentMemories agent={a.id} />
            </Tabs.Panel>
            <Tabs.Panel id="history" className="pt-3">
              <HistoryList agent={a.id} />
            </Tabs.Panel>
            <Tabs.Panel id="files" className="pt-3">
              <FileBrowser
                key={a.id}
                source={{
                  id: a.id,
                  list: (path) => api.workspace(a.id, path),
                  fileUrl: (path) => workspaceFileUrl(a.id, path),
                }}
                label="工作目录"
                emptyText="工作目录是空的"
              />
            </Tabs.Panel>
            <Tabs.Panel id="pages" className="pt-3">
              <SitePageList agent={a.id} />
            </Tabs.Panel>
          </Tabs>
        )}
      </Loading>
    </Page>
  )
}
