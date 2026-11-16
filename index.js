#!/usr/bin/env node
// NoGPU MCP server (stdio). Two tools over the public API documented at https://nogpu.dev/openapi.json.
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

async function runReport(path, source = "file", { poll = 3000, timeout = 300000 } = {}) {
  const size = (await stat(path)).size;  // the server takes at most 20 MB: don't read a huge log into memory
  if (size === 0 || size > 20 * 1024 * 1024) return { error: `file is ${size} bytes; upload 1 byte to 20 MB` };
  const form = new FormData();
  form.append("file", new Blob([await readFile(path)]), basename(path));
  const { status, body: job } = await call(`${BASE}/api/jobs`, { method: "POST", body: form, headers: { "X-NoGPU-Source": source } });
  if (status !== 201) return { error: job.error || `upload failed (${status})` };
  const headers = { "X-Claim-Token": job.claim_token };
  for (const end = Date.now() + timeout; Date.now() < end; await new Promise((s) => setTimeout(s, poll))) {
    const { status: code, body: v } = await call(`${BASE}/api/jobs/${job.job_id}`, { headers });
    if (code === 403 || code === 404) return { error: v.error || "job not found", job_id: job.job_id };
    if (code !== 200) continue;  // a blip: poll again until the timeout
    if (v.status === "failed") return { error: v.error || "job failed", job_id: job.job_id };
    if (v.report) {
      return {
        job_id: job.job_id, claim_token: job.claim_token,
        report_url: `${BASE}/job/${job.job_id}#${job.claim_token}`,
        gate: v.report.gate, bench: v.report.bench,
        checkout_url: v.checkout_url ? new URL(v.checkout_url, BASE).href : null,
        note: "share = fraction of decisions the CPU model answers alone; accuracy_when_sure and cp95_lower " +
              "(one-sided 95% Clopper-Pearson) are measured on held-out rows. Data is deleted within 24h (24h after payment if you buy the model).",
      };
    }
  }
  return { error: "timed out waiting for the report", job_id: job.job_id, report_url: `${BASE}/job/${job.job_id}#${job.claim_token}` };
}

const server = new McpServer({ name: "nogpu", version: "0.1.0" });
server.tool("nogpu_report",
  "Free report: upload a file of an LLM's past decisions ({\"text\": ..., \"label\": ...} rows as JSONL, a JSON array, " +
  "or a CSV with text and label columns; 160-10,000 rows, " +
  "at least 2 labels) and measure what share a small CPU model can answer alone, how accurate it is when it does " +
  "(with a 95% lower bound), and its latency. Takes about a minute. For raw LLM logs, first convert them locally with " +
  "https://nogpu.dev/static/nogpu_export.py. The returned report_url shows the report and a $99 option to keep the model.",
  { path: z.string().describe("Absolute path to the JSONL, JSON or CSV file"),
    source: z.enum(["history", "logging", "file"]).optional()
      .describe("Where the rows came from: the app's stored history, rows logged by the collect snippet, or a given file (default)") },
  async ({ path, source = "file" }) => text(await runReport(path, source)));
server.tool("nogpu_delete", "Delete an uploaded job's data now (otherwise deleted within 24h, or 24h after payment if you bought the model).",
  { job_id: z.string(), claim_token: z.string() },
  async ({ job_id, claim_token }) => {
    const { body } = await call(`${BASE}/api/jobs/${job_id}`, { method: "DELETE", headers: { "X-Claim-Token": claim_token } });
    return text(body);
  });

await server.connect(new StdioServerTransport());
