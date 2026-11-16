// Smoke test for plugin/skills/nogpu/collect.mjs against the AI SDK's mock models: every wrapped
// decision writes one local row, and a broken logger never breaks the call.
import assert from 'node:assert/strict'
import { mkdtempSync, readFileSync, readdirSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { experimental_decide, generateText, Output, wrapLanguageModel } from 'ai'
import { Experimental_DecisionMockModelV4, MockLanguageModelV4 } from 'ai/test'
import { nogpuDecisions, nogpuMiddleware } from '../plugin/skills/nogpu/collect.mjs'

const opts = ['billing', 'refund', 'card_arrival']
const pick = (s) => opts[s.length % 3]
const decider = () => new Experimental_DecisionMockModelV4({ doDecide: async ({ state }) => ({
  answers: { route: { type: 'choice', choice: pick(state[0].text) } }, warnings: [] }) })
const lm = () => new MockLanguageModelV4({ doGenerate: async ({ prompt }) => ({
  content: [{ type: 'text', text: JSON.stringify({ result: pick(prompt.find(m => m.role === 'user').content[0].text) }) }],
  finishReason: { unified: 'stop', raw: 'stop' }, usage: { inputTokens: { total: 1 }, outputTokens: { total: 1 } }, warnings: [] }) })
const run = (dm, model, q) => Promise.all([
  experimental_decide({ model: dm, state: q, questions: { route: { type: 'choice', criteria: Object.fromEntries(opts.map(o => [o, null])) } } }),
  generateText({ model, system: 'Route the ticket.', prompt: q, output: Output.choice({ options: opts }) })])

const dir = mkdtempSync(join(tmpdir(), 'nogpu-'))
const dm = nogpuDecisions(decider(), { dir }), model = wrapLanguageModel({ model: lm(), middleware: nogpuMiddleware({ dir }) })
for (let i = 0; i < 45; i++) await run(dm, model, `customer message number ${i} ${'x'.repeat(i % 7)}`)
const files = readdirSync(dir)
assert.equal(files.length, 2, 'one file per decision')
for (const f of files) {
  const rows = readFileSync(join(dir, f), 'utf8').trim().split('\n').map(JSON.parse)
  assert.equal(rows.length, 45)
  assert.ok(rows.every(r => r.text.startsWith('customer message') && opts.includes(r.label)))
}

process.chdir(dir)  // nogpuMiddleware(null) writes to ./.nogpu
const seen = []  // onRow (serverless) gets the rows instead of a file; a throwing callback is swallowed
const cb = nogpuDecisions(decider(), { onRow: (row) => { seen.push(row); throw new Error('db down') } })
const [d] = await run(cb, wrapLanguageModel({ model: lm(), middleware: nogpuMiddleware(null) }), 'where is my card')
assert.equal(d.answers.route.choice, pick('where is my card'))
assert.equal(seen.length, 1)
console.log('collect.mjs ok')
