"""WP2 plan tests: plan block, browser pointer, fallback/floors/flags, material delta, standalone, consent (§2.9)."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures" / "wp2"))
import wp2helpers as h  # noqa: E402
from wp2helpers import VL  # noqa: E402

from vlooplib import config, plan  # noqa: E402


class BrowserPlanTest(h.Case):  # (4)
    herdr = True

    def test_browser_plan_pointer_and_block(self):
        self.write_config(qa_browser_consent="always")
        root = self.make_repo()
        self.prompt(root)
        self.code_change(root)
        self.edit(root, "src/page.html", "<p>new</p>\n", append=True)
        rid, _ = self.begin(root, "--app-url", "https://localhost:8100")
        self.triage_output("browser")
        out = self.brief(rid)
        self.assertEqual(out.splitlines(), [
            "verify-loop · plan · attempt 1 · triage sonnet (6s, ≈$0.0042, confidence high)",
            "  review: full · sonnet/medium · standard — service + template; no auth/data paths",
            "  qa: browser · full · sonnet/medium — UI template changed (page.html); flow reachable at /scan",
            "  auth: missing (proj)",
            "  consent: browser QA approved (config)",
            "next (Herdr, parallel — one Bash call per line in ONE message, run_in_background: true):",
            f"  {VL} spawn reviewer --run {rid}",
            f"  {VL} spawn qa --run {rid}",
        ])
        att = self.run_dir(rid) / "attempt-1"
        p = self.plan(rid)
        self.assertEqual(p["source"], "triage")
        self.assertEqual(p["triage_path"], "attempt-1/triage.json")
        self.assertEqual(p["transport"], "herdr")
        qa = p["roles"]["qa"]
        self.assertEqual((qa["run"], qa["mode"], qa["scope"], qa["model"], qa["effort"], qa["timeout_s"]),
                         (True, "full", "browser", "sonnet", "medium", 1800))
        self.assertEqual((qa["focus"], qa["needs_login"], qa["auth_status"]), (["step 3 Next button"], False, "missing"))
        rv = p["roles"]["reviewer"]
        self.assertEqual((rv["tier"], rv["model"], rv["effort"], rv["timeout_s"], rv["carried_from"]),
                         ("standard", "sonnet", "medium", 1200, None))
        ptr = json.loads((att / "pw-pointer.json").read_text())
        self.assertEqual(ptr["run_id"], rid)
        self.assertEqual(ptr["attempt"], 1)
        self.assertEqual(ptr["output_dir"], str(att / "qa-artifacts"))
        self.assertIsNone(ptr["storage_state"])  # auth missing
        self.assertEqual((ptr["ignore_https_errors"], ptr["viewport"]), (True, "1366x900"))
        key = self.ctx(rid)["repo"]["key"]
        self.assertEqual(json.loads((self.state / "qa-current" / f"{key}.json").read_text()), ptr)
        mcp = json.loads((att / "mcp.json").read_text())
        self.assertEqual(mcp, {"mcpServers": {"playwright": {
            "type": "stdio", "command": str(h.PW_MCP),
            "args": ["--pointer", str(att / "pw-pointer.json")]}}})
        task = (att / "qa.prompt.md").read_text()
        self.assertIn("Playwright tools (mcp__playwright__*) are available.", task)
        # QA passed filename: to the screenshot tool despite a buried note (WP7 issue #7): exact call form
        self.assertIn("call `mcp__playwright__browser_take_screenshot` with NO `filename` argument, exactly `{}`", task)
        self.assertIn("missing — no saved login state", task)
        self.assertIn(f"Refresh: ! {VL} auth capture --url https://localhost:8100", task)
        self.assertIn("https://localhost:8100", task)
        tri_in = (att / "triage.input.md").read_text()
        self.assertIn("+export const extra0 = 0;", tri_in)
        ev = [e for e in self.events() if e["event"] == "plan"][-1]["data"]
        self.assertEqual(ev["qa"], {"run": True, "mode": "full", "scope": "browser", "model": "sonnet",
                                    "effort": "medium"})
        tev = [e for e in self.events() if e["event"] == "triage"][-1]["data"]
        self.assertEqual((tev["source"], tev["model"], tev["qa_scope"], tev["cost_usd"]),
                         ("triage", "claude-haiku-5-5", "browser", 0.0042))

    def test_saved_login_state_goes_into_pointer(self):
        self.write_config(qa_browser_consent="always")
        root = self.make_repo()
        auth = self.state / "auth" / "proj.json"
        auth.parent.mkdir(parents=True)
        auth.write_text(json.dumps({"cookies": [{"name": "a", "expires": 4102444800}], "origins": []}))
        self.prompt(root)
        self.code_change(root)
        rid, _ = self.begin(root)
        self.triage_output("browser")
        out = self.brief(rid)
        self.assertIn("  auth: ok (proj, saved 0 days ago)", out)
        ptr = json.loads((self.run_dir(rid) / "attempt-1" / "pw-pointer.json").read_text())
        self.assertEqual(ptr["storage_state"], str(auth))


class FallbackFlagsTest(h.Case):
    def setUp(self):
        super().setUp()
        self.root = self.make_repo()
        self.prompt(self.root)
        self.code_change(self.root)

    def test_no_triage_fallback(self):
        rid, _ = self.begin(self.root, "--no-triage")
        out = self.brief(rid)
        self.assertEqual(self.claude_calls(), [])
        self.assertEqual(out.splitlines()[0], "verify-loop · plan · attempt 1 · triage unavailable (--no-triage) → "
                                              "fallback")
        p = self.plan(rid)
        self.assertEqual(p["source"], "fallback")
        self.assertEqual(p["roles"]["qa"]["scope"], "checks")
        self.assertTrue(p["roles"]["qa"]["reason"].startswith("--no-triage: "))
        self.assertTrue(p["roles"]["reviewer"]["reason"].startswith("--no-triage: "))

    def test_both_flags_skip_triage(self):
        rid, _ = self.begin(self.root, "--qa", "run", "--review", "deep")
        out = self.brief(rid)
        self.assertEqual(self.claude_calls(), [])
        p = self.plan(rid)
        self.assertEqual(p["source"], "user")
        self.assertEqual((p["roles"]["reviewer"]["tier"], p["roles"]["reviewer"]["model"]), ("deep", "opus"))
        self.assertEqual((p["roles"]["qa"]["scope"], p["roles"]["qa"]["model"]), ("run", "sonnet"))
        self.assertEqual(out.splitlines()[0], "verify-loop · plan · attempt 1 · user flags")

    def test_single_flag_overrides_triage(self):
        self.triage_output("browser")
        rid, _ = self.begin(self.root, "--qa", "none")
        self.brief(rid)
        self.assertEqual(len(self.claude_calls()), 1)
        qa = self.plan(rid)["roles"]["qa"]
        self.assertEqual((qa["run"], qa["scope"], qa["skipped_by"], qa["reason"]),
                         (False, "none", "user", "--qa none (user flag)"))

    def test_repo_floors_raise_only(self):
        h.write(self.root / ".claude" / "verify.json", json.dumps({"floors": {"qa": "run", "review": "deep"}}))
        self.triage_output("checks")
        rid, _ = self.begin(self.root)
        out = self.brief(rid)
        p = self.plan(rid)
        self.assertEqual((p["roles"]["qa"]["scope"], p["roles"]["reviewer"]["tier"]), ("run", "deep"))
        self.assertIn("repo floor: qa checks → run", p["notes"])
        self.assertIn("  notes: repo floor: qa checks → run; repo floor: review standard → deep", out)

    def test_agent_transport_without_browser(self):
        self.write_config(agent_transport_browser=False, qa_browser_consent="always")
        self.triage_output("browser")
        rid, _ = self.begin(self.root)
        out = self.brief(rid)
        qa = self.plan(rid)["roles"]["qa"]
        self.assertEqual(qa["scope"], "run")
        self.assertIn("browser unavailable on the Agent-tool transport", out)
        self.assertIsNone(self.ctx(rid)["browser_consent"])

    def test_fallback_rules(self):
        cfg = config.defaults()
        s = {"risky_files": [], "changed_lines": 700, "ui_files": ["a.html"], "source_files": ["a.ts"],
             "test_files": [], "files": 2}
        fb = plan.fallback_triage(s, cfg, {"app_url": "https://x", "auth": {}}, "p: ")
        self.assertEqual((fb["review"]["tier"], fb["qa"]["scope"]), ("deep", "browser"))
        fb = plan.fallback_triage({**s, "changed_lines": 10}, cfg, {"app_url": None, "auth": {}}, "p: ")
        self.assertEqual((fb["review"]["tier"], fb["qa"]["scope"]), ("standard", "checks"))
        fb = plan.fallback_triage({**s, "risky_files": ["auth/x.ts"], "ui_files": [], "source_files": [],
                                   "changed_lines": 3}, cfg, {"app_url": None, "auth": {}}, "p: ")
        self.assertEqual((fb["review"]["tier"], fb["qa"]["scope"]), ("deep", "none"))


class MaterialDeltaTest(h.Case):  # (12)
    def test_retriage_takes_the_maximum(self):
        root = self.make_repo()
        self.prompt(root)
        self.code_change(root)
        rid, _ = self.begin(root)
        self.triage_output("deep-run")
        self.brief(rid)
        p1 = self.plan(rid)
        self.assertEqual((p1["roles"]["qa"]["scope"], p1["roles"]["reviewer"]["tier"]), ("run", "deep"))
        att1 = self.run_dir(rid) / "attempt-1"
        (att1 / "reviewer.json").write_text(json.dumps(h.reviewer_fail()))
        (att1 / "qa.json").write_text(json.dumps(h.qa_pass("run")))
        self.ok("judge", "--run", rid)
        self.triage_output("checks")  # the new triage is lower on both dimensions
        self.edit(root, "src/helper.js", "export const helper = () => 1;\n")  # new source file → material
        out = self.ok("begin", "--rerun", "--run", rid, "--fix-notes", "-", stdin="R1: moved to helper", cwd=root)
        self.assertEqual(len(self.claude_calls()), 2)
        p2 = self.plan(rid, 2)
        self.assertEqual(p2["source"], "triage")
        self.assertEqual((p2["roles"]["qa"]["scope"], p2["roles"]["reviewer"]["tier"]), ("run", "deep"))
        self.assertEqual((p2["roles"]["reviewer"]["mode"], p2["roles"]["reviewer"]["model"]), ("recheck", "opus"))
        self.assertEqual((p2["roles"]["qa"]["mode"], p2["roles"]["qa"]["model"], p2["roles"]["qa"]["effort"]),
                         ("delta", "sonnet", "low"))
        self.assertTrue(p2["notes"][0].startswith("re-triage: delta adds source files (src/helper.js)"))
        self.assertIn("  notes: re-triage: delta adds source files (src/helper.js)", out)
        tri_in = (self.run_dir(rid) / "attempt-2" / "triage.input.md").read_text()
        self.assertIn("  review: full · opus/medium · deep — new source file with request handling", tri_in)
        self.assertIn("since the previous attempt", tri_in)

    def test_raised_scope_from_retriage(self):
        root = self.make_repo()
        self.prompt(root)
        self.code_change(root)
        rid, _ = self.begin(root)
        self.triage_output("none")
        self.brief(rid)
        self.assertFalse(self.plan(rid)["roles"]["qa"]["run"])
        att1 = self.run_dir(rid) / "attempt-1"
        (att1 / "reviewer.json").write_text(json.dumps(h.reviewer_fail()))
        out = self.ok("judge", "--run", rid)
        self.assertIn("qa SKIPPED (triage: none)", out)
        self.triage_output("checks")
        self.edit(root, "src/helper.js", "export const helper = () => 1;\n")
        self.ok("begin", "--rerun", "--run", rid, "--fix-notes", "-", stdin="R1: x", cwd=root)
        qa = self.plan(rid, 2)["roles"]["qa"]
        self.assertEqual((qa["run"], qa["mode"], qa["scope"]), (True, "full", "checks"))


class StandaloneTest(h.Case):  # (11)
    def test_commit_range_review(self):
        root = self.make_repo()
        a = h.git(root, "rev-parse", "HEAD").strip()
        h.write(root / "src/a.js", h.SRC + "export const g = 2;\n" * 20)
        h.git(root, "commit", "-qam", "add g")
        b = h.git(root, "rev-parse", "HEAD").strip()
        h.write(root / "src/a.js", "dirty working tree\n")
        out = self.ok("begin", "--standalone", "--roles", "reviewer", "--target", f"{a}..{b}", "--review", "standard",
                      "--criteria", "g is exported", cwd=root)
        rid = self.run_id_of(out)
        self.assertTrue(out.startswith(f"verify-review · run {rid} · standalone · proj · {a}..{b} · 1 files, +20/−0"))
        self.assertEqual(self.claude_calls(), [])
        ctx = self.ctx(rid)
        self.assertEqual((ctx["kind"], ctx["roles"], ctx["target"]["code_ref"]), ("standalone", ["reviewer", "security", "perf", "ux"], b))
        brief = (self.run_dir(rid) / "brief.md").read_text()
        self.assertIn("## Acceptance criteria\n- g is exported", brief)
        self.assertIn("## Commit messages\n- add g", brief)
        self.assertEqual((self.run_dir(rid) / "prompts.md").read_text().strip(), "(standalone run: no user prompts)")
        task = (self.run_dir(rid) / "attempt-1" / "reviewer.prompt.md").read_text()
        self.assertIn(f"Review the code at commit {b}: read files with git show {b}:<path>", task)
        self.assertFalse((self.state / "sessions" / f"{h.SID}.json").exists())  # session loop untouched
        (self.run_dir(rid) / "attempt-1" / "reviewer.json").write_text(json.dumps(h.reviewer_pass()))
        out = self.ok("judge", "--run", rid)
        self.assertEqual(out.splitlines()[:2], ["RESULT: PASS", "verify-review · attempt 1 · review PASS · " +
                                                out.splitlines()[1].rsplit(" · ", 1)[1]])
        self.assertEqual(out.splitlines()[-1], "next: report this to the user; fix only if asked")
        self.assertEqual(self.ctx(rid)["status"], "done")

    def test_url_qa_is_browser_without_consent_prompt(self):
        root = self.make_repo()
        self.prompt(root)
        self.code_change(root)
        out = self.ok("begin", "--standalone", "--roles", "qa", "--url", "https://localhost:9", "--focus",
                      "home page loads", cwd=root)
        rid = self.run_id_of(out)
        self.assertNotIn("CONSENT NEEDED", out)
        self.assertEqual(self.claude_calls(), [])
        qa = self.plan(rid)["roles"]["qa"]
        self.assertEqual((qa["scope"], qa["run"]), ("browser", True))
        bc = self.ctx(rid)["browser_consent"]
        self.assertEqual((bc["status"], bc["source"], bc["decision"]), ("granted", "user flags", "flags"))
        self.assertIn("  consent: browser QA approved (user flags)", out)
        self.assertEqual(self.ctx(rid)["target"]["spec"], "session")
        att = self.run_dir(rid) / "attempt-1"
        self.assertTrue((att / "mcp.json").exists())
        task = (att / "qa.prompt.md").read_text()
        self.assertIn("- home page loads", task)
        self.assertIn("https://localhost:9", task)
        self.assertIsNone(self.loop_state(root)["loop"]["run_id"])  # the session loop is not touched

    def test_clean_tree_url_qa_is_exploratory(self):
        # `/verify-qa <url> --focus …` on a clean tree printed NOTHING (WP7 bug #4)
        root = self.make_repo()
        out = self.ok("begin", "--standalone", "--roles", "qa", "--url", "https://localhost:9", "--focus",
                      "account page shows the signed-in user", cwd=root)
        rid = self.run_id_of(out)
        self.assertTrue(out.startswith(f"verify-qa · run {rid} · standalone · proj · worktree · no code diff — "
                                       f"exploratory QA against the focus/criteria"), out)
        self.assertEqual(self.claude_calls(), [])
        qa = self.plan(rid)["roles"]["qa"]
        self.assertEqual((qa["scope"], qa["run"]), ("browser", True))
        self.assertTrue(self.ctx(rid)["standalone"]["exploratory"])
        task = (self.run_dir(rid) / "attempt-1" / "qa.prompt.md").read_text()
        self.assertIn("No code diff: this is an exploratory check of the running app", task)
        self.assertIn("- account page shows the signed-in user", task)
        (self.run_dir(rid) / "attempt-1" / "qa.json").write_text(json.dumps(h.qa_pass("browser")))
        self.assertEqual(self.ok("judge", "--run", rid).splitlines()[0], "RESULT: PASS")

    def test_clean_tree_criteria_only_qa_runs_and_review_dropped(self):
        root = self.make_repo()
        out = self.ok("begin", "--standalone", "--roles", "reviewer,qa", "--criteria", "cli prints usage", cwd=root)
        rid = self.run_id_of(out)
        self.assertTrue(out.startswith(f"verify-qa · run {rid}"), out)
        self.assertEqual(self.ctx(rid)["roles"], ["qa"])
        qa = self.plan(rid)["roles"]["qa"]
        self.assertEqual((qa["scope"], qa["reason"]), ("run", "exploratory check, no code diff"))

    def test_clean_tree_without_target_hints_is_nothing(self):
        root = self.make_repo()
        for roles in ("qa", "reviewer"):
            out = self.ok("begin", "--standalone", "--roles", roles, cwd=root)
            self.assertIn("· NOTHING · target worktree has no changes", out)
        out = self.ok("begin", "--standalone", "--roles", "reviewer", "--criteria", "x", cwd=root)
        self.assertEqual(out.strip(), "verify-review · NOTHING · target worktree has no changes")

    def test_triage_for_missing_parameter_and_qa_floor(self):
        root = self.make_repo()
        h.write(root / "src/a.js", h.SRC + "// x\n" * 20)
        self.triage_output("none")
        out = self.ok("begin", "--standalone", "--roles", "reviewer,qa", "--target", "worktree", cwd=root)
        rid = self.run_id_of(out)
        self.assertTrue(out.startswith(f"verify · run {rid}"))
        self.assertEqual(len(self.claude_calls()), 1)
        qa = self.plan(rid)["roles"]["qa"]
        self.assertEqual((qa["scope"], qa["run"]), ("checks", True))
        self.assertEqual(self.ctx(rid)["target"]["spec"], "worktree")

    def test_unknown_target(self):
        root = self.make_repo()
        rc, _, err = self.vl("begin", "--standalone", "--roles", "reviewer", "--target", "nope", cwd=root)
        self.assertEqual(rc, 2)
        rc, _, err = self.vl("begin", "--standalone", "--roles", "reviewer", "--target", "abc..def", cwd=root)
        self.assertEqual((rc, err.strip()), (2, "vloop: error: commits not found locally; run git fetch"))


class ConsentUnitTest(h.HermeticCase):
    def mk(self, scope="browser", run=True, bc=None, n=2):
        ctx = {"browser_consent": bc}
        p = {"attempt": n, "roles": {"qa": {"run": run, "scope": scope, "mode": "full", "reason": "r"}}}
        return ctx, p

    def test_order(self):
        cfg = {"qa_browser_consent": "ask"}
        ctx, p = self.mk(bc={"status": "granted", "attempt": 1, "decision": "allow"})
        self.assertEqual(plan.apply_consent(ctx, p, cfg), "reused")
        ctx, p = self.mk(bc={"status": "declined", "attempt": 1, "decision": "no-browser"})
        self.assertEqual(plan.apply_consent(ctx, p, cfg), "declined")
        self.assertEqual(p["roles"]["qa"]["scope"], "run")
        ctx, p = self.mk(bc={"status": "declined", "attempt": 1, "decision": "no-browser"})
        self.assertEqual(plan.apply_consent(ctx, p, cfg, rule1=True), "pending")
        ctx, p = self.mk(scope="checks", bc={"status": "declined", "attempt": 1, "decision": "skip-qa"})
        self.assertEqual(plan.apply_consent(ctx, p, cfg), "skipped")
        self.assertEqual((p["roles"]["qa"]["run"], p["roles"]["qa"]["skipped_by"]), (False, "user"))
        ctx, p = self.mk(bc={"status": "declined", "attempt": 1, "decision": "skip-qa"})
        self.assertEqual(plan.apply_consent(ctx, p, cfg, rule1=True), "pending")
        ctx, p = self.mk(scope="run")
        self.assertIsNone(plan.apply_consent(ctx, p, cfg))
        ctx, p = self.mk()
        self.assertEqual(plan.apply_consent(ctx, p, cfg, explicit=True), "granted")
        self.assertEqual(ctx["browser_consent"]["source"], "user flags")
        ctx, p = self.mk()
        self.assertEqual(plan.apply_consent(ctx, p, {"qa_browser_consent": "never"}), "declined")
        ctx, p = self.mk()
        self.assertEqual(plan.apply_consent(ctx, p, {"qa_browser_consent": "always"}), "granted")


if __name__ == "__main__":
    unittest.main()
