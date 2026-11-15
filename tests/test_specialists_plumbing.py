"""Specialist plumbing: guards/hooks/transports for the new roles, plan selection (precedence, UX gate, model keys),
re-run ratchet/recheck/delta/carry, `begin --security/--perf/--ux`, loop spawn/collect/record, metrics."""
from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "fixtures" / "wp2"))
import wp2helpers as h  # noqa: E402
from wp2helpers import VL  # noqa: E402

from vlooplib import guard, hooks, metrics, plan, roles  # noqa: E402
from vlooplib.transports import common, herdr  # noqa: E402


def triage_obj(security=False, perf=False, ux=False, ui_spec="none", scope="none", risk="medium") -> dict:
    return {"change_size": "small", "risk": risk,
            "qa": {"scope": scope, "reason": "r", "focus": [], "needs_login": False},
            "review": {"tier": "standard", "reason": "small change", "focus": []},
            "specialists": {"security": {"run": security, "reason": "auth input handled", "focus": ["src/a.js"]},
                            "perf": {"run": perf, "reason": "loop over rows", "focus": []},
                            "ux": {"run": ux, "ui_spec": ui_spec, "reason": "page markup", "focus": []}},
            "confidence": "high"}


def draft(**kw) -> dict:
    return plan._draft(triage_obj(**kw))


def spec_verdict(role, verdict="pass", findings=(), rechecked=None) -> dict:
    return {"schema": 2, "role": role, "verdict": verdict, "summary": "s", "findings": list(findings),
            "suggestions": [], "rechecked": rechecked or []}


NEW_LINE = len(h.SRC.splitlines()) + 1
SEC_FINDING = {"id": "SC1", "severity": "major", "confidence": "high", "category": "security", "file": "src/a.js",
               "line": NEW_LINE, "title": "Unchecked input", "problem": "x is not validated.",
               "scenario": "POST /f1 with x=__proto__ → f1 in src/a.js:2 → prototype pollution for every user",
               "evidence_ref": f"src/a.js:{NEW_LINE}", "suggestion": "Validate x.", "status": "new"}


class RoleWiringTest(unittest.TestCase):
    def test_guard_and_hook_tables(self):
        self.assertEqual(hooks.GUARD_ROLES, roles.ALL_ROLES)
        for r in roles.ALL_ROLES:
            self.assertEqual(hooks.agent_kind(f"agent-verify-loop:{roles.AGENT[r]}"), (True, r))
            self.assertEqual(hooks.agent_kind(roles.AGENT[r]), (True, r))

    def test_guard_cli_accepts_specialists(self):
        import argparse

        p = argparse.ArgumentParser()
        sub = p.add_subparsers()
        hooks.register(sub)
        for r in roles.SPECIALISTS:
            self.assertEqual(p.parse_args(["guard", r]).role, r)

    def test_transport_tables(self):
        for r in roles.SPECIALISTS:
            self.assertEqual(common.AGENT_TYPES[r], f"verify-{r}")
            self.assertIn(r, common.OWN_AGENTS)
            self.assertEqual(common.claude_tools(r, "/runs/x")[:len(common.REVIEWER_TOOLS)], common.REVIEWER_TOOLS)
            self.assertEqual(herdr.HERDR_ROLES[r], f"verify-{r}")


class GuardSpecialistTest(h.Case):
    def test_specialists_use_the_reviewer_guard(self):
        runs = self.state / "runs"
        for r in roles.SPECIALISTS:
            ok = guard.check(r, {"tool_name": "Write", "tool_input": {"file_path": str(runs / "x" / f"{r}.json")}})
            self.assertEqual(ok["hookSpecificOutput"]["permissionDecision"], "allow")
            bad = guard.check(r, {"tool_name": "Write", "tool_input": {"file_path": str(self.tmp / "a.js")}})
            self.assertEqual(bad["hookSpecificOutput"]["permissionDecision"], "deny")
            self.assertTrue(bad["hookSpecificOutput"]["permissionDecisionReason"].startswith(f"{r} guard: "))
            sed = guard.check(r, {"tool_name": "Bash", "tool_input": {"command": "sed -i s/a/b/ x"}})
            self.assertEqual(sed["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIsNone(guard.check("nope", {"tool_name": "Write", "tool_input": {"file_path": "/x"}}))


class SelectionTest(unittest.TestCase):
    def run_spec(self, d, cfg=None, flags=None, summary=None, keep_on=None):
        notes: list[str] = []
        plan._specialists(d, cfg or {}, flags or {}, summary or {}, notes, keep_on)
        return {s: d["spec"][s]["run"] for s in roles.SPECIALISTS}, notes

    def test_triage_decides_by_default(self):
        got, notes = self.run_spec(draft(security=True, perf=True), summary={})
        self.assertEqual(got, {"security": True, "perf": True, "ux": False})
        self.assertEqual(notes, [])

    def test_precedence_user_flag_over_repo_over_triage(self):
        d = draft(security=True)
        got, notes = self.run_spec(d, cfg={"specialists": {"security": "off", "perf": "on"}})
        self.assertEqual(got, {"security": False, "perf": True, "ux": False})
        self.assertEqual(notes, ["security off: repo specialists.security=off (triage: on)",
                                 "perf on: repo specialists.perf=on (triage: off)"])
        self.assertEqual((d["spec"]["perf"]["source"], d["spec"]["security"]["source"]), ("repo", "repo"))
        d = draft(security=True)
        got, notes = self.run_spec(d, cfg={"specialists": {"security": "off", "perf": "on"}},
                                   flags={"security": "on", "perf": "off"})
        self.assertEqual(got, {"security": True, "perf": False, "ux": False})
        self.assertEqual(d["spec"]["security"]["source"], "user")
        self.assertTrue(d["spec"]["security"]["reason"].startswith("--security on (user flag)"))

    def test_ux_gate(self):
        ui = {"ui_files": ["src/page.html"]}
        self.assertTrue(self.run_spec(draft(ux=True, ui_spec="none"), summary=ui)[0]["ux"])
        self.assertTrue(self.run_spec(draft(ux=True, ui_spec="partial"), summary=ui)[0]["ux"])
        got, notes = self.run_spec(draft(ux=True, ui_spec="clear"), summary=ui)
        self.assertFalse(got["ux"])
        self.assertEqual(notes, ["ux off: the request gives a clear UI spec (triage: on)"])
        got, notes = self.run_spec(draft(ux=True, ui_spec="none"), summary={})
        self.assertFalse(got["ux"])
        self.assertEqual(notes, ["ux off: no UI files changed (triage: on)"])
        self.assertTrue(self.run_spec(draft(ux=True, ui_spec="clear"), summary=ui, flags={"ux": "on"})[0]["ux"])
        self.assertTrue(self.run_spec(draft(ui_spec="clear"), cfg={"specialists": {"ux": "on"}})[0]["ux"])

    def test_keep_on_ratchets(self):
        d = draft()
        d["spec"]["security"]["focus"] = []
        got, _ = self.run_spec(d, keep_on={"security": ["src/auth.js"]})
        self.assertTrue(got["security"])
        self.assertEqual((d["spec"]["security"]["source"], d["spec"]["security"]["focus"]), ("earlier", ["src/auth.js"]))
        got, notes = self.run_spec(draft(), flags={"security": "off"}, keep_on={"security": []})
        self.assertFalse(got["security"])
        self.assertEqual(notes, ["security off: --security off (user flag)"])

    def test_old_triage_without_specialists_is_all_off(self):
        d = plan._draft({"qa": {"scope": "checks"}, "review": {"tier": "standard"}})
        self.assertEqual({s: d["spec"][s]["run"] for s in roles.SPECIALISTS},
                         {"security": False, "perf": False, "ux": False})

    def test_roles_only_for_listed_context_roles(self):
        d = draft(security=True, perf=True)
        self.run_spec(d)
        cfg = {"timeouts_s": {"security": 1200, "perf": 900}}
        r = plan._roles({"roles": list(roles.ALL_ROLES)}, d, cfg, "triage")
        self.assertEqual(sorted(r), ["perf", "qa", "reviewer", "security"])
        self.assertEqual((r["security"]["timeout_s"], r["perf"]["timeout_s"]), (1200, 900))
        self.assertEqual(r["security"]["focus"], ["src/a.js"])
        old = plan._roles({"roles": ["reviewer", "qa"]}, d, cfg, "triage")
        self.assertEqual(sorted(old), ["qa", "reviewer"])

    def test_finalize_model_keys(self):
        cfg = {"models": {"security": {"standard": ["sonnet", "high"], "high_risk": ["opus", "medium"],
                                       "delta": ["sonnet", "low"]},
                          "perf": {"standard": ["sonnet", "medium"], "delta": ["sonnet", "low"]}}}

        def fin(role, mode, risk):
            p = {"roles": {role: {"run": True, "mode": mode, "tier": "standard"}}}
            plan.finalize(p, cfg, {"risk": risk}, "/nonexistent")
            return p["roles"][role]["model"], p["roles"][role]["effort"], p["roles"][role]["tier"]
        self.assertEqual(fin("security", "full", "medium"), ("sonnet", "high", "standard"))
        self.assertEqual(fin("security", "full", "high"), ("opus", "medium", "high_risk"))
        self.assertEqual(fin("security", "delta", "high"), ("sonnet", "low", "standard"))
        self.assertEqual(fin("security", "recheck", "high"), ("opus", "medium", "high_risk"))
        self.assertEqual(fin("perf", "full", "high"), ("sonnet", "medium", "standard"))

    def test_role_lines_and_relevance(self):
        p = {"roles": {"reviewer": {"run": True, "mode": "full", "model": "sonnet", "effort": "medium",
                                    "tier": "standard", "reason": "r"},
                       "security": {"run": True, "mode": "full", "model": "sonnet", "effort": "high", "reason": "s"},
                       "ux": {"run": False, "carried_from": 1}}}
        self.assertEqual(plan.role_lines(p), ["  review: full · sonnet/medium · standard — r",
                                              "  security: full · sonnet/high — s",
                                              "  ux (advisory): carried from #1 (PASS)"])
        rows = {"rows": [{"path": "src/a.js"}]}
        self.assertFalse(plan._relevant("security", rows, True, {}))
        self.assertTrue(plan._relevant("security", rows, True, {"focus": ["src/a.js validation"]}))
        self.assertTrue(plan._relevant("security", {**rows, "security_files": ["src/auth.js"]}, True, {}))
        self.assertTrue(plan._relevant("perf", {**rows, "source_files": ["src/a.js"]}, True, {}))
        self.assertFalse(plan._relevant("ux", {**rows, "source_files": ["src/a.js"]}, True, {}))
        self.assertTrue(plan._relevant("ux", {"ui_files": ["a.css"]}, True, {}))


class LoopSpecialistTest(h.Case):
    def setUp(self):
        super().setUp()
        h.prompts.TEMPLATES_DIR = None
        self.root = self.make_repo()
        self.prompt(self.root)
        self.code_change(self.root)

    def triage(self, **kw):
        p = self.tmp / "triage-out.json"
        p.write_text(json.dumps(triage_obj(**kw)))
        os.environ["FAKE_CLAUDE_OUTPUT"] = str(p)

    def test_spawn_lines_prompts_record_judge(self):
        self.triage(security=True, perf=True)
        rid, _ = self.begin(self.root)
        self.assertEqual(self.ctx(rid)["roles"], list(roles.ALL_ROLES))
        out = self.brief(rid)
        att = self.run_dir(rid) / "attempt-1"
        self.assertIn("  security: full · sonnet/high — auth input handled", out)
        self.assertIn("  perf (advisory): full · sonnet/medium — loop over rows", out)
        for r in ("security", "perf"):
            self.assertIn(f'Agent(subagent_type="verify-{r}", model="sonnet", description="verify {r} #1", '
                          f'prompt="Your task file is {att}/{r}.prompt.md.', out)
            self.assertNotIn("{{", (att / f"{r}.prompt.md").read_text())
        self.assertNotIn("verify-ux", out)
        rv = (att / "reviewer.prompt.md").read_text()
        self.assertIn("A security specialist reviews this change in parallel", rv)
        self.assertIn("A performance specialist reviews this change in parallel", rv)
        self.assertNotIn("A UX specialist", rv)
        p = self.plan(rid)
        self.assertEqual(p["specialists"]["ux"], {"run": False, "reason": "page markup", "source": "triage"})
        ev = [e for e in self.events() if e["event"] == "plan"][-1]["data"]
        self.assertEqual(sorted(ev["specialists"]), ["perf", "security"])
        agents = self.ctx(rid)["attempts"][0]["agents"]
        self.assertEqual(agents["security"]["status"], "planned")

        (att / "reviewer.json").write_text(json.dumps(h.reviewer_pass()))
        out = self.ok("collect", "--run", rid)
        self.assertEqual(out.splitlines()[-1], "waiting for: security, perf")
        rc, _, err = self.vl("judge", "--run", rid)
        self.assertEqual((rc, err.strip()), (2, "vloop: error: waiting for: security, perf"))
        msg = "Done.\n```json\n" + json.dumps(spec_verdict("security", "fail", [SEC_FINDING])) + "\n```\n"
        out = self.ok("record", "security", "--run", rid, stdin=msg)
        self.assertTrue(out.startswith("security: FAIL"), out)
        out = self.ok("record", "perf", "--run", rid, stdin=json.dumps(spec_verdict("perf")))
        self.assertTrue(out.startswith("perf: PASS (advisory)"), out)
        self.assertEqual(out.splitlines()[-1], f"next: {VL} judge --run {rid}")
        out = self.ok("judge", "--run", rid)
        self.assertTrue(out.startswith("RESULT: FAIL"), out)
        jev = [e for e in self.events() if e["event"] == "judge"][-1]["data"]
        self.assertEqual(jev["blocking"], {"reviewer": 0, "security": 1, "perf": 0, "qa": 0})

        self.edit(self.root, "README.md", "# docs only\n")
        out = self.ok("begin", "--rerun", "--run", rid, "--fix-notes", "-", stdin="SC1: n/a", cwd=self.root)
        p2 = self.plan(rid, 2)
        self.assertEqual((p2["roles"]["security"]["run"], p2["roles"]["security"]["mode"]), (True, "recheck"))
        self.assertEqual((p2["roles"]["perf"]["run"], p2["roles"]["perf"]["carried_from"]), (False, 1))
        self.assertEqual((p2["roles"]["reviewer"]["run"], p2["roles"]["reviewer"]["carried_from"]), (False, 1))
        self.assertIn("  perf (advisory): carried from #1 (PASS)", out)
        att2 = self.run_dir(rid) / "attempt-2"
        self.assertTrue((att2 / "security.prompt.md").exists())
        self.assertFalse((att2 / "perf.prompt.md").exists())
        self.assertIn("SC1", (att2 / "security.prompt.md").read_text())

    def test_begin_flags_force_specialists(self):
        self.triage(security=False)
        rid, _ = self.begin(self.root, "--security", "on", "--perf", "off")
        self.assertEqual(self.ctx(rid)["flags"]["security"], "on")
        self.assertEqual(self.ctx(rid)["flags"]["perf"], "off")
        out = self.brief(rid)
        p = self.plan(rid)
        self.assertTrue(p["roles"]["security"]["run"])
        self.assertEqual(p["roles"]["security"]["source"], "user")
        self.assertNotIn("perf", p["roles"])
        self.assertIn("security on: --security on (user flag) (triage: off)", p["notes"])
        self.assertIn("  security: full · sonnet/high — --security on (user flag)", out)

    def test_repo_config_forces_and_rerun_turns_on(self):
        self.write_config(specialists={"perf": "on"})
        self.triage()
        rid, _ = self.begin(self.root)
        self.brief(rid)
        p1 = self.plan(rid)
        self.assertTrue(p1["roles"]["perf"]["run"])
        self.assertNotIn("security", p1["roles"])
        att = self.run_dir(rid) / "attempt-1"
        (att / "reviewer.json").write_text(json.dumps(h.reviewer_fail()))
        (att / "perf.json").write_text(json.dumps(spec_verdict("perf")))
        self.ok("judge", "--run", rid)
        self.triage(security=True)
        self.edit(self.root, "src/session.js", "export const s = 1;\n")
        self.ok("begin", "--rerun", "--run", rid, "--fix-notes", "-", stdin="R1: x", cwd=self.root)
        p2 = self.plan(rid, 2)
        self.assertEqual((p2["roles"]["security"]["run"], p2["roles"]["security"]["mode"]), (True, "full"))
        self.assertEqual((p2["roles"]["perf"]["run"], p2["roles"]["perf"]["mode"]), (True, "delta"))
        self.assertEqual(p2["roles"]["perf"]["model"], "sonnet")
        self.assertEqual(p2["roles"]["perf"]["effort"], "low")

    def test_rerun_never_turns_specialists_off(self):
        self.triage(security=True)
        rid, _ = self.begin(self.root)
        self.brief(rid)
        att = self.run_dir(rid) / "attempt-1"
        (att / "reviewer.json").write_text(json.dumps(h.reviewer_fail()))
        (att / "security.json").write_text(json.dumps(spec_verdict("security")))
        self.ok("judge", "--run", rid)
        self.triage(security=False)
        self.edit(self.root, "src/helper.js", "export const helper = () => 1;\n")
        self.ok("begin", "--rerun", "--run", rid, "--fix-notes", "-", stdin="R1: x", cwd=self.root)
        p2 = self.plan(rid, 2)
        self.assertEqual(p2["source"], "triage")
        self.assertIn("security", p2["roles"])
        self.assertEqual(p2["specialists"]["security"]["source"], "earlier")

    def test_old_context_keeps_two_roles(self):
        self.triage(security=True)
        rid, _ = self.begin(self.root)
        ctxp = self.run_dir(rid) / "context.json"
        c = json.loads(ctxp.read_text())
        c["roles"] = ["reviewer", "qa"]
        ctxp.write_text(json.dumps(c))
        out = self.brief(rid)
        p = self.plan(rid)
        self.assertEqual(sorted(p["roles"]), ["qa", "reviewer"])
        self.assertNotIn("specialists", p)
        self.assertNotIn("verify-security", out)
        att = self.run_dir(rid) / "attempt-1"
        (att / "reviewer.json").write_text(json.dumps(h.reviewer_fail()))
        self.ok("judge", "--run", rid)
        self.edit(self.root, "src/session.js", "export const s = 1;\n")
        self.ok("begin", "--rerun", "--run", rid, "--fix-notes", "-", stdin="R1: x", cwd=self.root)
        self.assertEqual(sorted(self.plan(rid, 2)["roles"]), ["qa", "reviewer"])

    def test_old_plan_rerun_all_off(self):
        self.triage(security=True)
        rid, _ = self.begin(self.root)
        self.brief(rid)
        att = self.run_dir(rid) / "attempt-1"
        p = self.plan(rid)
        for s in roles.SPECIALISTS:
            p["roles"].pop(s, None)
        p.pop("specialists", None)
        (att / "plan.json").write_text(json.dumps(p))
        (att / "reviewer.json").write_text(json.dumps(h.reviewer_fail()))
        self.ok("judge", "--run", rid)
        self.triage(security=False)
        self.edit(self.root, "README.md", "# r2\n")
        self.ok("begin", "--rerun", "--run", rid, "--fix-notes", "-", stdin="R1: x", cwd=self.root)
        self.assertNotIn("security", self.plan(rid, 2)["roles"])

    def test_standalone_roles(self):
        self.triage(security=True)
        rc, _, err = self.vl("begin", "--standalone", "--roles", "nope", cwd=self.root)
        self.assertEqual(rc, 2)
        self.assertIn("reviewer,security,perf,ux,qa", err)
        out = self.ok("begin", "--standalone", "--roles", "security", "--target", "worktree", cwd=self.root)
        rid = self.run_id_of(out)
        self.assertTrue(out.startswith(f"verify-review · run {rid}"), out)
        self.assertEqual(self.ctx(rid)["roles"], ["security"])
        self.assertEqual(sorted(self.plan(rid)["roles"]), ["security"])
        self.assertIn('subagent_type="verify-security"', out)


class SpecialistMetricsTest(unittest.TestCase):
    def hist(self, **kw):
        base = {"attempt": 1, "result": "pass", "blocking_by_role": {}, "findings_by_role": {},
                "reviewer_blocking_categories": {}, "categories": {}, "unverified": []}
        base.update(kw)
        return base

    def test_triage_eval_notes(self):
        change = {"qa_scope": "none", "review_tier": "standard", "specialists_ran": ["security", "ux"],
                  "ux_forced_clear_spec": True}
        te = metrics.triage_eval(change, [self.hist(findings_by_role={"ux": 2})], {"attempts": 1})
        self.assertIn("security specialist ran and found nothing", te["mismatches"])
        self.assertNotIn("ux specialist ran and found nothing", te["mismatches"])
        self.assertIn("UX specialist forced on despite a clear UI spec", te["mismatches"])
        self.assertEqual(te["specialists_found"], {"security": False, "ux": True})
        te = metrics.triage_eval({"specialists_ran": []}, [self.hist(
            blocking_by_role={"reviewer": 2}, reviewer_blocking_categories={"security": 1, "performance": 1})],
            {"attempts": 1})
        self.assertIn("reviewer found a security blocker while the security specialist was off", te["mismatches"])
        self.assertIn("reviewer found a performance blocker while the perf specialist was off", te["mismatches"])
        te = metrics.triage_eval({}, [self.hist()], {"attempts": 1})
        self.assertEqual(te["specialists_ran"], [])

    def test_ledger_row_and_summary(self):
        by = {r: 0 for r in metrics.ROLES}
        by.update(security=500, reviewer=1000)
        cost = {r: 0.0 for r in metrics.ROLES}
        cost.update(security=0.25)
        m = {"run_id": "r", "session_id": "s", "project": "p", "platform": "claude",
             "tokens": {"by_role": by, "cost_by_role": cost, "main": {"total": 10, "cost_usd": 0.1},
                        "cost_usd": 0.35},
             "window": {"wall_min": 1.0}, "phases": [],
             "change": {"specialists_ran": ["security"]}, "outcome": {"status": "passed", "attempts": 1,
                                                                      "failed_attempts": 0},
             "triage_eval": {"specialists_found": {"security": True}}}
        row = metrics.ledger_row(m, {"kind": "loop"}, "light")
        self.assertEqual(row["specialists_ran"], ["security"])
        self.assertEqual((row["tokens"]["security"], row["tokens"]["reviewer"], row["tokens"]["ux"]), (500, 1000, 0))
        self.assertEqual(row["cost_usd"]["security"], 0.25)
        self.assertTrue(row["found"]["security"])
        s = metrics.ledger_summary([row, {**row, "specialists_ran": ["security", "perf"]}, {"project": "p"}],
                                   project="p")
        self.assertEqual(s["specialists"], {"security": 2, "perf": 1})
        cal = metrics.triage_calibration("p", rows=[row, {**row, "found": {"security": False}}])
        self.assertIn("- specialists (any finding / runs): security 1/2", cal)
        self.assertEqual(metrics._role_of("agent-verify-loop:verify-security"), "security")


if __name__ == "__main__":
    unittest.main()
