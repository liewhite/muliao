import { useEffect, useRef, useState } from 'react'
import { Button, Card, toast } from '@heroui/react'
import { Download, Plus, Trash2, Upload } from 'lucide-react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { api, ApiError, asAgentId, skillFileUrl, type AgentId, type AssetOf, type Schemas, type Skill } from '../api'
import { FileBrowser } from './FileBrowser'
import { AssetAccess, OwnerSelect } from './assetParts'
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

const SKILLS = '/ai-assets?kind=skill'

function editorPath(name?: string): string {
  return name ? `/ai-assets/skill?${new URLSearchParams({ name })}` : '/ai-assets/skill'
}

/** skill 的资产页：assets 是资产池里（已筛选的）skill，点进编辑 */
export function SkillTab({ assets, onChange }: { assets: AssetOf<'skill'>[]; onChange: () => void }) {
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
      {assets.length === 0 ? (
        <EmptyHint>没有 skill</EmptyHint>
      ) : (
        <div className="flex flex-col gap-2">
          {assets.map((s) => (
            <Card key={s.name}>
              <Card.Header>
                <Link to={editorPath(s.name)} className="hover:underline">
                  <Card.Title className="font-mono text-base">{s.name}</Card.Title>
                </Link>
                <Card.Description>{s.description}</Card.Description>
              </Card.Header>
              <Card.Content>
                <AssetAccess asset={s} onTransferred={onChange} />
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
          body: '覆盖后它的整个目录（SKILL.md 与附件）换成包里的内容，旧内容不保留。',
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
      {result && <SkippedFiles what="导入" skipped={result.skipped} />}
    </>
  )
}

/** 导入、导出时没收进来的文件与原因（二进制、不是 UTF-8 文本……）；没有时不显示 */
function SkippedFiles({ what, skipped }: { what: string; skipped: Schemas['SkippedOut'][] }) {
  if (skipped.length === 0) return null
  return (
    <div className="w-full text-sm text-muted">
      <div>以下文件没有{what}：</div>
      <ul className="list-disc pl-5">
        {skipped.map((s) => (
          <li key={s.path}>
            <span className="font-mono">{s.path}</span>：{s.reason}
          </li>
        ))}
      </ul>
    </div>
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
  // 上一次导出没打进包的文件，带着是哪个 skill 的：切到别的 skill 就不显示
  const [exportSkipped, setExportSkipped] = useState<{ name: string; skipped: Schemas['SkippedOut'][] } | null>(null)
  useEffect(() => {
    if (loaded.data) setSkill(loaded.data)
  }, [loaded.data])

  const set = (patch: Partial<Skill>) => setSkill((prev) => ({ ...prev, ...patch }))

  async function save() {
    const ok = await action.run(async () => {
      await api.saveSkill(skill.name, { owner: skill.owner, description: skill.description, body: skill.body })
      toast.success('skill 已保存')
    })
    if (ok) navigate(back)
  }

  async function exportPackage() {
    if (!existing) return
    setExportSkipped(null)
    await action.run(async () => {
      const exported = await api.exportSkill(existing)
      setExportSkipped({ name: existing, skipped: exported.skipped })
      const url = URL.createObjectURL(exported.blob)
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
          <div className="flex flex-col gap-4">
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
                {exportSkipped?.name === existing && <SkippedFiles what="打进导出的包" skipped={exportSkipped.skipped} />}
              </Card.Content>
            </Card>
            {existing && (
              <section className="flex flex-col gap-2">
                <h2 className="text-base font-semibold">文件</h2>
                <FileBrowser
                  key={existing}
                  source={{
                    id: existing,
                    list: (path) => api.skillFiles(existing, path),
                    fileUrl: (path) => skillFileUrl(existing, path),
                  }}
                  label="skill 目录"
                  emptyText="skill 目录是空的"
                />
              </section>
            )}
          </div>
        )}
      </Loading>
    </Page>
  )
}
