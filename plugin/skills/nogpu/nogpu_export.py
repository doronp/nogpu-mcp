#!/usr/bin/env python3
"""nogpu_export.py: turn LLM decision logs into NoGPU upload rows, locally.

Runs on your machine with the Python standard library only; nothing is sent
anywhere. Emits exactly two things per call, the last HUMAN message and the
decision the model returned, and fails closed: a row whose shape it does not
positively recognise is skipped, never dumped. System prompts, earlier turns,
tool results and request metadata are never written out.

    python3 nogpu_export.py logs.jsonl > nogpu.jsonl          # auto-detect
    python3 nogpu_export.py logs.json --input-field input.query --label-field output.route
    python3 nogpu_export.py chats.jsonl --label-field messages.-1.content
    cat logs.jsonl | python3 nogpu_export.py - > nogpu.jsonl

Recognised per row (JSONL, a JSON array, or an API list page such as Langfuse's
{"data": [...], "meta"} or OpenAI's {"object": "list", "data": [...]}; JSON-encoded
string fields are decoded): chat messages under messages / request.messages /
request.body.messages / request_body.messages / input.messages / inputs.messages /
input (OpenAI, Anthropic, LangChain and LangSmith turns); a Langfuse @observe
input {"args", "kwargs"}; a LangSmith chain run's inputs.question / query / input /
text. The decision comes from an OpenAI choices[0] message, Anthropic or Responses
API content (a forced tool_use block's input included), a tool call's arguments,
LangSmith outputs (generations, output), a plain string or number under output /
response / response_body / completion, or the model's reply as the chat's last
turn (OpenAI fine-tune JSONL). A JSON reply such as {"category": ..., "reasoning":
...} gives its one obvious label field (category, label, intent, class, route,
...). Already-converted {"text", "label"} rows pass through. Covers Langfuse
exports (observations, traces, API pages), LangSmith runs, Helicone exports made
with --include-body, and OpenAI stored completions joined with their messages.
Anything else: point --input-field / --label-field at the exact dotted paths
(negative indexes count from the end).

Spellings of one label are merged into its most frequent spelling: they may
differ in surrounding space, quotes, backticks or **, trailing punctuation, a
"Category:"-style prefix, "_" versus " ", and case. Every merge is listed on
stderr, with labels that have fewer than 5 rows and whether the file is ready
to upload (rows, distinct rows, size). No row text is printed to stderr.
Output: one {"id", "text", "label"} per line.
"""
import argparse
import collections
import itertools
import json
import re
import sys
import tempfile

MAX_LABEL_LEN = 200  # the server's limit (pipeline.py)
MAX_ROWS, MAX_BYTES = 10000, 20 * 1024 * 1024  # the server's upload limits (config.py)
FREE_LABELS, FREE_SHARE = 50, 0.3  # more labels than both means free text to the server (pipeline.py)
MIN_ROWS = 160  # ~25% are held out, and 39 held-out rows all right is the fewest that clear the 90% bar
RARE = 5  # labels with fewer rows are listed so typos and one-offs show
_WARNED = set()
SEEN = collections.Counter()  # field names and shapes seen, for the summary; never user text


def _warn_once(msg):
    if msg not in _WARNED:  # GLM P05-7: say so when a system prompt may ride along
        _WARNED.add(msg)
        print(msg, file=sys.stderr)
MSG_PATHS = ("messages", "request.messages", "request.body.messages",
             "request_body.messages", "input.messages", "inputs.messages", "input")
OUT_PATHS = ("output", "response.body", "response", "response_body", "completion",
             "outputs.output", "outputs")
SKIP_BLOCKS = ("tool_result", "tool_use", "function_call_output", "image", "image_url",
               "input_image", "thinking", "redacted_thinking", "document")
LABEL_KEYS = ("label", "category", "intent", "class", "route", "classification",
              "decision", "answer", "sentiment", "topic")
TEXT_KEYS = ("input", "question", "query", "text")  # a function's or a chain's human-text argument
CHAT_ROLES = ("user", "human", "assistant", "ai", "system")
MODEL_ROLES = ("assistant", "ai")
PREFIX = re.compile(r"^(?:%s)\s*:\s*" % "|".join(LABEL_KEYS), re.I)  # "Category: billing"
FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.I)  # a reply's ```json ... ``` wrapper
INDEX = re.compile(r"-?\d+")
IDENT = re.compile(r"[A-Za-z_][\w-]{0,40}")  # a field name; anything else may be user text


def decode(v):
    """Logs often store JSON as a string (LiteLLM spend logs, CSV exports), and models
    often wrap a JSON reply in a ```json fence."""
    s = FENCE.sub("", v) if isinstance(v, str) and v.lstrip().startswith("```") else v
    if isinstance(s, str) and s[:1] in "[{" and s[-1:] in "]}":
        try:
            return json.loads(s)
        except ValueError:
            return v
    return v


def dig(obj, path, text=False):
    """The value at a dotted path; text=True keeps a string that holds a JSON object (an input
    the user chose as the text) instead of decoding it."""
    for part in path.split("."):
        obj = decode(obj)
        if isinstance(obj, list) and INDEX.fullmatch(part):
            k = int(part)
            obj = obj[k] if -len(obj) <= k < len(obj) else None
        elif isinstance(obj, dict):
            obj = obj.get(part)
        else:
            return None
    v = decode(obj)
    return obj if text and isinstance(v, dict) else v


def lc(m):
    """A LangChain-serialized message, as LangSmith stores them ({"lc": 1, "type":
    "constructor", "id": [..., "HumanMessage"], "kwargs": {"content": ...}}), as a plain one."""
    if isinstance(m, dict) and m.get("type") == "constructor" and isinstance(m.get("kwargs"), dict):
        ids = m.get("id")
        cls = str(ids[-1]) if isinstance(ids, list) and ids else ""
        return dict(m["kwargs"], type={"HumanMessage": "human", "AIMessage": "ai"}.get(cls, cls))
    return m


def role(m):
    return (m.get("role") or m.get("type")) if isinstance(m, dict) else None


def is_chat(v):
    return isinstance(v, list) and any(role(lc(m)) in CHAT_ROLES for m in v)


def block_text(content):
    """Plain text of a message's content; tool results, images, thinking dropped."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [p.get("text") for p in content
                 if isinstance(p, dict) and p.get("type") not in SKIP_BLOCKS
                 and isinstance(p.get("text"), str)]
        return " ".join(t.strip() for t in parts if t.strip())
    return ""


def last_human(messages):
    """Text of the last human turn. A user-role turn that only carries tool
    results is the harness talking, not the human, so the search continues."""
    if not isinstance(messages, list):
        return None
    if messages and all(isinstance(m, list) for m in messages):  # LangSmith batches: [[msg, ...]]
        messages = messages[0]
    for m in reversed(messages):
        m = lc(m)
        if role(m) in ("user", "human"):
            text = block_text(m.get("content")) or block_text(m.get("content_parts"))  # OpenAI stored
            if text:
                return text
    return None


def asked_before(chat, k):
    """The last human turn before turn k, when turn k is the model's reply; else None.
    A fixed index into chats of mixed length never pairs a later question with an
    earlier reply this way (DS-13)."""
    m = lc(chat[k]) if -len(chat) <= k < len(chat) else None
    if role(m) not in MODEL_ROLES:
        SEEN["not_reply"] += 1
        return None
    return last_human(chat[:k])


def chat_turn(row, path):
    """(chat, N) when --label-field indexes into a chat (messages.N...), else None. A list
    with no human turn (a Responses API output list) is not the chat: the text is found as usual."""
    parts = path.split(".")
    for j in range(1, len(parts)):
        if INDEX.fullmatch(parts[j]):
            seq = dig(row, ".".join(parts[:j]))
            if is_chat(seq) and any(role(lc(m)) in ("user", "human") for m in seq):
                return seq, int(parts[j])
    return None


def named_text(d):
    """The one human-text argument of a function or chain call; None when unclear."""
    found = [d[k].strip() for k in TEXT_KEYS if isinstance(d.get(k), str) and d[k].strip()]
    return found[0] if len(found) == 1 else None


def scalar(v):
    if isinstance(v, bool) or v is None:
        return None if v is None else str(v).lower()
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, str):
        return v.strip() or None
    return None


def pick(d, reply=False, at=None):
    """The decision inside a structured value: its one obvious label field, or, for a
    reply the model wrote (JSON text, tool arguments), its only field. None when unclear.
    A label object ({"label": {"intent", "confidence"}}) gives its own label field; at=""
    is a whole row, so the hint can name --label-field label.<field>."""
    keys = [k for k in d if str(k).lower() in LABEL_KEYS]
    if len(keys) != 1:
        keys = list(d) if reply and len(d) == 1 else []
    if not keys:
        if reply:
            SEEN["nokey", at, tuple(sorted(map(str, d)))[:6]] += 1
        return None
    if isinstance(d[keys[0]], dict) and (at == "" or reply):
        return pick(d[keys[0]], reply=True, at=str(keys[0]) if at == "" else at)
    label = scalar(d[keys[0]])
    if label is not None:
        SEEN["key", str(keys[0])] += 1
    return label


def reply(content):
    """The decision in a reply's content: its text, the label field of a JSON reply, or
    the input of its one tool_use block (an Anthropic tool call; text beside it is preamble)."""
    if isinstance(content, list):
        uses = [b for b in content if isinstance(b, dict) and b.get("type") == "tool_use"]
        if len(uses) == 1 and isinstance(uses[0].get("input"), dict):
            return pick(uses[0]["input"], reply=True)
    text = block_text(content)
    obj = decode(text)
    if isinstance(obj, dict):
        return pick(obj, reply=True)
    return None if cut_json(obj) else text or None


def cut_json(s):
    """A reply that starts as JSON but does not decode was cut off mid-answer: never a label."""
    if isinstance(s, str) and FENCE.sub("", s).lstrip()[:1] in ("{", "["):
        SEEN["cut_json"] += 1
        return True
    return False


def decision(v):
    """The model's decision, from shapes we recognise; None for anything else."""
    v = decode(v.strip() if isinstance(v, str) else v)  # a reply in a plain string field ("output")
    if cut_json(v):
        return None
    s = scalar(v)
    if s is not None or not isinstance(v, (dict, list)):
        return s
    if isinstance(v, list):
        items = [o for o in v if isinstance(o, dict) and o.get("type") in ("message", "function_call")]
        return decision(items[-1]) if items else reply(v)  # Responses API items / content blocks
    msg = dig(v, "choices.0.message")
    msg = lc(msg if isinstance(msg, dict) else v)  # else a bare assistant message (Langfuse, LangChain)
    content = msg.get("content")
    got = reply(content) if isinstance(content, (str, list)) else None
    if got:
        return got
    call = dig(msg, "tool_calls.0")  # OpenAI {"function": {name, arguments}}, LangChain {name, args}
    call = call.get("function", call) if isinstance(call, dict) else msg if msg.get("type") == "function_call" else None
    if isinstance(call, dict):
        args = decode(call.get("arguments", call.get("args")))
        return pick(args, reply=True) if isinstance(args, dict) and args else scalar(call.get("name"))
    if isinstance(v.get("output"), list):  # Responses API object
        return decision(v["output"])
    gen = dig(v, "generations.0.0")  # LangSmith llm run outputs: [[ChatGeneration]]
    if isinstance(gen, dict):
        return decision(gen.get("message")) or scalar(gen.get("text"))
    msgs = v.get("messages")  # a LangGraph state: the last turn, when it is the model's
    if is_chat(msgs) and role(lc(msgs[-1])) in MODEL_ROLES:
        return decision(msgs[-1])
    return pick(v)


def find_text(row):
    for p in MSG_PATHS:
        text = last_human(dig(row, p))
        if text:
            return text
    inp = dig(row, "input")
    if isinstance(inp, dict) and isinstance(inp.get("args"), list) and isinstance(inp.get("kwargs"), dict):
        args = inp["args"]  # Langfuse @observe: the decorated function's arguments
        text = named_text(inp["kwargs"])
        if not text and len(args) == 1 and isinstance(args[0], str):
            text = scalar(args[0])
            _warn_once("note: an @observe function's one argument is kept whole; if it carries your system "
                       "prompt, re-run with --input-field pointing at the user text only")
        return text
    inputs = dig(row, "inputs")
    if isinstance(inputs, dict):  # a LangSmith chain run
        return named_text(inputs)
    for key in ("input", "prompt"):  # decoded like every other path; a dict here is unrecognised -> skip
        raw = dig(row, key)
        text = last_human(raw) if isinstance(raw, list) else scalar(raw)
        if text:
            if key == "prompt" and not isinstance(raw, list):
                _warn_once("note: a plain 'prompt' string is kept whole; if your logs put the system "
                           "prompt inside it, re-run with --input-field pointing at the user text only")
            return text
    return named_text(row)  # question / query / text, incl. already-converted rows


def convert(row, i, input_field=None, label_field=None):
    row = decode(row)
    if not isinstance(row, dict):
        return None
    turn, cut = None, SEEN["cut_json"]
    if label_field:
        label = decision(dig(row, label_field))
        turn = chat_turn(row, label_field)
    else:
        label = None
        for p in OUT_PATHS:
            got = dig(row, p)
            if got is not None:
                label = decision(got)
                if label:  # an unrecognised field (metadata) does not stop the search
                    break
        if label is None and "choices" in row:
            label = decision(row)
        if label is None:  # a chat that ends with the model's reply (OpenAI fine-tune JSONL)
            chat = next((c for c in (dig(row, p) for p in MSG_PATHS) if is_chat(c)), None)
            if chat and role(lc(chat[-1])) in MODEL_ROLES:
                label, turn = decision(chat[-1]), (chat, -1)
        if label is None:
            label = pick(row, at="")  # {"query", "intent"}, {"text", "label"}, ...
    if input_field:
        raw = dig(row, input_field, text=True)
        text = last_human(raw) if isinstance(raw, list) else scalar(raw)
    elif turn:
        text = asked_before(*turn)
    else:
        text = find_text(row)
    if not text:
        if row.get("object") == "chat.completion":
            SEEN["stored"] += 1  # an OpenAI stored completion: the list API leaves the prompt out
        elif "signed_body_url" in row and "request_body" not in row:
            SEEN["no_body"] += 1  # a Helicone export without --include-body
    SEEN["cut_json"] = cut + (SEEN["cut_json"] > cut and not label)  # once per row it skips
    if not text or not label or len(label) > MAX_LABEL_LEN:
        return None
    rid = row.get("id", row.get("request_id", i))
    if isinstance(rid, bool) or not isinstance(rid, (str, int, float)) or str(rid) == "":
        rid = i  # an id we do not positively recognise is metadata: never written out
    return {"id": str(rid)[:128], "text": text, "label": label}


def same(label):
    """Labels with equal keys are one label: space, quotes, backticks, ** and trailing
    punctuation dropped, a "Category:"-style prefix dropped, "_" read as " ", case ignored.
    The server's _label_key (pipeline.py) is the same rule; .lower() keeps 'Maße' and 'Masse' apart."""
    t = PREFIX.sub("", label.strip().strip("\"'`*").strip())
    t = t.strip("\"'`* ").rstrip(".,;:!?").strip("\"'`* ") or label
    return " ".join(t.replace("_", " ").split()).lower()


def merge_labels(counts):
    """{label: the spelling it becomes}: each group's most frequent spelling, so a label
    with no variants keeps its exact name (Banking77's 'reverted_card_payment?')."""
    best = {}
    for label, _ in counts.most_common():
        best.setdefault(same(label), label)
    return {label: best[same(label)] for label in counts}


def pages(obj):
    """An API list page (Langfuse {"data": [...], "meta"}, OpenAI {"object": "list",
    "data": [...]}) is its rows; anything else is one row."""
    if isinstance(obj, dict) and isinstance(obj.get("data"), list) and (
            "meta" in obj or obj.get("object") == "list"):
        return obj["data"]
    return [obj]


def rows(path, stats=None):
    """Bytes in, like the server: a BOM is stripped and a torn or non-UTF-8 line is
    counted and skipped instead of crashing halfway through the output."""
    stats = {} if stats is None else stats
    f = sys.stdin.buffer if path == "-" else open(path, "rb")
    with f:
        first = f.read(1)
        if first == b"\xef":  # UTF-8 BOM
            first = first + f.read(2)
            first = b"" if first == b"\xef\xbb\xbf" else first
            first = first or f.read(1)
        if first and first in b"\xff\xfe" and first + f.read(1) in (b"\xff\xfe", b"\xfe\xff"):
            stats["utf16"] = True  # PowerShell's > writes UTF-16: say so, not 'CSV?'
            return
        while first and first.isspace():
            first = f.read(1)
        if first and first not in b"[{":  # CSV or plain text: say so, not 'N bad lines'
            stats["not_json"] = True
            return
        line = first + f.readline()
        data = None
        if first == b"[":  # a JSON array has to be read whole
            data = line + f.read()
        elif first == b"{":
            try:
                json.loads(line)
            except ValueError:  # a pretty-printed JSON document (an API page), not JSONL
                data = line + f.read()
        if data is not None:
            try:
                doc = json.loads(data)
            except ValueError as e:  # JSONL whose rows are arrays, or a torn first line: per line (GLM P05-8)
                if first == b"{" and str(e).startswith("Extra data"):
                    stats["pretty"] = True  # jq . output: several pretty-printed objects, one after another
                lines = data.splitlines()
            else:
                yield from doc if isinstance(doc, list) else pages(doc)
                return
        else:
            lines = itertools.chain([line], iter(f.readline, b""))
        for line in lines:  # JSONL streams: multi-GB logs never sit in memory
            if line.startswith(b"\xef\xbb\xbf"):
                line = line[3:]
            if line.strip():
                try:
                    yield from pages(json.loads(line))
                except ValueError:  # not JSON, or bad UTF-8 (UnicodeDecodeError is a ValueError)
                    stats["bad_lines"] = stats.get("bad_lines", 0) + 1


SECRET = "SECRET-SYSTEM-PROMPT"
DEMO = [  # every shape a hostile review found (Argon, 2026-11-13), plus the basics
    ({"id": "a", "messages": [{"role": "system", "content": SECRET}, {"role": "user", "content": "refund my order"}],
      "response": {"choices": [{"message": {"content": "refunds"}}]}}, ("refund my order", "refunds")),
    ({"id": "lc", "input": [{"type": "system", "content": SECRET}, {"type": "human", "content": "refund order"}],
      "output": "refunds"}, ("refund order", "refunds")),
    ({"id": "kw", "input": {"system": SECRET, "kwargs": {"api_key": "sk-live-123"}}, "output": "allow"}, None),
    ({"id": "tr", "messages": [{"role": "user", "content": "is this spam?"},
                               {"role": "assistant", "content": [{"type": "tool_use", "name": "lookup"}]},
                               {"role": "user", "content": [{"type": "tool_result", "content": "INTERNAL_DB_ROW"}]}],
      "response": {"content": [{"type": "text", "text": "spam"}]}}, ("is this spam?", "spam")),
    ({"id": "big", "messages": [{"role": "user", "content": "x"}],
      "response": {"id": "r", "usage": {"secret_header": SECRET}}}, None),
    ({"request_id": "ll", "messages": json.dumps([{"role": "system", "content": SECRET}, {"role": "user", "content": "hi"}]),
      "response": json.dumps({"choices": [{"message": {"content": "greet"}}]})}, ("hi", "greet")),
    ({"request_id": "h1", "request_body": {"messages": [{"role": "system", "content": SECRET}, {"role": "user", "content": "cancel"}]},
      "response_body": {"choices": [{"message": {"content": "churn"}}]}}, ("cancel", "churn")),
    ({"id": "p1", "request": {"body": {"messages": [{"role": "user", "content": "cancel"}]}},
      "response": {"body": {"choices": [{"message": {"content": "churn"}}]}}}, ("cancel", "churn")),
    ({"id": "o1", "messages": [{"role": "user", "content": "cancel"}],
      "choices": [{"message": {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "churn"}}]}}]},
     ("cancel", "churn")),
    ({"id": "one", "messages": [{"role": "user", "content": "where is my card"}],
      "choices": [{"message": {"content": None, "tool_calls": [{"function": {
          "name": "classify", "arguments": json.dumps({"route": "card_arrival"})}}]}}]}, ("where is my card", "card_arrival")),
    ({"id": "lf1", "input": [{"role": "user", "content": "refund"}],
      "output": {"role": "assistant", "content": None, "tool_calls": [{"function": {"name": "refunds"}}]}}, ("refund", "refunds")),
    ({"id": "ws", "messages": [{"role": "user", "content": [{"type": "image_url"}, {"type": "image_url"}]}],
      "output": "x"}, None),
    ({"id": "zero", "input": "legit", "output": 0}, ("legit", "0")),
    ({"id": "long", "input": "q", "output": "w" * 500}, None),
    ({"id": "none", "input": "hello", "output": None}, None),
    ({"id": {"env": {"api_key": "sk-live-123", "system": SECRET}}, "input": "hi", "output": "greet"}, ("hi", "greet")),
    ({"id": "enc", "prompt": json.dumps({"system": SECRET, "user": "refund"}), "completion": "refunds"}, None),
    ({"id": "ls", "inputs": {"messages": [[{"lc": 1, "type": "constructor", "id": ["SystemMessage"], "kwargs": {"content": SECRET}},
                                           {"lc": 1, "type": "constructor", "id": ["HumanMessage"], "kwargs": {"content": "refund"}}]]},
      "outputs": {"generations": [[{"text": "refunds"}]]}}, ("refund", "refunds")),
    ({"id": "sys", "messages": [{"role": "system", "content": SECRET}, {"role": "assistant", "content": "x"}]}, None),
    ({"id": "obs", "input": {"args": [SECRET, "refund"], "kwargs": {}}, "output": "refunds"}, None),
]


def demo():
    for row, want in DEMO:
        got = convert(row, 0)
        assert (got and (got["text"], got["label"])) == (want or None), (row.get("id", row.get("request_id")), got)
        if got:
            assert SECRET not in json.dumps(got) and "INTERNAL_DB_ROW" not in json.dumps(got)
            assert got["text"].strip() and 1 <= len(got["label"]) <= MAX_LABEL_LEN
    assert convert(DEMO[0][0], 1, None, "response.choices.0.message.content")["label"] == "refunds"
    assert convert({"input": {"query": "card?"}, "output": {"route": "card_arrival"}}, 2,
                   "input.query", "output.route") == {"id": "2", "text": "card?", "label": "card_arrival"}
    print(f"demo ok ({len(DEMO)} shapes, no secret leaked)")


def show(label):
    """A label as stderr lists it: at most 40 characters, like the server's notes."""
    return repr(label if len(label) <= 40 else label[:40] + "…")


def names(keys):
    """Field names to print: identifiers only, since a reply keyed by data would echo user text."""
    ok = sorted({k for k in keys if IDENT.fullmatch(k)})
    other = len(set(keys)) - len(ok)
    return (", ".join(ok[:12]) + (f" (+{other} not shown)" if other else "")).strip()


def summary(kept, skipped, repeats, counts, to, stats):
    """What happened, what to fix, and what to upload next: all on stderr, never user text."""
    say = lambda *m: print(*m, file=sys.stderr)
    final = collections.Counter()
    for label, n in counts.items():
        final[to[label]] += n
    distinct, size = stats.get("distinct", kept), stats.get("size", 0)
    free = (len(final) > FREE_LABELS and len(final) > FREE_SHARE * distinct) or (
        kept >= 20 and len(final) > kept // 2)
    say(f"kept {kept} row{'s' if kept != 1 else ''}, {len(final)} distinct labels, skipped {skipped}"
        + (f" (incl. {repeats} repeats of a decision within one trace)" if repeats else ""))
    if stats.get("utf16"):
        say("warning: this file is UTF-16 (PowerShell's > writes it): re-save it as UTF-8 and run again.")
    elif stats.get("not_json"):
        say("warning: this file is not JSON (CSV?). The exporter reads JSONL, a JSON array or an "
            "API page: export your logs as JSON or JSONL.")
    if stats.get("pretty"):
        say("warning: this file holds several pretty-printed JSON objects: use jq -c to write one per "
            "line (jq -c . logs.json > logs.jsonl) and run again.")
    elif stats["bad_lines"]:
        say(f"warning: {stats['bad_lines']} lines were not valid UTF-8 JSON and were skipped")
    if stats.get("bad_text"):
        say(f"warning: {stats['bad_text']} rows held a broken character (a lone surrogate escape) and were skipped")
    used = sorted((n, k[1]) for k, n in SEEN.items()
                  if isinstance(k, tuple) and k[0] == "key" and IDENT.fullmatch(k[1]))
    if used:
        say("label field used: " + ", ".join(f"'{k}' ({n} rows)" for n, k in reversed(used)))
    for at in (None, *sorted({k[1] for k in SEEN if isinstance(k, tuple) and k[0] == "nokey" and k[1]})):
        missed = [(n, k[2]) for k, n in SEEN.items() if isinstance(k, tuple) and k[0] == "nokey" and k[1] == at]
        if not missed:
            continue
        fields = names([f for _, keys in missed for f in keys])
        if at is None:
            say(f"warning: {sum(n for n, _ in missed)} structured replies had no single obvious label "
                f"field (fields seen: {fields}). Re-run with --label-field <path to the "
                "reply>.<field>, e.g. --label-field response.choices.0.message.content.<field>")
        else:  # a row's label object: {"text", "label": {"name", "score"}}
            say(f"warning: {sum(n for n, _ in missed)} rows have a '{at}' object with no single obvious "
                f"label field (fields seen: {fields}). Re-run with --label-field {at}.<field>")
    if SEEN["not_reply"]:
        say(f"warning: {SEEN['not_reply']} rows skipped because the turn --label-field points at is not "
            "the model's reply there; for the last reply use --label-field messages.-1.content")
    if SEEN["cut_json"]:
        say(f"warning: {SEEN['cut_json']} rows skipped: unparseable JSON reply (cut off mid-answer?); "
            "a broken reply is never used as a label")
    if SEEN["stored"]:
        say(f"warning: {SEEN['stored']} OpenAI stored completions have no prompt: the list API leaves "
            "the input messages out. Fetch each one's messages (GET /v1/chat/completions/{id}/messages), "
            "write one {\"messages\": [...], \"response\": <completion>} line per completion, and run "
            "this script on that file.")
    if SEEN["no_body"]:
        say(f"warning: {SEEN['no_body']} Helicone rows have no request/response body: "
            "re-run the Helicone export with --include-body.")
    merged = collections.defaultdict(list)
    for label, n in counts.most_common():
        if to[label] != label:
            merged[to[label]].append(f"{show(label)} ({n})")
    if merged:
        say(f"merged {sum(map(len, merged.values()))} label spellings that differ only in case, "
            "'_' vs ' ', quotes or **, a 'Category:'-style prefix or trailing punctuation:")
        for canon, spellings in ([] if free else list(merged.items())[:20]):  # free text: the model's replies
            say(f"  {show(canon)} ({final[canon]}) <- " + ", ".join(spellings))
        if len(merged) > 20 and not free:
            say(f"  ... and {len(merged) - 20} more labels with merged spellings")
    rare = [f"{show(label)} ({n})" for label, n in sorted(final.items(), key=lambda x: x[1]) if n < RARE]
    if rare and not free:  # free text would list the model's replies
        say(f"labels with fewer than {RARE} rows ({len(rare)}): " + ", ".join(rare[:20])
            + (", ..." if len(rare) > 20 else "") + ". Check they are not typos, or collect more rows for them.")
    if not kept:
        say("warning: no rows kept; point --input-field / --label-field at your fields")
        return
    if sys.stdout.isatty():
        say("note: the rows went to this terminal; re-run with '> nogpu.jsonl' to save them to a file.")
    if len(final) < 2:
        say("warning: fewer than 2 distinct labels; NoGPU needs at least 2.")
    elif free:
        say(f"warning: {len(final)} distinct labels in {distinct} distinct rows, so these look like free "
            f"text, not a decision (NoGPU turns down more than {FREE_LABELS} labels that are over "
            f"{FREE_SHARE:.0%} of the rows). Point --label-field at the route/class field.")
    elif distinct < MIN_ROWS:
        say(f"next: export more rows before uploading: {distinct} distinct rows (a repeated text and label "
            f"counts once). About 25% of rows are held out to measure the model, and fewer than about "
            f"{MIN_ROWS} rows cannot clear the 90% bar even if every answer is right. A few hundred rows "
            "per label is better.")
    elif kept > MAX_ROWS or size > MAX_BYTES:
        n = min(MAX_ROWS, kept * 9 * MAX_BYTES // (10 * max(size, 1)))  # 10% margin for rows of uneven length
        say(f"next: NoGPU takes at most {MAX_ROWS:,} rows and {MAX_BYTES >> 20} MB per upload, and this file "
            f"has {kept} rows, {size / 2**20:.1f} MB: upload a slice of about {n:,} rows (e.g. the most "
            "recent rows) at https://nogpu.dev/#try")
    else:
        n = min(final.values())
        say(f"next: get the free report (ask your coding agent, or upload at https://nogpu.dev/#try). {distinct} "
            f"distinct rows is enough to try; the smallest label has {n} row{'s' if n != 1 else ''}, and a few "
            "hundred rows per label is better.")


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("path", nargs="?", help="log file (JSONL or JSON array), or - for stdin")
    p.add_argument("--input-field", help="dotted path to the input, e.g. input.query")
    p.add_argument("--label-field", help="dotted path to the decision, e.g. output.route or messages.-1.content")
    p.add_argument("--demo", action="store_true", help="run the self-check")
    a = p.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")  # the server only takes UTF-8 (GLM r2 P06-S2)
    sys.stderr.reconfigure(errors="backslashreplace")  # label names on a non-UTF-8 console
    if a.demo:
        return demo()
    if not a.path:
        if a.input_field or a.label_field:
            p.error("give the log file (or - for stdin) before --input-field / --label-field")
        return demo()
    kept = skipped = repeats = 0
    stats = {"bad_lines": 0}
    counts = collections.Counter()
    # A span and the LLM call inside it log one decision twice (with the prompt template around its
    # text or not): within a trace, a non-LLM row is dropped once an LLM row has the same label.
    # Two LLM rows are two decisions unless their text is the same too (a batch job is one trace).
    spans, llm_keys, seen, drop, written = {}, set(), set(), set(), 0
    with tempfile.TemporaryFile("w+", encoding="utf-8") as tmp:  # labels are merged after the last row
        for i, r in enumerate(rows(a.path, stats)):
            out = convert(r, i, a.input_field, a.label_field)
            line = out and json.dumps(out, ensure_ascii=False)
            try:
                line and line.encode("utf-8")
            except UnicodeEncodeError:  # a lone surrogate escape ("\ud83d") is no text the server reads
                stats["bad_text"] = stats.get("bad_text", 0) + 1
                line = None
            if not line:
                skipped += 1
                continue
            trace = (r.get("trace_id") or r.get("traceId")) if isinstance(r, dict) else None
            if isinstance(trace, (str, int)):
                llm = str(r.get("type") or r.get("run_type") or "").lower() in ("generation", "llm")
                key = hash((str(trace), same(out["label"])))
                exact = hash((key, out["text"]))
                if exact in seen or (not llm and key in llm_keys):
                    repeats += 1
                    skipped += 1
                    continue
                seen.add(exact)
                if llm:
                    llm_keys.add(key)
                    for n, lab in spans.pop(key, ()):  # keep the LLM call's row: the text the model saw
                        drop.add(n)
                        kept -= 1
                        counts[lab] -= 1
                        repeats += 1
                        skipped += 1
                else:
                    spans.setdefault(key, []).append((written, out["label"]))
            kept += 1
            counts[out["label"]] += 1
            tmp.write(line + "\n")
            written += 1
        skipped += stats["bad_lines"]
        counts = +counts  # labels whose only rows were dropped
        to = merge_labels(counts)
        tmp.seek(0)
        distinct = set()
        stats["size"] = 0
        for n, line in enumerate(tmp):
            if n in drop:
                continue
            out = json.loads(line)
            out["label"] = to[out["label"]]
            line = json.dumps(out, ensure_ascii=False)
            stats["size"] += len(line.encode("utf-8")) + 1
            if len(distinct) <= MAX_ROWS:  # past the upload limit the count no longer matters
                distinct.add(hash((" ".join(out["text"].casefold().split()), out["label"])))  # as the server dedupes
            print(line)
        stats["distinct"] = len(distinct)
    summary(kept, skipped, repeats, counts, to, stats)
    if not kept:
        sys.exit(1)


if __name__ == "__main__":
    main()
