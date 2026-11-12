"""Shared helpers for the WP2 tests: env isolation, temp repos prepared through WP1 hooks, in-process `vloop`."""
from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import time
from pathlib import Path

FIX = Path(__file__).resolve().parent
TESTS = FIX.parents[1]
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))

from helpers import LIB, PW_MCP, SKILL, VL, VLOOP, HermeticCase  # noqa: E402,F401
from vlooplib import cli, prompts  # noqa: E402

SID = "62d51460-0000-4000-8000-0000000000aa"


def git(root, *args) -> str:
    p = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=60)
    if p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {p.stderr}")
    return p.stdout


def write(path: Path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


SRC = "".join(f"export function f{i}(x) {{\n  return x + {i};\n}}\n" for i in range(12))


class Case(HermeticCase):
    herdr = False

    def setUp(self):
        super().setUp()
        self.state = self.tmp / "state"
        self.state.mkdir()
        (self.state / "gc.stamp").write_text(time.strftime("%Y-%m-%d") + "\n")
        self.cfgfile = self.tmp / "config.json"
        os.environ.update({
            "VERIFY_STATE_DIR": str(self.state), "VERIFY_CONFIG": str(self.cfgfile), "VERIFY_LOOP_ALLOW_TMP": "1",
            "CLAUDE_VAULT_DIR": str(self.tmp / "vault" / "{project}"), "CLAUDE_CODE_ENTRYPOINT": "cli",
            "CLAUDE_CODE_SESSION_ID": SID,
            "VERIFY_TRIAGE_CMD": json.dumps([sys.executable, str(FIX / "fake-claude.py")]),
            "FAKE_CLAUDE_LOG": str(self.tmp / "claude-calls.jsonl"),
            "VERIFY_HERDR_AGENT": str(FIX / "fake-herdr-agent"), "FAKE_HERDR_DIR": str(self.tmp / "herdr"),
            "VERIFY_VAULT_CLI": str(self.tmp / "no-vault"), "TMPDIR": str(self.tmp / "t"),
        })
        (self.tmp / "herdr").mkdir()
        (self.tmp / "t").mkdir()
        if self.herdr:
            os.environ["HERDR_ENV"] = "1"
        self.write_config()
        prompts.TEMPLATES_DIR = FIX / "templates"

    def tearDown(self):
        prompts.TEMPLATES_DIR = None
        super().tearDown()

    # ------------------------------------------------------------------ config / fixtures

    def write_config(self, **kv):
        self.cfgfile.write_text(json.dumps({"schema": 1, "mode": "warn", **kv}))

    def triage_output(self, name: str):
        os.environ["FAKE_CLAUDE_OUTPUT"] = str(FIX / f"triage-{name}.json")

    def claude_calls(self) -> list[dict]:
        p = self.tmp / "claude-calls.jsonl"
        return [json.loads(ln) for ln in p.read_text().splitlines()] if p.exists() else []

    def herdr_role(self, role: str, wait: bool = False, **cfg):
        (self.tmp / "herdr" / (f"wait-{role}.json" if wait else f"{role}.json")).write_text(json.dumps(cfg))

    def herdr_calls(self) -> list[list[str]]:
        p = self.tmp / "herdr" / "calls.jsonl"
        return [json.loads(ln)["argv"] for ln in p.read_text().splitlines()] if p.exists() else []

    # ------------------------------------------------------------------ repos via WP1 hooks

    def make_repo(self, name="proj", files=None) -> Path:
        root = self.tmp / name
        root.mkdir()
        git(root, "init", "-q", "-b", "master")
        git(root, "config", "user.email", "t@t")
        git(root, "config", "user.name", "t")
        for rel, text in (files or {"src/a.js": SRC, "README.md": "# r\n", "src/page.html": "<p>hi</p>\n"}).items():
            write(root / rel, text)
        git(root, "add", "-A")
        git(root, "commit", "-qm", "init")
        return root

    def hook(self, event: str, payload: dict):
        p = subprocess.run([sys.executable, str(VLOOP), "hook", event], input=json.dumps(payload),
                           capture_output=True, text=True, timeout=60, env=dict(os.environ))
        self.assertEqual(p.returncode, 0, p.stderr)
        return p.stdout

    def prompt(self, root: Path, text: str = "Make f1 add 100 instead"):
        self.hook("prompt", {"session_id": SID, "transcript_path": "/dev/null", "cwd": str(root),
                             "hook_event_name": "UserPromptSubmit", "prompt": text})

    def edit(self, root: Path, rel: str, text: str, append: bool = False):
        f = root / rel
        pl = {"session_id": SID, "cwd": str(root), "tool_name": "Edit", "tool_input": {"file_path": str(f)}}
        self.hook("pre-tool", {**pl, "hook_event_name": "PreToolUse"})
        f.parent.mkdir(parents=True, exist_ok=True)
        if append and f.exists():
            with open(f, "a") as fh:
                fh.write(text)
        else:
            f.write_text(text)
        self.hook("post-tool", {**pl, "hook_event_name": "PostToolUse", "tool_response": {}})

    def code_change(self, root: Path, lines: int = 30):
        """A non-trivial source change in src/a.js (> trivial_max_lines)."""
        self.edit(root, "src/a.js", "".join(f"export const extra{i} = {i};\n" for i in range(lines)), append=True)

    # ------------------------------------------------------------------ vloop in-process

    def vl(self, *args, stdin: str = "", cwd: Path | None = None) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        old_stdin = sys.stdin
        sys.stdin = io.StringIO(stdin)
        if cwd:
            os.chdir(cwd)
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                try:
                    rc = cli.main([str(a) for a in args])
                except SystemExit as e:
                    rc = e.code if isinstance(e.code, int) else 2
        finally:
            sys.stdin = old_stdin
        return rc, out.getvalue(), err.getvalue()

    def ok(self, *args, **kw) -> str:
        rc, out, err = self.vl(*args, **kw)
        self.assertEqual(rc, 0, f"vloop {' '.join(map(str, args))} → {rc}\nstdout:\n{out}\nstderr:\n{err}")
        return out

    # ------------------------------------------------------------------ state readers

    def session(self) -> dict:
        return json.loads((self.state / "sessions" / f"{SID}.json").read_text())

    def loop_state(self, root: Path) -> dict:
        st = self.session()
        for rs in st["repos"].values():
            if rs["root"] == str(root):
                return rs
        raise AssertionError("repo not in state")

    def events(self) -> list[dict]:
        p = self.state / "sessions" / f"{SID}.events.jsonl"
        return [json.loads(ln) for ln in p.read_text().splitlines()] if p.exists() else []

    def run_dir(self, run_id: str) -> Path:
        return self.state / "runs" / run_id

    def ctx(self, run_id: str) -> dict:
        return json.loads((self.run_dir(run_id) / "context.json").read_text())

    def plan(self, run_id: str, n: int = 1) -> dict:
        return json.loads((self.run_dir(run_id) / f"attempt-{n}" / "plan.json").read_text())

    @staticmethod
    def run_id_of(out: str) -> str:
        for tok in out.split():
            if tok.count("-") >= 3 and tok[:8].isdigit():
                return tok
        raise AssertionError(f"no run id in:\n{out}")

    # ------------------------------------------------------------------ flows

    BRIEF = ("## Acceptance criteria\n- f1 returns x + 100\n- the page still renders\n## How to exercise\n"
             "- npm test\n## Out of scope\n- other functions\n")

    def begin(self, root: Path, *extra) -> tuple[str, str]:
        out = self.ok("begin", *extra, cwd=root)
        return self.run_id_of(out), out

    def brief(self, run_id: str, text: str | None = None) -> str:
        return self.ok("brief", "--run", run_id, stdin=text or self.BRIEF)


# ---------------------------------------------------------------------- verdict fixtures


def reviewer_fail(fid="R1", line=2, status="new"):
    return {"schema": 2, "role": "reviewer", "verdict": "fail", "summary": "f1 still adds 1.",
            "findings": [{"id": fid, "severity": "major", "confidence": "high", "category": "correctness",
                          "file": "src/a.js", "line": line, "title": "f1 does not add 100",
                          "problem": "The return value is unchanged.",
                          "scenario": "Calling f1(1) returns 2 instead of 101 as the user asked.",
                          "suggestion": "Return x + 100.", "status": status}],
            "suggestions": [{"id": "S1", "text": "Name the constant."}]}


def reviewer_pass(rechecked=None):
    return {"schema": 2, "role": "reviewer", "verdict": "pass", "summary": "Looks right.", "findings": [],
            "suggestions": [], "rechecked": rechecked or []}


def qa_pass(scope="checks"):
    return {"schema": 2, "role": "qa", "verdict": "pass", "scope": scope, "summary": "All checks pass.",
            "checks": [{"id": "C1", "criterion": "tests pass", "method": "test", "result": "pass",
                        "evidence": ["npm test → exit 0"]}],
            "findings": [], "suggestions": []}
