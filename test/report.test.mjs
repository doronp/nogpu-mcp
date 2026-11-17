// nogpu_report against a fake NoGPU API: vol/price land before the '#', and the result carries
// verdict, data, notes and buy_before so the agent never has to work the counts out itself.
import assert from 'node:assert/strict'
import { once } from 'node:events'
import { writeFileSync, mkdtempSync } from 'node:fs'
import { createServer } from 'node:http'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { StdioClientTransport } from '@modelcontextprotocol/sdk/client/stdio.js'

const job = { status: 'report_ready', notes: ['140 repeated rows dropped'], checkout_url: '/go/checkout/abc123',
  buy_before: '2026-11-18T15:12:34Z', report: { gate: { certified: true, share: 0.9 }, bench: { p50_ms: 3 },
    data: { n_rows: 505, n_dev: 108 } } }
const api = createServer((req, res) => {
  req.resume()
  res.setHeader('content-type', 'application/json')
  if (req.method === 'POST') { res.statusCode = 201; return res.end(JSON.stringify({ job_id: 'abc123', claim_token: 'TOK' })) }
  assert.equal(req.headers['x-claim-token'], 'TOK')
  res.end(JSON.stringify(job))
})
api.listen(0, '127.0.0.1')
await once(api, 'listening')

const file = join(mkdtempSync(join(tmpdir(), 'nogpu-')), 'nogpu.jsonl')
writeFileSync(file, '{"text": "a", "label": "x"}\n')
const client = new Client({ name: 'test', version: '0' })
await client.connect(new StdioClientTransport({ command: process.execPath, args: ['index.js'],
  env: { ...process.env, NOGPU_URL: `http://127.0.0.1:${api.address().port}` } }))
const call = async (args) => JSON.parse((await client.callTool({ name: 'nogpu_report', arguments: { path: file, ...args } })).content[0].text)

const r = await call({ calls_per_day: 33.4, usd_per_1k_calls: 0.15 })
assert.equal(r.report_url, `http://127.0.0.1:${api.address().port}/job/abc123?vol=33&price=0.15#TOK`)
assert.equal(r.verdict, 'fit')
assert.deepEqual(r.data, job.report.data)
assert.deepEqual(r.notes, job.notes)
assert.equal(r.buy_before, job.buy_before)
assert.ok(r.checkout_url.endsWith('/go/checkout/abc123'))
assert.ok((await call({})).report_url.endsWith('/job/abc123#TOK'), 'no query without volume or price')
job.report.gate.certified = false
assert.equal((await call({})).verdict, 'not_fit')

await client.close()
api.close()
console.log('nogpu_report ok')
