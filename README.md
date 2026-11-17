# nogpu-mcp

MCP server that lets your agent measure, on your own data, how many of your LLM's classification or
routing decisions a small CPU model can take alone. It calls the free [NoGPU](https://nogpu.dev) report.

Tools:
- `nogpu_report(path, source?, calls_per_day?, usd_per_1k_calls?)`: uploads a file of `{"text": ..., "label": ...}` rows (JSONL, a JSON array, or a CSV with
  text and label columns; 160 to 10,000 rows, at least 2 labels) and returns, usually in 15-60 seconds: a fit / not-fit verdict, the share the CPU model answers alone,
  how often it then matches your LLM with a one-sided 95% Clopper-Pearson lower bound (held-out rows), latency, the row counts,
  what the upload check changed, and a link to the report. Keeping the model is $99 one-time per model (a new label set is a new
  upload and a new model); the report is free. `source` (`history`, `logging` or `file`, the default) is sent as the
  `X-NoGPU-Source` header so NoGPU can count where reports come from. `calls_per_day` and `usd_per_1k_calls` (your estimate)
  are added to the report link so its savings calculator starts from your numbers.
- `nogpu_buy(job_id, claim_token)`: read-only, never pays. For a fit report it returns the $99 offer, the yes/no question
  to ask you, a card checkout link for you to open, and the USDC (x402) payment details when the server offers them; for any
  other job, the reason it can't be bought. The plugin's skill pays only after your explicit yes.
- `nogpu_delete(job_id, claim_token)`: deletes the uploaded data now (otherwise deleted within 24 hours; 24 hours after payment if you buy).

Measured example (3,000 rows of the public Banking77 set, 77 intents): 66.7% answered alone, matching the
dataset's labels 94.1% of the time when it answered (95% lower bound 91.4%), about 5 ms per decision.

Raw LLM logs (OpenAI, Anthropic, Langfuse, LangSmith, Helicone)? Convert them locally first with
[nogpu_export.py](https://nogpu.dev/static/nogpu_export.py) (Python stdlib only).

## Install

Claude Code:
```bash
claude mcp add nogpu -- npx -y github:doronp/nogpu-mcp
```

Any MCP client (Claude Desktop, Cursor, ...):
```json
{ "mcpServers": { "nogpu": { "command": "npx", "args": ["-y", "github:doronp/nogpu-mcp"] } } }
```

Then ask your agent: "Run a NoGPU report on ./decisions.jsonl".

## As a Claude Code plugin

```bash
claude plugin marketplace add doronp/nogpu-mcp
claude plugin install nogpu@nogpu
```

Then ask: "get me a NoGPU report for this app". The plugin adds this MCP server (`npx -y github:doronp/nogpu-mcp#v0.2.0`)
and one skill. The skill has your coding agent find the recurring LLM classification call in your repo, follow
its result to where the app already stores it (a database column, a log, an observability export), and write a
local extraction script that creates `nogpu.jsonl` and prints only the row count and label histogram. With no
stored history, it adds a local logger at the call site ([collect.mjs](plugin/skills/nogpu/collect.mjs) for the
AI SDK, a few lines in other languages) so rows build up. After you buy the model, ask "install my NoGPU model": the
skill downloads it with your claim token and puts it in front of the call site, with your LLM as the fallback.

## Data

- Uploaded: only the file you approve, `{"text", "label"}` rows, sent by `nogpu_report` (or `curl`) to
  `https://nogpu.dev/api/jobs`, used to train and test a CPU model for your report.
- No row reaches nogpu.dev before you say yes: the skill shows the file path, row count and label
  histogram, and asks first. The logger and the extraction script only write local files.
- Your coding agent's model provider sees what the agent reads, as always: code, schemas, counts and
  label names. The skill tells the agent never to print row text.
- Deleted within 24 hours (if you buy the model, within 24 hours of payment), or at once with
  `nogpu_delete(job_id, claim_token)`.
- The skill tells the agent to read schemas and counts and never print rows into the conversation. That is an
  instruction to the agent, not something the plugin enforces.

API reference: https://nogpu.dev/openapi.json · MIT license.
