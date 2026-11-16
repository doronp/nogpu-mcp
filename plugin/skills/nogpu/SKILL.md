---
name: nogpu
description: Get a free NoGPU report on how many of an app's recurring LLM classification or routing decisions (intent routing, ticket triage, moderation or "pick one category" calls) a small CPU model can take alone. Finds the call site, extracts its past decisions locally into nogpu.jsonl without printing any row text, and uploads only after the user says yes. Use when the user asks for a NoGPU report, asks whether a CPU or small model could take over their LLM's classification calls, or wants to cut LLM cost or latency on intent routing, ticket triage or another label-picking call.
---

# NoGPU report

NoGPU (https://nogpu.dev) trains a small CPU model on an app's past LLM decisions and reports the
share it can answer alone. That share is certified only when the one-sided 95% Clopper-Pearson lower
bound of its accuracy on held-out rows is at least 90%. The report is free; keeping the model is $99.
It needs 160 to 10,000 rows of `{"text": ..., "label": ...}` with at least 2 labels. Uploaded data is
deleted within 24 hours, or at once with `nogpu_delete`. Work from the root of the user's project.

## Hard rules (privacy)

The rows are the app's users' data, and anything you print enters this conversation.
- Read code, schemas and counts only. Never print row text: no `SELECT *`, no `head`, `cat`, `tail`
  or `less` on data, logs or `nogpu.jsonl`, no sample rows "to check the format". Use `COUNT(*)`,
  `GROUP BY`, `PRAGMA table_info`, `\d table`, ORM models, migrations and type definitions.
- Labels are category names and may be printed. If they look like free text (sentences, or more
  than about 50 distinct values), stop: this is not a classification call.
- The extraction writes `nogpu.jsonl` on this machine and prints only the row count, the label
  histogram, the skipped rows by reason, and the file's absolute path and size.
- Nothing leaves the machine until the user answers yes to the question in step 5. Never upload in
  the turn where you ask. In a non-interactive run, stop after asking.
- Keep `nogpu.jsonl`, `nogpu_extract.*` and `.nogpu/` out of git: add them to `.gitignore`.

## 1. Find the decision

Search the code for a recurring LLM call whose answer is one label from a fixed set:
- AI SDK: `Output.choice(`, `experimental_decide`, `generateObject` with `output: 'enum'`, or a
  `z.enum` as the only field of the output schema.
- OpenAI: a `response_format` / `text.format` JSON schema with an `enum`, a `tool_choice` that forces
  one function whose argument is an enum, or `.parse(` with an Enum or Literal field.
- Anthropic: a forced `tool_use` (`tool_choice: {"type": "tool", ...}`) with an enum in `input_schema`.
- LangChain / Pydantic: `with_structured_output(` on a model with a `Literal[...]` or `Enum` field.
- Any prompt that says "reply with one of", "classify into" or "respond with only the category".

Note what input the call sends (which fields, how they are joined) and the label set. If you find
several, list them with file:line and ask which one; suggest the one called most often.

## 2. Follow the result to where it is stored

Trace the returned label through the code: a column next to the input (e.g. `tickets.category`), an
events or audit table, application logs, or an observability SDK (Langfuse, LangSmith, Helicone,
LiteLLM callbacks). Look at schemas and code, then count, e.g. `SELECT category, COUNT(*) ... GROUP BY 1`.
- Label: the value the LLM returned. If the app overwrites it with a human correction and keeps no
  original, tell the user; the report then measures the corrected labels.
- Text: what the LLM was given, built the way the call site builds it (e.g. subject + "\n" + body),
  without the system prompt or few-shot examples.
- Raw LLM logs or an observability export: convert them locally (stdlib Python, nothing is sent):
  `curl -sSO https://nogpu.dev/static/nogpu_export.py && python3 nogpu_export.py <logs> > nogpu.jsonl`
  (`--input-field` / `--label-field` for unusual shapes). Always redirect its stdout to the file.
- Nothing stored: go to step 6.

## 3. Extract locally

Write `nogpu_extract.<ext>` at the project root, in the app's language, using its own database
driver or files, read-only. It must:
- write `nogpu.jsonl` with one `{"text": ..., "label": ...}` object per line;
- skip rows with empty text or label, and trim whitespace around labels;
- keep at most 10,000 rows, the most recent;
- print only: rows written, rows skipped by reason, the label histogram, and the output's absolute
  path and size. Never a row.

Run it. To check the file, parse it and print counts; never print its lines.

## 4. Check

- 160 to 10,000 rows and at least 2 labels. Under 160: not enough yet, go to step 6.
- Labels that differ only by case, spacing or spelling (`Billing`, `billing `, `billing_issue`) are
  probably drift in the stored data. Show the variants with their counts and propose a merge map;
  merge only with the user's OK (in the script, then rerun and recheck).
- One label above about 95% of rows: warn that the report will mostly measure that label.

## 5. Ask, then upload

Show the user exactly this and stop:
- the absolute path of `nogpu.jsonl`, its row count and size, and the label histogram;
- what happens on yes: every row (text and label) in that file is uploaded to https://nogpu.dev,
  used to train and test a CPU model for this report, and deleted within 24 hours (or at once with
  `nogpu_delete`). Nothing else from the project is sent;
- the question: "Upload these N rows to nogpu.dev for the free report? (yes/no)".

Only after an explicit yes, call `nogpu_report` with the absolute path and `source`: `history` (rows
from stored data or logs), `logging` (rows from the step 6 logger) or `file` (a file the user gave).
Report the share answered alone, accuracy when sure, its cp95 lower bound, latency and `report_url`.
Keep `job_id` and `claim_token`: `nogpu_delete` needs them.

If the `nogpu_report` tool is unavailable, use the API with curl (same rules, same yes first):
```bash
curl -sS -H 'X-NoGPU-Source: history' -F file=@/abs/path/nogpu.jsonl https://nogpu.dev/api/jobs
# -> {"job_id": ..., "claim_token": ...}; poll every 3 s until it has "report" or status "failed":
curl -sS -H "X-Claim-Token: $TOKEN" https://nogpu.dev/api/jobs/$JOB_ID
# report page: https://nogpu.dev/job/$JOB_ID#$TOKEN ; delete now:
curl -sS -X DELETE -H "X-Claim-Token: $TOKEN" https://nogpu.dev/api/jobs/$JOB_ID
```

## 6. No history yet: log decisions locally

Ask before changing the app. Then add a logger at the call site so rows build up:
- AI SDK (JS/TS): copy `collect.mjs` from this skill's base directory into the app and wrap the model:
  `wrapLanguageModel({ model, middleware: nogpuMiddleware() })`, or `nogpuDecisions(model)` for
  `experimental_decide`. It appends to `.nogpu/<decision>.jsonl`, stops at 10,000 rows and makes no
  network call. On serverless hosts (Vercel, Lambda) local files vanish: pass
  `nogpuMiddleware({ onRow: (row, name) => ... })` and store rows in the app's own database instead.
- Python or another language: write the equivalent few lines right after the call returns, in the
  app's language, so a logging failure never breaks the call:
  ```python
  try:
      os.makedirs(".nogpu", exist_ok=True)
      with open(".nogpu/route.jsonl", "a") as f:
          f.write(json.dumps({"text": ticket_text, "label": category}) + "\n")
  except Exception:
      pass
  ```

Add `.nogpu/` to `.gitignore`. Tell the user the AI SDK logger prints a line at 160 rows; then ask
again ("get me a NoGPU report") and upload that file with `source: "logging"`.
