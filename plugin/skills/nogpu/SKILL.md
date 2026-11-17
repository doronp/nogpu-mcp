---
name: nogpu
description: Get a free NoGPU report on how many of an app's recurring LLM classification or routing decisions (intent routing, ticket triage, moderation or "pick one category" calls) a small CPU model can take alone. Finds the call site, extracts its past decisions locally into nogpu.jsonl without printing any row text, and uploads only after the user says yes. Use when the user asks for a NoGPU report, asks whether a CPU or small model could take over their LLM's classification calls, or wants to cut LLM cost or latency on intent routing, ticket triage or another label-picking call, or says "install my NoGPU model" after buying one.
---

# NoGPU report

NoGPU (https://nogpu.dev) trains a small CPU model on an app's past LLM decisions and reports the
share it can answer alone. That share is certified only when the one-sided 95% Clopper-Pearson lower
bound of its accuracy on held-out rows is at least 90%. The report is free; keeping the model is $99
once (a new label set means a new upload and a new $99 model).
It needs 160 to 10,000 distinct rows of `{"text": ..., "label": ...}` with at least 2 labels. Uploaded
data is deleted within 24 hours (if the user buys the model, within 24 hours of payment), or at once
with `nogpu_delete`. Work from the root of the user's project.

## Hard rules (privacy)

The rows are the app's users' data, and anything you print enters this conversation.
- Read code, schemas and counts only. Never print row text: no `SELECT *`, no `head`, `cat`, `tail`
  or `less` on data, logs or `nogpu.jsonl`, no sample rows "to check the format". Use `COUNT(*)`,
  `GROUP BY`, `PRAGMA table_info`, `\d table`, ORM models, migrations and type definitions.
- Labels can hold text too (a stray LLM reply stored in the label column). Before printing any
  label, count the distinct labels and the longest label's length. More than 50 distinct labels
  that are also more than 30% of the rows: stop, these look like free text, not a decision (77
  intents over a few thousand rows is fine). Print a label by name only if it has at least 3 rows
  and at most 40 characters; fold the rest into one line, "N other labels (k rows)". This rule
  applies to every histogram and variant list below.
- The extraction writes `nogpu.jsonl` on this machine and prints only the row count, the distinct
  row count, the label histogram (rule above), the skipped rows by reason, and the file's absolute
  path and size.
- Nothing leaves the machine until the user answers yes to the question in step 5. Never upload in
  the turn where you ask. In a non-interactive run, stop after asking.
- Keep `nogpu.jsonl`, `nogpu_extract.*` and `.nogpu/` out of git: if the project is a git repo, add
  them to `.gitignore`.
- Code, schemas, counts and label names you read go to this coding agent's model provider, as
  everything in this conversation does; row text must not.

## 1. Find the decision

Search the code for a recurring LLM call whose answer is one label from a fixed set:
- AI SDK: `Output.choice(`, `experimental_decide`, `generateObject` with `output: 'enum'`, or a
  `z.enum` as the only field of the output schema.
- OpenAI: a `response_format` / `text.format` JSON schema with an `enum`, a `tool_choice` that forces
  one function whose argument is an enum, or `.parse(` with an Enum or Literal field.
- Anthropic: a forced `tool_use` (`tool_choice: {"type": "tool", ...}`) with an enum in `input_schema`.
- LangChain / Pydantic: `with_structured_output(` on a model with a `Literal[...]` or `Enum` field.
- Any prompt matching `rg -i 'classif(y|ication)\b.{0,60}\binto\b|reply with (only )?one of|respond with only'`.

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
- Volume and model: count rows per day over the last 30 days (from `created_at`, or the log's
  timestamp span) and note the model at the call site. If a model column shows several models, list
  them with counts; if the current model alone has at least 160 rows, extract only its rows and say
  so, e.g. 'using only the 515 gpt-4o-mini rows; say "all" to include the 185 gpt-3.5 rows'.
- Raw LLM logs or an observability export: convert them locally (stdlib Python, nothing is sent):
  `mkdir -p .nogpu && curl -sS -o .nogpu/nogpu_export.py https://nogpu.dev/static/nogpu_export.py &&
  (sha256sum .nogpu/nogpu_export.py || shasum -a 256 .nogpu/nogpu_export.py) && python3 -I .nogpu/nogpu_export.py <logs> > nogpu.jsonl 2> .nogpu/export.log`
  Its log can quote rare labels, so print only its `kept ...` line (grep), never the whole log; then
  check `nogpu.jsonl` with a count-only parse (no `nogpu_extract` needed; repeats are fine, the server
  drops them; at most 10,000 rows) and go to step 4.
  (`--input-field` / `--label-field` for unusual shapes). Always redirect its stdout to the file.
- Nothing stored: go to step 6.

## 3. Extract locally

Write `nogpu_extract.<ext>` at the project root, in the app's language, using its own database
driver or files, read-only. It must:
- write `nogpu.jsonl` with one `{"text": ..., "label": ...}` object per line;
- skip rows with empty text or label, and trim whitespace around labels;
- drop repeats as it reads, newest first (a text, lowercased with whitespace collapsed, with the
  same label counts once) and keep at most 10,000 distinct rows;
- print only: rows read, repeats dropped, rows written, rows skipped by reason, the label histogram
  (privacy rule above), and the output's absolute path and size. Never a row.

Run it. To check the file, parse it and print counts; never print its lines.

## 4. Check

- 160 to 10,000 rows, at least 160 of them distinct, and at least 2 labels. Under 160 distinct:
  not enough yet, go to step 6.
- Labels that are neither in the label set from step 1 nor a variant of one (e.g. a cut-off reply):
  drop them (in the script, or by filtering `nogpu.jsonl`) and report their count as a skip reason.
  No question needed.
- The server merges labels that differ only by case, underscores vs spaces, surrounding quotes,
  trailing punctuation or a `Category:`-style prefix, and lists the merges on the report: say "N look-alike
  labels will be merged automatically". Ask only about real spelling variants (`billing_issue` vs
  `billing`); merge those only with the user's OK (in the script, then rerun and recheck).
- One label above about 95% of rows: warn that the report will mostly measure that label.

## 5. Ask, then upload

Show the user exactly this and stop:
- the absolute path of `nogpu.jsonl`, its row count and size, and the label histogram;
- what happens on yes: every row (text and label) in that file is uploaded to https://nogpu.dev,
  used to train and test a CPU model for this report, and deleted within 24 hours (if they buy the
  model, within 24 hours of payment; or at once with `nogpu_delete`). Nothing else from the project
  is sent;
- the question: "Upload these N rows (D distinct) to nogpu.dev for the free report? (yes/no)".

Only after an explicit yes, call `nogpu_report` with the absolute path, `source` (`history`: rows
from stored data or logs; `logging`: rows from the step 6 logger; `file`: a file the user gave) and,
when known, the step 2 volume as `calls_per_day` and price as `usd_per_1k_calls` (estimate it as
(input + output tokens per call) × the model's $ per 1M tokens ÷ 1,000, and call it an estimate).
Then report:
- Say "matches your LLM" for `accuracy_when_sure`, never "accuracy" or "right", and give the bound as
  "certified: at least X% (95% confidence)" on `data.n_dev` rows it never saw (no "certified" if not
  fit). Mention `notes`. Never quote the `method` strings.
- `verdict` "fit": the share it answers alone, how often it matches your LLM, the bound and
  `bench.p50_ms` per decision. With volume N and price P known, then say: "At ~N calls/day on <model> (~$P per
  1,000 calls, estimate) it saves about $S a month and answers in X ms instead of an LLM round trip.
  Keeping it is $99 once: <report_url>." (S = N × 30 × share × P / 1,000.) If S × 12 < 99, say plainly
  that the gain is latency, not cost, and that $99 pays back within a year only above
  ~99 / (365 × share × P / 1,000) calls/day. Link `report_url` (it has the $99 button and carries the
  claim token), not `checkout_url`; if `checkout_url` is null, skip the $99 offer. End with: "After you
  buy, say 'install my NoGPU model': I'll download it with your claim token and put it in front of
  <file:line>, with <model> as the fallback."
- `verdict` "not_fit": start with "Not a fit on this data yet". Give the share and the bound against
  the bar (`gate.bar`) and, if `gate.n_sure_needed` is set, roughly how many rows would clear it
  (`n_sure_needed` × `data.n_rows` / `gate.n_sure`, rounded up). Offer the step 6 logger. Never call it
  a saving.

Keep `job_id` and `claim_token`: `nogpu_delete` and step 7 need them. On delete, also `rm nogpu.jsonl`
(and `.nogpu/` if the exporter made it) and tell the user both copies are gone.

If the `nogpu_report` tool is unavailable, use the API with curl (same rules, same yes first;
`SOURCE` is history, logging or file as above):
```bash
curl -sS -H "X-NoGPU-Source: $SOURCE" -F file=@/abs/path/nogpu.jsonl https://nogpu.dev/api/jobs
# -> {"job_id": ..., "claim_token": ...}; poll every 3 s until it has "report" or status "failed":
curl -sS -H "X-Claim-Token: $TOKEN" https://nogpu.dev/api/jobs/$JOB_ID
# report page: https://nogpu.dev/job/$JOB_ID?vol=$CALLS_PER_DAY&price=$USD_PER_1K#$TOKEN (no ?... if unknown); delete now:
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

In a git repo, add `.nogpu/` to `.gitignore`. Check the logger works before leaving it: run the app's
own test or one real call, then count the lines in the `.nogpu/` file (it must have grown; print the
count only).
If it did not, the call site is not one the snippet recognises: log in the app's code instead, as for
Python. Tell the user the AI SDK logger prints a line at 160 rows; then ask
again ("get me a NoGPU report") and upload that file with `source: "logging"`.

## 7. After purchase: install the model

When the user has paid and says "install my NoGPU model", download the bundle within 24 hours of
payment with the claim token (or the part after `#` in their report link):
```bash
curl -sS -o nogpu-model.tar.gz -w '%{http_code}\n' -H "X-Claim-Token: $TOKEN" https://nogpu.dev/api/jobs/$JOB_ID/bundle
# 200: tar -xzf nogpu-model.tar.gz && rm nogpu-model.tar.gz (makes nogpu/); 402: not paid yet; 409: still building, retry in a minute
```
Read the docstring at the top of `nogpu/serve.py` and run `python3 nogpu/serve.py --selftest` (needs
Python 3 and the packages in `nogpu/README.md`). Ask before changing the app, then put it in front
of the step 1 call site: send the text built exactly as the extraction built it, use `label` when
`gate` is `"answer"`, and otherwise (or on any error or timeout) call the LLM as today. In Python import
`serve` in-process; elsewhere run `serve.py --http` locally or pipe JSONL to it. Then run the app's own
test or one real call.
