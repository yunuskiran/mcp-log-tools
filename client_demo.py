#!/usr/bin/env python3
"""Drive server.py over stdio: handshake, list tools, call each one, show the guardrails.

    python3 client_demo.py

Stdlib only. This is what an MCP host does before a model ever sees a tool.
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys

from citations import check_citations

HERE = pathlib.Path(__file__).parent


class Client:
    def __init__(self) -> None:
        self.proc = subprocess.Popen(
            [sys.executable, str(HERE / "server.py")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1)
        self.next_id = 0

    def call(self, method: str, params: dict | None = None, notify: bool = False):
        message = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        if not notify:
            self.next_id += 1
            message["id"] = self.next_id
        self.proc.stdin.write(json.dumps(message) + "\n")
        self.proc.stdin.flush()
        if notify:
            return None
        return json.loads(self.proc.stdout.readline())

    def tool(self, name: str, arguments: dict) -> dict:
        response = self.call("tools/call", {"name": name, "arguments": arguments})
        payload = response["result"]
        text = payload["content"][0]["text"]
        return {"isError": payload.get("isError", False), "text": text}

    def close(self) -> None:
        self.proc.stdin.close()
        self.proc.wait(timeout=10)


def show(title: str, body: str) -> None:
    print(f"\n=== {title} ===")
    print(body if len(body) < 1400 else body[:1400] + "\n  … truncated for display")


def main() -> int:
    client = Client()

    handshake = client.call("initialize", {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "client_demo", "version": "1.0.0"},
    })["result"]
    client.call("notifications/initialized", {}, notify=True)
    show("initialize", json.dumps({
        "protocolVersion": handshake["protocolVersion"],
        "serverInfo": handshake["serverInfo"],
        "instructions": handshake["instructions"],
    }, indent=2))

    tools = client.call("tools/list")["result"]["tools"]
    show("tools/list", "\n".join(
        f"- {t['name']}: {t['description'].split('.')[0]}." for t in tools))

    show("summarise_errors  (what is broken, whole retained window)",
         client.tool("summarise_errors", {"from_utc": "2026-08-01T00:00:00Z"})["text"])

    show("search_requests  partner=lumen-pay status=502",
         client.tool("search_requests",
                     {"partner": "lumen-pay", "status": 502,
                      "from_utc": "2026-08-01T00:00:00Z"})["text"])

    show("get_trace  t-4410aa  (one request path end to end)",
         client.tool("get_trace", {"trace_id": "t-4410aa"})["text"])

    print("\n--- guardrails ---")
    capped = json.loads(client.tool(
        "search_requests", {"from_utc": "2026-08-01T00:00:00Z", "max_rows": 2})["text"])
    print(f"row cap:          asked for 2 -> returned {capped['returned']} of "
          f"{capped['matched']} matched, truncated={capped['truncated']} "
          f"(and any request above the hard ceiling of 100 is clamped, not honoured)")

    rolling = json.loads(client.tool("search_requests", {})["text"])
    print(f"default window:   no dates given -> {rolling['window']['from_utc']} .. "
          f"{rolling['window']['to_utc']} ({rolling['matched']} rows)")

    rejected = client.tool("search_requests", {"error": "'; drop table logs--"})
    print(f"unknown filter:   isError={rejected['isError']}  {rejected['text']}")

    bad_window = client.tool("search_requests",
                             {"from_utc": "2026-09-03T00:00:00Z", "to_utc": "2026-09-01T00:00:00Z"})
    print(f"bad window:       isError={bad_window['isError']}  {bad_window['text']}")

    unknown_tool = client.call("tools/call", {"name": "delete_everything", "arguments": {}})
    print(f"unknown tool:     {unknown_tool['error']['message']}")

    row = json.loads(client.tool("get_trace", {"trace_id": "t-4410aa"})["text"])["rows"][0]
    print(f"payload redaction: keys returned -> {sorted(row)}")
    print("                   payload_path never leaves the server; only payload_available does")

    print("\n--- citation check ---")
    trace_output = client.tool("get_trace", {"trace_id": "t-4410aa"})["text"]
    answers = {
        "grounded":   "REQ-1002 on trace t-4410aa timed out at lumen-pay (502); the retry succeeded.",
        "fabricated": "REQ-1002 on trace t-4410aa timed out; REQ-9999 shows the same failure.",
    }
    for label, answer in answers.items():
        result = check_citations(answer, [trace_output])
        flag = "  <- flag before sending" if result["unverified"] else ""
        print(f"{label:<11} verified={result['verified']} "
              f"unverified={result['unverified']}{flag}")

    client.close()
    print("\nAll calls completed. No network, no API key, no MCP SDK.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
