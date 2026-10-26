import { useEffect, useRef, useState } from 'react'
import { Button, Card, toast } from '@heroui/react'
import { Download, Plus, Trash2, Upload } from 'lucide-react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { api, ApiError, asAgentId, type AgentId, type CatalogItem, type Schemas, type Skill } from '../api'
import { ItemAccess, OwnerSelect } from './itemParts'
import { ResourceHistory } from './Memory'
import { ActionButton,
  CollapsibleForm,
  EmptyHint,
  ErrorAlert,
  LinkButton,
  Loading,
  Page,
  TextAreaInput,
  TextInput,
  useAction,
  agentLabel,
  useAgents,
  useConfirm,
  useLoad,
} from '../ui'

const SKILLS = '/items?kind=skill'

function editorPath(name?: string): string {
  return name ? `/items/skill?${new URLSearchParams({ name })}` : '/items/skill'
}

/** skill 的条目页：items 是条目池里（已筛选的）skill，点进编辑 */
export function SkillTab({ items, onChange }: { items: CatalogItem[]; onChange: () => void }) {
  const [owner, setOwner] = useState<AgentId | null>(null)
  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap gap-2">
        <LinkButton to={editorPath()} variant="secondary">
          <Plus className="size-4" />
          新建 skill
        </LinkButton>
      </div>
      <CollapsibleForm title="导入 .skill">
        <OwnerSelect value={owner} onChange={setOwner} />
        <ImportSkill owner={owner} onImported={onChange} />
      </CollapsibleForm>
      {items.length === 0 ? (
        <EmptyHint>没有 skill</EmptyHint>
      ) : (
        <div className="flex flex-col gap-2">
          {items.map((s) => (
            <Card key={s.name}>
              <Card.Header>
                <Link to={editorPath(s.name)} className="hover:underline">
                  <Card.Title className="font-mono text-base">{s.name}</Card.Title>
                </Link>
                <Card.Description>{s.description}</Card.Description>
              </Card.Header>
              <Card.Content>
                <ItemAccess item={s} />
              </Card.Content>
            </Card>
          ))}
        </div>
      )}
    </div>
  )
}

/** 导入标准 .skill 文件为 owner 的 skill；同名已存在时问是否覆盖，没有导入的文件列在下面。 */
function ImportSkill({ owner, onImported }: { owner: AgentId | null; onImported: () => void }) {
  const input = useRef<HTMLInputElement>(null)
  const confirm = useConfirm()
  const action = useAction()
  const [result, setResult] = useState<Schemas['SkillImportOut'] | null>(null)

  async function importFile(file: File) {
    setResult(null)
    await action.run(async () => {
      let imported: Schemas['SkillImportOut']
      try {
        imported = await api.importSkill(owner, file, false)
      } catch (e) {
        if (!(e instanceof ApiError && e.status === 409)) throw e
        const ok = await confirm({
          title: '同名 skill 已存在，覆盖它？',
          body: '覆盖后它的 SKILL.md 与附件换成包里的版本，旧版本留在历史里。',
          confirmLabel: '覆盖',
          danger: true,
        })
        if (!ok) return
        imported = await api.importSkill(owner, file, true)
      }
      setResult(imported)
      toast.success(`已导入 skill「${imported.skill.name}」`)
      onImported()
    })
  }

  return (
    <>
      <input
        ref={input}
        type="file"
        accept=".skill,.zip"
        hidden
        onChange={(e) => {
          const file = e.target.files?.[0]
          // 允许再次选择同一个文件
          e.target.value = ''
          if (file) void importFile(file)
        }}
      />
      <ActionButton variant="secondary" size="sm" isPending={action.busy} onPress={() => input.current?.click()}>
        <Upload className="size-4" />
        导入 .skill
      </ActionButton>
      {action.error && (
        <div className="w-full">
          <ErrorAlert error={action.error} />
        </div>
      )}
      {result && result.skipped.length > 0 && (
        <div className="w-full text-sm text-muted">
          <div>以下文件没有导入：</div>
          <ul className="list-disc pl-5">
            {result.skipped.map((s) => (
              <li key={s.path}>
                <span className="font-mono">{s.path}</span>：{s.reason}
              </li>
            ))}
          </ul>
        </div>
      )}
    </>
  )
}

export function SkillEditor() {
  const [params] = useSearchParams()
  const navigate = useNavigate()
  const confirm = useConfirm()
  const existing = params.get('name')
  const back = SKILLS
  const agents = useAgents()
  const [skill, setSkill] = useState<Skill>({ owner: null, name: existing ?? '', description: '', body: '' })
  const loaded = useLoad(() => (existing ? api.skill(existing) : Promise.resolve(null)), [existing])
  const action = useAction()
  useEffect(() => {
    if (loaded.data) setSkill(loaded.data)
  }, [loaded.data])

  const set = (patch: Partial<Skill>) => setSkill((prev) => ({ ...prev, ...patch }))

  async function save() {
    const ok = await action.run(async () => {
      await api.saveSkill(skill)
      toast.success('skill 已保存')
    })
    if (ok) navigate(back)
  }

  async function exportPackage() {
    if (!existing) return
    await action.run(async () => {
      const url = URL.createObjectURL(await api.exportSkill(existing))
      const link = document.createElement('a')
      link.href = url
      link.download = `${existing}.skill`
      link.click()
      // 点击触发的下载异步开始，太早释放会取消它
      setTimeout(() => URL.revokeObjectURL(url), 10_000)
    })
  }

  async function remove() {
    if (!existing) return
    const ok = await confirm({ title: `删除 skill「${existing}」？`, confirmLabel: '删除', danger: true })
    if (ok && (await action.run(() => api.deleteSkill(existing)))) navigate(back)
  }

  return (
    <Page title={existing ?? '新建 skill'} back={back}>
      <Loading state={loaded}>
        {() => (
          <Card>
            <Card.Content className="flex flex-col gap-4">
              {existing ? (
                <div className="text-sm text-muted">
                  归属：{skill.owner === null ? '平台' : agentLabel(agents.data, asAgentId(skill.owner))}
                </div>
              ) : (
                <OwnerSelect
                  value={skill.owner === null ? null : asAgentId(skill.owner)}
                  onChange={(owner) => set({ owner })}
                />
              )}
              {!existing && (
                <TextInput
                  label="名称"
                  hint="小写字母、数字、- 或 _"
                  value={skill.name}
                  onChange={(name) => set({ name })}
                  mono
                  isRequired
                />
              )}
              <TextInput
                label="描述"
                hint="何时使用这个 skill，会出现在 agent 的 skill 索引里"
                value={skill.description}
                onChange={(description) => set({ description })}
                isRequired
              />
              <TextAreaInput label="内容（Markdown）" value={skill.body} onChange={(body) => set({ body })} rows={18} mono />
              <ErrorAlert error={action.error} />
              <div className="flex gap-2">
                <ActionButton isPending={action.busy} onPress={() => void save()}>
                  保存
                </ActionButton>
                {existing && (
                  <Button variant="secondary" isDisabled={action.busy} onPress={() => void exportPackage()}>
                    <Download className="size-4" />
                    导出 .skill
                  </Button>
                )}
                {existing && (
                  <Button variant="danger-soft" isDisabled={action.busy} onPress={() => void remove()}>
                    <Trash2 className="size-4" />
                    删除
                  </Button>
                )}
              </div>
              {existing && loaded.data && (
                <ResourceHistory
                  kind="skill"
                  target={{ name: existing }}
                  current={loaded.data.body + loaded.data.description}
                  onRestored={() => void loaded.reload()}
                />
              )}
            </Card.Content>
          </Card>
        )}
      </Loading>
    </Page>
  )
}
