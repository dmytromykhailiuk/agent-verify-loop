#!/usr/bin/env python3
"""Test double for `codex exec`. Env:
FAKE_CODEX_OUTPUT   path of a file whose text is written to the `-o` file (the last agent message)
FAKE_CODEX_RC       exit code (default 0); non-zero prints an error on stderr and a `turn.failed` event
FAKE_CODEX_LOG      append one JSON line per call ({"argv", "stdin", "cwd", "env": {...}})
FAKE_CODEX_SLEEP    seconds to sleep before answering
FAKE_CODEX_USAGE    JSON usage object for `turn.completed` (default: a fixed one)
"""
import json
import os
import sys
import time
from pathlib import Path

argv = sys.argv[1:]
stdin = sys.stdin.read() if "-" in argv else ""
log = os.environ.get("FAKE_CODEX_LOG")
if log:
    with open(log, "a") as f:
        f.write(json.dumps({"argv": argv, "stdin": stdin, "cwd": os.getcwd(),
                            "env": {k: os.environ.get(k) for k in ("VLOOP_GUARD_ROLE", "VLOOP_RUN_DIR",
                                                                   "VLOOP_PARENT_SESSION_ID", "VERIFY_LOOP_MODE",
                                                                   "HERDR_ENV", "CLAUDE_CODE_SESSION_ID")}}) + "\n")
if os.environ.get("FAKE_CODEX_SLEEP"):
    time.sleep(float(os.environ["FAKE_CODEX_SLEEP"]))
usage = json.loads(os.environ.get("FAKE_CODEX_USAGE") or
                   '{"input_tokens": 1200, "cached_input_tokens": 1000, "output_tokens": 90, '
                   '"reasoning_output_tokens": 40}')
print(json.dumps({"type": "thread.started", "thread_id": "019a-fake-thread"}))
print(json.dumps({"type": "turn.started"}))
code = int(os.environ.get("FAKE_CODEX_RC") or 0)
if code:
    print(json.dumps({"type": "turn.failed", "error": {"message": "simulated failure"}}))
    sys.stderr.write("fake codex: simulated failure\n")
    sys.exit(code)
src = os.environ.get("FAKE_CODEX_OUTPUT")
text = Path(src).read_text() if src else ""
if "-o" in argv and text:
    Path(argv[argv.index("-o") + 1]).write_text(text)
print(json.dumps({"type": "item.completed", "item": {"id": "item_1", "type": "agent_message", "text": text}}))
print(json.dumps({"type": "turn.completed", "usage": usage}))
