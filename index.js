#!/usr/bin/env node
// NoGPU MCP server (stdio). Two tools over the public API documented at https://nogpu.dev/openapi.json.
import { readFile } from "node:fs/promises";
import { basename } from "node:path";
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";

const BASE = process.env.NOGPU_URL || "https://nogpu.dev";
const text = (o) => ({ content: [{ type: "text", text: typeof o === "string" ? o : JSON.stringify(o, null, 2) }] });

async function runReport(path, { poll = 3000, timeout = 300000 } = {}) {
  const form = new FormData();
  form.append("file", new Blob([await readFile(path)]), basename(path));
  const r = await fetch(`${BASE}/api/jobs`, { method: "POST", body: form });
  const job = await r.json();
  if (r.status !== 201) return { error: job.error || `upload failed (${r.status})` };
  const headers = { "X-Claim-Token": job.claim_token };
  for (const end = Date.now() + timeout; Date.now() < end; await new Promise((s) => setTimeout(s, poll))) {
    const v = await (await fetch(`${BASE}/api/jobs/${job.job_id}`, { headers })).json();
    if (v.status === "failed") return { error: v.error || "job failed", job_id: job.job_id };
    if (v.report) {
      return {
        job_id: job.job_id, claim_token: job.claim_token,
        report_url: `${BASE}/job/${job.job_id}#${job.claim_token}`,
        gate: v.report.gate, bench: v.report.bench, checkout_url: v.checkout_url ?? null,
        note: "share = fraction of decisions the CPU model answers alone; accuracy_when_sure and cp95_lower " +
              "(one-sided 95% Clopper-Pearson) are measured on held-out rows. Data is deleted within 24h.",
      };
    }
  }
  return { error: "timed out waiting for the report", job_id: job.job_id, report_url: `${BASE}/job/${job.job_id}#${job.claim_token}` };
}

const server = new McpServer({ name: "nogpu", version: "0.1.0" });
server.tool("nogpu_report",
  "Free report: upload a JSONL file of an LLM's past decisions ({\"text\": ..., \"label\": ...} per line, 40-10,000 rows, " +
  "at least 2 labels) and measure what share a small CPU model can answer alone, how accurate it is when it does " +
  "(with a 95% lower bound), and its latency. Takes about a minute. For raw LLM logs, first convert them locally with " +
  "https://nogpu.dev/static/nogpu_export.py. The returned report_url shows the report and a $99 option to keep the model.",
  { path: z.string().describe("Absolute path to the JSONL file") },
  async ({ path }) => text(await runReport(path)));
server.tool("nogpu_delete", "Delete an uploaded job's data now (otherwise deleted within 24h).",
  { job_id: z.string(), claim_token: z.string() },
  async ({ job_id, claim_token }) => {
    const r = await fetch(`${BASE}/api/jobs/${job_id}`, { method: "DELETE", headers: { "X-Claim-Token": claim_token } });
    return text(await r.json());
  });

await server.connect(new StdioServerTransport());
