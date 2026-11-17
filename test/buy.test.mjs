// nogpu_buy against a fake NoGPU API: it never pays, falls back to the job's checkout_url on a server
// whose 402 has no buy object, and says why when a job is not buyable.
import assert from 'node:assert/strict'
import { once } from 'node:events'
import { createServer } from 'node:http'
import { Client } from '@modelcontextprotocol/sdk/client/index.js'
import { StdioClientTransport } from '@modelcontextprotocol/sdk/client/stdio.js'

let job, bundle, posts = 0, bundles = 0
const api = createServer((req, res) => {
  req.resume()
  if (req.method !== 'GET') posts++
  assert.equal(req.headers['x-claim-token'], 'TOK')
  res.setHeader('content-type', 'application/json')
  if (req.url.endsWith('/bundle')) { bundles++; res.statusCode = 402; return res.end(JSON.stringify(bundle)) }
  res.end(JSON.stringify(job))
})
api.listen(0, '127.0.0.1')
await once(api, 'listening')
const base = `http://127.0.0.1:${api.address().port}`
const client = new Client({ name: 'test', version: '0' })
await client.connect(new StdioClientTransport({ command: process.execPath, args: ['index.js'], env: { ...process.env, NOGPU_URL: base } }))
const tool = (await client.listTools()).tools.find((t) => t.name === 'nogpu_buy')
assert.equal(tool.annotations.readOnlyHint, true)
const buy = async () => JSON.parse((await client.callTool({ name: 'nogpu_buy', arguments: { job_id: 'abc123', claim_token: 'TOK' } })).content[0].text)
const fit = () => ({ status: 'report_ready', paid: false, checkout_url: '/go/checkout/abc123', buy_before: '2026-11-18T15:12:34Z',
  report: { gate: { certified: true, threshold: 0.8, share: 0.9 } } })

// buyable, new server: card and USDC from the 402 buy object
job = fit()
const usdc = { method: 'POST', url: `${base}/api/jobs/abc123/unlock`, network: 'eip155:8453', amount: '99000000' }
bundle = { error: 'this model is not unlocked yet', price_usd: 99, buy_before: '2026-11-18T15:00:00Z',
  buy: { card: `${base}/go/checkout/abc123`, usdc_x402: usdc } }
let r = await buy()
assert.equal(r.buyable, true)
assert.equal(r.ask_user, 'Buy the NoGPU model for job abc123 for $99? (yes/no)')
assert.equal(r.card, `${base}/go/checkout/abc123`)
assert.deepEqual(r.usdc, usdc)
assert.equal(r.buy_before, '2026-11-18T15:00:00Z')
assert.match(r.what_you_get, /90%/)
assert.ok(r.price && r.how)

// buyable, current server: 402 has no buy object, card from the job's checkout_url, no USDC
bundle = { error: 'this model is not unlocked yet' }
r = await buy()
assert.equal(r.buyable, true)
assert.equal(r.card, `${base}/go/checkout/abc123`)
assert.equal(r.usdc, null)
assert.equal(r.buy_before, '2026-11-18T15:12:34Z')

// not buyable: a reason, never an offer
job = fit(); job.report.gate.certified = false; delete job.checkout_url
r = await buy()
assert.equal(r.buyable, false)
assert.match(r.reason, /not a fit/)
assert.equal(r.ask_user, undefined)
bundles = 0
job = fit(); job.paid = true
assert.match((await buy()).reason, /already bought/)
job = fit(); job.demo = true
assert.match((await buy()).reason, /demo/)
assert.equal(bundles, 0, 'a paid or demo job: the bundle is never fetched')
job = fit(); delete job.checkout_url; job.buy_closed = true
assert.match((await buy()).reason, /closed/)
job = { status: 'running', paid: false }
assert.match((await buy()).reason, /not ready/)
job = fit(); delete job.checkout_url
assert.equal((await buy()).buyable, false, 'no card and no USDC')
assert.equal(posts, 0, 'nogpu_buy only reads')

await client.close()
api.close()
console.log('nogpu_buy ok')
