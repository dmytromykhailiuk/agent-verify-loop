"""WP4 tests: transcript accounting, phases, signals, retro policy, ledger, retro-prep, gate report, digest, task."""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import shutil
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fakevault  # noqa: E402
from helpers import VL, HermeticCase  # noqa: E402

from vlooplib import metrics  # noqa: E402

FIX = HERE / "fixtures" / "wp4"
S = "aaaaaaaa-0000-4000-8000-000000000001"
H1 = "bbbbbbbb-0000-4000-8000-000000000001"
H2 = "bbbbbbbb-0000-4000-8000-000000000002"
R = "20261013-102000-toy-ab12"
NOW = "2026-10-13T11:00:00.000Z"
REQ = 1110                       # input + cache_write + output of one fixture main-session request
REQ_COST = (10 * 4 + 1000 * 8 + 5000 * 0.2 + 100 * 20) / 1e6


class Base(HermeticCase):
    def setUp(self):
        super().setUp()
        self.state = self.tmp / "state"
        self.projects = self.tmp / "projects"
        shutil.copytree(FIX / "state", self.state)
        shutil.copytree(FIX / "projects", self.projects)
        sp = self.state / "sessions" / f"{S}.json"
        sp.write_text(sp.read_text().replace("__TRANSCRIPTS__", str(self.projects)))
        os.environ["VERIFY_STATE_DIR"] = str(self.state)
        os.environ["VERIFY_CONFIG"] = str(self.tmp / "config.json")
        os.environ["VERIFY_TRANSCRIPTS_DIR"] = str(self.projects)
        self.vault = fakevault.install(self.tmp / "agent-vault")
        os.environ["AGENT_VAULT_CLI"] = str(self.vault.cli)

    def main_path(self):
        return self.projects / "-repo-toy" / f"{S}.jsonl"

    def cli(self, *argv):
        p = argparse.ArgumentParser(prog="vloop")
        metrics.register(p.add_subparsers(dest="cmd"))
        args = p.parse_args(list(argv))
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = args.fn(args)
        return rc, out.getvalue(), err.getvalue()

    def ctx(self):
        return json.loads((self.state / "runs" / R / "context.json").read_text())

    def write_ctx(self, ctx):
        (self.state / "runs" / R / "context.json").write_text(json.dumps(ctx))


class TestTranscripts(Base):
    def test_no_double_counting_and_synthetic_and_sidechain(self):
        es = metrics.usage_entries(self.main_path(), "2026-10-13T10:00:00.000Z", NOW, main_only=True)
        agg = metrics.aggregate(es)
        self.assertEqual(list(agg["by_model"]), ["claude-opus-5-5"])        # <synthetic> ignored
        b = agg["by_model"]["claude-opus-5-5"]
        self.assertEqual(b["requests"], 14)                                   # m1 split entries counted once
        self.assertEqual(agg["total"], 14 * REQ)                              # sidechain 888888 excluded
        self.assertEqual(b["cache_read"], 14 * 5000)
        self.assertEqual(b["thinking"], 14 * 40)
        self.assertEqual(b["max_context"], 10 + 1000 + 5000)
        self.assertAlmostEqual(agg["cost_usd"], 14 * REQ_COST, places=6)

    def test_sidechain_included_when_not_main_only(self):
        es = metrics.usage_entries(self.main_path(), "2026-10-13T10:00:00.000Z", NOW)
        self.assertEqual(len(es), 15)

    def test_window_excludes_previous_task(self):
        self.assertEqual(len(metrics.usage_entries(self.main_path(), None, None, main_only=True)), 15)

    def test_pricing_haiku_long_context_and_aliases(self):
        small = {"input_tokens": 1000, "output_tokens": 1000}
        self.assertAlmostEqual(metrics.request_cost("claude-haiku-5-5", small), (1000 * 0.1 + 1000 * 0.5) / 1e6)
        big = {"input_tokens": 50000, "cache_read_input_tokens": 60000, "output_tokens": 1000}
        self.assertAlmostEqual(metrics.request_cost("claude-haiku-5-5", big),
                               (50000 * 0.5 + 60000 * 0.05 + 1000 * 2.5) / 1e6)
        # opus has no long_context block: same rates above 100k
        self.assertAlmostEqual(metrics.request_cost("claude-opus-5-5", big), (50000 * 4 + 60000 * 0.2 + 1000 * 20) / 1e6)
        self.assertEqual(metrics.resolve_model("claude-sonnet-5-9"), "claude-sonnet-5-5")
        self.assertEqual(metrics.resolve_model("haiku"), "claude-haiku-5-5")
        self.assertIsNone(metrics.resolve_model("something-else"))
        self.assertIsNone(metrics.resolve_model("gpt-5-codex"))
        self.assertIsNone(metrics.resolve_model(None))

    def test_cache_write_without_breakdown_uses_5m_rate(self):
        u = {"cache_creation_input_tokens": 1000}
        self.assertAlmostEqual(metrics.request_cost("claude-sonnet-5-5", u), 1000 * 2.5 / 1e6)

    def test_money_format(self):
        self.assertEqual(metrics.money(0.0042), "≈$0.0042")
        self.assertEqual(metrics.money(0.004), "≈$0.004")
        self.assertEqual(metrics.money(0.31), "≈$0.31")
        self.assertEqual(metrics.money(2.1), "≈$2.1")


class TestMetrics(Base):
    def setUp(self):
        super().setUp()
        self.m = metrics.compute_metrics(R, now=NOW)

    def agents(self, role):
        return [a for a in self.m["tokens"]["agents"] if a["role"] == role]

    def test_main_tokens_window(self):
        self.assertEqual(self.m["window"]["start"], "2026-10-13T10:00:00.000Z")
        self.assertEqual(self.m["tokens"]["main"]["total"], 14 * REQ)
        self.assertEqual(self.m["window"]["active_min"], 3.0)
        self.assertEqual(self.m["window"]["wall_min"], 45.0)                  # task start → close
        self.assertIn("trimmed_min", self.m["window"])

    def test_herdr_by_id_with_cost_state(self):
        r1 = [a for a in self.agents("reviewer") if a["attempt"] == 1][0]
        self.assertEqual(r1["session_id"], H1)
        self.assertEqual(r1["total"], 2 * 2505)
        self.assertEqual(r1["cost_usd"], 0.42)
        self.assertEqual(r1["cost_source"], "cost-state")
        self.assertEqual(r1["duration_s"], 450.0)

    def test_herdr_by_agent_name(self):
        qa = self.agents("qa")
        self.assertEqual(len(qa), 1)
        self.assertEqual(qa[0]["total"], 3703)
        self.assertEqual(qa[0]["cost_source"], "estimate")
        self.assertAlmostEqual(qa[0]["cost_usd"], (3 * 2 + 3000 * 2.5 + 700 * 10) / 1e6)

    def test_subagent_role_attribution(self):
        r2 = [a for a in self.agents("reviewer") if a["attempt"] == 2]
        self.assertEqual(len(r2), 1)
        self.assertEqual(r2[0]["transport"], "agent")
        self.assertEqual(r2[0]["total"], 1101)
        other = self.agents("other_subagents")
        self.assertEqual([a["total"] for a in other], [100])                  # agent-old (before window) excluded
        tri = self.agents("triage")
        self.assertEqual([(a["attempt"], a["total"], a["cost_usd"]) for a in tri], [(1, 9432, 0.004)])
        self.assertEqual(self.m["tokens"]["by_role"],
                         {"triage": 9432, "reviewer": 6111, "qa": 3703, "retro": 0, "other_subagents": 100})

    def test_phases(self):
        ph = {p["name"]: p for p in self.m["phases"]}
        self.assertEqual(list(ph), ["implement", "verify#1", "fix#1", "verify#2", "wrap-up"])
        self.assertEqual(ph["implement"]["main_tokens"], 10 * REQ)
        self.assertEqual(ph["implement"]["active_min"], 2.0)
        self.assertEqual(ph["implement"]["agent_tokens"], 100)
        self.assertEqual(ph["verify#1"]["main_tokens"], REQ)
        self.assertEqual(ph["verify#1"]["agent_tokens"], 9432 + 5010 + 3703)
        self.assertEqual(ph["fix#1"]["main_tokens"], REQ)
        self.assertEqual(ph["fix#1"]["active_min"], 1.0)
        self.assertEqual(ph["fix#1"]["wall_min"], 10.0)
        self.assertEqual(ph["verify#2"]["agent_tokens"], 1101)
        self.assertEqual(ph["wrap-up"]["main_tokens"], REQ)

    def test_signals(self):
        s = self.m["signals"]
        self.assertEqual(s["tool_calls"], {"Bash": 5, "Read": 3, "Grep": 1, "Glob": 1})
        self.assertEqual(s["rereads"], [{"file": "/repo/src/a.ts", "count": 3}])
        self.assertEqual(s["repeated_failures"], [{"command": "npm test", "count": 2}])
        self.assertEqual([(x["tool"], x["chars"]) for x in s["large_outputs"]], [("Bash", 25000)])
        self.assertEqual(s["exploration_before_vault"]["count"], 5)
        self.assertTrue(s["exploration_before_vault"]["vault_search"])
        self.assertEqual((s["user_prompts"], s["corrections"], s["interrupts"]), (2, 1, 1))
        self.assertEqual(s["permission_denials"], 1)
        self.assertEqual(s["compactions"], 1)
        self.assertEqual(s["efforts"], {"medium": 13, "high": 1})
        self.assertEqual(s["gate_events"], {"gate_warn": 1})

    def test_outcome_findings_triage_eval(self):
        oc = self.m["outcome"]
        self.assertEqual((oc["status"], oc["attempts"], oc["failed_attempts"], oc["escalated"], oc["inconclusive"]),
                         ("passed", 2, 1, False, 0))
        self.assertEqual(oc["browser_consent"]["decision"], "allow")
        h = self.m["findings_history"]
        self.assertEqual(h[0]["blocking"], ["R1"])
        self.assertEqual(h[0]["downgraded"][0]["id"], "R4")
        self.assertEqual(h[0]["disputed"], ["R4"])
        self.assertEqual(h[0]["unverified"], [{"check_id": "C4", "need": "auth_expired"}])
        self.assertEqual(h[1]["late"], ["R7"])
        te = self.m["triage_eval"]
        self.assertTrue(te["reviewer_found_blocking"])
        self.assertFalse(te["qa_found_blocking"])
        self.assertIn("QA (run) found no blocking issue", te["mismatches"])
        ch = self.m["change"]
        self.assertEqual((ch["files"], ch["change_size"], ch["qa_scope"], ch["review_tier"], ch["prefilter"]),
                         (3, "medium", "run", "standard", "triage"))

    def test_session_transcript_found_by_glob(self):
        sp = self.state / "sessions" / f"{S}.json"
        st = json.loads(sp.read_text())
        st["transcript_path"] = None
        sp.write_text(json.dumps(st))
        m = metrics.compute_metrics(R, now=NOW)
        self.assertEqual(m["tokens"]["main"]["total"], 14 * REQ)


def base_metrics(**over):
    m = {"outcome": {"status": "passed", "attempts": 1, "failed_attempts": 0, "escalated": False, "inconclusive": 0},
         "change": {"change_size": "small", "prefilter": "triage"},
         "tokens": {"main": {"total": 1000}}, "window": {"wall_min": 10.0}}
    for k, v in over.items():
        m[k].update(v)
    return m


class TestPolicy(HermeticCase):
    CFG = {"retro_policy": {"full_if_main_tokens_over": 400000, "full_if_minutes_over": 45}}

    def test_light(self):
        self.assertEqual(metrics.decide_policy(base_metrics(), self.CFG), ("light", "small change, first-pass PASS"))
        p, r = metrics.decide_policy(base_metrics(change={"prefilter": "trivial"}), self.CFG)
        self.assertEqual((p, r), ("light", "trivial change, first-pass PASS"))

    def test_full_rules(self):
        cases = [
            (dict(outcome={"failed_attempts": 1}), "1 failed attempt(s)"),
            (dict(outcome={"escalated": True}), "loop escalated"),
            (dict(outcome={"inconclusive": 1}), "1 inconclusive attempt(s)"),
            (dict(outcome={"status": "accepted"}), "outcome accepted"),
            (dict(outcome={"status": "abandoned"}), "outcome abandoned"),
            (dict(change={"change_size": "medium"}), "medium change"),
            (dict(change={"change_size": "large"}), "large change"),
            (dict(tokens={"main": {"total": 500000}}), "main session 500k tokens > 400k"),
            (dict(window={"wall_min": 50.0}), "wall 50 min > 45 min"),
            (dict(window={"wall_min": 60.0, "active_min": 50.0}), "active 50 min > 45 min"),
        ]
        for over, reason in cases:
            with self.subTest(reason=reason):
                self.assertEqual(metrics.decide_policy(base_metrics(**over), self.CFG), ("full", reason))


class TestPolicyMinutes(HermeticCase):
    CFG = TestPolicy.CFG

    def test_idle_heavy_window_is_light(self):
        # verify-toy-2331: a one-line change, 51.6 wall min of which 3.1 min active
        m = base_metrics(window={"wall_min": 51.6, "active_min": 3.1, "trimmed_min": 51.6})
        self.assertEqual(metrics.decide_policy(m, self.CFG), ("light", "small change, first-pass PASS"))

    def test_long_active_is_full(self):
        m = base_metrics(window={"wall_min": 70.0, "active_min": 46.5, "trimmed_min": 60.0})
        self.assertEqual(metrics.decide_policy(m, self.CFG), ("full", "active 46.5 min > 45 min"))

    def test_no_turn_duration_uses_gap_trimmed_wall(self):
        def ev(hm):
            return {"type": "user", "timestamp": f"2026-10-13T{hm}:00.000Z"}
        # 10:00..10:20 busy, 50 min idle, 11:10..11:40 busy: wall 100, trimmed 50
        tl = [ev(x) for x in ("10:00", "10:10", "10:20", "11:10", "11:20", "11:30", "11:40")]
        trimmed = metrics._gap_trimmed_minutes(tl, "2026-10-13T10:00:00.000Z", "2026-10-13T11:40:00.000Z")
        self.assertEqual(trimmed, 50.0)
        m = base_metrics(window={"wall_min": 100.0, "active_min": 0.0, "trimmed_min": trimmed})
        self.assertEqual(metrics.decide_policy(m, self.CFG), ("full", "gap-trimmed 50 min > 45 min"))
        tl = [ev(x) for x in ("10:00", "10:10", "11:10", "11:20")]   # one 60-min gap → 20 min
        trimmed = metrics._gap_trimmed_minutes(tl, "2026-10-13T10:00:00.000Z", "2026-10-13T11:20:00.000Z")
        self.assertEqual(trimmed, 20.0)
        m = base_metrics(window={"wall_min": 80.0, "active_min": 0.0, "trimmed_min": trimmed})
        self.assertEqual(metrics.decide_policy(m, self.CFG)[0], "light")


class TestRetroPlan(Base):
    def test_full_plan_ledger_once_and_artifacts(self):
        rc, out, _ = self.cli("retro-plan", "--run", R)
        self.assertEqual(rc, 0)
        rdir = self.state / "runs" / R / "retro"
        first, nxt = out.strip().splitlines()
        self.assertEqual(first, f"retro: full — 1 failed attempt(s) · task: {rdir / 'task.md'}")
        self.assertTrue(nxt.startswith('next (Agent tool, run_in_background: true): Agent(subagent_type="vault-keeper", '
                                       'model="sonnet", description="retro", prompt="Your task file is '))
        for name in ("metrics.json", "digest.md", "task.md", "plan.json"):
            self.assertTrue((rdir / name).is_file(), name)
        self.assertEqual(json.loads((rdir / "plan.json").read_text())["policy"], "full")
        m = json.loads((rdir / "metrics.json").read_text())
        self.assertNotIn("_conv", m)
        led = (self.state / "ledger.jsonl").read_text().splitlines()
        self.assertEqual(len(led), 1)
        row = json.loads(led[0])
        self.assertEqual((row["run_id"], row["project"], row["outcome"], row["attempts"], row["retro"]),
                         (R, "toy", "passed", 2, "full"))
        self.assertEqual(row["tokens"]["reviewer"], 6111)
        self.assertEqual(row["minutes"]["fix"], 10.0)
        evs = [json.loads(x) for x in (self.state / "sessions" / f"{S}.events.jsonl").read_text().splitlines()
               if x.startswith("{") and x.endswith("}")]
        rp = [e for e in evs if e["event"] == "retro_plan"]
        self.assertEqual(rp[-1]["data"], {"policy": "full", "reason": "1 failed attempt(s)"})
        # second run: ledger untouched
        os.environ["HERDR_ENV"] = "1"
        rc, out, _ = self.cli("retro-plan", "--run", R)
        self.assertEqual(rc, 0)
        self.assertIn(f"next (Herdr, run_in_background: true): {VL} spawn retro --run {R}", out)
        self.assertEqual(len((self.state / "ledger.jsonl").read_text().splitlines()), 1)

    def test_light_plan(self):
        ctx = self.ctx()
        ctx["attempts"] = ctx["attempts"][:1]
        ctx["attempts"][0]["result"] = "pass"
        ctx["failed_attempts"] = 0
        self.write_ctx(ctx)
        rd = self.state / "runs" / R
        shutil.rmtree(rd / "attempt-2")
        j = json.loads((rd / "attempt-1" / "judge.json").read_text())
        j["result"] = "pass"
        (rd / "attempt-1" / "judge.json").write_text(json.dumps(j))
        t = json.loads((rd / "attempt-1" / "triage.json").read_text())
        t["change_size"] = "small"
        (rd / "attempt-1" / "triage.json").write_text(json.dumps(t))
        ev = self.state / "sessions" / f"{S}.events.jsonl"
        ev.write_text("".join(x + "\n" for x in ev.read_text().splitlines()
                              if '"attempt":2' not in x and '"result":"fail"' not in x))
        # close right after the first judge
        ev.write_text(ev.read_text().replace("2026-10-13T10:45:00.500Z", "2026-10-13T10:31:00.000Z"))
        rc, out, _ = self.cli("retro-plan", "--run", R)
        self.assertEqual(rc, 0, out)
        self.assertRegex(out.strip(), r"^retro: light — small change, first-pass PASS · ledger updated \(≈\d+k tokens, ≈\$[0-9.]+\)$")
        self.assertFalse((rd / "retro" / "task.md").exists())
        self.assertEqual(json.loads((rd / "retro" / "plan.json").read_text())["policy"], "light")
        self.assertEqual(len((self.state / "ledger.jsonl").read_text().splitlines()), 1)

    def test_open_run_and_missing_run(self):
        ctx = self.ctx()
        ctx["status"] = "open"
        self.write_ctx(ctx)
        rc, _, err = self.cli("retro-plan", "--run", R)
        self.assertEqual(rc, 2)
        self.assertIn("vloop: error:", err)
        rc, _, err = self.cli("retro-plan", "--run", "nope")
        self.assertEqual((rc, err.strip()), (2, "vloop: error: run nope not found"))

    def test_task_md_fully_rendered(self):
        self.cli("retro-plan", "--run", R)
        task = (self.state / "runs" / R / "retro" / "task.md").read_text()
        self.assertNotIn("{{", task)
        self.assertIn("Operation: retro", task)
        self.assertIn("_harness", task)
        self.assertIn(str(self.state / "runs" / R / "attempt-1" / "judge.json"), task)
        self.assertIn(f"{VL} ledger --project toy --summary", task)
        self.assertIn(f"Follow {self.vault.ops}\n(after reading reference/schema.md).", task)
        self.assertIn(f"vloop: {VL}\nvault_cli: {self.vault.cli}\nops: {self.vault.ops}\n", task)

    def test_digest_content(self):
        self.cli("retro-plan", "--run", R)
        d = (self.state / "runs" / R / "retro" / "digest.md").read_text()
        self.assertLessEqual(len(d), metrics.DIGEST_BUDGET)
        self.assertIn("| browser QA consent | granted (allow, user) — note: staging only |", d)
        self.assertIn("repeated failing command ×2: `npm test`", d)
        self.assertIn("Read ✓ /repo/src/a.ts (out 0.0k chars)", d)
        self.assertIn("Bash ✗ npm test", d)
        self.assertIn("## Verify history", d)


class TestDigestBudget(Base):
    def test_truncation_marker(self):
        m = metrics.compute_metrics(R, now=NOW)
        m["_conv"]["tools"] = [{"ts": "2026-10-13T10:00:00.000Z", "id": f"t{i}", "name": "Bash",
                                "input": {"command": f"echo {i} " + "y" * 90}, "is_error": False, "chars": 10,
                                "text": ""} for i in range(3000)]
        m["_conv"]["texts"] = [{"ts": "2026-10-13T10:00:00.000Z", "text": "z" * 400} for _ in range(300)]
        d = metrics.render_digest(m)
        self.assertLessEqual(len(d), metrics.DIGEST_BUDGET)
        self.assertRegex(d, r"\[… \d+ tool calls omitted …\]")
        self.assertIn("echo 0 ", d)
        self.assertIn("echo 2999 ", d)


class TestRetroPrep(Base):
    def test_session_creates_retro_run(self):
        rc, out, err = self.cli("retro-prep", "--session", S)
        self.assertEqual(rc, 0, err)
        runs = [p for p in (self.state / "runs").iterdir() if p.name.startswith("retro-")]
        self.assertEqual(len(runs), 1)
        ctx = json.loads((runs[0] / "context.json").read_text())
        self.assertEqual((ctx["kind"], ctx["session_id"]), ("retro", S))
        self.assertIsNone(ctx["repo"])                                         # cwd is not a repo
        self.assertTrue(out.startswith(f"retro task: {runs[0] / 'retro' / 'task.md'}\nnext (Agent tool"))
        task = (runs[0] / "retro" / "task.md").read_text()
        self.assertNotIn("{{", task)
        self.assertIn("Project slug: _harness", task)
        m = json.loads((runs[0] / "retro" / "metrics.json").read_text())
        self.assertEqual(m["window"]["start"], "2026-10-13T09:00:00.000Z")     # whole session
        self.assertEqual(m["tokens"]["main"]["total"], 15 * REQ)
        self.assertFalse((self.state / "ledger.jsonl").exists())

    def test_run_prep_and_errors(self):
        rc, out, _ = self.cli("retro-prep", "--run", R)
        self.assertEqual(rc, 0)
        self.assertTrue((self.state / "runs" / R / "retro" / "digest.md").is_file())
        rc, _, err = self.cli("retro-prep")
        self.assertEqual(rc, 2)
        rc, _, err = self.cli("retro-prep", "--session", "unknown-session")
        self.assertEqual(rc, 2)


class TestLedgerAndGateReport(Base):
    def test_ledger_commands(self):
        self.cli("retro-plan", "--run", R)
        rc, out, _ = self.cli("ledger")
        self.assertEqual(rc, 0)
        self.assertIn(R, out)
        rc, out, _ = self.cli("ledger", "--json", "--project", "toy", "--last", "5")
        self.assertEqual(len(json.loads(out)), 1)
        rc, out, _ = self.cli("ledger", "--summary", "--project", "toy", "--json")
        s = json.loads(out)
        self.assertEqual(s["by_change_size"]["medium"]["n"], 1)
        self.assertEqual(s["by_change_size"]["medium"]["attempts"], 2)
        self.assertEqual(s["qa_scope"], {"run": 1})
        rc, out, _ = self.cli("ledger", "--summary")
        self.assertIn("medium: n=1", out)

    def test_summary_medians_last_20(self):
        rows = [{"project": "p", "change_size": "small", "tokens": {"main": i}, "cost_usd": {"total": i / 10},
                 "minutes": {"wall": i}, "attempts": 1, "qa_scope": "none"} for i in range(30)]
        s = metrics.ledger_summary(rows + [{"project": "q", "change_size": "small"}], "p")
        self.assertEqual(s["rows"], 20)
        self.assertEqual(s["by_change_size"]["small"]["tokens_main"], 19.5)

    def test_gate_report(self):
        self.cli("retro-plan", "--run", R)
        rc, out, _ = self.cli("gate-report", "--since", "2026-10-13", "--json")
        g = json.loads(out)
        self.assertEqual(g["sessions"], 2)
        self.assertEqual((g["gate_warn"], g["warn_verified"], g["warn_never_verified"]), (2, 1, 1))
        self.assertEqual((g["gate_block"], g["gate_ignored"], g["docs_skips"], g["user_skips"]), (1, 1, 1, 1))
        self.assertEqual(g["gate_defer"], {"question": 1, "defer-cmd": 1})
        self.assertEqual(g["prefilter"], {"skip": 0, "trivial": 1, "triage": 2})
        self.assertEqual(g["triage_source"], {"triage": 1, "reused": 1})
        self.assertEqual(g["avg_triage_cost_usd"], 0.004)
        rc, out, _ = self.cli("gate-report", "--since", "2026-10-13")
        self.assertEqual(out.strip().splitlines()[-1], "ready for enforce? decide yourself — switch with: vloop mode enforce")
        rc, out, _ = self.cli("gate-report", "--since", "2026-09-01", "--json")
        self.assertEqual(json.loads(out)["gate_warn"], 3)

    def test_gate_report_default_since_from_user_config(self):
        (self.tmp / "config.json").write_text(json.dumps({"mode": "warn", "installed_at": "2026-10-13"}))
        rc, out, _ = self.cli("gate-report", "--json")
        self.assertEqual(json.loads(out)["since"], "2026-10-13")
        self.assertEqual(json.loads(out)["gate_warn"], 2)


if __name__ == "__main__":
    unittest.main()
