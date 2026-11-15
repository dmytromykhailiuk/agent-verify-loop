#!/usr/bin/env python3
"""Fake `claude -p` for E2E-A triage. Env E2E_TRIAGE_SCOPE (none|checks|run|browser, default run),
E2E_TRIAGE_TIER (standard|deep), E2E_TRIAGE_SPECIALISTS (comma list turned on), E2E_TRIAGE_LOG (append argv as JSON
lines)."""
import json
import os
import sys

stdin = sys.stdin.read()
if os.environ.get("E2E_TRIAGE_LOG"):
    with open(os.environ["E2E_TRIAGE_LOG"], "a") as f:
        f.write(json.dumps({"argv": sys.argv[1:], "stdin_chars": len(stdin),
                            "mode": os.environ.get("VERIFY_LOOP_MODE")}) + "\n")
scope = os.environ.get("E2E_TRIAGE_SCOPE", "run")
obj = {"change_size": "small", "risk": "medium",
       "qa": {"scope": scope, "reason": f"e2e fake triage: {scope}", "focus": ["sum of two values"],
              "needs_login": False},
       "review": {"tier": os.environ.get("E2E_TRIAGE_TIER", "standard"), "reason": "e2e fake triage",
                  "focus": ["loop bounds"]},
       "confidence": "high"}
on = [x for x in os.environ.get("E2E_TRIAGE_SPECIALISTS", "").split(",") if x]
obj["specialists"] = {s: {"run": s in on, "reason": f"e2e fake triage: {s}", "focus": []} for s in ("security", "perf")}
obj["specialists"]["ux"] = {"run": "ux" in on, "ui_spec": "none", "reason": "e2e fake triage: ux", "focus": []}
print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "duration_ms": 1200,
                  "total_cost_usd": 0.001, "result": json.dumps(obj), "structured_output": obj,
                  "usage": {"input_tokens": 10, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0,
                            "output_tokens": 100},
                  "modelUsage": {"claude-haiku-5-5": {"inputTokens": 10, "outputTokens": 100}}}))
