# nogpu-mcp

MCP server that lets your agent measure, on your own data, how many of your LLM's classification or
routing decisions a small CPU model can take alone. It calls the free [NoGPU](https://nogpu.dev) report.

Tools:
- `nogpu_report(path)`: uploads a JSONL file (`{"text": ..., "label": ...}` per line, 40 to 10,000 rows,
  at least 2 labels) and returns, in about a minute: the share the CPU model answers alone, its accuracy
  when it does with a one-sided 95% Clopper-Pearson lower bound (held-out rows), latency, and a link to the
  report. Keeping the model is $99 one-time; the report is free.
- `nogpu_delete(job_id, claim_token)`: deletes the uploaded data now (otherwise deleted within 24 hours).

Measured example (3,000 rows of the public Banking77 set, 77 intents): 58.7% answered alone, 94.7% accurate
when it answered (lower bound 91.8%), about 4 ms per decision.

Raw LLM logs (OpenAI, Anthropic, LangChain, LiteLLM, Langfuse shapes)? Convert them locally first with
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

API reference: https://nogpu.dev/openapi.json · MIT license.
