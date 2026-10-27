import { useState } from 'react'
import { Button, Card, Chip, toast } from '@heroui/react'
import { Plug, Trash2 } from 'lucide-react'
import { api, type AgentId, type CatalogItem, type McpInput, type McpServer } from '../api'
import { ItemAccess, OwnerSelect } from './itemParts'
import { ResourceHistory } from './Memory'
import { CollapsibleForm, ToggleSwitch, ActionButton,
  EmptyHint,
  ErrorAlert,
  Loading,
  Segmented,
  TextAreaInput,
  TextInput,
  useAction,
  useConfirm,
  useLoad,
} from '../ui'


function parsePairs(text: string): Record<string, string> {
  const result: Record<string, string> = {}
  for (const line of text.split('\n')) {
    if (!line.trim()) continue
    const idx = line.indexOf('=')
    if (idx <= 0) throw new Error(`无法解析「${line}」，格式应为 KEY=VALUE`)
    result[line.slice(0, idx).trim()] = line.slice(idx + 1).trim()
  }
  return result
}

function ToolChips({ tools }: { tools: string[] }) {
  return (
    <div className="flex flex-wrap gap-1.5">
      {tools.length === 0 ? (
        <span className="text-sm text-muted">已连接，没有工具</span>
      ) : (
        tools.map((t) => (
          <Chip key={t} size="sm" color="success" variant="soft">
            {t}
          </Chip>
        ))
      )}
    </div>
  )
}

function McpForm({ onCreated }: { onCreated: () => void }) {
  const [owner, setOwner] = useState<AgentId | null>(null)
  const [name, setName] = useState('')
  const [transport, setTransport] = useState<'stdio' | 'http'>('stdio')
  const [command, setCommand] = useState('')
  const [args, setArgs] = useState('')
  const [url, setUrl] = useState('')
  const [pairs, setPairs] = useState('')
  const [tools, setTools] = useState<string[] | null>(null)
  const action = useAction()

  async function create() {
    const ok = await action.run(async () => {
      const kv = parsePairs(pairs)
      const input: McpInput = {
        owner,
        name,
        transport,
        command: transport === 'stdio' ? command : null,
        args:
          transport === 'stdio'
            ? args
                .split('\n')
                .map((a) => a.trim())
                .filter(Boolean)
            : [],
        url: transport === 'http' ? url : null,
        env: transport === 'stdio' ? kv : {},
        headers: transport === 'http' ? kv : {},
        probe: true,
      }
      const created = await api.addMcp(input)
      setTools(created.tools)
      toast.success(`已连接并保存 ${created.name}`)
    })
    if (ok) onCreated()
  }

  return (
    <CollapsibleForm title="添加 MCP server">
            <p className="text-sm text-muted">也可以直接在对话里把 MCP 信息告诉 agent，让它自己调用 mcp_add 创建。</p>
            <TextInput label="名称" hint="工具名会以「名称_」为前缀" value={name} onChange={setName} mono isRequired />
            <OwnerSelect value={owner} onChange={setOwner} />
            <div>
              <Segmented
                label="传输"
                showLabel
                value={transport}
                onChange={setTransport}
                options={[
                  { id: 'stdio', label: 'stdio（本地命令）' },
                  { id: 'http', label: 'HTTP / SSE' },
                ]}
              />
            </div>
            {transport === 'stdio' ? (
              <>
                <TextInput label="命令" value={command} onChange={setCommand} placeholder="npx" mono />
                <TextAreaInput label="参数（每行一个）" value={args} onChange={setArgs} rows={3} mono />
              </>
            ) : (
              <TextInput label="URL" value={url} onChange={setUrl} mono />
            )}
            <TextAreaInput
              label={transport === 'stdio' ? '环境变量（KEY=VALUE，每行一个）' : '请求头（Name=Value，每行一个）'}
              hint="secret 请先存入 Secret 标签，再用 ${vault:KEY} 引用；按正在用这个 server 的 agent 能用的 secret 解析"
              value={pairs}
              onChange={setPairs}
              rows={3}
              mono
            />
            <ErrorAlert error={action.error} />
            {tools && <ToolChips tools={tools} />}
            <ActionButton isDisabled={!name} isPending={action.busy} onPress={() => void create()}>
              连接并保存
            </ActionButton>
          </CollapsibleForm>
  )
}

/** 连接地址与环境变量 / 请求头：管理与只读展示共用。 */
function McpTarget({ server }: { server: McpServer }) {
  const pairs = { ...server.env, ...server.headers }
  return (
    <>
      <code className="block whitespace-pre-wrap break-all rounded-lg bg-surface-secondary p-2 font-mono text-xs">
        {server.command ? [server.command, ...server.args].join(' ') : server.url}
      </code>
      {Object.keys(pairs).length > 0 && (
        <code className="block whitespace-pre-wrap break-all rounded-lg bg-surface-secondary p-2 font-mono text-xs">
          {Object.entries(pairs)
            .map(([k, v]) => `${k}=${v}`)
            .join('\n')}
        </code>
      )}
    </>
  )
}

function McpItem({ item, server, onChange }: { item: CatalogItem; server: McpServer; onChange: () => void }) {
  const confirm = useConfirm()
  const [tools, setTools] = useState<string[] | null>(null)
  const action = useAction()

  async function remove() {
    const ok = await confirm({ title: `删除 MCP server「${server.name}」？`, confirmLabel: '删除', danger: true })
    if (ok) await action.run(() => api.deleteMcp(server.name).then(onChange))
  }

  return (
    <Card>
      <Card.Header className="flex flex-row items-start gap-3">
        <div className="min-w-0 flex-1">
          <Card.Title className="font-mono text-base">{server.name}</Card.Title>
          <div className="mt-1 flex flex-wrap gap-1.5">
            <Chip size="sm" variant="secondary">
              {server.transport}
            </Chip>
          </div>
          <div className="mt-1.5">
            <ItemAccess item={item} onTransferred={onChange} />
          </div>
        </div>
        <ToggleSwitch
          label={`启用 MCP server「${server.name}」`}
          isSelected={server.enabled}
          isDisabled={action.busy}
          onChange={(enabled) => void action.run(() => api.setMcpEnabled(server.name, enabled).then(onChange))}
        />
      </Card.Header>
      <Card.Content className="flex flex-col gap-2">
        <McpTarget server={server} />
        {tools && <ToolChips tools={tools} />}
        <ErrorAlert error={action.error} />
        <ResourceHistory kind="mcp" target={{ name: server.name }} current={JSON.stringify(server)} onRestored={onChange} />
      </Card.Content>
      <Card.Footer className="flex gap-2">
        <ActionButton
          size="sm"
          variant="secondary"
          isPending={action.busy}
          onPress={() => void action.run(async () => setTools(await api.probeMcp(server.name)))}
        >
          <Plug className="size-4" />
          测试连接
        </ActionButton>
        <Button size="sm" variant="danger-soft" isDisabled={action.busy} onPress={() => void remove()}>
          <Trash2 className="size-4" />
          删除
        </Button>
      </Card.Footer>
    </Card>
  )
}

/** MCP server 的条目页：items 是条目池里（已筛选的）MCP 条目，配置另从 /mcp 取 */
export function McpTab({ items, onChange }: { items: CatalogItem[]; onChange: () => void }) {
  const servers = useLoad(() => api.mcp(), [])
  // 增删、启停之后条目池与配置都重读
  const changed = () => {
    onChange()
    void servers.reload()
  }
  return (
    <div className="flex flex-col gap-3">
      <McpForm onCreated={changed} />
      <Loading state={servers}>
        {(all) => {
          const byName = new Map(all.map((s) => [s.name, s]))
          // 条目池与配置分两次读：只显示两边都有的，两次读之间增删的 server 下次重读时补齐
          const shown = items.flatMap((item) => {
            const server = byName.get(item.name)
            return server ? [{ item, server }] : []
          })
          return shown.length === 0 ? (
            <EmptyHint>没有 MCP server</EmptyHint>
          ) : (
            <div className="flex flex-col gap-3">
              {shown.map(({ item, server }) => (
                <McpItem key={`${server.name}-${server.enabled}`} item={item} server={server} onChange={changed} />
              ))}
            </div>
          )
        }}
      </Loading>
    </div>
  )
}
