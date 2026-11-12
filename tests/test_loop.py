"""WP2 loop tests: begin, brief, spawn/collect/record, judge, re-runs, escalation and decide, consent (§3 WP2)."""
from __future__ import annotations

import json
import shutil
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures" / "wp2"))
import wp2helpers as h  # noqa: E402
from wp2helpers import VL  # noqa: E402


class BeginTest(h.Case):
    def test_begin_block_and_run_files(self):  # (1)
        root = self.make_repo()
        self.prompt(root)
        self.code_change(root)
        rid, out = self.begin(root)
        lines = out.splitlines()
        self.assertRegex(lines[0], rf"^verify-loop · run {rid} · attempt 1 · proj · 1 files, \+30/−0 · "
                                   r"baseline: snapshot @ \d\d:\d\d$")
        self.assertEqual(lines[1], "prefilter: triage")
        self.assertEqual(lines[2], f"next: write the brief → {VL} brief --run {rid} <<'EOF'")
        self.assertEqual(lines[3:], ["## Acceptance criteria", "- <testable behavior from the user's request>",
                                     "## How to exercise", "- <routes, commands, test data, user role>",
                                     "## Out of scope", "- <…>", "EOF"])
        rd = self.run_dir(rid)
        for f in ("context.json", "prompts.md", "attempt-1/tree", "attempt-1/diff.patch", "attempt-1/diff.numstat.json"):
            self.assertTrue((rd / f).exists(), f)
        ctx = self.ctx(rid)
        self.assertEqual(ctx["kind"], "loop")
        self.assertEqual(ctx["transport"], "agent")
        self.assertEqual(ctx["target"]["spec"], "session")
        self.assertEqual(ctx["attempts"][0]["prefilter"], "triage")
        self.assertIn("Make f1 add 100 instead", (rd / "prompts.md").read_text())
        self.assertIn("+export const extra0 = 0;", (rd / "attempt-1/diff.patch").read_text())
        loop = self.loop_state(root)["loop"]
        self.assertEqual((loop["run_id"], loop["status"], loop["attempt"], loop["retro"]), (rid, "open", 1, "none"))
        self.assertIn("begin", [e["event"] for e in self.events()])
        self.assertEqual(self.claude_calls(), [])  # triage runs in brief, not begin

    def test_docs_only_skip(self):  # (2)
        root = self.make_repo()
        self.prompt(root)
        self.edit(root, "README.md", "more docs\n", append=True)
        out = self.ok("begin", cwd=root)
        self.assertEqual(out.strip(), "verify-loop · SKIP · proj · docs-only change (README.md) · loop closed, "
                                      "nothing to verify")
        rs = self.loop_state(root)
        self.assertEqual(rs["loop"]["status"], "skipped")
        self.assertEqual(rs["touched"], [])
        self.assertFalse((self.state / "runs").exists() and any((self.state / "runs").iterdir()))
        skip = [e for e in self.events() if e["event"] == "skip"]
        self.assertEqual(skip[-1]["data"]["by"], "prefilter")

    def test_nothing(self):
        root = self.make_repo()
        self.prompt(root)
        out = self.ok("begin", cwd=root)
        self.assertEqual(out.strip(), "verify-loop · NOTHING · no tracked changes in this session and no diff vs the "
                                      "task baseline")

    def test_untracked_diff_vs_baseline(self):
        root = self.make_repo()
        self.prompt(root)
        h.write(root / "src/a.js", h.SRC + "".join(f"// line {i}\n" for i in range(20)))  # no hook: not touched
        rid, out = self.begin(root)
        self.assertIn("note: no tracked edits; verifying the diff vs the task baseline", out)

    def test_begin_on_escalated_exits_2(self):  # (14)
        root = self.make_repo()
        self.prompt(root)
        self.code_change(root)
        rid, _ = self.begin(root, "--qa", "none")
        self.triage_output("checks")
        self.brief(rid)
        _escalate(self, root, rid)
        rc, out, err = self.vl("begin", cwd=root)
        self.assertEqual(rc, 2)
        self.assertIn(f"run {rid} is escalated: ask the user, then vloop decide", err)
        rc, _, err = self.vl("begin", "--rerun", "--run", rid, cwd=root)
        self.assertEqual(rc, 2)


class BriefTest(h.Case):
    def setUp(self):
        super().setUp()
        self.root = self.make_repo()
        self.prompt(self.root)
        self.code_change(self.root)
        self.rid, _ = self.begin(self.root)
        self.triage_output("checks")

    def test_narrative_rejected(self):  # (13)
        bad = self.BRIEF + "- I fixed the off-by-one in f1\n"
        rc, out, err = self.vl("brief", "--run", self.rid, stdin=bad)
        self.assertEqual(rc, 2)
        self.assertIn('brief must hold criteria and facts only, not the implementer\'s narrative: '
                      '"- I fixed the off-by-one in f1"', err)
        rc, _, err = self.vl("brief", "--run", self.rid, stdin=self.BRIEF + "It works now.\n")
        self.assertEqual(rc, 2)
        rc, _, err = self.vl("brief", "--run", self.rid, stdin="## How to exercise\n- npm test\n")
        self.assertEqual(rc, 2)
        self.assertIn("Acceptance criteria", err)
        rc, _, err = self.vl("brief", "--run", self.rid, stdin="## Acceptance criteria\n- x\n" + "y" * 6001)
        self.assertEqual(rc, 2)
        self.assertFalse((self.run_dir(self.rid) / "brief.md").exists())
        self.assertEqual(self.claude_calls(), [])

    def test_brief_plans_renders_and_sets_inflight(self):
        out = self.brief(self.rid)
        self.assertTrue(out.startswith("verify-loop · plan · attempt 1 · triage haiku (6s, ≈$0.0042, confidence high)"))
        att = self.run_dir(self.rid) / "attempt-1"
        for f in ("triage.input.md", "triage.json", "plan.json", "reviewer.prompt.md", "qa.prompt.md"):
            self.assertTrue((att / f).exists(), f)
        self.assertTrue((att / "qa-artifacts").is_dir() and (att / "qa-logs").is_dir())
        self.assertFalse((att / "mcp.json").exists())
        self.assertIsNotNone(self.loop_state(self.root)["loop"]["inflight_since"])
        self.assertEqual(len(self.claude_calls()), 1)
        qa_task = (att / "qa.prompt.md").read_text()
        self.assertIn("No browser in this scope.", qa_task)
        self.assertIn("- sum handles negatives", qa_task)

    def test_second_brief_keeps_plan(self):
        self.brief(self.rid)
        plan1 = self.plan(self.rid)
        self.brief(self.rid, self.BRIEF.replace("f1 returns x + 100", "f1 returns x + 200"))
        self.assertEqual(self.plan(self.rid), plan1)
        self.assertEqual(len(self.claude_calls()), 1)
        task = (self.run_dir(self.rid) / "attempt-1" / "reviewer.prompt.md").read_text()
        self.assertIn("f1 returns x + 200", task)

    def test_trivial_no_triage(self):  # (3)
        root = self.make_repo("tiny")
        self.edit(root, "src/a.js", "// one more line\n", append=True)
        rid, out = self.begin(root)
        self.assertIn("prefilter: trivial change: 1 lines in 1 file(s) — reviewer only, no QA, no triage", out)
        out = self.brief(rid)
        self.assertEqual(self.claude_calls(), [])
        p = self.plan(rid)
        self.assertEqual(p["source"], "prefilter-trivial")
        self.assertEqual(p["roles"]["qa"]["scope"], "none")
        self.assertFalse(p["roles"]["qa"]["run"])
        self.assertEqual(p["roles"]["qa"]["skipped_by"], "prefilter")
        self.assertEqual((p["roles"]["reviewer"]["model"], p["roles"]["reviewer"]["effort"]), ("sonnet", "low"))
        tri = json.loads((self.run_dir(rid) / "attempt-1" / "triage.json").read_text())
        self.assertEqual((tri["source"], tri["agent"], tri["change_size"], tri["risk"]), ("prefilter", None, "small", "low"))
        lines = out.splitlines()
        self.assertEqual(lines[0], "verify-loop · plan · attempt 1 · prefilter (no triage)")
        self.assertEqual(lines[2], "  qa: none — trivial change: 1 lines in 1 file(s) (prefilter, no triage)")
        self.assertFalse((self.run_dir(rid) / "attempt-1" / "qa.prompt.md").exists())

    def test_trivial_rerun_keeps_prefilter_skipped_by(self):
        # attempt 2 of a prefilter-trivial loop showed "qa SKIPPED (triage: none)" (WP7 bug #5)
        root = self.make_repo("tiny")
        self.prompt(root)
        self.edit(root, "src/a.js", "// one more line\n", append=True)
        rid, _ = self.begin(root)
        self.brief(rid)
        _fail_attempt(self, rid, 1)
        self.edit(root, "src/a.js", "// fixed\n", append=True)
        self.ok("begin", "--rerun", "--run", rid, "--fix-notes", "-", stdin="R1: fixed", cwd=root)
        p2 = self.plan(rid, 2)
        self.assertEqual(p2["source"], "reused")
        self.assertEqual((p2["roles"]["qa"]["run"], p2["roles"]["qa"]["skipped_by"]), (False, "prefilter"))
        att = self.run_dir(rid) / "attempt-2"
        (att / "reviewer.json").write_text(json.dumps(h.reviewer_pass()))
        out = self.ok("judge", "--run", rid)
        self.assertIn("qa SKIPPED (prefilter: none)", out)


class HerdrLoopTest(h.Case):
    herdr = True

    def test_fail_rerun_pass(self):  # (6)
        root = self.make_repo()
        self.prompt(root)
        self.code_change(root)
        rid, _ = self.begin(root)
        self.assertEqual(self.ctx(rid)["transport"], "herdr")
        self.triage_output("checks")
        self.brief(rid)
        self.herdr_role("reviewer", verdict=h.reviewer_fail(), result="@verdict")
        self.herdr_role("qa", verdict=h.qa_pass(), result="QA done")
        out = self.ok("spawn", "reviewer", "--run", rid)
        self.assertEqual(out.splitlines(), ["reviewer: FAIL · 1 major, 0 minor · sonnet/low · 0s", "waiting for: qa"])
        out = self.ok("spawn", "qa", "--run", rid)
        self.assertEqual(out.splitlines()[-1], f"next: {VL} judge --run {rid}")
        att1 = self.run_dir(rid) / "attempt-1"
        self.assertTrue((att1 / "reviewer.final.md").exists())
        agents = self.ctx(rid)["attempts"][0]["agents"]
        self.assertEqual(agents["reviewer"]["herdr_id"], "verify-review-1a2b3c")
        self.assertEqual(agents["reviewer"]["session_id"], "sess-reviewer")
        self.assertEqual(agents["reviewer"]["status"], "done")
        out = self.ok("judge", "--run", rid)
        self.assertTrue(out.startswith("RESULT: FAIL\nverify-loop · attempt 1 · failures 1/3 · review FAIL (1 major)"))
        self.assertIn(f"  {VL} begin --rerun --run {rid} --fix-notes - <<'EOF'", out)
        loop = self.loop_state(root)["loop"]
        self.assertEqual((loop["status"], loop["last_result"], loop["failed_attempts"]), ("open", "fail", 1))
        self.assertIsNone(loop["inflight_since"])
        self.assertTrue(loop["last_status_line"].startswith("verify-loop · attempt 1 · failures 1/3"))

        # fix in a docs file only → delta without code: reviewer recheck, QA carried
        self.edit(root, "README.md", "note\n", append=True)
        rc, out, err = self.vl("begin", "--rerun", "--run", rid, "--fix-notes", "-",
                               stdin="R1: documented the behavior in README.md", cwd=root)
        self.assertEqual(rc, 0, err)
        self.assertTrue(out.startswith(f"verify-loop · run {rid} · attempt 2 · failures 1/3 · delta: 1 files, +1/−0"))
        att2 = self.run_dir(rid) / "attempt-2"
        for f in ("tree", "diff.patch", "diff.numstat.json", "delta.patch", "delta.numstat.json", "delta.hunks.json",
                  "fix-notes.md", "plan.json", "reviewer.prompt.md"):
            self.assertTrue((att2 / f).exists(), f)
        self.assertFalse((att2 / "qa.prompt.md").exists())
        p2 = self.plan(rid, 2)
        self.assertEqual(p2["source"], "reused")
        self.assertEqual(p2["roles"]["reviewer"]["mode"], "recheck")
        self.assertEqual((p2["roles"]["qa"]["run"], p2["roles"]["qa"]["carried_from"]), (False, 1))
        self.assertIn("  qa: carried from #1 (PASS)", out)
        self.assertEqual(out.splitlines()[-1], f"  {VL} spawn reviewer --run {rid}")
        task = (att2 / "reviewer.prompt.md").read_text()
        self.assertIn("- R1 [major·correctness·blocking] src/a.js:2 — f1 does not add 100", task)
        self.assertIn("R1: documented the behavior", task)
        self.assertIn(str(att2 / "delta.patch"), task)
        tri2 = json.loads((att2 / "triage.json").read_text())
        self.assertEqual((tri2["source"], tri2["reused_from"]), ("reused", 1))

        self.herdr_role("reviewer", verdict=h.reviewer_pass([{"id": "R1", "status": "fixed", "note": "ok",
                                                              "dispute_ruling": "n/a"}]), result="ok")
        out = self.ok("spawn", "reviewer", "--run", rid)
        self.assertEqual(out.splitlines()[-1], f"next: {VL} judge --run {rid}")
        out = self.ok("judge", "--run", rid)
        self.assertTrue(out.startswith("RESULT: PASS"), out)
        self.assertIn("qa PASS (carried from #1, 1/1 checks, checks)", out)
        self.assertEqual(out.splitlines()[-1], f"next: {VL} retro-plan --run {rid}")
        rs = self.loop_state(root)
        self.assertEqual((rs["loop"]["status"], rs["loop"]["retro"]), ("passed", "pending"))
        self.assertEqual(rs["touched"], [])  # touched filtered against the passed tree
        self.assertEqual(rs["baseline"]["trigger"], "closure")
        self.assertEqual(self.ctx(rid)["status"], "passed")
        names = [e["event"] for e in self.events()]
        for ev in ("spawn", "agent_done", "judge", "plan", "triage", "close"):
            self.assertIn(ev, names)


def _fail_attempt(case, rid, n, status="new"):
    att = case.run_dir(rid) / f"attempt-{n}"
    (att / "reviewer.json").write_text(json.dumps(h.reviewer_fail(status=status)))
    return case.ok("judge", "--run", rid)


def _escalate(case, root, rid):
    """Three failed attempts on a QA-less loop → escalated."""
    _fail_attempt(case, rid, 1)
    for n in (2, 3):
        case.edit(root, "src/a.js", f"export const try{n} = {n};\n", append=True)
        case.ok("begin", "--rerun", "--run", rid, "--fix-notes", "-", stdin=f"R1: attempt {n}", cwd=root)
        out = _fail_attempt(case, rid, n, status="still_present")
    return out


class EscalationTest(h.Case):
    def setUp(self):
        super().setUp()
        self.root = self.make_repo()
        self.prompt(self.root)
        self.code_change(self.root)
        self.rid, _ = self.begin(self.root, "--qa", "none")
        self.triage_output("checks")
        self.brief(self.rid)
        self.out = _escalate(self, self.root, self.rid)
        self.backup = self.tmp / "state-escalated"
        shutil.copytree(self.state, self.backup)

    def restore(self):
        shutil.rmtree(self.state)
        shutil.copytree(self.backup, self.state)

    def test_escalation_report(self):  # (7)
        lines = self.out.splitlines()
        self.assertEqual(lines[0], "RESULT: ESCALATE")
        self.assertTrue(lines[1].startswith("verify-loop · attempt 3 · failures 3/3 · review FAIL (1 major)"))
        self.assertTrue(any(ln.startswith("verify-loop · ESCALATION · 3 failed attempts · ") for ln in lines))
        self.assertIn(f"  Accept as is → {VL} decide accept --run {self.rid}", lines)
        self.assertNotIn("next: fix every blocking finding", self.out)
        self.assertEqual(self.loop_state(self.root)["loop"]["status"], "escalated")
        self.assertEqual(self.ctx(self.rid)["status"], "escalated")
        self.assertIn("escalate", [e["event"] for e in self.events()])
        out = self.ok("findings", "--run", self.rid)
        self.assertTrue(out.startswith("RESULT: ESCALATE"))
        j = json.loads(self.ok("findings", "--run", self.rid, "--attempt", "1", "--json"))
        self.assertEqual(j["result"], "fail")

    def test_decide_retry(self):
        self.restore()
        out = self.ok("decide", "retry", "--run", self.rid, "--note", "try a different approach")
        self.assertEqual(out.splitlines()[0], "verify-loop · decision: retry · failures reset to 0/3 · your note is "
                                              "added for the next agents")
        ctx = self.ctx(self.rid)
        self.assertEqual((ctx["status"], ctx["failed_attempts"], ctx["user_notes"]),
                         ("open", 0, ["try a different approach"]))
        self.assertEqual(self.loop_state(self.root)["loop"]["status"], "open")
        self.edit(self.root, "src/a.js", "export const try4 = 4;\n", append=True)
        out = self.ok("begin", "--rerun", "--run", self.rid, "--fix-notes", "-", stdin="R1: x", cwd=self.root)
        self.assertIn("attempt 4 · failures 0/3", out)
        task = (self.run_dir(self.rid) / "attempt-4" / "reviewer.prompt.md").read_text()
        self.assertIn("- try a different approach", task)

    def test_decide_waive(self):
        self.restore()
        out = self.ok("decide", "waive", "--run", self.rid, "--ids", "R1")
        lines = out.splitlines()
        self.assertEqual(lines[0], "verify-loop · decision: waive R1")
        self.assertEqual(lines[1], "RESULT: PASS")
        rs = self.loop_state(self.root)
        self.assertEqual((rs["loop"]["status"], rs["loop"]["retro"]), ("passed", "pending"))
        self.assertEqual(self.ctx(self.rid)["waived"], ["R1"])

    def test_decide_accept(self):
        self.restore()
        out = self.ok("decide", "accept", "--run", self.rid)
        self.assertEqual(out.splitlines(), ["verify-loop · decision: accept · loop closed (accepted with open "
                                            "findings: R1)", f"next: {VL} retro-plan --run {self.rid}"])
        rs = self.loop_state(self.root)
        self.assertEqual((rs["loop"]["status"], rs["loop"]["retro"]), ("accepted", "pending"))
        self.assertEqual(self.ctx(self.rid)["status"], "accepted")

    def test_decide_stop(self):
        self.restore()
        out = self.ok("decide", "stop", "--run", self.rid)
        self.assertEqual(out.splitlines()[0], "verify-loop · decision: stop · loop closed (abandoned) · the change is "
                                              "UNVERIFIED — say so in your final message and list: R1")
        rs = self.loop_state(self.root)
        self.assertEqual((rs["loop"]["status"], rs["loop"]["retro"]), ("abandoned", "pending"))
        decide = [e for e in self.events() if e["event"] == "decide"]
        self.assertEqual(decide[-1]["data"]["action"], "stop")
        rc, _, err = self.vl("decide", "retry", "--run", self.rid)
        self.assertEqual(rc, 2)


class AgentTransportTest(h.Case):
    def setUp(self):
        super().setUp()
        self.root = self.make_repo()
        self.prompt(self.root)
        self.code_change(self.root)
        self.rid, _ = self.begin(self.root)
        self.triage_output("checks")
        self.out = self.brief(self.rid)
        self.att = self.run_dir(self.rid) / "attempt-1"

    def test_agent_lines_collect_record(self):  # (8)
        lines = self.out.splitlines()
        i = lines.index("next (Agent tool, parallel — all calls in ONE message, run_in_background: true):")
        self.assertEqual(lines[i + 1], f'  Agent(subagent_type="verify-reviewer", model="sonnet", description="verify '
                                       f'reviewer #1", prompt="Your task file is {self.att}/reviewer.prompt.md. Read it '
                                       f'completely and follow it.")')
        self.assertEqual(lines[i + 2], f'  Agent(subagent_type="verify-qa", model="haiku", description="verify qa #1", '
                                       f'prompt="Your task file is {self.att}/qa.prompt.md. Read it completely and '
                                       f'follow it.")')
        self.assertEqual(lines[i + 3], f"then: {VL} collect --run {self.rid}")
        agents = self.ctx(self.rid)["attempts"][0]["agents"]
        self.assertEqual((agents["qa"]["transport"], agents["qa"]["effort"]), ("agent", "medium"))
        rc, _, err = self.vl("spawn", "reviewer", "--run", self.rid)
        self.assertEqual(rc, 2)
        self.assertIn("this session uses the Agent-tool transport; follow the plan's next: lines", err)

        (self.att / "reviewer.json").write_text(json.dumps(h.reviewer_pass()))
        out = self.ok("collect", "--run", self.rid)
        lines = out.splitlines()
        self.assertTrue(lines[0].startswith("reviewer: PASS · 0 blocking, 0 minor, 0 suggestions · sonnet/medium"))
        self.assertIn(f"{VL} record qa --run {self.rid}", lines[1])
        self.assertEqual(lines[-1], "waiting for: qa")
        rc, _, err = self.vl("judge", "--run", self.rid)
        self.assertEqual((rc, err.strip()), (2, "vloop: error: waiting for: qa"))

        msg = "QA finished.\n\n```json\n" + json.dumps(h.qa_pass()) + "\n```\n"
        final = self.tmp / "final.md"
        final.write_text(msg)
        out = self.ok("record", "qa", "--run", self.rid, "--file", final)
        self.assertTrue(out.startswith("qa: PASS · 1/1 checks · checks · haiku/medium"))
        self.assertEqual(out.splitlines()[-1], f"next: {VL} judge --run {self.rid}")
        self.assertEqual(json.loads((self.att / "qa.json").read_text())["run_id"], self.rid)
        self.assertEqual((self.att / "qa.final.md").read_text(), msg)
        out = self.ok("judge", "--run", self.rid)
        self.assertTrue(out.startswith("RESULT: PASS"))

    def test_record_garbage_is_bad_verdict(self):
        out = self.ok("record", "reviewer", "--run", self.rid, stdin="no json here")
        self.assertIn("reviewer: INCONCLUSIVE · bad verdict", out)
        self.assertEqual(json.loads((self.att / "reviewer.error.json").read_text())["status"], "bad_verdict")

    def test_taint(self):  # (10) a file changed between brief and judge without touch events
        (self.att / "reviewer.json").write_text(json.dumps(h.reviewer_pass()))
        (self.att / "qa.json").write_text(json.dumps(h.qa_pass()))
        h.write(self.root / "src/side-effect.js", "generated by QA\n")
        out = self.ok("judge", "--run", self.rid)
        self.assertTrue(out.startswith("RESULT: INCONCLUSIVE"), out)
        self.assertIn("verifier modified files: src/side-effect.js", out)
        taint = json.loads((self.att / "qa.taint.json").read_text())
        self.assertEqual(taint, {"changed_files": ["src/side-effect.js"], "main_edits": False, "tainted": True})
        j = json.loads((self.att / "judge.json").read_text())
        for role in ("reviewer", "qa"):
            self.assertEqual((j["roles"][role]["state"], j["roles"][role]["modified_files"]),
                             ("inconclusive", ["src/side-effect.js"]))

    def test_integrity_fingerprint_and_collect(self):
        integ = json.loads((self.att / "integrity.json").read_text())
        self.assertEqual(sorted(integ["roles"]), ["qa", "reviewer"])
        self.assertRegex(integ["before"], r"^[0-9a-f]{40}$")
        h.write(self.root / "src/a.js", "// rewritten by a verifier\n")
        (self.att / "reviewer.json").write_text(json.dumps(h.reviewer_pass()))
        out = self.ok("collect", "--run", self.rid)
        self.assertIn("reviewer: integrity · verifier modified files: src/a.js → INCONCLUSIVE", out)
        self.assertIn("qa: integrity · verifier modified files: src/a.js → INCONCLUSIVE", out)
        integ = json.loads((self.att / "integrity.json").read_text())
        self.assertEqual((integ["changed"], integ["flagged"]), (["src/a.js"], {"qa": ["src/a.js"],
                                                                                "reviewer": ["src/a.js"]}))
        (self.att / "qa.json").write_text(json.dumps(h.qa_pass()))
        out = self.ok("judge", "--run", self.rid)
        self.assertTrue(out.startswith("RESULT: INCONCLUSIVE"), out)
        self.assertIn("review INCONCLUSIVE (verifier modified files: src/a.js)", out)

    def test_integrity_qa_allowed_paths(self):
        self.write_config(integrity={"qa_allowed_paths": ["reports"]})
        h.write(self.root / "reports/run.txt", "qa output\n")
        (self.att / "reviewer.json").write_text(json.dumps(h.reviewer_pass()))
        (self.att / "qa.json").write_text(json.dumps(h.qa_pass()))
        out = self.ok("judge", "--run", self.rid)
        self.assertTrue(out.startswith("RESULT: PASS"), out)
        integ = json.loads((self.att / "integrity.json").read_text())
        self.assertEqual((integ["changed"], integ["flagged"]), (["reports/run.txt"], {"qa": [], "reviewer": []}))
        self.assertFalse(json.loads((self.att / "qa.taint.json").read_text())["tainted"])

    def test_integrity_disabled(self):
        self.write_config(integrity={"enabled": False})
        h.write(self.root / "src/a.js", "// rewritten\n")
        (self.att / "reviewer.json").write_text(json.dumps(h.reviewer_pass()))
        (self.att / "qa.json").write_text(json.dumps(h.qa_pass()))
        self.ok("judge", "--run", self.rid)
        j = json.loads((self.att / "judge.json").read_text())
        self.assertEqual(j["roles"]["reviewer"]["state"], "pass")
        self.assertNotIn("flagged", json.loads((self.att / "integrity.json").read_text()))

    def test_main_edit_is_warning_only(self):  # (10) with a touch event
        (self.att / "reviewer.json").write_text(json.dumps(h.reviewer_pass()))
        (self.att / "qa.json").write_text(json.dumps(h.qa_pass()))
        self.edit(self.root, "src/a.js", "// main agent edit\n", append=True)
        out = self.ok("judge", "--run", self.rid)
        self.assertTrue(out.startswith("RESULT: PASS"), out)
        self.assertIn("Warnings: tree changed during verification (main agent edited src/a.js)", out)
        self.assertTrue(json.loads((self.att / "qa.taint.json").read_text())["main_edits"])
        integ = json.loads((self.att / "integrity.json").read_text())
        self.assertEqual((integ["main_edits"], integ["flagged"]), (["src/a.js"], {"qa": [], "reviewer": []}))

    def test_dispute(self):
        (self.att / "reviewer.json").write_text(json.dumps(h.reviewer_fail()))
        (self.att / "qa.json").write_text(json.dumps(h.qa_pass()))
        self.ok("judge", "--run", self.rid)
        out = self.ok("dispute", "R1", "--run", self.rid, "--reason", "f1 is documented to add 1")
        self.assertEqual(out.strip(), "disputed R1: the next reviewer will rule on it")
        self.assertEqual(self.ctx(self.rid)["disputes"]["R1"], {"reason": "f1 is documented to add 1", "attempt": 1})
        rc, _, _ = self.vl("dispute", "R9", "--run", self.rid, "--reason", "x")
        self.assertEqual(rc, 2)
        rc, out, err = self.vl("begin", "--rerun", "--run", self.rid, cwd=self.root)
        self.assertEqual(rc, 0, err)
        self.assertIn("no --fix-notes", err)
        self.assertNotIn("no changes since attempt 1", err)  # a new dispute counts as a change
        task = (self.run_dir(self.rid) / "attempt-2" / "reviewer.prompt.md").read_text()
        self.assertIn("- R1: f1 is documented to add 1", task)

    def test_rerun_requires_judged_failure(self):
        rc, _, err = self.vl("begin", "--rerun", "--run", self.rid, cwd=self.root)
        self.assertEqual(rc, 2)
        self.assertIn("has not been judged yet", err)
        rc, out, err = self.vl("begin", cwd=self.root)  # open loop → treated as --rerun, same precondition
        self.assertEqual(rc, 2)


class HerdrIntegrityTest(h.Case):
    herdr = True

    def test_first_fingerprint_is_kept(self):
        root = self.make_repo()
        self.prompt(root)
        self.code_change(root)
        rid, _ = self.begin(root)
        self.triage_output("checks")
        self.brief(rid)
        att = self.run_dir(rid) / "attempt-1"
        self.assertFalse((att / "integrity.json").exists(), "vloop-run roles fingerprint at spawn")
        self.herdr_role("reviewer", verdict=h.reviewer_pass(), result="ok")
        self.ok("spawn", "reviewer", "--run", rid)
        before = json.loads((att / "integrity.json").read_text())["before"]
        h.write(root / "src/new.js", "made while verifying\n")
        self.herdr_role("qa", verdict=h.qa_pass(), result="ok")
        self.ok("spawn", "qa", "--run", rid)
        integ = json.loads((att / "integrity.json").read_text())
        self.assertEqual((integ["before"], integ["roles"]), (before, ["reviewer", "qa"]))
        out = self.ok("judge", "--run", rid)
        self.assertTrue(out.startswith("RESULT: INCONCLUSIVE"), out)
        self.assertIn("verifier modified files: src/new.js", out)


class ConsentLoopTest(h.Case):  # (15)
    herdr = True

    def start(self, policy=None, triage="browser"):
        if policy:
            self.write_config(qa_browser_consent=policy)
        self.root = self.make_repo()
        self.prompt(self.root)
        self.code_change(self.root)
        self.edit(self.root, "src/page.html", "<p>new</p>\n", append=True)
        self.rid, _ = self.begin(self.root)
        self.triage_output(triage)
        self.att = self.run_dir(self.rid) / "attempt-1"
        return self.brief(self.rid)

    def assert_no_qa_files(self, att):
        for f in ("qa.prompt.md", "pw-pointer.json", "mcp.json"):
            self.assertFalse((att / f).exists(), f)
        self.assertFalse((self.state / "qa-current").exists() and any((self.state / "qa-current").iterdir()))

    def test_pending_then_allow(self):
        out = self.start()
        self.assert_no_qa_files(self.att)
        self.assertTrue((self.att / "reviewer.prompt.md").exists())
        self.assertIsNone(self.loop_state(self.root)["loop"]["inflight_since"])
        lines = out.splitlines()
        i = lines.index(f"verify-loop · CONSENT NEEDED · browser QA (Playwright, headless Chrome) · run {self.rid} · "
                        f"attempt 1")
        self.assertEqual(lines[i + 1], "  why: UI template changed (page.html); flow reachable at /scan")
        self.assertEqual(lines[i + 2], "  url: not set — QA will start the app per recipes   auth: missing")
        self.assertEqual(lines[i + 3:i + 5], ["  checks:", "    - step 3 Next button"])
        self.assertEqual(lines[i + 5:], ['next: ask the user with AskUserQuestion (header "Browser QA"):',
                                         f"  Run browser QA → {VL} consent allow --run {self.rid}",
                                         f"  QA without browser (tests/run only) → {VL} consent no-browser --run "
                                         f"{self.rid}",
                                         f"  Skip QA for this change → {VL} consent skip-qa --run {self.rid}"])
        self.assertFalse(any(ln.startswith("next (") for ln in lines))
        bc = self.ctx(self.rid)["browser_consent"]
        self.assertEqual((bc["status"], bc["source"], bc["decision"], bc["attempt"]), ("pending", None, None, 1))
        for cmd in (("judge", "--run", self.rid), ("spawn", "qa", "--run", self.rid), ("collect", "--run", self.rid)):
            rc, _, err = self.vl(*cmd)
            self.assertEqual((rc, err.strip()), (2, "vloop: error: waiting for browser QA consent"), cmd)
        self.assertIn("consent_pending", [e["event"] for e in self.events()])

        out = self.ok("consent", "allow", "--run", self.rid, "--note", "staging is up")
        self.assertEqual(out.splitlines(), [
            "verify-loop · consent: browser QA approved",
            "next (Herdr, parallel — one Bash call per line in ONE message, run_in_background: true):",
            f"  {VL} spawn reviewer --run {self.rid}", f"  {VL} spawn qa --run {self.rid}"])
        for f in ("qa.prompt.md", "pw-pointer.json", "mcp.json"):
            self.assertTrue((self.att / f).exists(), f)
        self.assertTrue((self.state / "qa-current" / f"{self.ctx(self.rid)['repo']['key']}.json").exists())
        self.assertIsNotNone(self.loop_state(self.root)["loop"]["inflight_since"])
        ctx = self.ctx(self.rid)
        self.assertEqual((ctx["browser_consent"]["status"], ctx["browser_consent"]["source"],
                          ctx["browser_consent"]["note"]), ("granted", "user", "staging is up"))
        self.assertEqual(ctx["user_notes"], ["staging is up"])
        ev = [e for e in self.events() if e["event"] == "consent"]
        self.assertEqual(ev[-1]["data"], {"decision": "allow", "note": "staging is up"})
        rc, _, err = self.vl("consent", "allow", "--run", self.rid)
        self.assertEqual((rc, err.strip()), (2, f"vloop: error: no browser consent pending for run {self.rid}"))

        # attempt 2 reuses the grant silently
        self.herdr_role("reviewer", verdict=h.reviewer_fail(), result="x")
        self.herdr_role("qa", verdict=h.qa_pass("browser"), result="x")
        self.ok("spawn", "reviewer", "--run", self.rid)
        self.ok("spawn", "qa", "--run", self.rid)
        self.ok("judge", "--run", self.rid)
        self.edit(self.root, "src/a.js", "export const z = 1;\n", append=True)
        out = self.ok("begin", "--rerun", "--run", self.rid, "--fix-notes", "-", stdin="R1: x", cwd=self.root)
        self.assertIn("  consent: browser QA approved earlier in this run", out)
        self.assertNotIn("CONSENT NEEDED", out)
        self.assertTrue((self.run_dir(self.rid) / "attempt-2" / "qa.prompt.md").exists())

    def test_no_browser_then_declined_reused(self):
        self.start()
        out = self.ok("consent", "no-browser", "--run", self.rid)
        lines = out.splitlines()
        self.assertEqual(lines[0], "verify-loop · consent: browser QA declined → QA without browser (run)")
        self.assertIn("  qa: run · full · sonnet/medium — user declined browser QA", lines)
        self.assertEqual(lines[-1], f"  {VL} spawn qa --run {self.rid}")
        p = self.plan(self.rid)
        self.assertEqual((p["roles"]["qa"]["scope"], p["roles"]["qa"]["auth_status"]), ("run", "not_needed"))
        self.assertFalse((self.att / "mcp.json").exists())
        task = (self.att / "qa.prompt.md").read_text()
        self.assertIn("No browser in this scope: the user declined browser QA.", task)
        self.assertEqual(sum(1 for e in self.events() if e["event"] == "plan"), 2)
        # attempt 2: material re-triage wants browser again, no UI delta → declined reused without asking
        self.herdr_role("reviewer", verdict=h.reviewer_fail(), result="x")
        self.herdr_role("qa", verdict=h.qa_pass("run"), result="x")
        self.ok("spawn", "reviewer", "--run", self.rid)
        self.ok("spawn", "qa", "--run", self.rid)
        self.ok("judge", "--run", self.rid)
        self.edit(self.root, "src/new.js", "export const n = 1;\n")
        out = self.ok("begin", "--rerun", "--run", self.rid, "--fix-notes", "-", stdin="R1: x", cwd=self.root)
        self.assertNotIn("CONSENT NEEDED", out)
        p2 = self.plan(self.rid, 2)
        self.assertEqual(p2["source"], "triage")
        self.assertEqual((p2["roles"]["qa"]["scope"], p2["roles"]["qa"]["reason"]),
                         ("run", "user declined browser QA earlier in this run"))

    def test_skip_qa_sticky(self):
        self.start()
        out = self.ok("consent", "skip-qa", "--run", self.rid)
        self.assertEqual(out.splitlines()[0], "verify-loop · consent: QA skipped by user")
        self.assertIn("  qa: none — user skipped QA", out)
        p = self.plan(self.rid)
        self.assertEqual((p["roles"]["qa"]["run"], p["roles"]["qa"]["skipped_by"]), (False, "user"))
        self.assertFalse((self.att / "qa.prompt.md").exists())
        self.assertEqual(out.splitlines()[-1], f"  {VL} spawn reviewer --run {self.rid}")
        self.herdr_role("reviewer", verdict=h.reviewer_fail(), result="x")
        self.ok("spawn", "reviewer", "--run", self.rid)
        out = self.ok("judge", "--run", self.rid)
        self.assertIn("qa SKIPPED (user: none)", out)
        # material re-triage raises QA to checks: still skipped (sticky)
        self.triage_output("checks")
        self.edit(self.root, "src/new.js", "export const n = 1;\n")
        self.ok("begin", "--rerun", "--run", self.rid, "--fix-notes", "-", stdin="R1: x", cwd=self.root)
        p2 = self.plan(self.rid, 2)
        self.assertEqual((p2["roles"]["qa"]["run"], p2["roles"]["qa"]["skipped_by"]), (False, "user"))
        self.herdr_role("reviewer", verdict=h.reviewer_fail(status="still_present"), result="x")
        self.ok("spawn", "reviewer", "--run", self.rid)
        self.ok("judge", "--run", self.rid)
        # a UI delta that re-plans browser asks again
        self.triage_output("browser")
        self.edit(self.root, "src/page.html", "<p>again</p>\n", append=True)
        out = self.ok("begin", "--rerun", "--run", self.rid, "--fix-notes", "-", stdin="R1: y", cwd=self.root)
        self.assertIn("CONSENT NEEDED", out)
        self.assertEqual(self.ctx(self.rid)["browser_consent"]["status"], "pending")

    def test_policy_always(self):
        out = self.start(policy="always")
        self.assertIn("  consent: browser QA approved (config)", out)
        self.assertTrue((self.att / "qa.prompt.md").exists())
        bc = self.ctx(self.rid)["browser_consent"]
        self.assertEqual((bc["status"], bc["source"], bc["decision"]), ("granted", "config", "always"))

    def test_policy_never(self):
        out = self.start(policy="never")
        self.assertNotIn("CONSENT NEEDED", out)
        p = self.plan(self.rid)
        self.assertEqual((p["roles"]["qa"]["scope"], p["roles"]["qa"]["reason"]),
                         ("run", "browser QA disabled by config (qa_browser_consent=never)"))
        self.assertEqual((p["roles"]["qa"]["model"], p["roles"]["qa"]["effort"]), ("sonnet", "medium"))
        bc = self.ctx(self.rid)["browser_consent"]
        self.assertEqual((bc["status"], bc["source"], bc["decision"]), ("declined", "config", "never"))
        self.assertFalse((self.att / "mcp.json").exists())


class RealTemplatesTest(h.Case):
    """WP5's prompts/*.md render with every value WP2 produces (no unknown placeholder)."""
    herdr = True

    def test_real_templates_render(self):
        tpl = h.SKILL / "prompts"
        if not all((tpl / f"{r}.md").exists() for r in ("triage", "reviewer", "qa")):
            self.skipTest("WP5 templates not installed")
        h.prompts.TEMPLATES_DIR = None
        self.write_config(qa_browser_consent="always")
        root = self.make_repo()
        self.prompt(root)
        self.code_change(root)
        rid, _ = self.begin(root)
        self.triage_output("browser")
        self.brief(rid)
        att = self.run_dir(rid) / "attempt-1"
        for f in ("triage.input.md", "reviewer.prompt.md", "qa.prompt.md"):
            text = (att / f).read_text()
            self.assertNotIn("{{", text, f)
        self.assertIn(str(att / "reviewer.json"), (att / "reviewer.prompt.md").read_text())

    def test_cli_entry_point(self):
        import subprocess
        p = subprocess.run([sys.executable, str(h.VLOOP), "findings", "--run", "nope"], capture_output=True,
                           text=True, env=dict(h.os.environ), timeout=60)
        self.assertEqual((p.returncode, p.stderr.strip()), (2, "vloop: error: unknown run nope"))
        p = subprocess.run([sys.executable, str(h.VLOOP), "--help"], capture_output=True, text=True,
                           env={**h.os.environ, "VLOOP_STRICT": "1"}, timeout=60)
        self.assertEqual(p.returncode, 0, p.stderr)
        for cmd in ("begin", "brief", "spawn-wait", "collect", "record", "judge", "consent", "dispute", "decide",
                    "findings"):
            self.assertIn(cmd, p.stdout)


if __name__ == "__main__":
    unittest.main()
