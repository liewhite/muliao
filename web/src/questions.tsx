import { useState } from 'react'
import { Chip } from '@heroui/react'
import { CircleHelp } from 'lucide-react'
import type { AnsweredQuestion, Question, QuestionAnswer } from './api'
import { ActionButton, ErrorAlert, useAction } from './ui'

const CARD = 'flex w-full max-w-[88%] flex-col gap-3 self-start rounded-2xl border border-accent bg-surface p-3.5 text-sm'

interface Draft {
  selected: string[]
  other: string
}

const answered = (d: Draft) => d.selected.length > 0 || d.other.trim() !== ''

/** agent 在等用户回答的问题：单选或多选，每题都可以在「其他」里自己写；全部答完才能提交 */
export function QuestionForm({
  questions,
  onSubmit,
}: {
  questions: Question[]
  onSubmit: (answers: QuestionAnswer[]) => Promise<unknown>
}) {
  const [drafts, setDrafts] = useState<Draft[]>(() => questions.map(() => ({ selected: [], other: '' })))
  const action = useAction()

  function toggle(qi: number, label: string) {
    setDrafts((prev) =>
      prev.map((d, i) => {
        if (i !== qi) return d
        const on = d.selected.includes(label)
        if (questions[qi].multi_select) {
          return { ...d, selected: on ? d.selected.filter((l) => l !== label) : [...d.selected, label] }
        }
        return { ...d, selected: on ? [] : [label] }
      }),
    )
  }

  function submit() {
    void action.run(() =>
      onSubmit(drafts.map((d) => ({ selected: d.selected, other: d.other.trim() === '' ? null : d.other.trim() }))),
    )
  }

  return (
    <div className={CARD}>
      <div className="flex items-center gap-1.5 text-accent">
        <CircleHelp className="size-4" />
        需要你回答
      </div>
      {questions.map((q, qi) => (
        <fieldset key={qi} className="flex flex-col gap-2">
          <legend className="mb-2 flex items-center gap-2">
            <Chip size="sm" variant="soft">
              {q.header}
            </Chip>
            <span className="font-medium">{q.question}</span>
            {q.multi_select && <span className="text-xs text-muted">可多选</span>}
          </legend>
          {q.options.map((o) => {
            const on = drafts[qi].selected.includes(o.label)
            return (
              <button
                key={o.label}
                type="button"
                role={q.multi_select ? 'checkbox' : 'radio'}
                aria-checked={on}
                onClick={() => toggle(qi, o.label)}
                className={`flex flex-col items-start rounded-xl border px-3 py-2 text-left ${
                  on ? 'border-accent bg-accent-soft' : 'border-separator hover:bg-surface-hover'
                }`}
              >
                <span>{o.label}</span>
                {o.description && <span className="text-xs text-muted">{o.description}</span>}
              </button>
            )
          })}
          <input
            value={drafts[qi].other}
            onChange={(e) => {
              const other = e.target.value
              setDrafts((prev) => prev.map((d, i) => (i === qi ? { ...d, other } : d)))
            }}
            placeholder="其他（自己写）"
            aria-label={`${q.header}：其他`}
            className="rounded-xl border border-separator bg-transparent px-3 py-2 outline-none focus:border-accent"
          />
        </fieldset>
      ))}
      <ErrorAlert error={action.error} />
      <ActionButton className="self-start" isDisabled={!drafts.every(answered)} isPending={action.busy} onPress={submit}>
        提交回答
      </ActionButton>
      <div className="text-xs text-muted">也可以不回答，直接在下面发消息。</div>
    </div>
  )
}

/** 已回答（或已提交、还没处理）的提问 */
export function AnsweredQuestions({ answers }: { answers: AnsweredQuestion[] }) {
  return (
    <div className="flex max-w-[88%] flex-col gap-1.5 self-start rounded-2xl border border-dashed border-separator px-3.5 py-2.5 text-sm">
      {answers.map((a, i) => (
        <div key={i}>
          <div className="text-muted">{a.question}</div>
          <div>{[...a.selected, ...(a.other ? [a.other] : [])].join('、')}</div>
        </div>
      ))}
    </div>
  )
}

/** 提问状态的一句话说明：用户没回答就发了新消息、正在提问、或回答已提交等待处理 */
export function QuestionNote({ text }: { text: string }) {
  return (
    <div className="flex items-center gap-1.5 self-start text-xs text-muted">
      <CircleHelp className="size-3.5" />
      {text}
    </div>
  )
}
