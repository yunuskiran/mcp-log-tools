"""Post-check the IDs a model cites against what the tools actually returned.

A model can mistype an ID or invent a plausible-looking one, and a wrong request or trace id in an
incident reply sends someone chasing the wrong row. So before the reply goes out, every ID in the
answer is matched against the tool outputs from the same turn; anything unverified gets flagged.

Stdlib only.
"""
from __future__ import annotations

import re

# Formats used in data/synthetic_logs.jsonl.
ID_PATTERNS = (
    re.compile(r"\bREQ-\d+\b"),         # request ids, e.g. REQ-1002
    re.compile(r"\bc-[0-9a-f]{6}\b"),   # correlation ids, e.g. c-9f3a11
    re.compile(r"\bt-[0-9a-f]{6}\b"),   # trace ids, e.g. t-4410aa
)


def extract_ids(text: str) -> list[str]:
    """IDs in order of first appearance, de-duplicated."""
    found: dict[int, str] = {}
    for pattern in ID_PATTERNS:
        for match in pattern.finditer(text):
            found.setdefault(match.start(), match.group())
    return list(dict.fromkeys(found[pos] for pos in sorted(found)))


def check_citations(answer: str, tool_outputs: list[str]) -> dict:
    """Split IDs cited in `answer` into those seen in `tool_outputs` and those that were not.

    Matching is on whole IDs extracted with the same patterns, so REQ-100 does not "verify" against
    REQ-1002.
    """
    seen = {i for output in tool_outputs for i in extract_ids(output)}
    cited = extract_ids(answer)
    return {
        "verified": [i for i in cited if i in seen],
        "unverified": [i for i in cited if i not in seen],
    }
