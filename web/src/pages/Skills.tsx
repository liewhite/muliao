import { useRef, useState } from 'react'
import { Button, Card, toast } from '@heroui/react'
import { Download, Plus, Trash2, Upload } from 'lucide-react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { api, ApiError, skillFileUrl, type AgentId, type AssetOf, type Schemas, type Skill } from '../api'
import { FileBrowser } from './FileBrowser'
import { AssetAccess, OwnerSelect, StaleDraft } from './assetParts'
import { useVersionedDraft } from '../drafts'
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
  useAgents,
  useConfirm,
} from '../ui'
import { agentLabel } from '../agentGroups'
import { useLoad } from '../load'

const SKILLS = '/ai-assets?kind=skill'

function editorPath(name?: string): string {
  return name ? `/ai-assets/skill?${new URLSearchParams({ name })}` : '/ai-assets/skill'
}

/** skill 的资产页：assets 是资产池里（已筛选的）skill，点进编辑 */
export function SkillTab({ assets }: { assets: AssetOf<'skill'>[] }) {
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
        <ImportSkill owner={owner} />
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
                <AssetAccess asset={s} />
              </Card.Content>
            </Card>
          ))}
        </div>
      )}
    </div>
  )
}

/** 导入标准 .skill 文件为 owner 的 skill；同名已存在时问是否覆盖，没有导入的文件列在下面。 */
function ImportSkill({ owner }: { owner: AgentId | null }) {
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

/** skill 的描述与正文（新建与编辑共用），值由调用方管 */
function SkillFields({ value, set }: { value: SkillText; set: (patch: Partial<SkillText>) => void }) {
  return (
    <>
      <TextInput
        label="描述"
        hint="何时使用这个 skill，会出现在 agent 的 skill 索引里"
        value={value.description}
        onChange={(description) => set({ description })}
        isRequired
      />
      <TextAreaInput label="内容（Markdown）" value={value.body} onChange={(body) => set({ body })} rows={18} mono />
    </>
  )
}

type SkillText = Pick<Skill, 'description' | 'body'>

export function SkillEditor() {
  const [params] = useSearchParams()
  const existing = params.get('name')
  return existing ? <ExistingSkill name={existing} /> : <NewSkill />
}

function NewSkill() {
  const navigate = useNavigate()
  const [owner, setOwner] = useState<AgentId | null>(null)
  const [name, setName] = useState('')
  const [text, setText] = useState<SkillText>({ description: '', body: '' })
  const action = useAction()
  return (
    <Page title="新建 skill" back={SKILLS}>
      <Card>
        <Card.Content className="flex flex-col gap-4">
          <OwnerSelect value={owner} onChange={setOwner} />
          <TextInput label="名称" hint="小写字母、数字、- 或 _" value={name} onChange={setName} mono isRequired />
          <SkillFields value={text} set={(patch) => setText((prev) => ({ ...prev, ...patch }))} />
          <ErrorAlert error={action.error} />
          <div className="flex gap-2">
            <ActionButton
              isPending={action.busy}
              onPress={() =>
                void action
                  .run(async () => {
                    await api.saveSkill(name, { owner, ...text, version: null })
                    toast.success('skill 已保存')
                  })
                  .then((ok) => ok && navigate(SKILLS))
              }
            >
              保存
            </ActionButton>
          </div>
        </Card.Content>
      </Card>
    </Page>
  )
}

/** 已有的 skill：agent 随时会改它，随 changed 重新拉取，保存带打开时的版本 */
function ExistingSkill({ name }: { name: string }) {
  const loaded = useLoad('agents', () => api.skill(name), [name])
  return (
    <Page title={name} back={SKILLS}>
      <Loading state={loaded}>{(s) => <SkillDraft latest={s} />}</Loading>
    </Page>
  )
}

function SkillDraft({ latest }: { latest: Skill }) {
  const navigate = useNavigate()
  const confirm = useConfirm()
  const { name, owner } = latest
  const agents = useAgents()
  const doc = useVersionedDraft(latest, (s): SkillText => ({ description: s.description, body: s.body }))
  const action = useAction()
  // 上一次导出没打进包的文件
  const [exportSkipped, setExportSkipped] = useState<Schemas['SkippedOut'][] | null>(null)

  async function save() {
    const ok = await action.run(async () => {
      await api.saveSkill(name, { owner, ...doc.draft, version: doc.version })
      toast.success('skill 已保存')
    })
    if (ok) navigate(SKILLS)
  }

  async function exportPackage() {
    setExportSkipped(null)
    await action.run(async () => {
      const exported = await api.exportSkill(name)
      setExportSkipped(exported.skipped)
      const url = URL.createObjectURL(exported.blob)
      const link = document.createElement('a')
      link.href = url
      link.download = `${name}.skill`
      link.click()
      // 点击触发的下载异步开始，太早释放会取消它
      setTimeout(() => URL.revokeObjectURL(url), 10_000)
    })
  }

  async function remove() {
    const ok = await confirm({ title: `删除 skill「${name}」？`, confirmLabel: '删除', danger: true })
    if (ok && (await action.run(() => api.deleteAsset('skill', name)))) navigate(SKILLS)
  }

  return (
    <div className="flex flex-col gap-4">
      <Card>
        <Card.Content className="flex flex-col gap-4">
          <div className="text-sm text-muted">归属：{owner === null ? '平台' : agentLabel(agents.data, owner)}</div>
          <SkillFields value={doc.draft} set={(patch) => doc.setDraft((prev) => ({ ...prev, ...patch }))} />
          {doc.stale && <StaleDraft what="这个 skill" onDiscard={doc.discard} />}
          <ErrorAlert error={action.error} />
          <div className="flex gap-2">
            <ActionButton isPending={action.busy} onPress={() => void save()}>
              保存
            </ActionButton>
            <Button variant="secondary" isDisabled={action.busy} onPress={() => void exportPackage()}>
              <Download className="size-4" />
              导出 .skill
            </Button>
            <Button variant="danger-soft" isDisabled={action.busy} onPress={() => void remove()}>
              <Trash2 className="size-4" />
              删除
            </Button>
          </div>
          {exportSkipped && <SkippedFiles what="打进导出的包" skipped={exportSkipped} />}
        </Card.Content>
      </Card>
      <section className="flex flex-col gap-2">
        <h2 className="text-base font-semibold">文件</h2>
        <FileBrowser
          source={{
            id: name,
            list: (path) => api.skillFiles(name, path),
            fileUrl: (path) => skillFileUrl(name, path),
          }}
          label="skill 目录"
          emptyText="skill 目录是空的"
        />
      </section>
    </div>
  )
}
