"""Fake `agent-cost` CLI for tests: contract 1 (with or without `waiting_ms`), a wrong contract, bad JSON, a hanging
export, a failing export. `late_agents` are only exported without `--until` (agents after the run's close)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

SCRIPT = """#!{python}
import json, os, sys, time
here = os.path.dirname(os.path.realpath(__file__))
cfg = json.load(open(os.path.join(here, "fake.json")))
a = sys.argv[1:]
with open(os.path.join(here, "calls.log"), "a") as f:
    f.write(json.dumps(a) + "\\n")
cmd = a[0] if a else ""
if cmd == "version":
    print(json.dumps({{"name": cfg.get("name", "agent-cost"), "version": "0.3.0", "contract": cfg.get("contract", 1)}}))
elif cmd == "export":
    mode = cfg.get("export_mode", "ok")
    if mode == "hang":
        time.sleep(5)
    if mode == "bad":
        print("{{not json")
    elif mode == "fail":
        print(json.dumps({{"error": "unknown session"}}))
        sys.exit(1)
    elif "--until" not in a and cfg.get("late_mode") == "fail":
        print(json.dumps({{"error": "late export failed"}}))
        sys.exit(1)
    else:
        sid = a[a.index("--session") + 1]
        agents = cfg.get("agents", []) + ([] if "--until" in a else cfg.get("late_agents", []))
        out = {{"contract": cfg.get("export_contract", 1), "generated_at": "2026-10-13T11:00:00Z", "session_id": sid,
                "window": {{"since": None, "until": None}}, "main": cfg.get("main", {{}}), "agents": agents}}
        print(json.dumps(out))
"""


class FakeAgentCost:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.cli = self.root / "skills" / "agent-cost" / "bin" / "agent-cost"
        self.cfg_path = self.cli.parent / "fake.json"
        self.log = self.cli.parent / "calls.log"

    def configure(self, **cfg) -> "FakeAgentCost":
        current = json.loads(self.cfg_path.read_text()) if self.cfg_path.is_file() else {}
        current.update(cfg)
        self.cfg_path.write_text(json.dumps(current))
        return self

    def calls(self) -> list[list[str]]:
        return [json.loads(x) for x in self.log.read_text().splitlines()] if self.log.is_file() else []


def tokens(i=0, o=0, cc=0, cr=0) -> dict:
    return {"input": i, "output": o, "cache_creation": cc, "cache_read": cr}


def time_(wall, active, waiting=None) -> dict:
    t = {"wall_ms": wall, "active_ms": active, "idle_ms": wall - active - (waiting or 0)}
    if waiting is not None:
        t["waiting_ms"] = waiting
    return t


def install(root: Path, **cfg) -> FakeAgentCost:
    fc = FakeAgentCost(root)
    fc.cli.parent.mkdir(parents=True, exist_ok=True)
    fc.cli.write_text(SCRIPT.format(python=sys.executable))
    fc.cli.chmod(0o755)
    fc.configure(**cfg)
    return fc
