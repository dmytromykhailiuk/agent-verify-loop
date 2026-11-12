"""Shared helpers for the WP1 tests: env isolation, temp git repos, running `vloop` / hooks."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

TESTS = Path(__file__).resolve().parents[2]
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))

from helpers import LIB, SKILL, VLOOP, HermeticCase  # noqa: E402,F401

FIXTURES = Path(__file__).resolve().parent


class EnvCase(HermeticCase):
    """Isolated STATE_DIR / user config; tmp paths trackable (VERIFY_LOOP_ALLOW_TMP=1)."""

    def setUp(self):
        super().setUp()
        self.state = self.tmp / "state"
        self.cfgfile = self.tmp / "config" / "config.json"
        os.environ["VERIFY_STATE_DIR"] = str(self.state)
        os.environ["VERIFY_CONFIG"] = str(self.cfgfile)
        os.environ["VERIFY_LOOP_ALLOW_TMP"] = "1"
        os.environ["CLAUDE_VAULT_DIR"] = str(self.tmp / "vault" / "{project}")
        os.environ["CLAUDE_CODE_ENTRYPOINT"] = "cli"
        self.state.mkdir(parents=True)
        # no background gc fork during tests
        (self.state / "gc.stamp").write_text(time.strftime("%Y-%m-%d") + "\n")

    # ------------------------------------------------------------------ git

    def make_repo(self, name: str = "proj", files: dict | None = None) -> Path:
        root = self.tmp / name
        root.mkdir(parents=True)
        git(root, "init", "-q", "-b", "master")
        git(root, "config", "user.email", "t@t")
        git(root, "config", "user.name", "t")
        for rel, text in (files or {"a.js": "a\n", "README.md": "# r\n"}).items():
            write(root / rel, text)
        git(root, "add", "-A")
        git(root, "commit", "-qm", "init")
        return Path(os.path.realpath(root))

    # ------------------------------------------------------------------ vloop

    def vloop(self, *args, stdin: str = "", env: dict | None = None) -> subprocess.CompletedProcess:
        e = dict(os.environ)
        for k, v in (env or {}).items():
            if v is None:
                e.pop(k, None)
            else:
                e[k] = v
        return subprocess.run([sys.executable, str(VLOOP), *args], input=stdin, capture_output=True, text=True,
                              env=e, timeout=60)

    def hook(self, event: str, payload, env: dict | None = None) -> subprocess.CompletedProcess:
        raw = payload if isinstance(payload, str) else json.dumps(payload)
        p = self.vloop("hook", event, stdin=raw, env=env)
        self.assertEqual(p.returncode, 0, p.stderr)
        return p

    def hook_json(self, event: str, payload, env: dict | None = None):
        out = self.hook(event, payload, env).stdout.strip()
        return json.loads(out) if out else None

    def session(self, sid: str) -> dict | None:
        p = self.state / "sessions" / f"{sid}.json"
        return json.loads(p.read_text()) if p.exists() else None

    def events(self, sid: str) -> list[dict]:
        p = self.state / "sessions" / f"{sid}.events.jsonl"
        if not p.exists():
            return []
        return [json.loads(ln) for ln in p.read_text().splitlines() if ln.strip()]

    def event_names(self, sid: str) -> list[str]:
        return [e["event"] for e in self.events(sid)]

    def errors(self) -> str:
        p = self.state / "hook-errors.log"
        return p.read_text() if p.exists() else ""

    def write_user_config(self, **kv):
        self.cfgfile.parent.mkdir(parents=True, exist_ok=True)
        self.cfgfile.write_text(json.dumps({"schema": 1, **kv}))

    # ------------------------------------------------------------------ payloads

    @staticmethod
    def p_prompt(sid, cwd, prompt="do the thing"):
        return {"session_id": sid, "transcript_path": "/dev/null", "cwd": str(cwd),
                "hook_event_name": "UserPromptSubmit", "prompt": prompt}

    @staticmethod
    def p_edit(sid, cwd, path, event="PostToolUse", tool="Edit", **extra):
        d = {"session_id": sid, "cwd": str(cwd), "hook_event_name": event, "tool_name": tool,
             "tool_input": {"file_path": str(path)}}
        if event == "PostToolUse":
            d["tool_response"] = {}
        d.update(extra)
        return d

    @staticmethod
    def p_stop(sid, cwd, msg="Done.", **extra):
        d = {"session_id": sid, "cwd": str(cwd), "hook_event_name": "Stop", "stop_hook_active": False,
             "last_assistant_message": msg}
        d.update(extra)
        return d

    def edit(self, sid, root: Path, rel: str, text: str = "changed\n", env=None, append=True):
        """pre-tool, change the file, post-tool (like the Edit tool)."""
        f = root / rel
        self.hook("pre-tool", self.p_edit(sid, root, f, event="PreToolUse"), env=env)
        f.parent.mkdir(parents=True, exist_ok=True)
        if append and f.exists():
            with open(f, "a") as fh:
                fh.write(text)
        else:
            f.write_text(text)
        self.hook("post-tool", self.p_edit(sid, root, f), env=env)


def git(root, *args, env=None) -> str:
    p = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, env=env, timeout=60)
    if p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {p.stderr}")
    return p.stdout


def write(path: Path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
