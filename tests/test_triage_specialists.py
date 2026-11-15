"""Smarter triage: specialists in the triage output, reshape tolerance, fallback/prefilter/user defaults, triage
prompt inputs (security files, UI-spec hints, perf hints, calibration), roles, config keys."""
from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "fixtures" / "wp2"))
import wp2helpers as h  # noqa: E402

from vlooplib import classify, config, metrics, plan, prompts, roles, triage, util, verdict  # noqa: E402

OFF = {"security": {"run": False, "reason": "", "focus": []}, "perf": {"run": False, "reason": "", "focus": []},
       "ux": {"run": False, "ui_spec": "n/a", "reason": "", "focus": []}}


def base_output(**kw) -> dict:
    o = {"change_size": "small", "risk": "low",
         "qa": {"scope": "checks", "reason": "r", "focus": [], "needs_login": False},
         "review": {"tier": "standard", "reason": "r", "focus": []}, "confidence": "high"}
    o.update(kw)
    return o


class RolesTest(unittest.TestCase):
    def test_role_tables(self):
        self.assertEqual(roles.REVIEW_ROLES, ("reviewer", "security", "perf", "ux"))
        self.assertEqual(roles.ALL_ROLES, ("reviewer", "security", "perf", "ux", "qa"))
        self.assertEqual(roles.SPECIALISTS, ("security", "perf", "ux"))
        for r in roles.ALL_ROLES:
            self.assertIn(r, roles.PREFIX)
            self.assertIn(r, roles.SUGGESTION_PREFIX)
            self.assertEqual(roles.AGENT[r], f"verify-{r if r != 'reviewer' else 'reviewer'}")
        self.assertEqual(len(set(roles.PREFIX.values())), len(roles.PREFIX))
        self.assertTrue(roles.is_review_role("security"))
        self.assertFalse(roles.is_review_role("qa"))


class DefaultsTest(h.HermeticCase):
    def test_triage_and_specialist_defaults(self):
        d = config.defaults()
        t = d["triage"]
        self.assertEqual((t["model"], t["effort"], t["timeout_s"], t["max_budget_usd"]), ("sonnet", "medium", 300, 0.6))
        self.assertEqual((t["diff_excerpt_max_lines"], t["diff_excerpt_max_chars"]), (900, 60000))
        self.assertEqual(d["platforms"]["codex"]["triage"]["effort"], "medium")
        self.assertEqual(d["specialists"], {"security": "auto", "perf": "auto", "ux": "auto"})
        m = d["models"]
        self.assertEqual(m["security"], {"standard": ["sonnet", "high"], "high_risk": ["opus", "medium"],
                                         "delta": ["sonnet", "low"]})
        for r in ("perf", "ux"):
            self.assertEqual(m[r], {"standard": ["sonnet", "medium"], "delta": ["sonnet", "low"]})
        self.assertEqual({r: d["timeouts_s"][r] for r in roles.SPECIALISTS}, {"security": 1200, "perf": 900, "ux": 900})
        for r in roles.SPECIALISTS:
            self.assertIn(r, d["platforms"]["codex"]["models"])
            self.assertIsNone(d["platforms"]["codex"]["models"][r])
        from vlooplib.platforms import codex_cmds

        for r in roles.REVIEW_ROLES:
            self.assertEqual(codex_cmds.SANDBOX[r], "read-only")

    def test_security_globs(self):
        cfg = config.defaults()
        m = util.path_matcher(cfg["security_globs"])
        for p in ("src/auth/login.ts", "src/oauth.py", "api/session_store.go", "lib/jwt_token.ts",
                  "server/middleware/cors.ts", "billing/payment_service.py", ".github/workflows/ci.yml",
                  "db/001_init.sql", "src/crypto/aes.ts", "src/permissions.ts"):
            self.assertTrue(m(p), p)
        for p in ("src/a.ts", "README.md", "src/page.html"):
            self.assertFalse(m(p), p)

    def test_repo_keys_include_specialists(self):
        self.assertIn("specialists", config.REPO_KEYS)
        self.assertIn("security_globs", config.REPO_KEYS)
        root = self.tmp / "repo"
        root.mkdir()
        from vlooplib import paths

        cfg_path = paths.repo_config_paths(root)[0]
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(json.dumps({"specialists": {"ux": "off"}, "security_globs": ["*.secret"]}))
        rc = config.repo_config(root)
        self.assertEqual((rc["specialists"], rc["security_globs"]), ({"ux": "off"}, ["*.secret"]))


class SummaryTest(h.HermeticCase):
    def test_security_files_and_lines(self):
        cfg = config.defaults()
        rows = [{"path": "src/auth/login.ts", "status": "M", "added": 8, "removed": 4},
                {"path": "src/auth/login.spec.ts", "status": "M", "added": 30, "removed": 0},
                {"path": ".github/workflows/ci.yml", "status": "M", "added": 2, "removed": 1},
                {"path": "src/a.ts", "status": "M", "added": 50, "removed": 0},
                {"path": "docs/auth.md", "status": "M", "added": 9, "removed": 0}]
        s = classify.summarize(rows, cfg)
        self.assertEqual(s["security_files"], ["src/auth/login.ts", "src/auth/login.spec.ts",
                                               ".github/workflows/ci.yml", "docs/auth.md"])
        self.assertEqual(s["security_changed_lines"], 12 + 3)
        flags = {r["path"]: r["flags"] for r in s["rows"]}
        self.assertIn("security", flags["src/auth/login.ts"])
        self.assertNotIn("security", flags["src/a.ts"])
        self.assertIn("security: 4", prompts.signals(s))


class ReshapeSpecialistsTest(h.HermeticCase):
    def test_missing_specialists_are_off(self):
        o = triage.reshape(base_output())
        self.assertEqual(o["specialists"], OFF)
        self.assertEqual(verdict.validate("triage-output", o), [])

    def test_full_shape_kept(self):
        spec = {"security": {"run": True, "reason": "token check", "focus": ["auth.ts"]},
                "perf": {"run": False, "reason": "no loops", "focus": []},
                "ux": {"run": True, "ui_spec": "none", "reason": "new form", "focus": ["form.tsx"]}}
        obj = base_output(specialists=spec)
        self.assertEqual(triage.reshape(obj), obj)

    def test_flat_and_loose_forms(self):
        o = triage.reshape(base_output(security="yes", perf_run="true", perf_reason=["loop", "query"],
                                       ux={"run": "false"}, ui_spec="Partial", security_focus="login()"))
        sp = o["specialists"]
        self.assertEqual(sp["security"], {"run": True, "reason": "", "focus": ["login()"]})
        self.assertEqual(sp["perf"], {"run": True, "reason": "loop; query", "focus": []})
        self.assertEqual(sp["ux"], {"run": False, "ui_spec": "partial", "reason": "", "focus": []})
        for k in ("security", "perf_run", "perf_reason", "ui_spec", "security_focus"):
            self.assertNotIn(k, o)
        self.assertEqual(verdict.validate("triage-output", o), [])

    def test_list_form_and_bad_ui_spec(self):
        o = triage.reshape(base_output(specialists=["Security", "ux"]))
        self.assertEqual([r for r, v in o["specialists"].items() if v["run"]], ["security", "ux"])
        self.assertEqual(o["specialists"]["ux"]["ui_spec"], "n/a")
        o = triage.reshape(base_output(specialists={"ux": {"run": True, "ui_spec": "sort of"}}))
        self.assertEqual(o["specialists"]["ux"]["ui_spec"], "n/a")

    def test_triage_keys_detect_specialists_only_answer(self):
        self.assertIn("specialists", triage.TRIAGE_KEYS)

    def test_codex_schema_requires_specialists_strictly(self):
        s = triage.codex_schema()
        sp = s["properties"]["specialists"]
        self.assertIn("specialists", s["required"])
        self.assertEqual(sp["required"], ["security", "perf", "ux"])
        self.assertFalse(sp["additionalProperties"])
        ux = sp["properties"]["ux"]
        self.assertEqual(ux["required"], ["run", "ui_spec", "reason", "focus"])
        self.assertNotIn(None, ux["properties"]["ui_spec"]["enum"])

    def test_stored_schema_keeps_specialists_optional(self):
        old = {"schema": 1, "role": "triage", "run_id": "r", "attempt": 1, "source": "triage", **base_output()}
        self.assertEqual(verdict.validate("triage", old), [])
        n, _ = verdict.normalize("triage", old, run_id="r", attempt=1)
        self.assertEqual(n["specialists"], OFF)
        self.assertEqual(verdict.validate("triage", n), [])
        partial = dict(old, specialists={"security": True, "ux": {"run": True}})
        n, _ = verdict.normalize("triage", partial, run_id="r", attempt=1)
        self.assertEqual(n["specialists"]["security"], {"run": True, "reason": "", "focus": []})
        self.assertEqual(n["specialists"]["ux"], {"run": True, "reason": "", "focus": [], "ui_spec": "n/a"})
        self.assertEqual(n["specialists"]["perf"], OFF["perf"])


class SyntheticTriageTest(h.HermeticCase):
    def summary(self, **kw):
        s = {"risky_files": [], "changed_lines": 40, "ui_files": [], "source_files": ["a.ts"], "test_files": [],
             "files": 2, "security_files": [], "security_changed_lines": 0}
        s.update(kw)
        return s

    def test_fallback_security_threshold(self):
        cfg = config.defaults()
        fb = plan.fallback_triage(self.summary(security_files=["src/auth.ts"], security_changed_lines=10), cfg,
                                  {"auth": {}}, "p: ")
        self.assertTrue(fb["specialists"]["security"]["run"])
        self.assertTrue(fb["specialists"]["security"]["reason"].startswith("p: security-sensitive files changed"))
        self.assertEqual(fb["specialists"]["security"]["focus"], ["src/auth.ts"])
        self.assertFalse(fb["specialists"]["perf"]["run"])
        self.assertFalse(fb["specialists"]["ux"]["run"])
        fb = plan.fallback_triage(self.summary(security_files=["src/auth.ts"], security_changed_lines=9,
                                               ui_files=["a.tsx"]), cfg, {"auth": {}}, "p: ")
        self.assertFalse(any(v["run"] for v in fb["specialists"].values()))
        fb = plan.fallback_triage({"changed_lines": 3}, cfg, {"auth": {}}, "p: ")
        self.assertFalse(any(v["run"] for v in fb["specialists"].values()))
        n, _ = verdict.normalize("triage", dict(fb, run_id="r", attempt=1), run_id="r", attempt=1)
        self.assertEqual(verdict.validate("triage", n), [])

    def test_trivial_and_user_are_off(self):
        for tri in (plan._trivial_triage("tiny"), plan._user_triage(self.summary(), "checks", "standard")):
            self.assertFalse(any(v["run"] for v in tri["specialists"].values()))
            self.assertEqual(tri["specialists"]["ux"]["ui_spec"], "n/a")
            n, _ = verdict.normalize("triage", dict(tri, run_id="r", attempt=1), run_id="r", attempt=1)
            self.assertEqual(verdict.validate("triage", n), [])


class UiSpecHintsTest(unittest.TestCase):
    def test_none(self):
        self.assertTrue(prompts.ui_spec_hints("Fix the null check in parser", "- returns 0 for empty input")
                        .startswith("(none found"))

    def test_finds_design_references(self):
        prompts_text = ("Make the header like in the figma https://www.figma.com/file/abc/Header?node-id=1 "
                        "use #1A2B3C and 12px padding, the button text \"Save changes\". See shots/header.png")
        brief = "- The Save button is visible on mobile\n- returns 0 for empty input\nзроби як на макеті"
        out = prompts.ui_spec_hints(prompts_text, brief)
        self.assertIn("design references: like in, figma", out)
        self.assertIn("макеті", out)
        self.assertIn("figma links: https://www.figma.com/file/abc/Header?node-id=1", out)
        self.assertIn("image paths: shots/header.png", out)
        self.assertIn("colours: #1A2B3C", out)
        self.assertIn("sizes: 12px", out)
        self.assertIn('quoted UI copy: "Save changes"', out)
        self.assertIn("UI-worded criterion: The Save button is visible on mobile", out)
        self.assertNotIn("returns 0", out)

    def test_issue_refs_are_not_colours(self):
        self.assertNotIn("colours", prompts.ui_spec_hints("fixes #123 and #4567", ""))


class PerfHintsTest(unittest.TestCase):
    SUMMARY = {"rows": [{"path": "src/a.ts", "flags": ["source"]}, {"path": "src/a.spec.ts", "flags": ["test"]},
                        {"path": "web/App.tsx", "flags": ["ui", "source"]}],
               "ui_files": ["web/App.tsx"]}

    def patch(self, path, body, start=10):
        n = len(body.splitlines())
        return (f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -{start},{n} +{start},{n} @@\n"
                + body)

    def test_await_in_loop_nested_loop_sync_io(self):
        body = (" async function f(ids) {\n"
                "+  for (const id of ids) {\n"
                "+    const row = await db.query(id);\n"
                "+    rows.forEach((r) => {\n"
                "+      fs.readFileSync(r.path);\n"
                "+    });\n"
                "+  }\n"
                " }\n")
        out = prompts.perf_hints(self.patch("src/a.ts", body), self.SUMMARY)
        self.assertIn("- src/a.ts:12 IO/await inside a loop: const row = await db.query(id);", out)
        self.assertIn("- src/a.ts:13 nested loop: rows.forEach((r) => {", out)
        self.assertIn("- src/a.ts:14 synchronous IO: fs.readFileSync(r.path);", out)
        self.assertNotIn("src/a.ts:11", out)

    def test_tests_skipped_and_heavy_import(self):
        loop = "+for (const x of xs) {\n+  for (const y of ys) {\n+  }\n+}\n"
        self.assertEqual(prompts.perf_hints(self.patch("src/a.spec.ts", loop), self.SUMMARY), prompts.NO_HINTS)
        out = prompts.perf_hints(self.patch("web/App.tsx", "+import _ from 'lodash';\n+import x from './x';\n", 1),
                                 self.SUMMARY)
        self.assertEqual(out, "- web/App.tsx:1 large client import: import _ from 'lodash';")

    def test_removed_lines_and_await_outside_loop(self):
        body = "-for (const x of xs) {\n-  await a(x);\n-}\n+await a(xs);\n"
        self.assertEqual(prompts.perf_hints(self.patch("src/a.ts", body), self.SUMMARY), prompts.NO_HINTS)


class CalibrationTest(h.HermeticCase):
    def setUp(self):
        super().setUp()
        self.state = self.tmp / "state"
        (self.state / "runs").mkdir(parents=True)
        os.environ["VERIFY_STATE_DIR"] = str(self.state)

    def test_empty(self):
        self.assertEqual(metrics.triage_calibration("p", rows=[]), "(no past runs for this project)")

    def test_counts_from_ledger_and_retro_metrics(self):
        rows = [
            {"run_id": "r1", "project": "p", "qa_scope": "browser", "review_tier": "deep",
             "found": {"qa": False, "reviewer": True}},
            {"run_id": "r2", "project": "p", "qa_scope": "browser", "review_tier": "standard",
             "found": {"qa": False, "reviewer": False}},
            {"run_id": "r3", "project": "p", "qa_scope": "checks", "review_tier": "standard"},
            {"run_id": "r4", "project": "p", "qa_scope": "none", "review_tier": "standard"},
            {"run_id": "r5", "project": "other", "qa_scope": "run", "review_tier": "deep",
             "found": {"qa": True, "reviewer": True}},
            {"run_id": "r6", "project": "p", "kind": "standalone", "qa_scope": "run", "review_tier": "deep",
             "found": {"qa": True, "reviewer": True}},
        ]
        d = self.state / "runs" / "r3" / "retro"
        d.mkdir(parents=True)
        (d / "metrics.json").write_text(json.dumps({"triage_eval": {"qa_found_blocking": True,
                                                                    "reviewer_found_blocking": False}}))
        out = metrics.triage_calibration("p", rows=rows)
        self.assertEqual(out.splitlines(), [
            "last 4 runs (3 with findings data); blocking issues found / runs:",
            "- QA: checks 1/1, browser 0/2",
            "- review: standard 0/2, deep 1/1"])
        self.assertEqual(metrics.triage_calibration("p", last=1, rows=rows),
                         "last 1 runs: no findings data recorded")

    def test_reads_ledger_file_and_survives_errors(self):
        (self.state / "ledger.jsonl").write_text(json.dumps(
            {"run_id": "r1", "project": "p", "qa_scope": "run", "review_tier": "standard",
             "found": {"qa": False, "reviewer": False}}) + "\n")
        self.assertIn("- QA: run 0/1", metrics.triage_calibration("p"))
        self.assertIn("- QA: run 0/1", prompts.calibration_text("p", config.defaults()))
        orig = metrics.triage_calibration
        metrics.triage_calibration = lambda *a, **k: 1 / 0
        try:
            self.assertEqual(prompts.calibration_text("p", {}), "(unavailable: ZeroDivisionError)")
        finally:
            metrics.triage_calibration = orig

    def test_ledger_row_records_found(self):
        m = {"run_id": "r", "session_id": "s", "project": "p", "window": {"wall_min": 1.0}, "phases": [],
             "outcome": {"status": "passed", "attempts": 1, "failed_attempts": 0},
             "change": {}, "triage_eval": {"qa_found_blocking": False, "reviewer_found_blocking": True},
             "tokens": {"main": {"total": 1, "cost_usd": 0.0}, "cost_usd": 0.0,
                        "by_role": {"triage": 0, "reviewer": 0, "qa": 0, "retro": 0},
                        "cost_by_role": {"triage": 0, "reviewer": 0, "qa": 0, "retro": 0}}}
        row = metrics.ledger_row(m, {"repo": {"key": "k"}}, "light")
        self.assertEqual(row["found"], {"qa": False, "reviewer": True})


class TriageCallSpecialistsTest(h.Case):
    def setUp(self):
        super().setUp()
        self.root = self.make_repo()
        self.prompt(self.root)
        self.code_change(self.root)
        self.rid, _ = self.begin(self.root)
        self.att = self.run_dir(self.rid) / "attempt-1"

    def test_specialists_stored_and_logged(self):
        out = {**json.loads((Path(h.FIX) / "triage-checks.json").read_text())}
        out["specialists"] = {"security": {"run": True, "reason": "token handling", "focus": ["src/a.js"]},
                              "perf": {"run": False, "reason": "no loops", "focus": []},
                              "ux": {"run": False, "ui_spec": "n/a", "reason": "no UI", "focus": []}}
        f = self.tmp / "spec.json"
        f.write_text(json.dumps(out))
        os.environ["FAKE_CLAUDE_OUTPUT"] = str(f)
        self.brief(self.rid)
        t = json.loads((self.att / "triage.json").read_text())
        self.assertEqual(t["source"], "triage")
        self.assertEqual(t["specialists"], out["specialists"])
        ev = [e for e in self.events() if e["event"] == "triage"][-1]["data"]
        self.assertEqual(ev["specialists"], ["security"])
        text = (self.att / "triage.input.md").read_text()
        self.assertIn("(no past runs for this project)", text)
        self.assertIn("(none found", text)

    def test_old_output_without_specialists_is_accepted(self):
        old = {k: v for k, v in json.loads((Path(h.FIX) / "triage-checks.json").read_text()).items()
               if k != "specialists"}
        f = self.tmp / "old.json"
        f.write_text(json.dumps(old))
        os.environ["FAKE_CLAUDE_OUTPUT"] = str(f)
        self.brief(self.rid)
        t = json.loads((self.att / "triage.json").read_text())
        self.assertEqual((t["source"], t["specialists"]), ("triage", OFF))


if __name__ == "__main__":
    unittest.main()
