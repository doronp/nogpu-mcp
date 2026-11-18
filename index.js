#!/usr/bin/env node
// NoGPU MCP server (stdio). Three tools over the public API documented at https://nogpu.dev/openapi.json.
import { readFile, stat } from "node:fs/promises";
import { basename } from "node:path";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";

const BASE = process.env.NOGPU_URL || "https://nogpu.dev";
// Never throw out of a tool: a proxy's HTML error page, an empty body or a dropped connection
// comes back as { status, body: { error } } like any other failure.
async function call(url, opts) {
  let r;
  try { r = await fetch(url, opts); } catch (e) { return { status: 0, body: { error: `network error: ${e.message}` } }; }
  try { return { status: r.status, body: await r.json() }; }
  catch { return { status: r.status, body: { error: `unexpected response (HTTP ${r.status})` } }; }
}
const text = (o) => ({ content: [{ type: "text", text: typeof o === "string" ? o : JSON.stringify(o, null, 2) }] });

async function runReport(path, source = "file", { calls_per_day, usd_per_1k_calls, poll = 3000, timeout = 300000 } = {}) {
  const size = (await stat(path)).size;  // the server takes at most 20 MB: don't read a huge log into memory
  if (size === 0 || size > 20 * 1024 * 1024) return { error: `file is ${size} bytes; upload 1 byte to 20 MB` };
  const form = new FormData();
  form.append("file", new Blob([await readFile(path)]), basename(path));
  const { status, body: job } = await call(`${BASE}/api/jobs`, { method: "POST", body: form, headers: { "X-NoGPU-Source": source } });
  if (status !== 201) return { error: job.error || `upload failed (${status})` };
  const headers = { "X-Claim-Token": job.claim_token };
  // vol/price go before the '#' so the report page prefills its savings calculator; the token stays after it
  const q = new URLSearchParams();
  if (calls_per_day > 0) q.set("vol", String(calls_per_day >= 10 ? Math.round(calls_per_day) : Number(calls_per_day.toPrecision(2))));
  if (usd_per_1k_calls > 0) q.set("price", String(usd_per_1k_calls));
  const report_url = `${BASE}/job/${job.job_id}${String(q) ? `?${q}` : ""}#${job.claim_token}`;
  for (const end = Date.now() + timeout; Date.now() < end; await new Promise((s) => setTimeout(s, poll))) {
    const { status: code, body: v } = await call(`${BASE}/api/jobs/${job.job_id}`, { headers });
    if (code === 403 || code === 404) return { error: v.error || "job not found", job_id: job.job_id };
    if (code !== 200) continue;  // a blip: poll again until the timeout
    if (v.status === "failed") return { error: v.error || "job failed", job_id: job.job_id };
    if (v.report) {
      return {
        job_id: job.job_id, claim_token: job.claim_token, report_url,
        verdict: v.report.gate?.certified ? "fit" : "not_fit",
        gate: v.report.gate, bench: v.report.bench, data: v.report.data, notes: v.notes ?? [],
        checkout_url: v.checkout_url ? new URL(v.checkout_url, BASE).href : null, buy_before: v.buy_before ?? null,
        note: "share = fraction of decisions the CPU model answers alone; accuracy_when_sure = how often those answers " +
              "match your LLM, cp95_lower = its 95% lower bound, both on data.n_dev held-out rows. notes = what the upload " +
              "check changed (label merges, repeats dropped). Data is deleted within 24h (24h after payment if you buy the model).",
      };
    }
  }
  return { error: "timed out waiting for the report", job_id: job.job_id, report_url };
}

const server = new McpServer({ name: "nogpu", version: "0.2.3" });
server.tool("nogpu_report",
  "Free report: upload a file of an LLM's past decisions ({\"text\": ..., \"label\": ...} rows as JSONL, a JSON array, " +
  "or a CSV with text and label columns; 160-10,000 rows, " +
  "at least 2 labels) and measure what share a small CPU model can answer alone, how often it then matches the LLM " +
  "(with a 95% lower bound), and its latency. Usually 15-60 seconds. For raw LLM logs, first convert them locally with " +
  "https://nogpu.dev/static/nogpu_export.py. The returned report_url shows the report and a $99 option to keep the model.",
  { path: z.string().describe("Absolute path to the JSONL, JSON or CSV file"),
    source: z.enum(["history", "logging", "file"]).optional()
      .describe("Where the rows came from: the app's stored history, rows logged by the collect snippet, or a given file (default)"),
    calls_per_day: z.number().nonnegative().optional().describe("How many times a day the app makes this call; prefills the report's savings calculator"),
    usd_per_1k_calls: z.number().nonnegative().optional().describe("Estimated LLM cost in USD per 1,000 of these calls; prefills the calculator") },
  async ({ path, source = "file", calls_per_day, usd_per_1k_calls }) =>
    text(await runReport(path, source, { calls_per_day, usd_per_1k_calls })));
server.tool("nogpu_delete", "Delete an uploaded job's data now (otherwise deleted within 24h, or 24h after payment if you bought the model).",
  { job_id: z.string(), claim_token: z.string() },
  async ({ job_id, claim_token }) => {
    const { body } = await call(`${BASE}/api/jobs/${job_id}`, { method: "DELETE", headers: { "X-Claim-Token": claim_token } });
    return text(body);
  });

// Never pays: it returns the offer and the ways to pay; the user's own browser or x402 wallet pays.
async function buyOffer(job_id, claim_token) {
  const headers = { "X-Claim-Token": claim_token };
  const { status, body: v } = await call(`${BASE}/api/jobs/${job_id}`, { headers });
  if (status !== 200) return { buyable: false, reason: v.error || `job lookup failed (${status})` };
  if (v.paid) return { buyable: false, reason: "already bought: say 'install my NoGPU model' to download it" };
  if (v.demo) return { buyable: false, reason: "a demo job is not for sale" };
  if (!v.report) return { buyable: false, reason: v.status === "failed" ? (v.error || "the job failed") : "the report is not ready yet" };
  const gate = v.report.gate || {};
  if (!gate.certified) return { buyable: false, reason: "not a fit: the report did not certify a model, so there is nothing to buy" };
  if (v.buy_closed) return { buyable: false, reason: "the buy window closed (unpaid jobs are deleted within 24h); upload again for a new report" };
  // the bundle answers 402 while unpaid; newer servers add a buy object, older ones only the job's checkout_url
  const { status: code, body: b } = await call(`${BASE}/api/jobs/${job_id}/bundle`, { headers });
  if (code !== 402) return { buyable: false, reason: b.error || `not buyable (bundle answered ${code})` };
  const card = b.buy?.card || v.checkout_url;
  const usdc = b.buy?.usdc_x402 ?? null;
  if (!card && !usdc) return { buyable: false, reason: "no way to buy this job right now" };
  return {
    buyable: true, job_id, price: "99 USD one-time", buy_before: b.buy_before ?? v.buy_before ?? null,
    what_you_get: `The CPU model trained for this job (a tar.gz with serve.py) that answers ${Math.round((gate.share || 0) * 100)}% ` +
      "of these decisions alone and hands the rest to your LLM; download it within 24h of payment.",
    ask_user: `Buy the NoGPU model for job ${job_id} for $99? (yes/no)`,
    card: card ? new URL(card, BASE).href : null, usdc,
    how: "Show ask_user word for word; pay only after an explicit yes. card: give the user the link to pay in their " +
      "browser (never enter card details). usdc: only with the user's x402 wallet tool, POST usdc.url with a max payment of " +
      "exactly 99 USDC for this one call; never raise a wallet's cap, and if it refuses give the card link. Pay one way " +
      "only. Then poll GET /api/jobs/{id} (X-Claim-Token) until download_url and install.",
  };
}
server.registerTool("nogpu_buy", {
  description: "Read-only, never pays: for a certified (fit) report, returns the $99 offer to keep the model, the question " +
    "to ask the user word for word, a card checkout link for the user, and the USDC (x402) payment details when the server " +
    "offers them. If the job is not buyable, returns the reason.",
  inputSchema: { job_id: z.string(), claim_token: z.string() },
  annotations: { readOnlyHint: true, openWorldHint: true },
}, async ({ job_id, claim_token }) => text(await buyOffer(job_id, claim_token)));

await server.connect(new StdioServerTransport());
