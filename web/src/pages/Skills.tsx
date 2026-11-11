import { useState } from 'react'
import { Button, Card, toast } from '@heroui/react'
import { Plus, Trash2 } from 'lucide-react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { api, skillFileUrl, type AgentId, type AssetOf, type Skill } from '../api'
import { FileBrowser } from './FileBrowser'
import { AssetAccess, OwnerSelect } from './assetParts'
import { useDraft } from '../drafts'
import { ActionButton,
  DraftConflicts,
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
  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap gap-2">
        <LinkButton to={editorPath()} variant="secondary">
          <Plus className="size-4" />
          新建 skill
        </LinkButton>
      </div>
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

const SKILL_FIELD_LABELS: Record<keyof SkillText, string> = { description: '描述', body: '正文' }

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
  const loaded = useLoad(() => api.skill(name), [name])
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
  const doc = useDraft<SkillText>({ description: latest.description, body: latest.body })
  const action = useAction()

  async function save() {
    const ok = await action.run(async () => {
      // 整份保存带版本：没有冲突时草稿的基线就是 latest（drafts.useDraft）
      await api.saveSkill(name, { owner, ...doc.draft, version: latest.version })
      toast.success('skill 已保存')
    })
    if (ok) navigate(SKILLS)
  }

  async function remove() {
    const ok = await confirm({ title: `删除 skill「${name}」？`, confirmLabel: '删除', danger: true })
    if (ok) await action.run(() => api.deleteAsset('skill', name), { leave: () => navigate(SKILLS) })
  }

  return (
    <div className="flex flex-col gap-4">
      <Card>
        <Card.Content className="flex flex-col gap-4">
          <div className="text-sm text-muted">归属：{owner === null ? '平台' : agentLabel(agents.data, owner)}</div>
          <SkillFields value={doc.draft} set={doc.set} />
          <DraftConflicts draft={doc} labels={SKILL_FIELD_LABELS} />
          <ErrorAlert error={action.error} />
          <div className="flex gap-2">
            <ActionButton isPending={action.busy} isDisabled={doc.conflicts.length > 0} onPress={() => void save()}>
              保存
            </ActionButton>
            <Button variant="danger-soft" isDisabled={action.busy} onPress={() => void remove()}>
              <Trash2 className="size-4" />
              删除
            </Button>
          </div>
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
