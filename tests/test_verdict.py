"""Tests for vlooplib.verdict (WP3). Run: python3 -m unittest discover -s tests"""
from __future__ import annotations

import copy
import json
import os
import shutil
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from helpers import SKILL, VL, HermeticCase  # noqa: E402

from vlooplib import verdict as V  # noqa: E402

FIX = HERE / "fixtures" / "wp3"
SPEC_TRIAGE_OUTPUT = FIX / "triage.output.schema.expected.json"
UPDATE_GOLDEN = os.environ.get("WP3_UPDATE_GOLDEN") == "1"
RUN_ID = "20261015-122001-demo-3fa9"
SCENARIO = "Publish fails twice, then succeeds; the next failure gives up after one retry."


def rfinding(**kw) -> dict:
    f = {"id": "R1", "severity": "major", "confidence": "high", "category": "correctness",
         "file": "src/publisher.ts", "line": 3, "end_line": None, "title": "Retry counter never resets",
         "problem": "attempts only reset in the constructor.", "scenario": SCENARIO,
         "suggestion": "Reset this.attempts = 0 after a successful send.", "criterion": "", "convention_ref": "",
         "command": "", "status": "new", "missed_earlier": False, "missed_reason": ""}
    f.update(kw)
    return f


def reviewer(findings=(), verdict="fail", **kw) -> dict:
    v = {"schema": 2, "role": "reviewer", "run_id": RUN_ID, "attempt": 1, "mode": "full", "verdict": verdict,
         "summary": "summary", "findings": list(findings), "suggestions": [], "rechecked": [],
         "coverage": {"files_reviewed": 1, "files_skipped": [], "note": ""}}
    v.update(kw)
    return v


def check(cid="C1", result="pass", method="test", **kw) -> dict:
    c = {"id": cid, "criterion": f"criterion {cid}", "method": method, "result": result,
         "evidence": ["npm test → exit 0"], "notes": "", "need": None, "logged_in": False}
    c.update(kw)
    return c


def qfinding(**kw) -> dict:
    f = {"id": "Q1", "severity": "blocker", "confidence": "high", "category": "acceptance", "check_id": "C2",
         "title": "Next button does nothing", "expected": "Step 4 is shown", "actual": "Stays on step 3",
         "repro": ["open /scan", "click Next"], "evidence": ["shot.png"], "file": "", "line": None, "url": "",
         "command": "", "pre_existing": "no", "status": "new"}
    f.update(kw)
    return f


def qa(checks=None, findings=(), verdict="pass", scope="browser", **kw) -> dict:
    v = {"schema": 2, "role": "qa", "run_id": RUN_ID, "attempt": 1, "mode": "full", "scope": scope,
         "verdict": verdict, "summary": "qa summary", "checks": list(checks if checks is not None else [check()]),
         "findings": list(findings), "suggestions": [], "unverified": [], "skipped": [], "env": {}}
    v.update(kw)
    return v


class Base(HermeticCase):
    def setUp(self):
        super().setUp()
        os.environ["VERIFY_STATE_DIR"] = str(self.tmp / "state")
        os.environ["VERIFY_CONFIG"] = str(self.tmp / "config.json")
        self.repo = self.tmp / "repo"
        (self.repo / "src").mkdir(parents=True)
        (self.repo / "src" / "publisher.ts").write_text("".join(f"line {i}\n" for i in range(1, 21)))
        self.run = self.tmp / "state" / "runs" / RUN_ID
        self.run.mkdir(parents=True)
        self.ctx = {"schema": 1, "run_id": RUN_ID, "kind": "loop", "session_id": "s1",
                    "repo": {"key": "3fa9c1d2e4b5", "root": str(self.repo), "slug": "demo", "branch": "x",
                             "head": "abc"},
                    "target": {"spec": "session", "base_tree": "t", "base_kind": "snapshot", "code_ref": "worktree",
                               "paths": []},
                    "roles": ["reviewer", "qa"], "app_url": "https://localhost:8100", "transport": "herdr",
                    "attempts": [], "failed_attempts": 0, "inconclusive_streak": 0, "status": "open",
                    "waived": [], "disputes": {}, "user_notes": [],
                    "flags": {"qa": None, "review": None, "no_triage": False, "model": None},
                    "browser_consent": None, "standalone": None}
        self.write_ctx()

    def write_ctx(self, **kw):
        self.ctx.update(kw)
        (self.run / "context.json").write_text(json.dumps(self.ctx))

    def attempt(self, n=1, rev=None, q=None, plan=None, hunks=None, taint=None, rev_error=None, qa_error=None,
                deleted=(), fix_notes=None):
        a = self.run / f"attempt-{n}"
        a.mkdir(exist_ok=True)
        if plan is None:
            plan = {"reviewer": {"run": True, "mode": "full"}, "qa": {"run": True, "mode": "full", "scope": "browser"}}
        (a / "plan.json").write_text(json.dumps({"schema": 1, "run_id": RUN_ID, "attempt": n, "roles": plan}))
        for name, obj in (("reviewer.json", rev), ("qa.json", q), ("delta.hunks.json", hunks),
                          ("qa.taint.json", taint), ("reviewer.error.json", rev_error),
                          ("qa.error.json", qa_error)):
            if obj is not None:
                if isinstance(obj, dict):
                    obj = dict(obj)
                    if name in ("reviewer.json", "qa.json"):
                        obj["attempt"] = n
                (a / name).write_text(json.dumps(obj) if not isinstance(obj, str) else obj)
        (a / "diff.numstat.json").write_text(json.dumps({"files": 1, "deleted_files": list(deleted), "rows": []}))
        if fix_notes:
            (a / "fix-notes.md").write_text(fix_notes)
        return a

    def judge(self, n=1, **kw):
        return V.judge_attempt(self.run, n, **kw)

    def rf(self, j, fid):
        return next(f for f in j["findings"] if f["id"] == fid)


# ------------------------------------------------------------------ schemas, validate, normalize

class TestSchemas(Base):
    def test_triage_output_schema_byte_for_byte(self):
        spec = SPEC_TRIAGE_OUTPUT.read_text()
        actual = (SKILL / "schema" / "triage.output.schema.json").read_text()
        self.assertEqual(actual, spec)

    def test_schema_files_use_only_supported_keywords(self):
        allowed = {"type", "enum", "required", "properties", "additionalProperties", "items", "minItems",
                   "maxItems", "minLength", "maxLength", "minimum", "pattern"}

        def walk(node):
            if isinstance(node, dict):
                for k, v in node.items():
                    if k == "properties":
                        for sub in v.values():
                            walk(sub)
                    else:
                        self.assertIn(k, allowed)
                        walk(v)
            elif isinstance(node, list):
                for x in node:
                    walk(x)

        for kind in V.SCHEMA_FILES:
            walk(V.load_schema(kind))


class TestValidateNormalize(Base):
    def test_reviewer_valid(self):
        v = reviewer([rfinding()], suggestions=[{"id": "S1", "file": "a.ts", "line": 3, "text": "helper"}])
        self.assertEqual(V.validate("reviewer", v), [])
        n, w = V.normalize("reviewer", v, run_id=RUN_ID, attempt=1, mode="full")
        self.assertEqual(V.validate("reviewer", n), [])
        self.assertEqual(w, [])

    def test_reviewer_repairable(self):
        f = rfinding(line="142", severity="Major", id="X")
        f.pop("status")
        f.pop("scenario")
        v = reviewer([f, rfinding(id="R1")], run_id="wrong", attempt="2", role="Reviewer",
                     suggestions=[{"id": "s", "text": "t" * 600}] + [{"text": f"s{i}"} for i in range(6)],
                     summary="x" * 1200)
        v.pop("mode")
        self.assertTrue(V.validate("reviewer", v))
        n, w = V.normalize("reviewer", v, run_id=RUN_ID, attempt=1, mode="recheck")
        self.assertEqual(V.validate("reviewer", n), [], n)
        self.assertEqual(n["findings"][0]["line"], 142)
        self.assertEqual(n["findings"][0]["severity"], "major")
        self.assertEqual(n["findings"][0]["status"], "new")
        self.assertEqual(n["findings"][0]["scenario"], "")
        self.assertEqual([f["id"] for f in n["findings"]], ["R2", "R1"])
        self.assertEqual((n["run_id"], n["attempt"], n["role"], n["mode"]), (RUN_ID, 1, "reviewer", "recheck"))
        self.assertEqual(len(n["suggestions"]), 5)
        self.assertEqual(len(n["summary"]), 1000)
        self.assertTrue(any("run_id" in x for x in w))
        self.assertEqual(v["run_id"], "wrong")  # input untouched

    def test_reviewer_coverage_list_repaired(self):
        # live reviewer wrote files_reviewed as the list of files (WP7 bug #2, run 8e17)
        v = reviewer([], verdict="pass", coverage={"files_reviewed": ["cli.js", "lib/sum.js"],
                                                   "files_skipped": "dist/x.js", "note": ["a", "b"]})
        self.assertIn("coverage.files_reviewed: expected integer", V.validate("reviewer", v))
        n, w = V.normalize("reviewer", v, run_id=RUN_ID, attempt=1, mode="full")
        self.assertEqual(V.validate("reviewer", n), [], n)
        self.assertEqual(n["coverage"], {"files_reviewed": 2, "files_skipped": ["dist/x.js"], "note": "a; b"})
        self.assertTrue(any("coverage.files_reviewed: coerced" in x for x in w))

    def test_qa_type_repairs(self):
        c = check(evidence="shot.png", notes=["line 1", "line 2"], logged_in="yes")
        f = qfinding(expected=["0"], actual=["1"], evidence={"path": "a.png"})
        n, _ = V.normalize("qa", qa([c], [f], verdict="fail"), run_id=RUN_ID, attempt=1)
        self.assertEqual(V.validate("qa", n), [], n)
        self.assertEqual((n["checks"][0]["evidence"], n["checks"][0]["notes"], n["checks"][0]["logged_in"]),
                         (["shot.png"], "line 1; line 2", True))
        self.assertEqual((n["findings"][0]["expected"], n["findings"][0]["actual"]), ("0", "1"))
        self.assertEqual(n["findings"][0]["evidence"], ['{"path": "a.png"}'])

    def test_reviewer_invalid(self):
        v = reviewer([rfinding(severity="nightmare", category="style")])
        v.pop("summary")
        n, _ = V.normalize("reviewer", v, run_id=RUN_ID, attempt=1)
        errs = V.validate("reviewer", n)
        self.assertIn("summary: required", errs)
        self.assertIn("findings[0].severity: not in enum", errs)
        self.assertIn("findings[0].category: not in enum", errs)
        self.assertEqual(V.validate("reviewer", "nope"), ["(root): expected object"])

    def test_reviewer_nit_and_unknown_fields_kept(self):
        n, w = V.normalize("reviewer", reviewer([rfinding(severity="nit", extra="kept")], extra_top=1))
        self.assertEqual(n["findings"][0]["severity"], "minor")
        self.assertEqual(n["findings"][0]["extra"], "kept")
        self.assertEqual(n["extra_top"], 1)

    def test_qa_valid(self):
        v = qa([check(), check("C2", "fail")], [qfinding()], verdict="fail",
               suggestions=[{"id": "QS1", "text": "spinner"}],
               unverified=[{"check_id": "C3", "need": "auth_expired", "detail": "d", "how_to_fix": ""}])
        self.assertEqual(V.validate("qa", v), [])
        n, w = V.normalize("qa", v, run_id=RUN_ID, attempt=1)
        self.assertEqual(V.validate("qa", n), [])

    def test_qa_repairable_renumbers_checks_and_references(self):
        c = check("check-1", "fail", method="Browser")
        c.pop("evidence")
        f = qfinding(check_id="check-1", repro="click Next", line="12")
        f.pop("pre_existing")
        v = qa([c], [f], verdict="FAIL", unverified=[{"check_id": "check-1", "need": ""}])
        v.pop("suggestions")
        n, w = V.normalize("qa", v, run_id=RUN_ID, attempt=1)
        self.assertEqual(V.validate("qa", n), [], n)
        self.assertEqual(n["checks"][0]["id"], "C1")
        self.assertEqual(n["checks"][0]["method"], "browser")
        self.assertEqual(n["findings"][0]["check_id"], "C1")
        self.assertEqual(n["findings"][0]["repro"], ["click Next"])
        self.assertEqual(n["findings"][0]["line"], 12)
        self.assertEqual(n["findings"][0]["pre_existing"], "no")
        self.assertEqual(n["unverified"][0], {"check_id": "C1", "need": None, "detail": "", "how_to_fix": ""})
        self.assertEqual(n["verdict"], "fail")
        self.assertEqual(n["suggestions"], [])

    def test_qa_invalid(self):
        v = qa([check(result="maybe")], verdict="pass")
        v.pop("checks")
        errs = V.validate("qa", V.normalize("qa", v)[0])
        self.assertIn("checks: required", errs)
        errs = V.validate("qa", qa([check(method="vibes")]))
        self.assertIn("checks[0].method: not in enum", errs)

    def test_triage_kinds(self):
        out = {"change_size": "medium", "risk": "low",
               "qa": {"scope": "run", "reason": "r", "focus": [], "needs_login": False},
               "review": {"tier": "standard", "reason": "r", "focus": ["a"]},
               "specialists": {"security": {"run": True, "reason": "r", "focus": ["auth.ts"]},
                               "perf": {"run": False, "reason": "", "focus": []},
                               "ux": {"run": False, "ui_spec": "n/a", "reason": "", "focus": []}},
               "confidence": "high"}
        self.assertEqual(V.validate("triage-output", out), [])
        self.assertIn("specialists: required", V.validate("triage-output",
                                                          {k: v for k, v in out.items() if k != "specialists"}))
        self.assertIn("specialists.ux.ui_spec: not in enum", V.validate(
            "triage-output", dict(out, specialists=dict(out["specialists"], ux={"run": False, "ui_spec": "maybe",
                                                                                 "reason": "", "focus": []}))))
        bad = dict(out, extra=1)
        self.assertIn("extra: unexpected field", V.validate("triage-output", bad))
        self.assertIn("risk: not in enum", V.validate("triage-output", dict(out, risk="huge")))
        stored = dict(copy.deepcopy(out), qa={"scope": "Browser", "reason": "x" * 300, "focus": ["f" * 200] * 7,
                                              "needs_login": "true"})
        n, w = V.normalize("triage", stored, run_id=RUN_ID, attempt=1)
        self.assertEqual(V.validate("triage", n), [], n)
        self.assertEqual((n["schema"], n["role"], n["source"], n["agent"]), (1, "triage", "triage", None))
        self.assertEqual(n["qa"]["scope"], "browser")
        self.assertEqual(len(n["qa"]["reason"]), 160)
        self.assertEqual(len(n["qa"]["focus"]), 5)
        self.assertTrue(all(len(x) <= 120 for x in n["qa"]["focus"]))
        self.assertIs(n["qa"]["needs_login"], True)
        n2 = dict(n, source="nonsense")
        self.assertIn("source: not in enum", V.validate("triage", n2))

    def test_judge_schema_on_judge_output(self):
        self.attempt(1, rev=reviewer([rfinding()]), q=qa())
        j = self.judge()
        self.assertEqual(V.validate("judge", j), [])
        self.assertEqual(V.validate("judge", json.loads((self.run / "attempt-1" / "judge.json").read_text())), [])
        self.assertTrue(V.validate("judge", dict(j, result="maybe")))


# ------------------------------------------------------------------ extract_json

class TestExtractJson(HermeticCase):
    def test_fenced_block_at_end_of_long_message(self):
        text = ("Long analysis line.\n" * 400) + '```json\n{"role": "reviewer", "verdict": "pass"}\n```\n'
        self.assertEqual(V.extract_json(text), {"role": "reviewer", "verdict": "pass"})

    def test_two_fenced_blocks_last_wins(self):
        text = 'a\n```json\n{"verdict": "fail"}\n```\nb\n```json\n{"verdict": "pass"}\n```'
        self.assertEqual(V.extract_json(text)["verdict"], "pass")

    def test_last_parsing_block_wins_over_broken_last(self):
        text = '```json\n{"verdict": "fail"}\n```\n```json\n{broken\n```'
        self.assertEqual(V.extract_json(text)["verdict"], "fail")

    def test_bare_json(self):
        self.assertEqual(V.extract_json('  {"a": 1}  '), {"a": 1})

    def test_prose_wrapped_json(self):
        text = 'Here is my verdict: {"role": "qa", "verdict": "pass", "x": {"y": "}"}} — done. {"other": 1}'
        self.assertEqual(V.extract_json(text), {"role": "qa", "verdict": "pass", "x": {"y": "}"}})

    def test_garbage(self):
        for t in ("no json here", "", "{not: json}", '{"a": 1} trailing', "[1, 2]", None):
            self.assertIsNone(V.extract_json(t), t)


# ------------------------------------------------------------------ reviewer rules

class TestReviewerRules(Base):
    def one(self, n=1, plan=None, **kw):
        f = rfinding(**kw)
        self.attempt(n, rev=reviewer([f]), q=qa(), plan=plan, hunks=getattr(self, "_hunks", None),
                     deleted=getattr(self, "_deleted", ()))
        j = self.judge(n)
        return self.rf(j, f["id"]), j

    def assertDown(self, f, reason, sev="major"):
        self.assertFalse(f["blocking"])
        self.assertEqual(f["downgraded_from"], sev)
        self.assertEqual(f["severity"], "minor")
        self.assertEqual(f["downgrade_reason"], reason)

    def test_baseline_blocking(self):
        f, j = self.one()
        self.assertTrue(f["blocking"])
        self.assertEqual(j["roles"]["reviewer"]["state"], "fail")
        self.assertEqual(j["result"], "fail")

    def test_rule1_category(self):
        f, _ = self.one(category="performance")
        self.assertDown(f, "category performance is never blocking")

    def test_rule2_low_confidence(self):
        f, _ = self.one(confidence="low", category="other")  # rule 1 first
        self.assertDown(f, "category other is never blocking")
        f, _ = self.one(confidence="low")
        self.assertDown(f, "low confidence")

    def test_rule3_anchor(self):
        f, _ = self.one(file="")
        self.assertDown(f, "no file:line anchor")
        f, _ = self.one(line=None)
        self.assertDown(f, "no file:line anchor")
        f, _ = self.one(category="build", file="", line=None, command="npm run build")
        self.assertTrue(f["blocking"])

    def test_rule4_file_and_line(self):
        f, _ = self.one(file="src/missing.ts")
        self.assertDown(f, "anchor file not found")
        self._deleted = ("src/missing.ts",)
        f, _ = self.one(file="src/missing.ts")
        self.assertTrue(f["blocking"])
        f, _ = self.one(line=21)
        self.assertDown(f, "line out of range")
        f, _ = self.one(line=20, severity="blocker")
        self.assertTrue(f["blocking"])

    def test_rule4_skipped_for_commit_code_ref(self):
        self.ctx["target"]["code_ref"] = "a1b2c3"
        self.write_ctx()
        f, _ = self.one(file="src/only-in-commit.ts")
        self.assertTrue(f["blocking"])

    def test_rule5_build_command(self):
        f, _ = self.one(category="build")
        self.assertDown(f, "build finding without the failing command")

    def test_rule6_scenario(self):
        f, _ = self.one(scenario="breaks sometimes")
        self.assertDown(f, "no concrete failing scenario")
        f, _ = self.one(scenario="  " + "x" * 29 + "  ")
        self.assertDown(f, "no concrete failing scenario")
        f, _ = self.one(scenario="x" * 30)
        self.assertTrue(f["blocking"])

    def test_rule7_convention(self):
        f, _ = self.one(category="convention")
        self.assertDown(f, "convention finding without proof (convention_ref)")
        f, _ = self.one(category="convention", convention_ref="CLAUDE.md: use the logger")
        self.assertTrue(f["blocking"])

    def test_rule8_acceptance(self):
        f, _ = self.one(category="acceptance")
        self.assertDown(f, "acceptance finding without the criterion")
        f, _ = self.one(category="acceptance", criterion="retries 3 times")
        self.assertTrue(f["blocking"])

    def test_rule9_late(self):
        plan2 = {"reviewer": {"run": True, "mode": "recheck"}, "qa": {"run": False, "carried_from": 1}}
        self.attempt(1, rev=reviewer([]), q=qa())
        self.judge(1)
        self._hunks = {"src/publisher.ts": [[10, 2]]}
        # window: 10-3=7 .. 10+2+3=15
        for line, changed in ((6, False), (7, True), (15, True), (16, False)):
            f, _ = self.one(n=2, plan=plan2, line=line)
            if changed:
                self.assertTrue(f["blocking"], line)
            else:
                self.assertDown(f, "new finding on code unchanged since attempt 1")
        self._hunks = {"src/publisher.ts": [[4, 0]]}  # count 0 → max(count,1)=1 → 1..8
        f, _ = self.one(n=2, plan=plan2, line=8)
        self.assertTrue(f["blocking"])
        self._hunks = {"other.ts": [[3, 1]]}
        f, _ = self.one(n=2, plan=plan2, line=3)
        self.assertDown(f, "new finding on code unchanged since attempt 1")
        f, _ = self.one(n=2, plan=plan2, line=3, missed_earlier=True, missed_reason="too short")
        self.assertDown(f, "new finding on code unchanged since attempt 1")
        f, j = self.one(n=2, plan=plan2, line=3, missed_earlier=True,
                        missed_reason="I only read the diff hunks on attempt 1")
        self.assertTrue(f["blocking"])
        self.assertTrue(f["late"])
        self.assertIn("(missed earlier)", V.render_report(j, RUN_ID))
        f, _ = self.one(n=2, plan=plan2, line=3, missed_earlier=False,
                        missed_reason="I only read the diff hunks on attempt 1")
        self.assertDown(f, "new finding on code unchanged since attempt 1")
        f, _ = self.one(n=2, plan=plan2, line=3, status="still_present")
        self.assertTrue(f["blocking"])
        self.assertFalse(f["late"])

    def test_rule_order_first_failing_wins(self):
        f, _ = self.one(confidence="low", file="", scenario="")
        self.assertDown(f, "low confidence")

    def test_minor_and_closed_findings(self):
        self.attempt(1, rev=reviewer([rfinding(severity="minor"), rfinding(id="R2", status="fixed"),
                                      rfinding(id="R3", status="withdrawn")], verdict="pass"), q=qa())
        j = self.judge()
        self.assertEqual([f["id"] for f in j["findings"]], ["R1"])
        self.assertFalse(j["findings"][0]["blocking"])
        self.assertIsNone(j["findings"][0]["downgraded_from"])
        self.assertEqual(j["roles"]["reviewer"]["minor"], 1)
        self.assertEqual(j["result"], "pass")

    def test_waivers(self):
        self.write_ctx(waived=["R1", "Q1"])
        self.attempt(1, rev=reviewer([rfinding(), rfinding(id="R2", scenario="")]),
                     q=qa([check(), check("C2", "fail")], [qfinding()], verdict="fail"))
        j = self.judge()
        r1 = self.rf(j, "R1")
        self.assertEqual((r1["blocking"], r1["waived"], r1["severity"]), (False, True, "major"))
        self.assertTrue(self.rf(j, "Q1")["waived"])
        self.assertEqual(j["result"], "pass")
        self.assertIn("Waived: R1", V.render_report(j, RUN_ID))

    def test_reviewer_fail_without_blocking_normalized(self):
        self.attempt(1, rev=reviewer([rfinding(scenario="")]), q=qa())
        j = self.judge()
        self.assertEqual(j["roles"]["reviewer"]["state"], "pass")
        self.assertIn("normalized: no blocking findings survived", j["roles"]["reviewer"]["notes"])


# ------------------------------------------------------------------ QA rules

class TestQARules(Base):
    def one(self, checks=None, **kw):
        f = qfinding(**kw)
        checks = checks if checks is not None else [check(), check("C2", "fail", method="browser")]
        self.attempt(1, rev=reviewer([], verdict="pass"), q=qa(checks, [f], verdict="fail"))
        j = self.judge()
        return self.rf(j, f["id"]), j

    def assertDown(self, f, reason):
        self.assertFalse(f["blocking"])
        self.assertEqual((f["downgraded_from"], f["severity"], f["downgrade_reason"]), ("blocker", "minor", reason))

    def test_baseline(self):
        f, j = self.one()
        self.assertTrue(f["blocking"])
        self.assertEqual(j["roles"]["qa"]["state"], "fail")
        self.assertEqual(f["criterion"], "criterion C2")

    def test_rule1(self):
        self.assertDown(self.one(category="ux")[0], "category ux is never blocking")

    def test_rule2(self):
        self.assertDown(self.one(confidence="low")[0], "low confidence")

    def test_rule3(self):
        self.assertDown(self.one(check_id="")[0], "not tied to a failing check")
        self.assertDown(self.one(check_id="C1")[0], "not tied to a failing check")
        self.assertDown(self.one(check_id="C9")[0], "not tied to a failing check")

    def test_rule4(self):
        self.assertDown(self.one(repro=[])[0], "no reproduction")
        self.assertTrue(self.one(repro=[], command="npm test")[0]["blocking"])

    def test_rule5(self):
        self.assertDown(self.one(evidence=[])[0], "no evidence")

    def test_rule6(self):
        self.assertDown(self.one(pre_existing="confirmed")[0], "pre-existing failure")
        self.assertTrue(self.one(pre_existing="suspected")[0]["blocking"])

    def test_qa_fail_without_blocking_is_pass_with_note(self):
        f, j = self.one(category="ux")
        self.assertEqual(j["roles"]["qa"]["state"], "pass")
        self.assertIn("normalized: no blocking findings survived", j["roles"]["qa"]["notes"])
        self.assertEqual(j["result"], "pass")

    def test_verified_nothing(self):
        self.attempt(1, rev=reviewer([], verdict="pass"),
                     q=qa([check(result="skip"), check("C2", "blocked", need="app_not_running")], verdict="pass"))
        j = self.judge()
        self.assertEqual(j["roles"]["qa"]["state"], "inconclusive")
        self.assertEqual(j["roles"]["qa"]["reasons"], ["verified nothing"])
        self.assertEqual(j["result"], "inconclusive")

    def test_taint(self):
        self.attempt(1, rev=reviewer([], verdict="pass"), q=qa(),
                     taint={"changed_files": ["src/a.ts", "package-lock.json"], "main_edits": False, "tainted": True})
        j = self.judge()
        self.assertEqual(j["roles"]["qa"]["state"], "inconclusive")
        self.assertEqual(j["roles"]["qa"]["reasons"], ["side effects: src/a.ts, package-lock.json"])
        self.assertTrue(j["roles"]["qa"]["tainted"])

    def test_main_edits_warning(self):
        self.attempt(1, rev=reviewer([], verdict="pass"), q=qa(),
                     taint={"changed_files": ["src/x.ts"], "main_edits": True, "tainted": False})
        j = self.judge()
        self.assertEqual(j["result"], "pass")
        self.assertEqual(j["warnings"], ["tree changed during verification (main agent edited src/x.ts)"])

    def test_declined_consent_exception(self):
        consent = {"status": "declined", "source": "user", "decision": "no-browser", "attempt": 1, "at": "t",
                   "note": None}
        self.write_ctx(browser_consent=consent)
        unv = [{"check_id": "C1", "need": "env", "detail": "user declined browser QA", "how_to_fix": ""}]
        self.attempt(1, rev=reviewer([], verdict="pass"),
                     q=qa([check(result="blocked", method="browser")], verdict="inconclusive", scope="run",
                          unverified=unv))
        j = self.judge()
        self.assertEqual(j["roles"]["qa"]["state"], "skipped")
        self.assertEqual(j["roles"]["qa"]["skipped_by"], "user")
        self.assertIn("nothing verifiable without the browser (user declined)", j["roles"]["qa"]["notes"])
        self.assertEqual(j["result"], "pass")
        self.assertEqual(j["inconclusive_streak"], 0)
        self.assertEqual(len(j["unverified"]), 1)
        self.assertEqual(j["browser_consent"], consent)
        # same verdict without a user decline → inconclusive
        self.write_ctx(browser_consent=None)
        self.assertEqual(self.judge()["roles"]["qa"]["state"], "inconclusive")
        # decline by config is not the exception
        self.write_ctx(browser_consent=dict(consent, source="config", decision="never"))
        self.assertEqual(self.judge()["roles"]["qa"]["state"], "inconclusive")

    def test_declined_verified_nothing_pass_verdict(self):
        self.write_ctx(browser_consent={"status": "declined", "source": "user", "decision": "no-browser"})
        unv = [{"check_id": "C1", "need": "env", "detail": "user declined browser QA"}]
        self.attempt(1, rev=reviewer([], verdict="pass"),
                     q=qa([check(result="skip")], verdict="pass", scope="run", unverified=unv))
        self.assertEqual(self.judge()["roles"]["qa"]["state"], "skipped")


# ------------------------------------------------------------------ role sources, results, counters

class TestJudgeFlow(Base):
    def test_missing_and_bad_verdicts(self):
        self.attempt(1, rev_error={"status": "dead", "detail": "tab closed"})
        j = self.judge()
        self.assertEqual(j["roles"]["reviewer"]["reasons"], ["agent dead"])
        self.assertEqual(j["roles"]["qa"]["reasons"], ["no verdict"])
        self.assertEqual(j["result"], "inconclusive")
        bad = reviewer([rfinding(severity="huge")])
        self.attempt(1, rev=bad, q="not json")
        j = self.judge()
        self.assertEqual(j["roles"]["reviewer"]["reasons"], ["bad verdict: findings[0].severity: not in enum"])
        self.assertEqual(j["roles"]["qa"]["reasons"], ["bad verdict: not a JSON object"])

    def test_inconclusive_verdict(self):
        self.attempt(1, rev=reviewer([], verdict="inconclusive", summary="could not read the diff"), q=qa())
        j = self.judge()
        self.assertEqual(j["roles"]["reviewer"]["state"], "inconclusive")
        self.assertIn("review INCONCLUSIVE (could not read the diff)", j["status_line"])

    def test_scope_none_skipped(self):
        plan = {"reviewer": {"run": True}, "qa": {"run": False, "carried_from": None, "scope": "none",
                                                  "skipped_by": "user"}}
        self.attempt(1, rev=reviewer([], verdict="pass"), plan=plan)
        j = self.judge()
        self.assertEqual(j["roles"]["qa"]["state"], "skipped")
        self.assertEqual(j["result"], "pass")
        self.assertIn("qa SKIPPED (user: none)", j["status_line"])

    def test_role_missing_from_plan_skipped(self):
        self.attempt(1, rev=reviewer([], verdict="pass"), plan={"reviewer": {"run": True}})
        self.assertEqual(self.judge()["roles"]["qa"]["state"], "skipped")

    def test_carried_verdict(self):
        self.attempt(1, rev=reviewer([rfinding()]), q=qa([check(), check("C2")], verdict="pass",
                                                          unverified=[{"check_id": "C3", "need": "auth_expired"}]))
        j1 = self.judge(1, elapsed_s=100)
        self.write_ctx(failed_attempts=j1["failed_attempts"])
        plan2 = {"reviewer": {"run": True, "mode": "recheck"}, "qa": {"run": False, "carried_from": 1}}
        self.attempt(2, rev=reviewer([rfinding(status="still_present")]), plan=plan2,
                     hunks={"src/publisher.ts": [[3, 1]]})
        j = self.judge(2, elapsed_s=250)
        q = j["roles"]["qa"]
        self.assertEqual((q["state"], q["carried"], q["carried_from"], q["verdict_path"]),
                         ("pass", True, 1, "attempt-1/qa.json"))
        self.assertEqual(len(j["unverified"]), 1)
        self.assertEqual(j["status_line"], "verify-loop · attempt 2 · failures 2/3 · review FAIL (1 major) · "
                                           "qa PASS (carried from #1, 2/2 checks, 1 unverified, browser) · 4m10s")
        self.assertTrue(self.rf(j, "R1")["stuck"])

    def test_carried_without_judge_reevaluates(self):
        a = self.attempt(1, rev=reviewer([], verdict="pass"), q=qa())
        plan2 = {"reviewer": {"run": True}, "qa": {"run": False, "carried_from": 1}}
        self.attempt(2, rev=reviewer([], verdict="pass"), plan=plan2, hunks={})
        j = self.judge(2)
        self.assertEqual(j["roles"]["qa"]["state"], "pass")
        self.assertTrue(j["roles"]["qa"]["carried"])
        self.assertFalse((a / "judge.json").exists())

    def test_overall_result_table(self):
        cases = [("pass", "pass", "pass"), ("pass", "fail", "fail"), ("fail", "inconclusive", "fail"),
                 ("inconclusive", "pass", "inconclusive"), ("pass", "skipped", "pass"),
                 ("inconclusive", "fail", "fail")]
        for rs, qs, expected in cases:
            rev = {"pass": reviewer([], verdict="pass"), "fail": reviewer([rfinding()]),
                   "inconclusive": reviewer([], verdict="inconclusive")}[rs]
            qv = {"pass": qa(), "fail": qa([check("C2", "fail")], [qfinding()], verdict="fail"),
                  "inconclusive": qa(verdict="inconclusive"), "skipped": None}[qs]
            plan = None if qs != "skipped" else {"reviewer": {"run": True},
                                                 "qa": {"run": False, "scope": "none", "skipped_by": "triage"}}
            shutil.rmtree(self.run / "attempt-1", ignore_errors=True)
            self.attempt(1, rev=rev, q=qv, plan=plan)
            self.assertEqual(self.judge()["result"], expected, (rs, qs))

    def test_counters_and_escalation(self):
        self.attempt(1, rev=reviewer([rfinding()]), q=qa())
        self.write_ctx(failed_attempts=1, inconclusive_streak=1)
        j = self.judge()
        self.assertEqual((j["failed_attempts"], j["inconclusive_streak"], j["escalate"]), (2, 0, None))
        self.write_ctx(failed_attempts=2)
        (self.run / "attempt-1" / "judge.json").unlink()
        j = self.judge()
        self.assertEqual(j["escalate"], {"reason": "3 failed attempts"})
        self.assertTrue(V.render_report(j, RUN_ID).startswith("RESULT: ESCALATE\n"))
        # re-judge of the same attempt is idempotent (counters_before)
        self.write_ctx(failed_attempts=3)
        self.assertEqual(self.judge()["failed_attempts"], 3)
        # inconclusive streak
        (self.run / "attempt-1" / "judge.json").unlink()
        self.write_ctx(failed_attempts=1, inconclusive_streak=1)
        self.attempt(1, rev=reviewer([], verdict="inconclusive"), q=qa())
        j = self.judge()
        self.assertEqual((j["failed_attempts"], j["inconclusive_streak"]), (1, 2))
        self.assertEqual(j["escalate"], {"reason": "2 inconclusive attempts in a row"})
        # pass resets the streak
        (self.run / "attempt-1" / "judge.json").unlink()
        self.attempt(1, rev=reviewer([], verdict="pass"), q=qa())
        j = self.judge()
        self.assertEqual((j["failed_attempts"], j["inconclusive_streak"], j["escalate"]), (1, 0, None))
        # custom limits
        (self.run / "attempt-1" / "judge.json").unlink()
        self.attempt(1, rev=reviewer([rfinding()]), q=qa())
        self.assertEqual(self.judge(max_failed=2)["escalate"], {"reason": "2 failed attempts"})

    def test_standalone_never_escalates(self):
        self.write_ctx(kind="standalone", roles=["reviewer"], failed_attempts=5, inconclusive_streak=5)
        self.attempt(1, rev=reviewer([rfinding()]), plan={"reviewer": {"run": True}})
        j = self.judge()
        self.assertIsNone(j["escalate"])
        self.assertEqual(j["failed_attempts"], 6)
        self.assertTrue(j["status_line"].startswith("verify-review · attempt 1 · review FAIL"))
        self.assertNotIn("failures", j["status_line"])

    def test_stuck_detection(self):
        self.attempt(1, rev=reviewer([rfinding(), rfinding(id="R2", line=5)]), q=qa())
        self.judge(1)
        plan2 = {"reviewer": {"run": True, "mode": "recheck"}, "qa": {"run": False, "carried_from": 1}}
        self.attempt(2, rev=reviewer([rfinding(status="still_present"), rfinding(id="R3", line=3)]), plan=plan2,
                     hunks={"src/publisher.ts": [[3, 1]]})
        j = self.judge(2)
        self.assertTrue(self.rf(j, "R1")["stuck"])
        self.assertFalse(self.rf(j, "R3")["stuck"])
        # still_present now but absent before → not stuck
        self.attempt(2, rev=reviewer([rfinding(id="R9", status="still_present")]), plan=plan2,
                     hunks={"src/publisher.ts": [[3, 1]]})
        self.assertFalse(self.rf(self.judge(2), "R9")["stuck"])

    def test_unverified_auto_how_to_fix(self):
        unv = [{"check_id": "C3", "need": "auth_expired", "detail": "redirect", "how_to_fix": ""},
               {"check_id": "C4", "need": "env", "detail": "no redis", "how_to_fix": "start redis"}]
        checks = [check(), check("C3", "blocked", need="auth_expired"), check("C4", "blocked", need="env"),
                  check("C5", "blocked", need="auth_missing")]
        self.attempt(1, rev=reviewer([], verdict="pass"), q=qa(checks, unverified=unv))
        j = self.judge()
        by = {u["check_id"]: u for u in j["unverified"]}
        self.assertEqual(set(by), {"C3", "C4", "C5"})
        self.assertEqual(by["C3"]["how_to_fix"],
                         f"! {VL} auth capture --url https://localhost:8100")
        self.assertEqual(by["C4"]["how_to_fix"], "start redis")
        self.assertEqual(by["C5"]["need"], "auth_missing")
        (self.repo / ".claude").mkdir()
        (self.repo / ".claude" / "verify.json").write_text(json.dumps({"auth": {"login_url": "https://x/login"}}))
        j = self.judge()
        self.assertTrue(j["unverified"][0]["how_to_fix"].endswith("--url https://x/login"))
        self.assertIn("qa PASS (1/1 checks, 3 unverified, browser)", j["status_line"])

    def test_disputes_attached(self):
        self.write_ctx(disputes={"R1": {"reason": "guard exists in caller", "attempt": 1}})
        self.attempt(1, rev=reviewer([rfinding()]), q=qa())
        self.assertEqual(self.rf(self.judge(), "R1")["dispute"], "guard exists in caller")

    def test_only_judge_json_written(self):
        a = self.attempt(1, rev=reviewer([rfinding()]), q=qa())
        before = sorted(p.name for p in self.run.rglob("*"))
        self.judge()
        after = sorted(p.name for p in self.run.rglob("*"))
        self.assertEqual(set(after) - set(before), {"judge.json"})


# ------------------------------------------------------------------ status lines

def J(**kw) -> dict:
    j = {"kind": "loop", "attempt": 1, "failed_attempts": 0, "max_failed": 3, "elapsed_s": 250, "unverified": [],
         "roles": {}}
    j.update(kw)
    return j


def R(state, **kw) -> dict:
    r = {"state": state, "carried": False, "carried_from": None, "blocking": 0, "reasons": [],
         "blocking_by_severity": {"blocker": 0, "major": 0}}
    r.update(kw)
    return r


class TestStatusLine(HermeticCase):
    def test_golden(self):
        qa_pass = R("pass", scope="browser", checks={"pass": 6, "fail": 0, "skip": 0, "blocked": 0}, unverified=0)
        cases = [
            (J(failed_attempts=1, roles={"reviewer": R("fail", blocking=1, blocking_by_severity={"blocker": 0,
                                                                                                  "major": 1}),
                                         "qa": qa_pass}),
             "verify-loop · attempt 1 · failures 1/3 · review FAIL (1 major) · qa PASS (6/6 checks, browser) · 4m10s"),
            (J(roles={"reviewer": R("pass"), "qa": R("skipped", skipped_by="prefilter", scope="none")}),
             "verify-loop · attempt 1 · failures 0/3 · review PASS · qa SKIPPED (prefilter: none) · 4m10s"),
            (J(attempt=2, failed_attempts=2,
               roles={"reviewer": R("fail", blocking=2, blocking_by_severity={"blocker": 1, "major": 1}),
                      "qa": R("pass", carried=True, carried_from=1, scope="browser",
                              checks={"pass": 6, "fail": 0}, unverified=1)}),
             "verify-loop · attempt 2 · failures 2/3 · review FAIL (1 blocker, 1 major) · "
             "qa PASS (carried from #1, 6/6 checks, 1 unverified, browser) · 4m10s"),
            (J(attempt=3, failed_attempts=1, elapsed_s=45,
               roles={"reviewer": R("pass", carried=True, carried_from=2),
                      "qa": R("inconclusive", reasons=["agent dead"])}),
             "verify-loop · attempt 3 · failures 1/3 · review PASS (carried from #2) · qa INCONCLUSIVE (agent dead) · 45s"),
            (J(roles={"reviewer": R("inconclusive", reasons=["no verdict"]),
                      "qa": R("fail", blocking=1, blocking_by_severity={"blocker": 1, "major": 0})}),
             "verify-loop · attempt 1 · failures 0/3 · review INCONCLUSIVE (no verdict) · qa FAIL (1 blocker) · 4m10s"),
            (J(roles={"reviewer": R("pass"), "qa": R("skipped", skipped_by="user", scope="none")}, elapsed_s=None),
             "verify-loop · attempt 1 · failures 0/3 · review PASS · qa SKIPPED (user: none)"),
            (J(kind="standalone", roles={"qa": R("pass", scope="run", checks={"pass": 3, "fail": 1}, unverified=0)},
               elapsed_s=3725),
             "verify-qa · attempt 1 · qa PASS (3/4 checks, run) · 1h02m"),
            (J(kind="standalone", roles={"reviewer": R("pass"), "qa": R("pass", scope="checks",
                                                                         checks={"pass": 2, "fail": 0})}),
             "verify · attempt 1 · review PASS · qa PASS (2/2 checks, checks) · 4m10s"),
        ]
        for judge, expected in cases:
            self.assertEqual(V.status_line(judge), expected)


# ------------------------------------------------------------------ reports (golden files)

class TestReports(Base):
    def golden(self, name, text):
        path = FIX / name
        if UPDATE_GOLDEN or not path.exists():
            path.write_text(text.replace(VL, "{{vloop}}") + "\n")
        self.assertEqual(text + "\n", path.read_text().replace("{{vloop}}", VL), name)

    def fail_judge(self):
        self.write_ctx(browser_consent={"status": "granted", "source": "user", "decision": "allow", "attempt": 1,
                                        "at": "t", "note": None})
        rev = reviewer([rfinding(line=14, title="Retry counter never resets after success",
                                 scenario="Publish fails twice, then succeeds; the next failure is treated as "
                                          "attempt 3 and gives up after 1 retry instead of 3."),
                        rfinding(id="R4", scenario="short"),
                        rfinding(id="R2", severity="minor", line=10, title="debug console.log left")],
                       suggestions=[{"id": f"S{i}", "text": f"suggestion {i}"} for i in range(1, 5)])
        q = qa([check(), check("C2", "fail", method="browser", criterion="Next advances to step 4"),
                check("C3", "blocked", criterion="Admin can export", need="auth_expired")],
               [qfinding(repro=["open https://localhost:8100/scan", "complete steps 1–2", "click Next"],
                         evidence=["/tmp/run/attempt-1/qa-artifacts/page.png"], actual="stays on step 3")],
               verdict="fail", suggestions=[{"id": "QS1", "text": "spinner flickers"}])
        self.attempt(1, rev=rev, q=q, taint={"changed_files": ["src/x.ts"], "main_edits": True, "tainted": False})
        return self.judge(elapsed_s=250)

    def test_report_fail(self):
        j = self.fail_judge()
        self.assertEqual(j["result"], "fail")
        text = V.render_report(j, RUN_ID)
        self.assertTrue(text.startswith("RESULT: FAIL\nverify-loop · attempt 1 · failures 1/3 · "
                                        "review FAIL (1 major) · qa FAIL (1 blocker) · 4m10s\n"))
        self.assertIn("Browser QA: approved (user)", text)
        self.golden("report-fail.txt", text)

    def test_report_pass(self):
        self.write_ctx(browser_consent={"status": "declined", "source": "user", "decision": "no-browser",
                                        "attempt": 1, "at": "t", "note": "staging is down"})
        self.attempt(1, rev=reviewer([rfinding(severity="minor")], verdict="pass",
                                     suggestions=[{"id": "S1", "text": "extract helper"}]),
                     q=qa([check(), check("C2")], scope="run"))
        j = self.judge(elapsed_s=130)
        text = V.render_report(j, RUN_ID)
        self.assertIn("Browser QA: declined (no-browser, user) — note: staging is down", text)
        self.assertTrue(text.endswith(f"next: {VL} retro-plan --run {RUN_ID}"))
        self.golden("report-pass.txt", text)
        self.write_ctx(kind="standalone")
        (self.run / "attempt-1" / "judge.json").unlink()
        text = V.render_report(self.judge(), RUN_ID)
        self.assertTrue(text.endswith("next: report this to the user; fix only if asked"))
        self.assertFalse(V.render_report(self.judge(), RUN_ID, next_hint=False).splitlines()[-1].startswith("next"))

    def test_report_inconclusive(self):
        self.attempt(1, rev=reviewer([], verdict="pass"), qa_error={"status": "timeout", "detail": "30m"},
                     plan={"reviewer": {"run": True}, "qa": {"run": True, "scope": "run"}})
        j = self.judge(elapsed_s=1800)
        text = V.render_report(j, RUN_ID)
        self.assertNotIn("Browser QA", text)
        self.golden("report-inconclusive.txt", text)

    def test_report_skipped_by_user(self):
        self.write_ctx(browser_consent={"status": "declined", "source": "user", "decision": "skip-qa"})
        self.attempt(1, rev=reviewer([], verdict="pass"),
                     plan={"reviewer": {"run": True}, "qa": {"run": False, "scope": "none", "skipped_by": "user"}})
        text = V.render_report(self.judge(), RUN_ID)
        self.assertIn("qa SKIPPED (user: none)", text)
        self.assertIn("Browser QA: declined (skip-qa, user)", text)

    def test_escalation(self):
        self.write_ctx(disputes={"R1": {"reason": "counter is reset in finally", "attempt": 2}})
        for n in (1, 2, 3):
            st = "new" if n == 1 else "still_present"
            plan = None if n == 1 else {"reviewer": {"run": True, "mode": "recheck"},
                                        "qa": {"run": True, "mode": "recheck", "scope": "browser"}}
            rec = [{"id": "R1", "status": "still_present", "note": "finally block never runs on throw",
                    "dispute_ruling": "upheld"}] if n == 3 else []
            q = qa([check(), check("C2", "fail", criterion="Next advances"),
                    check("C4", "blocked", need="auth_expired")], [qfinding(id="Q2", status="new" if n == 3 else st)],
                   verdict="fail") if n == 3 else qa()
            self.attempt(n, rev=reviewer([rfinding(status=st)], rechecked=rec), q=q, plan=plan,
                         hunks={"src/publisher.ts": [[3, 1]]},
                         fix_notes=None if n == 1 else f"R1: attempt {n} change in publish() " + "x" * 100)
            j = self.judge(n, elapsed_s=540)
            self.write_ctx(failed_attempts=j["failed_attempts"], inconclusive_streak=j["inconclusive_streak"])
        self.assertEqual(j["escalate"], {"reason": "3 failed attempts"})
        text = V.render_escalation(self.run)
        lines = text.splitlines()
        self.assertLessEqual(len(lines), 25)
        self.assertEqual(lines[0], f"verify-loop · ESCALATION · 3 failed attempts · 27m00s · run {RUN_ID}")
        self.assertIn("R1 [major·stuck] src/publisher.ts:3", text)
        self.assertIn("raised #1, still #2,#3", text)
        self.assertIn('disputed: "counter is reset in finally"', text)
        self.assertIn('reviewer upheld: "finally block never runs on throw"', text)
        self.assertIn("Tried: #2 R1: attempt 2 change", text)
        self.assertIn("Unverified: C4 (auth_expired)", text)
        self.assertIn(f"decide waive --run {RUN_ID} --ids R1 ", text)
        self.golden("escalation.txt", text)

    def test_escalation_inconclusive(self):
        for n in (1, 2):
            self.attempt(n, rev=reviewer([], verdict="pass"), qa_error={"status": "dead", "detail": "tab closed"},
                         plan={"reviewer": {"run": True}, "qa": {"run": True, "scope": "run"}}, hunks={})
            j = self.judge(n, elapsed_s=60)
            self.write_ctx(failed_attempts=j["failed_attempts"], inconclusive_streak=j["inconclusive_streak"])
        text = V.render_escalation(self.run)
        self.assertTrue(text.startswith("verify-loop · ESCALATION · 2 inconclusive attempts in a row · 2m00s"))
        self.assertIn("Environment:\n  #1 qa: agent dead\n  #2 qa: agent dead", text)


# ------------------------------------------------------------------ role lines

class TestRoleLine(HermeticCase):
    def test_spec_examples(self):
        rv = reviewer([rfinding(severity="minor"), rfinding(id="R2", severity="minor")], verdict="pass",
                      suggestions=[{"id": f"S{i}", "text": "t"} for i in range(1, 4)])
        self.assertEqual(V.role_line("reviewer", rv, None, 220, "sonnet/medium"),
                         "reviewer: PASS · 0 blocking, 2 minor, 3 suggestions · sonnet/medium · 3m40s")
        rv = reviewer([rfinding(severity="blocker"), rfinding(id="R2"), rfinding(id="R3", severity="minor"),
                       rfinding(id="R4", severity="minor"), rfinding(id="R5", status="fixed")])
        self.assertEqual(V.role_line("reviewer", rv, None, 220, "sonnet/medium"),
                         "reviewer: FAIL · 1 blocker, 1 major, 2 minor · sonnet/medium · 3m40s")
        qv = qa([check(f"C{i}") for i in range(1, 7)] + [check("C7", "blocked", need="auth_expired")])
        self.assertEqual(V.role_line("qa", qv, None, 310, "sonnet/medium"),
                         "qa: PASS · 6/6 checks (1 unverified) · browser · sonnet/medium · 5m10s")
        qv = qa([check(f"C{i}") for i in range(1, 6)] + [check("C6", "fail")], [qfinding(check_id="C6")],
                verdict="fail")
        self.assertEqual(V.role_line("qa", qv, None, 310, "sonnet/medium"),
                         "qa: FAIL · 1 blocker · 5/6 checks · browser · sonnet/medium · 5m10s")
        self.assertEqual(V.role_line("qa", None, {"status": "dead", "detail": "tab closed"}, 121),
                         "qa: INCONCLUSIVE · agent died (tab closed) · 2m01s")

    def test_other_lines(self):
        self.assertEqual(V.role_line("reviewer", None, None, None), "reviewer: INCONCLUSIVE · no verdict")
        self.assertEqual(V.role_line("qa", None, {"status": "timeout", "detail": ""}, 1800, "sonnet/medium"),
                         "qa: INCONCLUSIVE · timed out · 30m00s")
        self.assertEqual(V.role_line("reviewer", reviewer([], verdict="inconclusive", summary="no access"), None,
                                     30, "opus/medium"),
                         "reviewer: INCONCLUSIVE · no access · opus/medium · 30s")



# ------------------------------------------------------------------ specialist roles (security / perf / ux)

PATCH = ("diff --git a/src/publisher.ts b/src/publisher.ts\n--- a/src/publisher.ts\n+++ b/src/publisher.ts\n"
         "@@ -3,2 +3,4 @@ x\n line 3\n+line 4\n+line 5\n line 6\n")
SEC_SCENARIO = "POST /publish name=../../etc → path.join in publisher.ts:5 → reads any file on the server"


def sfinding(**kw) -> dict:
    f = rfinding(id="SC1", category="security", line=5, scenario=SEC_SCENARIO, evidence_ref="src/publisher.ts:4",
                 title="Path traversal in publish")
    f.update(kw)
    return f


def specialist(role, findings=(), verdict="fail", **kw) -> dict:
    v = reviewer(findings, verdict=verdict, role=role)
    v.update(kw)
    return v


class SpecialistBase(Base):
    def spec_attempt(self, n=1, roles=None, rev=None, patch=PATCH, **kw):
        roles = roles or {}
        plan = {"reviewer": {"run": True, "mode": "full"}, "qa": {"run": False, "scope": "none",
                                                                   "skipped_by": "triage"}}
        for role in roles:
            plan[role] = {"run": True, "mode": "full"}
        plan.update(kw.pop("plan_extra", {}))
        a = self.attempt(n, rev=rev if rev is not None else reviewer([], verdict="pass"), plan=plan, **kw)
        for role, v in roles.items():
            if v is not None:
                v = dict(v)
                v["attempt"] = n
                (a / f"{role}.json").write_text(json.dumps(v))
        if patch is not None:
            (a / "diff.patch").write_text(patch)
        elif (a / "diff.patch").exists():
            (a / "diff.patch").unlink()
        return a


class TestSpecialistNormalize(Base):
    def test_prefixes_per_role(self):
        for role, fp, sp in (("security", "SC", "SCS"), ("perf", "PF", "PFS"), ("ux", "UX", "UXS")):
            raw = specialist(role, [rfinding(id="R1"), rfinding(id="x")], suggestions=["one", {"id": "S9", "text": "t"}])
            v, _ = V.normalize(role, raw, run_id=RUN_ID, attempt=1)
            self.assertEqual([f["id"] for f in v["findings"]], [f"{fp}1", f"{fp}2"])
            self.assertEqual([s["id"] for s in v["suggestions"]], [f"{sp}1", f"{sp}2"])
            self.assertEqual(v["role"], role)
            self.assertEqual(v["findings"][0]["evidence_ref"], "")
            self.assertEqual(V.validate(role, v), [])

    def test_reviewer_keeps_old_shape(self):
        v, _ = V.normalize("reviewer", reviewer([rfinding()]), run_id=RUN_ID, attempt=1)
        self.assertNotIn("evidence_ref", v["findings"][0])
        self.assertEqual(V.validate("reviewer", v), [])

    def test_new_categories_and_fields_validate(self):
        f = rfinding(id="UX1", category="accessibility", wcag="1.4.3", measurement="", evidence_ref="")
        v, _ = V.normalize("ux", specialist("ux", [f, rfinding(id="UX2", category="usability")]), run_id=RUN_ID,
                           attempt=1)
        self.assertEqual(V.validate("ux", v), [])
        self.assertTrue(V.validate("reviewer", dict(v, role="qa")))

    def test_schema_mapping(self):
        for role in ("security", "perf", "ux"):
            self.assertEqual(V.SCHEMA_FILES[role], "reviewer.schema.json")


class TestSecurityRules(SpecialistBase):
    def one(self, **kw):
        patch = kw.pop("_patch", PATCH)
        f = sfinding(**kw)
        self.spec_attempt(roles={"security": specialist("security", [f])}, patch=patch)
        j = self.judge()
        return self.rf(j, f["id"]), j

    def assertDown(self, f, reason):
        self.assertFalse(f["blocking"])
        self.assertEqual(f["downgraded_from"], "major")
        self.assertEqual(f["downgrade_reason"], reason)

    def test_blocks_with_full_evidence(self):
        f, j = self.one()
        self.assertTrue(f["blocking"])
        self.assertEqual(f["evidence_ref"], "src/publisher.ts:4")
        self.assertEqual(j["roles"]["security"]["state"], "fail")
        self.assertEqual(j["result"], "fail")
        self.assertIn("security FAIL (1 major)", j["status_line"])
        self.assertEqual(V.validate("judge", j), [])

    def test_category(self):
        f, _ = self.one(category="correctness")
        self.assertDown(f, "category correctness is never blocking for security")
        f, _ = self.one(category="data")
        self.assertTrue(f["blocking"])

    def test_confidence(self):
        f, _ = self.one(confidence="low")
        self.assertDown(f, "low confidence")
        f, _ = self.one(confidence="medium")
        self.assertTrue(f["blocking"])

    def test_anchor(self):
        f, _ = self.one(file="")
        self.assertDown(f, "no file:line anchor")
        f, _ = self.one(line=None)
        self.assertDown(f, "no file:line anchor")
        f, _ = self.one(file="src/nope.ts")
        self.assertDown(f, "anchor file not found")
        f, _ = self.one(line=99)
        self.assertDown(f, "line out of range")

    def test_scenario_length(self):
        f, _ = self.one(scenario="x" * 39)
        self.assertDown(f, "no concrete exploit scenario (input → sink → impact)")
        f, _ = self.one(scenario="x" * 40)
        self.assertTrue(f["blocking"])

    def test_evidence_ref(self):
        for bad in ("", "the publish route", "src/publisher.ts"):
            f, _ = self.one(evidence_ref=bad)
            self.assertDown(f, "no evidence_ref (path:line of the untrusted entry point)")
        f, _ = self.one(evidence_ref="see src/publisher.ts:L4 (handler)")
        self.assertTrue(f["blocking"])

    def test_inside_diff(self):
        f, _ = self.one(line=15, evidence_ref="src/other.ts:40")
        self.assertDown(f, "pre-existing: neither the anchor nor the evidence is in the diff")
        f, _ = self.one(line=15, evidence_ref="src/publisher.ts:4")
        self.assertTrue(f["blocking"], "evidence inside the diff is enough")
        f, _ = self.one(line=5, evidence_ref="src/other.ts:40")
        self.assertTrue(f["blocking"], "anchor inside the diff is enough")
        f, _ = self.one(line=15, evidence_ref="src/other.ts:40", _patch=None)
        self.assertTrue(f["blocking"], "no diff.patch: the rule is not applied")

    def test_patch_hunks(self):
        self.assertIsNone(V._patch_hunks(None))
        self.assertEqual(V._patch_hunks(PATCH), {"src/publisher.ts": [[3, 4]]})
        new_file = "diff --git a/n.py b/n.py\nnew file mode 100644\n--- /dev/null\n+++ b/n.py\n@@ -0,0 +1 @@\n+x\n"
        self.assertEqual(V._patch_hunks(new_file), {"n.py": [[1, 1]]})


class TestAdvisoryRoles(SpecialistBase):
    def test_never_block(self):
        for role, cat in (("perf", "performance"), ("ux", "accessibility")):
            f = rfinding(id=f"{'PF' if role == 'perf' else 'UX'}1", severity="blocker", category=cat)
            self.spec_attempt(roles={role: specialist(role, [f, dict(f, id=f["id"][:2] + "2", severity="minor")])})
            j = self.judge()
            info = j["roles"][role]
            self.assertEqual(info["state"], "pass", role)
            self.assertTrue(info["advisory"])
            self.assertEqual(info["blocking"], 0)
            self.assertEqual(j["result"], "pass")
            rec = self.rf(j, f["id"])
            self.assertFalse(rec["blocking"])
            self.assertEqual(rec["downgraded_from"], "blocker")
            self.assertEqual(rec["downgrade_reason"], "advisory role")
            report = V.render_report(j, RUN_ID)
            self.assertIn("Advisory (never blocking):", report)
            self.assertIn(f"{f['id']} [blocker]", report)
            self.assertNotIn("Not blocking (downgraded)", report)
            self.assertIn(f"{role} PASS (2 advisory)", j["status_line"])

    def test_inconclusive_does_not_count(self):
        self.spec_attempt(roles={"ux": None})
        j = self.judge()
        self.assertEqual(j["roles"]["ux"]["state"], "inconclusive")
        self.assertEqual(j["result"], "pass")
        self.assertTrue(any(w.startswith("ux inconclusive (advisory, not counted)") for w in j["warnings"]))

    def test_modified_files_still_count(self):
        a = self.spec_attempt(roles={"perf": specialist("perf", [], verdict="pass")})
        (a / "integrity.json").write_text(json.dumps({"flagged": {"perf": ["src/publisher.ts"]}}))
        j = self.judge()
        self.assertEqual(j["roles"]["perf"]["state"], "inconclusive")
        self.assertEqual(j["result"], "inconclusive")

    def test_role_line(self):
        v = specialist("perf", [rfinding(id="PF1")], verdict="fail")
        self.assertTrue(V.role_line("perf", v, None, 30).startswith("perf: FAIL (advisory) · 1 major"))


class TestSpecialistJudge(SpecialistBase):
    def test_omitted_when_not_run(self):
        plan_extra = {"security": {"run": False, "carried_from": None}}
        self.spec_attempt(plan_extra=plan_extra)
        j = self.judge()
        self.assertEqual(list(j["roles"]), ["reviewer", "qa"])
        self.assertNotIn("security", j["status_line"])
        self.assertEqual(V.validate("judge", j), [])

    def test_old_plan_and_old_judge(self):
        self.attempt(1, rev=reviewer([rfinding()]), q=qa())
        j1 = self.judge(1)
        self.assertEqual(list(j1["roles"]), ["reviewer", "qa"])
        old = dict(j1)
        old["findings"] = [{k: v for k, v in f.items() if k != "duplicate_of"} for f in j1["findings"]]
        (self.run / "attempt-1" / "judge.json").write_text(json.dumps(old))
        self.assertIn("review FAIL", V.status_line(old))
        self.assertIn("Blocking:", V.render_report(old, RUN_ID))
        plan = {"reviewer": {"run": True}, "qa": {"run": False, "carried_from": 1}}
        self.attempt(2, rev=reviewer([rfinding(status="fixed")], verdict="pass"), plan=plan, hunks={})
        j2 = self.judge(2)
        self.assertEqual(j2["roles"]["qa"]["carried_from"], 1)
        self.assertEqual(list(j2["roles"]), ["reviewer", "qa"])

    def test_order_and_status_line(self):
        self.spec_attempt(roles={"ux": specialist("ux", [], verdict="pass"),
                                 "security": specialist("security", [], verdict="pass"),
                                 "perf": specialist("perf", [], verdict="pass")})
        j = self.judge()
        self.assertEqual(list(j["roles"]), ["reviewer", "security", "perf", "ux", "qa"])
        self.assertEqual(j["status_line"], "verify-loop · attempt 1 · failures 0/3 · review PASS · security PASS · "
                                           "perf PASS · ux PASS · qa SKIPPED (triage: none)")

    def test_carried_specialist(self):
        self.spec_attempt(roles={"security": specialist("security", [sfinding()])})
        self.assertEqual(self.judge(1)["result"], "fail")
        self.spec_attempt(2, rev=reviewer([], verdict="pass"), hunks={},
                          plan_extra={"security": {"run": False, "carried_from": 1}})
        j = self.judge(2)
        self.assertTrue(j["roles"]["security"]["carried"])
        self.assertEqual(self.rf(j, "SC1")["role"], "security")
        self.assertEqual(j["result"], "fail")

    def test_dedupe(self):
        dup = rfinding(id="R1", category="security", line=6, scenario=SEC_SCENARIO)
        other = rfinding(id="R2", line=6)
        far = rfinding(id="R3", category="security", line=12)
        self.spec_attempt(rev=reviewer([dup, other, far]), roles={"security": specialist("security", [sfinding()])})
        j = self.judge()
        r1 = self.rf(j, "R1")
        self.assertEqual(r1["duplicate_of"], "SC1")
        self.assertFalse(r1["blocking"])
        self.assertIsNone(self.rf(j, "R2")["duplicate_of"])
        self.assertTrue(self.rf(j, "R2")["blocking"])
        self.assertIsNone(self.rf(j, "R3")["duplicate_of"])
        self.assertEqual(j["roles"]["reviewer"]["blocking"], 2)
        self.assertIn("Duplicates (not blocking): R1 = SC1", V.render_report(j, RUN_ID))

    def test_dedupe_turns_reviewer_fail_into_pass(self):
        dup = rfinding(id="R1", category="security", line=5, scenario=SEC_SCENARIO)
        self.spec_attempt(rev=reviewer([dup]), roles={"security": specialist("security", [sfinding()])})
        j = self.judge()
        self.assertEqual(j["roles"]["reviewer"]["state"], "pass")
        self.assertEqual(j["roles"]["reviewer"]["blocking"], 0)
        self.assertEqual(j["roles"]["security"]["state"], "fail")
        self.assertEqual(j["result"], "fail")

    def _kept_blocking(self, role, finding):
        rev = rfinding(id="R1", category=finding["category"], line=5)
        self.spec_attempt(rev=reviewer([rev]), roles={role: specialist(role, [finding])})
        j = self.judge()
        r1 = self.rf(j, "R1")
        self.assertTrue(r1["blocking"])
        self.assertIsNone(r1["duplicate_of"])
        self.assertEqual(j["roles"]["reviewer"]["state"], "fail")
        self.assertEqual(j["result"], "fail")

    def test_dedupe_skips_advisory_ux(self):
        self._kept_blocking("ux", rfinding(id="UX1", category="correctness", line=5))

    def test_dedupe_skips_advisory_perf(self):
        self._kept_blocking("perf", rfinding(id="PF1", category="correctness", line=5))

    def test_dedupe_skips_downgraded_security(self):
        self._kept_blocking("security", sfinding(confidence="low"))

    def test_report_shows_entry_point_and_escalation(self):
        self.spec_attempt(roles={"security": specialist("security", [sfinding()])})
        j = self.judge()
        report = V.render_report(j, RUN_ID)
        self.assertIn("SC1 [major·security·high] src/publisher.ts:5 — Path traversal in publish", report)
        self.assertIn("entry point: src/publisher.ts:4", report)
        esc = V.render_escalation(self.run)
        self.assertIn("SC1 [major] src/publisher.ts:5", esc)

    def test_standalone_review_prefix(self):
        judge = J(kind="standalone", roles={"reviewer": R("pass"), "security": R("pass")})
        self.assertTrue(V.status_line(judge).startswith("verify-review · attempt 1 · review PASS · security PASS"))


if __name__ == "__main__":
    unittest.main()
