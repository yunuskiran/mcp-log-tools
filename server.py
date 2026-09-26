#!/usr/bin/env python3
"""A minimal Model Context Protocol server exposing read-only log tools over stdio.

    python3 client_demo.py            # spawns this server and exercises every tool
    python3 server.py                 # speaks newline-delimited JSON-RPC on stdin/stdout

Stdlib only, no MCP SDK, so the protocol is visible rather than hidden behind a decorator.
Three tools, each deliberately narrow:

    search_requests   filter the request index by exact-match fields and a time window
    get_trace         all rows sharing a trace id, ordered
    summarise_errors  grouped error counts, for "what is broken right now"

The guardrails are the interesting part, not the plumbing - see README.md.
"""
from __future__ import annotations

import json
import pathlib
import sys
from datetime import datetime, timedelta, timezone

HERE = pathlib.Path(__file__).parent
PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "mcp-log-tools", "version": "1.0.0"}

# --- guardrails ---------------------------------------------------------------------
MAX_ROWS = 100          # hard ceiling, whatever the model asks for
DEFAULT_ROWS = 20
DEFAULT_LOOKBACK_HOURS = 48
MAX_LOOKBACK_DAYS = 30
# A model may only filter on these fields, and only by exact value. Nothing the model
# sends is ever interpolated into a query string - see filter_rows().
FILTERABLE = ("correlation_id", "trace_id", "service", "operation", "partner", "status")
# Fields that never leave the server, even if a future tool forgets to project.
REDACTED = ("payload_path",)


def load_rows() -> list[dict]:
    path = HERE / "data" / "synthetic_logs.jsonl"
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


ROWS = load_rows()


def parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def now() -> datetime:
    """Latest row timestamp, so the demo behaves the same whenever it is run."""
    return max(parse_ts(r["ts"]) for r in ROWS)


def project(row: dict) -> dict:
    """Drop server-only fields and expose payload presence without the location."""
    out = {k: v for k, v in row.items() if k not in REDACTED}
    out["payload_available"] = bool(row.get("payload_path"))
    return out


def clamp_window(from_utc: str | None, to_utc: str | None) -> tuple[datetime, datetime]:
    end = parse_ts(to_utc) if to_utc else now()
    start = parse_ts(from_utc) if from_utc else end - timedelta(hours=DEFAULT_LOOKBACK_HOURS)
    if start > end:
        raise ValueError("from_utc is after to_utc")
    earliest = end - timedelta(days=MAX_LOOKBACK_DAYS)
    return max(start, earliest), end


def filter_rows(args: dict) -> list[dict]:
    """Exact-match filtering only.

    The model supplies values, never predicates: unknown keys are rejected rather than
    ignored, and values are compared with ==. There is no query string to escape, which
    is the point - an injection defence you cannot forget to apply.
    """
    unknown = [k for k in args if k not in FILTERABLE + ("from_utc", "to_utc", "max_rows")]
    if unknown:
        raise ValueError(f"unsupported filter(s): {', '.join(sorted(unknown))}")

    start, end = clamp_window(args.get("from_utc"), args.get("to_utc"))
    selected = []
    for row in ROWS:
        ts = parse_ts(row["ts"])
        if not (start <= ts <= end):
            continue
        if any(field in args and row.get(field) != args[field] for field in FILTERABLE):
            continue
        selected.append(row)
    selected.sort(key=lambda r: r["ts"])
    return selected


def cap(requested: int | None) -> int:
    if not requested or requested < 1:
        return DEFAULT_ROWS
    return min(int(requested), MAX_ROWS)


# --- tools --------------------------------------------------------------------------
def tool_search_requests(args: dict) -> dict:
    rows = filter_rows(args)
    limit = cap(args.get("max_rows"))
    start, end = clamp_window(args.get("from_utc"), args.get("to_utc"))
    return {
        "window": {"from_utc": start.isoformat(), "to_utc": end.isoformat()},
        "matched": len(rows),
        "returned": min(len(rows), limit),
        "truncated": len(rows) > limit,
        "rows": [project(r) for r in rows[:limit]],
    }


def tool_get_trace(args: dict) -> dict:
    trace_id = args.get("trace_id")
    if not trace_id:
        raise ValueError("trace_id is required")
    rows = [r for r in ROWS if r["trace_id"] == trace_id]
    rows.sort(key=lambda r: r["ts"])
    if not rows:
        return {"trace_id": trace_id, "spans": 0, "rows": [],
                "note": "no rows for this trace id in the retained window"}
    span = parse_ts(rows[-1]["ts"]) - parse_ts(rows[0]["ts"])
    return {
        "trace_id": trace_id,
        "spans": len(rows),
        "elapsed_seconds": span.total_seconds(),
        "failed": [r["id"] for r in rows if r["status"] >= 400],
        "rows": [project(r) for r in rows[: cap(args.get("max_rows"))]],
    }


def tool_summarise_errors(args: dict) -> dict:
    rows = [r for r in filter_rows(args) if r["status"] >= 400]
    grouped: dict[tuple[str, str, str], dict] = {}
    for row in rows:
        kind = (row["error"] or "").split(":", 1)[0] or "Unknown"
        key = (row["service"], row["partner"], kind)
        bucket = grouped.setdefault(key, {
            "service": row["service"], "partner": row["partner"], "error_kind": kind,
            "count": 0, "example_correlation_id": row["correlation_id"],
            "first_seen": row["ts"], "last_seen": row["ts"],
        })
        bucket["count"] += 1
        bucket["last_seen"] = row["ts"]
    start, end = clamp_window(args.get("from_utc"), args.get("to_utc"))
    groups = sorted(grouped.values(), key=lambda g: g["count"], reverse=True)
    return {
        "window": {"from_utc": start.isoformat(), "to_utc": end.isoformat()},
        "failed_rows": len(rows),
        "groups": groups[: cap(args.get("max_rows"))],
    }


COMMON_FILTERS = {
    "correlation_id": {"type": "string", "description": "Exact correlation id."},
    "trace_id": {"type": "string", "description": "Exact trace id."},
    "service": {"type": "string", "description": "Exact service name, e.g. orders-api."},
    "operation": {"type": "string", "description": "Exact operation name, e.g. ChargeCard."},
    "partner": {"type": "string", "description": "Exact partner key, e.g. lumen-pay."},
    "status": {"type": "integer", "description": "Exact HTTP status code."},
    "from_utc": {"type": "string", "description": f"ISO-8601 start. Omit for a rolling {DEFAULT_LOOKBACK_HOURS}h window."},
    "to_utc": {"type": "string", "description": "ISO-8601 end. Omit for the latest data."},
    "max_rows": {"type": "integer", "description": f"Row cap, clamped to {MAX_ROWS} (default {DEFAULT_ROWS})."},
}

TOOLS = [
    {
        "name": "search_requests",
        "description": ("Find request-index rows by exact field match inside a time window. Returns "
                        "metadata only - payload bodies are never returned, only whether one exists. "
                        "Start here, then follow a trace_id into get_trace."),
        "inputSchema": {"type": "object", "properties": COMMON_FILTERS, "additionalProperties": False},
    },
    {
        "name": "get_trace",
        "description": ("Every row sharing a trace id, in order, with elapsed time and the ids of any "
                        "failed steps. Use after search_requests to see a whole request path."),
        "inputSchema": {
            "type": "object",
            "properties": {"trace_id": COMMON_FILTERS["trace_id"], "max_rows": COMMON_FILTERS["max_rows"]},
            "required": ["trace_id"],
            "additionalProperties": False,
        },
    },
    {
        "name": "summarise_errors",
        "description": ("Failed rows grouped by service, partner and error kind, most frequent first - "
                        "the 'what is broken right now' view. Accepts the same filters as search_requests."),
        "inputSchema": {"type": "object", "properties": COMMON_FILTERS, "additionalProperties": False},
    },
]

HANDLERS = {
    "search_requests": tool_search_requests,
    "get_trace": tool_get_trace,
    "summarise_errors": tool_summarise_errors,
}


# --- JSON-RPC / MCP plumbing --------------------------------------------------------
def result(request_id, payload) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": payload}


def error(request_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def handle(message: dict) -> dict | None:
    method, request_id = message.get("method"), message.get("id")

    if method == "initialize":
        return result(request_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
            "instructions": (
                "Read-only access to a request/telemetry index. Prefer calling a tool over "
                "guessing. Payload bodies are not available through this server - never claim "
                "you can read one. Results may be truncated; check the 'truncated' flag and "
                "narrow the filters instead of asking for more rows."),
        })

    if method in ("notifications/initialized", "initialized"):
        return None  # notification: no response

    if method == "tools/list":
        return result(request_id, {"tools": TOOLS})

    if method == "tools/call":
        params = message.get("params") or {}
        name = params.get("name")
        handler = HANDLERS.get(name)
        if handler is None:
            return error(request_id, -32602, f"unknown tool: {name}")
        try:
            payload = handler(params.get("arguments") or {})
        except ValueError as exc:
            # Tool-level failure: report it inside the result so the model can correct itself,
            # rather than as a protocol error that aborts the turn.
            return result(request_id, {
                "content": [{"type": "text", "text": f"tool error: {exc}"}],
                "isError": True,
            })
        return result(request_id, {
            "content": [{"type": "text", "text": json.dumps(payload, indent=2)}],
            "isError": False,
        })

    if method == "ping":
        return result(request_id, {})

    return error(request_id, -32601, f"method not found: {method}")


def main() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            print(json.dumps(error(None, -32700, "parse error")), flush=True)
            continue
        response = handle(message)
        if response is not None:
            print(json.dumps(response), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
