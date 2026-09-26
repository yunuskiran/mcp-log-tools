# mcp-log-tools

A **Model Context Protocol server** that lets a model answer "what is broken and why" against a
request/telemetry index — written to make the *guardrails* visible rather than the plumbing.

Synthetic data, stdlib only, no MCP SDK, runs offline.

```bash
python3 client_demo.py      # spawns the server over stdio and exercises every tool
```

## Why write it without the SDK

The official SDKs are the right choice in production — I used the C# one for the version of this
that runs against real telemetry. But a decorator hides the two things worth understanding: the
handshake (`initialize` → `tools/list` → `tools/call`) and the fact that **a tool schema is a
security boundary**, not just documentation. So here the JSON-RPC is in plain sight, in about 250
lines.

## The three tools

| Tool | What it answers |
|---|---|
| `summarise_errors` | "What is failing right now?" — failed rows grouped by service, partner and error kind, most frequent first |
| `search_requests` | "Find me the rows that look like this" — exact-match filters inside a time window |
| `get_trace` | "Show me one request end to end" — every row on a trace id, elapsed time, which step failed |

They are deliberately narrow. A single `run_query` tool would be more flexible and much worse: the
model would compose queries you never anticipated, and every guardrail below would have to be
re-implemented as string inspection.

Typical path: `summarise_errors` to see the shape of the problem → `search_requests` to isolate rows
→ `get_trace` to follow one request. That progression is written into the tool descriptions, because
the descriptions are the only place the model learns how the tools relate.

## The guardrails, which are the actual content

Output from `client_demo.py`:

```
row cap:          asked for 2 -> returned 2 of 15 matched, truncated=True
                  (and any request above the hard ceiling of 100 is clamped, not honoured)
default window:   no dates given -> 2026-09-01T04:19:12 .. 2026-09-03T04:19:12 (12 rows)
unknown filter:   isError=True  tool error: unsupported filter(s): error
bad window:       isError=True  tool error: from_utc is after to_utc
unknown tool:     unknown tool: delete_everything
payload redaction: payload_path never leaves the server; only payload_available does
```

**1. Exact-match filters instead of a query language.** The model supplies *values*, never
predicates, and unknown keys are rejected rather than ignored. There is no query string to escape —
which is an injection defence you cannot forget to apply. (In the production version the tools built
KQL, where every literal *had* to be escaped and the table name validated as a bare identifier. Not
needing to is better.)

**2. Row caps and time windows the model cannot override.** `max_rows` is clamped to a hard ceiling,
omitted dates fall back to a rolling window, and the look-back is bounded. A model asking for
everything gets a page and a `truncated: true` flag — and the server instructions tell it to narrow
the filters rather than ask again.

**3. The response says what was withheld.** `matched` vs `returned` vs `truncated` are separate
fields, so the model can tell "there were three" from "there were three hundred and you saw twenty".
Silent truncation is how a model ends up stating a confident wrong total.

**4. Field-level redaction in one place.** Payload locations never leave the server; rows carry
`payload_available: true` instead. The projection is applied centrally, so a new tool cannot leak the
field by forgetting to exclude it.

**5. Tool failures come back as results, not protocol errors.** A bad filter returns
`isError: true` with a message the model can act on, so it corrects itself inside the same turn.
Protocol-level errors are reserved for things a model cannot fix, like calling a tool that does not
exist.

**6. Server instructions set expectations, not personality.** The `initialize` response tells the
model that payload bodies are unavailable — so it cannot offer to fetch one — and that results may be
truncated. Constraints belong where the host can see them, not buried in a prompt.

## MCP or plain function calling?

Both work. The honest trade-off, having shipped both:

- **MCP** pays off when more than one host needs the same tools — an agent runtime today, an IDE
  assistant tomorrow — because the tool contract lives outside any one application. The cost is an
  extra process to host, secure and monitor, and remote hosts generally want HTTP rather than stdio.
- **Plain function/tool calling** is fewer moving parts when exactly one application uses the tools.
  The schemas sit next to the services and there is nothing extra to deploy.

The tool *design* is identical either way, which is the point: the schema, the caps and the
redaction are the engineering. The transport is a deployment decision.

## Layout

```
server.py                   MCP server over stdio: initialize, tools/list, tools/call
client_demo.py              a host: handshake, tool listing, one call per tool, guardrail probes
data/synthetic_logs.jsonl   15 synthetic rows across 3 services and 4 partners
```

## Scope

Data is synthetic — invented services (`orders-api`, `billing-worker`, `catalogue-sync`) and partners
(`northwind`, `lumen-pay`, `harbour`, `verdant`). Nothing here comes from any employer's system.

Not included: authentication, HTTP/SSE transport, pagination cursors, or a real datastore. Those are
what the production version has; this repo is about the tool contract.
