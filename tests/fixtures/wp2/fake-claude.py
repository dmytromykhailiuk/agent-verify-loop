#!/usr/bin/env python3
"""Test double for `claude -p` (triage). Env:
FAKE_CLAUDE_LOG      append one JSON line per call ({"argv", "stdin_chars", "env_mode", "herdr_env"})
FAKE_CLAUDE_OUTPUT   path of a JSON triage output (structured_output); default: triage-browser.json next to this file
FAKE_CLAUDE_EXIT     exit code (default 0); non-zero prints an error on stderr and no result
FAKE_CLAUDE_SLEEP    seconds to sleep before answering (timeout tests)
FAKE_CLAUDE_TEXT     "1" → no structured_output; the JSON is fenced inside `result` text
"""
import json
import os
import sys
import time
from pathlib import Path

stdin = sys.stdin.read()
log = os.environ.get("FAKE_CLAUDE_LOG")
if log:
    with open(log, "a") as f:
        f.write(json.dumps({"argv": sys.argv[1:], "stdin_chars": len(stdin),
                            "env_mode": os.environ.get("VERIFY_LOOP_MODE"),
                            "herdr_env": os.environ.get("HERDR_ENV"),
                            "session_env": os.environ.get("CLAUDE_CODE_SESSION_ID")}) + "\n")
if os.environ.get("FAKE_CLAUDE_SLEEP"):
    time.sleep(float(os.environ["FAKE_CLAUDE_SLEEP"]))
code = int(os.environ.get("FAKE_CLAUDE_EXIT") or 0)
if code:
    sys.stderr.write("fake claude: simulated failure\n")
    sys.exit(code)
out = Path(os.environ.get("FAKE_CLAUDE_OUTPUT") or Path(__file__).with_name("triage-browser.json"))
obj = json.loads(out.read_text())
doc = {"type": "result", "subtype": "success", "is_error": False, "duration_ms": 6100, "total_cost_usd": 0.0042,
       "usage": {"input_tokens": 2, "cache_creation_input_tokens": 9120, "cache_read_input_tokens": 0,
                 "output_tokens": 310},
       "modelUsage": {"claude-haiku-5-5": {"inputTokens": 2, "outputTokens": 310}}}
if os.environ.get("FAKE_CLAUDE_TEXT") == "1":
    doc["result"] = "Here is the triage:\n```json\n" + json.dumps(obj) + "\n```\n"
else:
    doc["result"] = json.dumps(obj)
    doc["structured_output"] = obj
print(json.dumps(doc))
