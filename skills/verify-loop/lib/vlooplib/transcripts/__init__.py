"""Platform transcripts: locating them and reading token usage, a normalized timeline and subagent files.

Each platform module exposes the same functions, so metrics never branches on the platform:
`transcript_for(sid)`, `usage_entries(path, start, end, main_only)`, `timeline(path, start, end)`,
`subagents(main_path, sid, events, start, session_state)` and `first_ts(path)`. A usage entry is
`{"ts", "model", "n": {input, cache_write, cache_write_5m, cache_write_1h, cache_read, output, thinking}, "cost"}`
where `cost` is USD, or None when the model is not in pricing.json.
"""
from __future__ import annotations

import json


def iter_jsonl(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if isinstance(d, dict):
                    yield d
    except (OSError, TypeError):
        return


def first_ts(path) -> str | None:
    for d in iter_jsonl(path):
        if d.get("timestamp"):
            return d["timestamp"]
    return None


def with_cost(entries: list[dict]) -> list[dict]:
    from .. import metrics

    for e in entries:
        e["cost"] = metrics.request_cost(e["model"], e["n"])
    return entries


def int_of(v) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def module_for(platform: str | None):
    if platform == "codex":
        from . import codex

        return codex
    from . import claude

    return claude


__all__ = ["iter_jsonl", "first_ts", "with_cost", "int_of", "module_for"]
