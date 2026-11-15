"""E2E-A (WP7 step 8): scripted, deterministic, no LLM.

Drives the real `bin/vloop` (hooks and loop commands) as subprocesses against a temp git repo and a temp
VERIFY_STATE_DIR. Triage is faked through VERIFY_TRIAGE_CMD (fake_triage.py); agent verdicts are fed with
`vloop record`. Agent-tool transport (no HERDR_ENV). Run:
    python3 -m unittest discover -s tests/e2e -p 'test_e2e_*.py' -v
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import unittest
from pathlib import Path

E2E_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(E2E_DIR.parent))
from helpers import VL, VLOOP, HermeticCase  # noqa: E402

SID = "e2e00000-0000-4000-8000-0000000000a1"
UNSET = ("E2E_TRIAGE_SCOPE", "E2E_TRIAGE_TIER", "E2E_TRIAGE_SPECIALISTS")

SUM_V1 = "export function sum(values) {\n  return values[0] + values[1];\n}\n"
SUM_V2 = ("export function sum(values) {\n  let total = 0;\n  for (let i = 0; i < values.length - 1; i++) {\n"
          "    total += values[i];\n  }\n  return total;\n}\n\nexport function avg(values) {\n"
          "  if (values.length === 0) return 0;\n  return sum(values) / values.length;\n}\n\n"
          "export function max(values) {\n  return Math.max(...values);\n}\n")
BRIEF = ("## Acceptance criteria\n- sum() adds every value of an array of any length\n- avg() and max() work\n"
         "## How to exercise\n- node --test\n## Out of scope\n- the server\n")


def git(root, *args) -> str:
    p = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=60)
    if p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {p.stderr}")
    return p.stdout


def reviewer_fail(status="new", rid="R1"):
    return {"schema": 2, "role": "reviewer", "verdict": "fail", "summary": "Loop skips the last value.",
            "findings": [{"id": rid, "severity": "major", "confidence": "high", "category": "correctness",
                          "file": "lib/sum.js", "line": 3, "title": "Loop bound skips the last element",
                          "problem": "i < values.length - 1 never adds the last value.",
                          "scenario": "sum([1, 2, 3]) returns 3 instead of 6 because the last value is skipped.",
                          "suggestion": "Use i < values.length.", "status": status}],
            "suggestions": [{"id": "S1", "text": "Consider reduce()."}]}


def reviewer_pass(rechecked=None):
    return {"schema": 2, "role": "reviewer", "verdict": "pass", "summary": "Correct.", "findings": [],
            "suggestions": [], "rechecked": rechecked or []}


def qa_pass(scope="run"):
    return {"schema": 2, "role": "qa", "verdict": "pass", "scope": scope, "summary": "Tests pass.",
            "checks": [{"id": "C1", "criterion": "node --test passes", "method": "test", "result": "pass",
                        "evidence": ["node --test → exit 0"]}],
            "findings": [], "suggestions": []}


class E2E(HermeticCase):
    def setUp(self):
        super().setUp()
        for k in UNSET:
            os.environ.pop(k, None)
        self.state = self.tmp / "state"
        self.state.mkdir()
        (self.state / "gc.stamp").write_text(time.strftime("%Y-%m-%d") + "\n")
        self.cfg = self.tmp / "config.json"
        self.set_config(mode="enforce")
        os.environ.update({
            "VERIFY_STATE_DIR": str(self.state), "VERIFY_CONFIG": str(self.cfg), "VERIFY_LOOP_ALLOW_TMP": "1",
            "CLAUDE_VAULT_DIR": str(self.tmp / "vault" / "{project}"), "CLAUDE_CODE_ENTRYPOINT": "cli",
            "CLAUDE_CODE_SESSION_ID": SID, "VERIFY_VAULT_CLI": str(self.tmp / "no-vault"),
            "VERIFY_TRANSCRIPTS_DIR": str(self.tmp / "transcripts"),
            "VERIFY_TRIAGE_CMD": json.dumps([sys.executable, str(E2E_DIR / "fake_triage.py")]),
            "E2E_TRIAGE_LOG": str(self.tmp / "triage-calls.jsonl"), "TMPDIR": str(self.tmp / "t"),
        })
        (self.tmp / "t").mkdir()
        self.repo = self.make_repo()
        self.prompt("Change sum() in lib/sum.js to accept an array of any length.")

    # ------------------------------------------------------------------ helpers

    def set_config(self, **kv):
        self.cfg.write_text(json.dumps({"schema": 1, **kv}))

    def make_repo(self) -> Path:
        root = self.tmp / "verify-toy"
        (root / "lib").mkdir(parents=True)
        (root / "test").mkdir()
        (root / "package.json").write_text('{"name": "verify-toy", "private": true, "type": "module", '
                                           '"scripts": {"test": "node --test"}}\n')
        (root / "lib" / "sum.js").write_text(SUM_V1)
        (root / "test" / "sum.test.js").write_text("import test from 'node:test';\n")
        (root / "README.md").write_text("# verify-toy\n")
        git(root, "init", "-q", "-b", "master")
        git(root, "config", "user.email", "e2e@example.com")
        git(root, "config", "user.name", "e2e")
        git(root, "add", "-A")
        git(root, "commit", "-qm", "init")
        return root

    def hook(self, event: str, payload: dict, env=None) -> str:
        p = subprocess.run([sys.executable, str(VLOOP), "hook", event], input=json.dumps(payload),
                           capture_output=True, text=True, timeout=60, env=env or dict(os.environ))
        self.assertEqual(p.returncode, 0, p.stderr)
        return p.stdout

    def prompt(self, text: str):
        self.hook("prompt", {"session_id": SID, "transcript_path": "/dev/null", "cwd": str(self.repo),
                             "hook_event_name": "UserPromptSubmit", "prompt": text})

    def edit(self, rel: str, text: str):
        f = self.repo / rel
        pl = {"session_id": SID, "cwd": str(self.repo), "tool_name": "Edit", "tool_input": {"file_path": str(f)}}
        self.hook("pre-tool", {**pl, "hook_event_name": "PreToolUse"})
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
        self.hook("post-tool", {**pl, "hook_event_name": "PostToolUse", "tool_response": {}})

    def stop(self, msg: str = "Done.") -> dict | None:
        out = self.hook("stop", {"session_id": SID, "cwd": str(self.repo), "hook_event_name": "Stop",
                                 "last_assistant_message": msg, "stop_hook_active": False})
        return json.loads(out) if out.strip() else None

    def vl(self, *args, stdin: str = "", state: Path | None = None) -> tuple[int, str, str]:
        env = dict(os.environ)
        if state:
            env["VERIFY_STATE_DIR"] = str(state)
        p = subprocess.run([sys.executable, str(VLOOP), *map(str, args)], input=stdin, capture_output=True,
                           text=True, timeout=120, cwd=str(self.repo), env=env)
        return p.returncode, p.stdout, p.stderr

    def ok(self, *args, **kw) -> str:
        rc, out, err = self.vl(*args, **kw)
        self.assertEqual(rc, 0, f"vloop {' '.join(map(str, args))} → {rc}\n{out}\n{err}")
        return out

    def record(self, role: str, run: str, verdict: dict, state: Path | None = None) -> str:
        text = f"Summary.\n```json\n{json.dumps(verdict)}\n```\n"
        return self.ok("record", role, "--run", run, stdin=text, state=state)

    def begin(self) -> tuple[str, str]:
        out = self.ok("begin")
        m = re.search(r"run (\S+) ·", out)
        self.assertIsNotNone(m, out)
        return m.group(1), out

    def events(self, state: Path | None = None) -> list[dict]:
        p = (state or self.state) / "sessions" / f"{SID}.events.jsonl"
        return [json.loads(ln) for ln in p.read_text().splitlines()] if p.exists() else []

    def session(self, state: Path | None = None) -> dict:
        return json.loads(((state or self.state) / "sessions" / f"{SID}.json").read_text())

    def loop(self, state: Path | None = None) -> dict:
        return next(iter(self.session(state)["repos"].values()))["loop"]

    def run_dir(self, run: str, state: Path | None = None) -> Path:
        return (state or self.state) / "runs" / run

    def plan(self, run: str, n: int) -> dict:
        return json.loads((self.run_dir(run) / f"attempt-{n}" / "plan.json").read_text())

    def copy_state(self, name: str) -> Path:
        dst = self.tmp / name
        shutil.copytree(self.state, dst)
        return dst

    def fail_attempt(self, run: str, status: str) -> str:
        self.record("reviewer", run, reviewer_fail(status=status))
        if (self.run_dir(run) / f"attempt-{self.attempt(run)}" / "qa.prompt.md").exists():
            self.record("qa", run, qa_pass())
        return self.ok("judge", "--run", run)

    def attempt(self, run: str) -> int:
        return len(json.loads((self.run_dir(run) / "context.json").read_text())["attempts"])

    def rerun(self, run: str, sum_text: str, note: str) -> str:
        self.edit("lib/sum.js", sum_text)
        return self.ok("begin", "--rerun", "--run", run, "--fix-notes", "-", stdin=note + "\n")

    # ------------------------------------------------------------------ scenarios

    def test_a1_docs_only_skip(self):
        self.edit("README.md", "# verify-toy\n\nMore docs.\n")
        out = self.stop()
        self.assertIsNotNone(out)
        self.assertNotEqual(out.get("decision"), "block", out)
        self.assertIn("verify-loop · skipped: docs-only change (README.md)", out["systemMessage"])
        self.assertIn("gate_skip_docs", [e["event"] for e in self.events()])
        self.assertEqual(self.loop()["status"], "skipped")

    def test_a2_trivial_prefilter(self):
        self.edit("lib/sum.js", SUM_V1.replace("values[1];", "values[1]; // two values"))
        run, out = self.begin()
        self.assertIn("prefilter: trivial change: 2 lines in 1 file(s)", out)
        plan_out = self.ok("brief", "--run", run, stdin=BRIEF)
        self.assertIn("prefilter (no triage)", plan_out)
        self.assertRegex(plan_out, r"qa: none — trivial change: 2 lines in 1 file\(s\) \(prefilter, no triage\)")
        self.assertIn('Agent(subagent_type="verify-reviewer"', plan_out)
        self.assertNotIn('subagent_type="verify-qa"', plan_out)
        self.assertFalse((self.tmp / "triage-calls.jsonl").exists(), "triage must not run for trivial")
        plans = [e for e in self.events() if e["event"] == "plan"]
        self.assertTrue(plans and plans[-1]["data"]["qa"]["run"] is False, plans)
        self.assertFalse((self.run_dir(run) / "attempt-1" / "qa.prompt.md").exists())
        self.record("reviewer", run, reviewer_pass())
        judge = self.ok("judge", "--run", run)
        self.assertTrue(judge.startswith("RESULT: PASS"), judge)
        self.assertIn("qa SKIPPED (prefilter: none)", judge)

    def test_a3_fail_fix_pass_retro(self):
        self.edit("lib/sum.js", SUM_V2)
        run, out = self.begin()
        self.assertIn("prefilter: triage", out)
        plan_out = self.ok("brief", "--run", run, stdin=BRIEF)
        self.assertIn("verify-loop · plan · attempt 1 · triage sonnet", plan_out)
        self.assertIn("qa: run · full", plan_out)
        self.assertIn(f"then: {VL} collect --run " + run, plan_out)
        calls = [json.loads(ln) for ln in (self.tmp / "triage-calls.jsonl").read_text().splitlines()]
        self.assertEqual(len(calls), 1)
        self.assertIn("--agent", calls[0]["argv"])
        self.assertEqual(calls[0]["mode"], "off")
        rl = self.record("reviewer", run, reviewer_fail())
        self.assertIn("reviewer: FAIL · ", rl)
        self.record("qa", run, qa_pass())
        self.assertIn(f"next: {VL} judge --run " + run,
                      self.ok("collect", "--run", run))
        j1 = self.ok("judge", "--run", run)
        self.assertTrue(j1.startswith("RESULT: FAIL"), j1)
        self.assertIn("R1 [major·correctness·high] lib/sum.js:3", j1)
        self.assertEqual(self.loop()["status"], "open")
        # stop gate while the loop is open (enforce) → blocks with the open-loop reason
        g = self.stop()
        self.assertEqual(g["decision"], "block")
        self.assertIn(f"run {run} is still open — attempt 1 was FAIL", g["reason"])
        r2 = self.rerun(run, SUM_V2.replace("values.length - 1", "values.length"), "R1: loop bound fixed")
        self.assertIn("review: recheck", r2)
        p2 = self.plan(run, 2)
        self.assertEqual(p2["roles"]["reviewer"]["mode"], "recheck")
        self.assertEqual(p2["source"], "reused")
        self.record("reviewer", run, reviewer_pass([{"id": "R1", "status": "fixed", "note": "bound fixed",
                                                     "dispute_ruling": "n/a"}]))
        if p2["roles"]["qa"]["run"]:
            self.record("qa", run, qa_pass())
        else:
            self.assertEqual(p2["roles"]["qa"]["carried_from"], 1)
        j2 = self.ok("judge", "--run", run)
        self.assertTrue(j2.startswith("RESULT: PASS"), j2)
        self.assertIn(f"next: {VL} retro-plan --run " + run, j2)
        lp = self.loop()
        self.assertEqual((lp["status"], lp["retro"]), ("passed", "pending"))
        rb = self.stop()
        self.assertEqual(rb["decision"], "block")
        self.assertIn(f"retro-plan --run {run}", rb["reason"])
        self.assertIsNone(self.stop(), "retro block happens once per loop")
        rp = self.ok("retro-plan", "--run", run)
        self.assertRegex(rp, r"^retro: (full|light) — ")
        ledger = [json.loads(ln) for ln in (self.state / "ledger.jsonl").read_text().splitlines()]
        self.assertEqual([r["run_id"] for r in ledger], [run])
        self.assertEqual((ledger[0]["outcome"], ledger[0]["attempts"]), ("passed", 2))
        self.assertTrue((self.run_dir(run) / "retro" / "plan.json").exists())

    def test_a9_security_fail_fix_recheck_pass(self):
        os.environ["E2E_TRIAGE_SPECIALISTS"] = "security"
        os.environ["E2E_TRIAGE_SCOPE"] = "none"
        self.edit("lib/sum.js", SUM_V2)
        run, _ = self.begin()
        plan_out = self.ok("brief", "--run", run, stdin=BRIEF)
        self.assertIn("  security: full · sonnet/high — e2e fake triage: security", plan_out)
        self.assertIn('Agent(subagent_type="verify-security", model="sonnet", description="verify security #1"',
                      plan_out)
        att1 = self.run_dir(run) / "attempt-1"
        self.assertIn("A security specialist reviews this change in parallel",
                      (att1 / "reviewer.prompt.md").read_text())
        self.assertIn(str(att1 / "security.json"), (att1 / "security.prompt.md").read_text())
        self.record("reviewer", run, reviewer_pass())
        sec = {"schema": 2, "role": "security", "verdict": "fail", "summary": "Unbounded input.",
               "findings": [{"id": "SC1", "severity": "major", "confidence": "high", "category": "security",
                             "file": "lib/sum.js", "line": 3, "title": "Attacker-sized loop",
                             "problem": "values comes from the request body unchecked.",
                             "scenario": "POST /sum with a 10^9-element array → loop in lib/sum.js:3 → the server "
                                         "hangs for every user",
                             "evidence_ref": "lib/sum.js:1", "suggestion": "Cap values.length.", "status": "new"}],
               "suggestions": []}
        self.assertIn("security: FAIL", self.record("security", run, sec))
        j1 = self.ok("judge", "--run", run)
        self.assertTrue(j1.startswith("RESULT: FAIL"), j1)
        self.assertIn("SC1", j1)
        r2 = self.rerun(run, SUM_V2.replace("let total = 0;", "if (values.length > 1000) throw new Error('too many');"
                                                               "\n  let total = 0;"), "SC1: capped the input size")
        p2 = self.plan(run, 2)
        self.assertEqual((p2["roles"]["security"]["run"], p2["roles"]["security"]["mode"]), (True, "recheck"))
        self.assertEqual((p2["roles"]["security"]["model"], p2["roles"]["security"]["effort"]), ("sonnet", "high"))
        self.assertEqual((p2["roles"]["reviewer"]["run"], p2["roles"]["reviewer"]["mode"]), (True, "delta"))
        self.assertIn("security: recheck", r2)
        self.record("reviewer", run, reviewer_pass())
        self.record("security", run, {"schema": 2, "role": "security", "verdict": "pass", "summary": "Capped.",
                                      "findings": [], "suggestions": [],
                                      "rechecked": [{"id": "SC1", "status": "fixed", "note": "cap added",
                                                     "dispute_ruling": "n/a"}]})
        j2 = self.ok("judge", "--run", run)
        self.assertTrue(j2.startswith("RESULT: PASS"), j2)
        self.assertEqual(self.loop()["status"], "passed")

    def test_a4_escalate_and_decide(self):
        self.edit("lib/sum.js", SUM_V2)
        run, _ = self.begin()
        self.ok("brief", "--run", run, stdin=BRIEF)
        self.assertTrue(self.fail_attempt(run, "new").startswith("RESULT: FAIL"))
        self.rerun(run, SUM_V2.replace("total = 0", "total = 0 /* a2 */"), "R1: tried a2")
        self.assertTrue(self.fail_attempt(run, "still_present").startswith("RESULT: FAIL"))
        self.rerun(run, SUM_V2.replace("total = 0", "total = 0 /* a3 */"), "R1: tried a3")
        j3 = self.fail_attempt(run, "still_present")
        self.assertTrue(j3.startswith("RESULT: ESCALATE"), j3)
        self.assertIn(f"verify-loop · ESCALATION · 3 failed attempts", j3)
        self.assertIn("R1 [major·stuck]", j3)
        self.assertIn("decide retry --run", j3)
        self.assertEqual(self.loop()["status"], "escalated")
        self.assertIn("escalate", [e["event"] for e in self.events()])
        g = self.stop()
        self.assertIn("escalated", g["systemMessage"])
        copies = {k: self.copy_state(f"state-{k}") for k in ("retry", "waive", "accept", "stop")}
        out = self.ok("decide", "retry", "--run", run, "--note", "try a different bound", state=copies["retry"])
        self.assertIn("verify-loop · decision: retry · failures reset to 0/3", out)
        self.assertEqual(self.loop(copies["retry"])["status"], "open")
        out = self.ok("decide", "waive", "--run", run, "--ids", "R1", state=copies["waive"])
        self.assertIn("verify-loop · decision: waive R1", out)
        self.assertIn("RESULT: PASS", out)
        self.assertEqual(self.loop(copies["waive"])["status"], "passed")
        out = self.ok("decide", "accept", "--run", run, state=copies["accept"])
        self.assertIn("verify-loop · decision: accept · loop closed (accepted with open findings: R1)", out)
        self.assertEqual(self.loop(copies["accept"])["status"], "accepted")
        out = self.ok("decide", "stop", "--run", run, state=copies["stop"])
        self.assertIn("verify-loop · decision: stop · loop closed (abandoned) · the change is UNVERIFIED", out)
        self.assertEqual(self.loop(copies["stop"])["status"], "abandoned")

    def test_a5_standalone_review(self):
        (self.repo / "lib" / "sum.js").write_text(SUM_V2)
        git(self.repo, "commit", "-qam", "generic sum")
        out = self.ok("begin", "--standalone", "--roles", "reviewer", "--target", "HEAD~1..HEAD")
        m = re.search(r"run (\S+) ·", out)
        self.assertIsNotNone(m, out)
        run = m.group(1)
        self.assertIn('Agent(subagent_type="verify-reviewer"', out)
        self.assertNotIn('subagent_type="verify-qa"', out)
        ctx = json.loads((self.run_dir(run) / "context.json").read_text())
        self.assertEqual((ctx["kind"], ctx["target"]["spec"]), ("standalone", "HEAD~1..HEAD"))
        self.record("reviewer", run, reviewer_fail())
        j = self.ok("judge", "--run", run)
        self.assertTrue(j.startswith("RESULT: FAIL"), j)
        self.assertIn("\nverify-review · ", j)
        self.assertNotIn("failures", j.splitlines()[1])
        # standalone never touches the session loop
        st = self.session()
        self.assertTrue(all(r["loop"]["status"] in ("idle", None) or r["loop"].get("run_id") != run
                            for r in st["repos"].values()))

    def test_a6_gate_block_defer_ignore(self):
        self.edit("lib/sum.js", SUM_V2)
        g1 = self.stop()
        self.assertEqual(g1["decision"], "block")
        self.assertIn("this session changed code that hasn't been verified — verify-toy: 1 files (lib/sum.js)",
                      g1["reason"])
        q = self.stop("Should I also update the README?")
        self.assertIsNone(q, "a question must not be blocked")
        out = self.ok("defer", "--reason", "waiting for the user")
        self.assertIn("verify-loop · deferred: waiting for the user (gate passes once)", out)
        self.assertIsNone(self.stop())
        g2 = self.stop()
        self.assertEqual(g2["decision"], "block")
        g3 = self.stop()
        self.assertNotIn("decision", g3)
        self.assertIn("the gate stopped blocking after 2 attempts", g3["systemMessage"])
        names = [e["event"] for e in self.events()]
        for ev in ("gate_block", "gate_defer", "gate_ignored"):
            self.assertIn(ev, names)
        self.assertEqual(names.count("gate_block"), 2)
        # warn mode never blocks
        self.set_config(mode="warn")
        self.edit("lib/sum.js", SUM_V2 + "\n// warn\n")
        w = self.stop()
        self.assertNotIn("decision", w or {})

    def test_a7_browser_consent(self):
        os.environ["E2E_TRIAGE_SCOPE"] = "browser"
        self.edit("lib/sum.js", SUM_V2)
        run, _ = self.begin()
        out = self.ok("brief", "--run", run, stdin=BRIEF)
        self.assertIn(f"verify-loop · CONSENT NEEDED · browser QA (Playwright, headless Chrome) · run {run} · attempt 1",
                      out)
        self.assertIn("consent allow --run " + run, out)
        self.assertNotIn("then: ", out)
        att = self.run_dir(run) / "attempt-1"
        for name in ("qa.prompt.md", "pw-pointer.json", "mcp.json"):
            self.assertFalse((att / name).exists(), name)
        self.assertTrue((att / "reviewer.prompt.md").exists())
        rc, _, err = self.vl("judge", "--run", run)
        self.assertEqual(rc, 2)
        self.assertIn("waiting for browser QA consent", err)
        allow_state = self.copy_state("state-allow")
        nb = self.ok("consent", "no-browser", "--run", run)
        self.assertIn("verify-loop · consent: browser QA declined → QA without browser (run)", nb)
        p = self.plan(run, 1)
        self.assertEqual(p["roles"]["qa"]["scope"], "run")
        self.assertIn("No browser in this scope: the user declined browser QA", (att / "qa.prompt.md").read_text())
        self.assertFalse((att / "pw-pointer.json").exists())
        al = self.ok("consent", "allow", "--run", run, state=allow_state)
        self.assertIn("verify-loop · consent: browser QA approved", al)
        att2 = allow_state / "runs" / run / "attempt-1"
        self.assertTrue((att2 / "pw-pointer.json").exists())
        mcp = json.loads((att2 / "mcp.json").read_text())
        self.assertEqual(mcp["mcpServers"]["playwright"]["args"], ["--pointer", str(att2 / "pw-pointer.json")])
        self.assertTrue((att2 / "qa.prompt.md").exists())
        rc, _, err = self.vl("consent", "allow", "--run", run, state=allow_state)
        self.assertEqual(rc, 2)
        self.assertIn("no browser consent pending", err)


if __name__ == "__main__":
    unittest.main()
