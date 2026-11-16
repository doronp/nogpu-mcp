// nogpu collect: log an AI SDK app's own classification decisions as NoGPU rows, LOCALLY.
// One JSONL file per recurring decision in `dir` (default .nogpu/, git-ignore it), {"text","label"} per line.
// This file makes no network call. On serverless hosts, where local files vanish, pass onRow(row, name)
// and store the row in your own database or log instead; it then replaces the file write.
// Covers generateText/generateObject whose output schema is one enum field (Output.choice) and
// experimental_decide choice questions. Not streamText.
//
//   import { wrapLanguageModel } from 'ai'
//   const model = wrapLanguageModel({ model: openai('gpt-4.1-mini'), middleware: nogpuMiddleware() })
//   const decider = nogpuDecisions(myDecisionModel)
import { appendFileSync, mkdirSync, readFileSync } from 'node:fs'
import { createHash } from 'node:crypto'
import { join } from 'node:path'

const MAX = 10000                 // a report takes at most 10,000 rows: stop writing there
const MARKS = [160, 1000, 3000]   // 160 rows is enough for a report
const counts = new Map()
const h8 = (o) => createHash('sha256').update(JSON.stringify(o)).digest('hex').slice(0, 8)
const textOf = (parts) => parts.map(p => p.type === 'text' ? p.text : p.type === 'json' ? JSON.stringify(p.value) : '')
  .filter(Boolean).join('\n').trim()

function record(opts, name, text, label) {
  try { // logging never breaks the app's call (read-only or full disk, odd options)
    const { dir = '.nogpu', onRow } = opts || {}
    if (!text || typeof label !== 'string' || !label || label.length > 200) return
    name = name.replace(/[^\w-]/g, '_').slice(0, 60)
    const f = onRow ? `onRow:${name}` : join(dir, `${name}.jsonl`)
    let n = counts.get(f)  // ponytail: with onRow the cap is per process (a serverless instance), not global
    if (n === undefined) { try { n = onRow ? 0 : readFileSync(f, 'utf8').split('\n').length - 1 } catch { n = 0 } }
    if (n >= MAX) return
    if (onRow) Promise.resolve(onRow({ text, label }, name)).catch(() => {})
    else { mkdirSync(dir, { recursive: true }); appendFileSync(f, JSON.stringify({ text, label }) + '\n') }
    counts.set(f, ++n)
    if (!onRow && MARKS.includes(n)) console.error(`nogpu: ${n} decisions in ${f}. Ask your coding agent for a free NoGPU report.`)
  } catch {}
}

/** Wrap a decision model (experimental_decide): every choice answer becomes a row for that question. */
export function nogpuDecisions(model, opts = {}) {
  return { ...model, specificationVersion: model.specificationVersion, provider: model.provider, modelId: model.modelId,
    supportedQuestionTypes: model.supportedQuestionTypes,
    async doDecide(params) {
      const r = await model.doDecide(params)
      try {
        const text = textOf(params.state)
        for (const [id, q] of Object.entries(params.questions)) {
          const a = r.answers[id]
          if (q.type === 'choice' && a?.type === 'choice') record(opts, `${id}-${h8([q.instructions, Object.keys(q.criteria)])}`, text, a.choice)
        }
      } catch { /* logging never breaks the call */ }
      return r
    } }
}

/** Language-model middleware: calls whose output schema is one enum field (Output.choice, z.object({x: z.enum()})). */
export function nogpuMiddleware(opts = {}) {
  return { specificationVersion: 'v4',
    async wrapGenerate({ doGenerate, params }) {
      const r = await doGenerate()
      try {
        const props = params.responseFormat?.type === 'json' ? params.responseFormat.schema?.properties ?? {} : {}
        const keys = Object.keys(props)
        if (keys.length === 1 && Array.isArray(props[keys[0]].enum)) {
          const user = [...params.prompt].reverse().find(m => m.role === 'user')
          const label = JSON.parse(r.content.filter(c => c.type === 'text').map(c => c.text).join(''))[keys[0]]
          const system = params.prompt.find(m => m.role === 'system')?.content
          record(opts, `${keys[0]}-${h8([props[keys[0]].enum, system])}`, user ? textOf(user.content) : '', label)
        }
      } catch { /* logging never breaks the call */ }
      return r
    } }
}
