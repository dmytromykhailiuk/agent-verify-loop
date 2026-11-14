"""agent-cost integration: overlay of USD, tokens and time, fallbacks, retro attribution, ledger fields."""
from __future__ import annotations

import json
import os
import sys
import unittest
from unittest import mock
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fakeagentcost  # noqa: E402
from fakeagentcost import time_, tokens  # noqa: E402
from test_metrics import H1, NOW, R, S, Base  # noqa: E402

from vlooplib import metrics, paths  # noqa: E402
from vlooplib.integrations import agentcost  # noqa: E402

CLOSED = "2026-10-13T10:45:00.500Z"
MAIN = {"session_id": "x", "usd": 1.25, "tokens": tokens(100, 200, 300, 4000),
        "models": {"claude-opus-5-5": {"usd": 1.25, "tokens": tokens(100, 200, 300, 4000)}},
        "time": time_(2_700_000, 600_000, 1_200_000)}
AGENTS = [
    {"session_id": H1, "herdr_id": "verify-review-111", "kind": "herdr", "purpose": "verify:review", "run_id": R,
     "model": "claude-sonnet-5-5", "usd": 0.5, "tokens": tokens(1, 2, 3, 9), "time": time_(400_000, 300_000)},
    {"session_id": "qa-sid", "herdr_id": "verify-qa-222", "kind": "herdr", "purpose": "verify:qa", "run_id": R,
     "model": None, "usd": 0.25, "tokens": tokens(10, 20, 30), "time": time_(480_000, 120_000)},
    {"session_id": "agent-r2", "herdr_id": None, "kind": "builtin", "purpose": "verify:review", "run_id": R,
     "model": None, "usd": 0.125, "tokens": tokens(5, 5, 5), "time": time_(180_000, 170_000)},
    {"session_id": "triage-synth", "herdr_id": None, "kind": "synthetic", "purpose": "verify:triage", "run_id": R,
     "model": "claude-haiku-5-5", "usd": 9.0, "tokens": tokens(), "time": time_(1000, 1000)},
]


class AgentCostBase(Base):
    def setUp(self):
        super().setUp()
        agentcost.reset_cache()
        os.environ["PATH"] = "/usr/bin:/bin"
        self.fc = fakeagentcost.install(self.tmp / "agent-cost", main=MAIN, agents=AGENTS)
        os.environ["AGENT_COST_CLI"] = str(self.fc.cli)

    def tearDown(self):
        agentcost.reset_cache()
        super().tearDown()

    def compute(self):
        agentcost.reset_cache()
        return metrics.compute_metrics(R, now=NOW)

    def set_config(self, **cfg):
        (self.tmp / "config.json").write_text(json.dumps(cfg))

    def add_keeper(self, name, prompt, ts="2026-10-13T10:50:00.000Z"):
        d = self.projects / "-repo-toy" / "aaaaaaaa-0000-4000-8000-000000000001" / "subagents"
        rec = {"parentUuid": None, "isSidechain": True, "type": "user", "timestamp": ts,
               "sessionId": "aaaaaaaa-0000-4000-8000-000000000001", "message": {"role": "user", "content": prompt}}
        asst = {"parentUuid": None, "isSidechain": True, "type": "assistant", "timestamp": ts, "requestId": f"req-{name}",
                "sessionId": "aaaaaaaa-0000-4000-8000-000000000001",
                "message": {"id": f"m-{name}", "model": "claude-sonnet-5-5", "role": "assistant",
                            "content": [{"type": "text", "text": "ok"}],
                            "usage": {"input_tokens": 7, "cache_creation_input_tokens": 0,
                                      "cache_read_input_tokens": 0, "output_tokens": 3}}}
        (d / f"{name}.jsonl").write_text(json.dumps(rec) + "\n" + json.dumps(asst) + "\n")
        (d / f"{name}.meta.json").write_text(json.dumps({"agentType": "agent-vault:vault-keeper"}))

    def agent(self, m, role, attempt=None):
        return [a for a in m["tokens"]["agents"] if a["role"] == role and (attempt is None or a["attempt"] == attempt)]


class TestOverlay(AgentCostBase):
    def test_main_and_agents_take_agent_cost_numbers(self):
        m = self.compute()
        self.assertEqual(m["cost_provider"]["source"], "agent-cost")
        self.assertEqual((m["cost_provider"]["version"], m["cost_provider"]["contract"]), ("0.3.0", 1))
        main = m["tokens"]["main"]
        self.assertEqual((main["total"], main["cost_usd"], main["cost_source"]), (600, 1.25, "agent-cost"))
        self.assertEqual(main["time"], {"wall_ms": 2_700_000, "active_ms": 600_000, "waiting_ms": 1_200_000,
                                        "idle_ms": 900_000})
        self.assertEqual(main["by_model"]["claude-opus-5-5"]["cache_write"], 300)
        self.assertEqual(main["own"]["total"], 14 * 1110)
        w = m["window"]
        self.assertEqual((w["wall_min"], w["active_min"], w["waiting_min"], w["idle_min"], w["active_source"]),
                         (45.0, 10.0, 20.0, 15.0, "agent-cost"))
        r1 = self.agent(m, "reviewer", 1)[0]
        self.assertEqual((r1["total"], r1["cost_usd"], r1["cost_source"], r1["model"], r1["duration_s"]),
                         (6, 0.5, "agent-cost", "claude-sonnet-5-5", 400.0))
        self.assertEqual(r1["time"], {"wall_ms": 400_000, "active_ms": 300_000, "waiting_ms": 0, "idle_ms": 100_000})
        qa = self.agent(m, "qa")[0]
        self.assertEqual((qa["total"], qa["cost_usd"], qa["cost_source"]), (60, 0.25, "agent-cost"))
        r2 = self.agent(m, "reviewer", 2)[0]
        self.assertEqual((r2["total"], r2["cost_usd"], r2["time"]["active_ms"]), (15, 0.125, 170_000))
        self.assertEqual(m["tokens"]["by_role"]["reviewer"], 21)
        self.assertEqual(metrics.policy_minutes(m["window"]), (10.0, "active"))
        call = [c for c in self.fc.calls() if c[0] == "export"][-1]
        self.assertEqual(call, ["export", "--session", "aaaaaaaa-0000-4000-8000-000000000001",
                                "--since", "2026-10-13T10:00:00.000Z", "--until", CLOSED, "--json"])

    def test_triage_keeps_claude_p_numbers(self):
        m = self.compute()
        tri = self.agent(m, "triage")
        self.assertEqual([(a["total"], a["cost_usd"], a["cost_source"]) for a in tri], [(9432, 0.004, "claude-p")])
        self.assertEqual(tri[0]["time"]["idle_ms"], 0)

    def test_agent_missing_from_export_keeps_own_numbers(self):
        m = self.compute()
        other = self.agent(m, "other_subagents")
        self.assertEqual([(a["total"], a["cost_source"]) for a in other], [(100, "own")])

    def test_digest_and_task_show_times_and_source(self):
        m = self.compute()
        d = metrics.render_digest(m)
        self.assertIn("source agent-cost", d)
        self.assertIn("waiting 20 min · idle 15 min", d)
        self.assertIn("| reviewer | 1 | herdr | claude-sonnet-5-5 |", d)
        self.assertIn("wall 6.7 · active 5 · waiting 0 · idle 1.7 min | agent-cost |", d)
        self.assertIn("main session time | wall 45 · active 10 · waiting 20 · idle 15 min", d)
        self.assertIn("Phase minutes and main tokens are vloop's own accounting", d)


class TestWaiting(AgentCostBase):
    def test_missing_waiting_counts_as_zero(self):
        self.fc.configure(main={**MAIN, "time": {"wall_ms": 2_700_000, "active_ms": 600_000, "idle_ms": 2_100_000}})
        m = self.compute()
        self.assertEqual(m["tokens"]["main"]["time"]["waiting_ms"], 0)
        self.assertEqual((m["window"]["waiting_min"], m["window"]["idle_min"]), (0.0, 35.0))

    def test_window_comes_from_agent_cost_not_vloop_wall(self):
        self.fc.configure(main={**MAIN, "time": time_(1_800_000, 600_000, 300_000)})
        w = self.compute()["window"]
        self.assertEqual((w["wall_min"], w["active_min"], w["waiting_min"], w["idle_min"]), (30.0, 10.0, 5.0, 15.0))

    def test_open_run_exports_until_now(self):
        ev = self.state / "sessions" / f"{S}.events.jsonl"
        ev.write_text("".join(x + "\n" for x in ev.read_text().splitlines() if '"event":"close"' not in x))
        self.compute()
        call = [c for c in self.fc.calls() if c[0] == "export"][-1]
        self.assertEqual(call[call.index("--until") + 1], NOW)

    def test_policy_keeps_active_time(self):
        self.fc.configure(main={**MAIN, "time": time_(6_000_000, 600_000, 5_000_000)})
        m = self.compute()
        self.assertEqual(metrics.policy_minutes(m["window"]), (10.0, "active"))


class TestBadExport(AgentCostBase):
    def test_non_numeric_tokens_fall_back(self):
        self.fc.configure(agents=[{**AGENTS[0], "tokens": {**tokens(1), "output": "lots"}}])
        m = self.compute()
        self.assertEqual(m["cost_provider"]["source"], "own")
        self.assertIn("contract 1", m["cost_provider"]["reason"])
        self.assertEqual(m["tokens"]["main"]["total"], 14 * 1110)

    def test_non_numeric_time_falls_back(self):
        self.fc.configure(main={**MAIN, "time": {"wall_ms": "1h", "active_ms": 1}})
        self.assertEqual(self.compute()["cost_provider"]["source"], "own")


class TestRepoConfig(AgentCostBase):
    def test_repo_config_can_set_cost_source(self):
        repo = self.tmp / "repo"
        repo.mkdir()
        (repo / paths.REPO_CONFIG).write_text(json.dumps({"cost_source": "own"}))
        ctx = self.ctx()
        ctx["repo"]["root"] = str(repo)
        self.write_ctx(ctx)
        m = self.compute()
        self.assertEqual((m["cost_provider"]["source"], m["cost_provider"]["reason"]), ("own", "cost_source own"))


class TestFallback(AgentCostBase):
    def assert_own(self, m, reason_part):
        self.assertEqual(m["cost_provider"]["source"], "own")
        self.assertIn(reason_part, m["cost_provider"]["reason"])
        self.assertIsNone(m["cost_provider"]["warning"])
        self.assertEqual(m["tokens"]["main"]["total"], 14 * 1110)
        self.assertEqual(m["tokens"]["main"]["cost_source"], "own")
        self.assertEqual(m["window"]["active_source"], "turn_duration")
        r1 = self.agent(m, "reviewer", 1)[0]
        self.assertEqual((r1["total"], r1["cost_source"], r1["cost_basis"]), (2 * 2505, "own", "cost-state"))
        self.assertEqual(r1["time"], {"wall_ms": 450_000, "active_ms": None, "waiting_ms": None, "idle_ms": None})
        self.assertEqual((m["window"]["waiting_min"], m["window"]["idle_min"]), (0.0, 42.0))
        self.assertEqual(m["tokens"]["main"]["time"]["waiting_ms"], 0)
        self.assertNotIn("Phase minutes and main tokens", metrics.render_digest(m))

    def test_no_cli(self):
        del os.environ["AGENT_COST_CLI"]
        self.assert_own(self.compute(), "not found")

    def test_timeout(self):
        self.fc.configure(export_mode="hang")
        old = agentcost.EXPORT_TIMEOUT_S
        agentcost.EXPORT_TIMEOUT_S = 0.5
        try:
            self.assert_own(self.compute(), "exited 124")
        finally:
            agentcost.EXPORT_TIMEOUT_S = old

    def test_bad_json(self):
        self.fc.configure(export_mode="bad")
        self.assert_own(self.compute(), "not JSON")

    def test_failing_export(self):
        self.fc.configure(export_mode="fail")
        self.assert_own(self.compute(), "exited 1")

    def test_contract_0(self):
        self.fc.configure(contract=0)
        self.assert_own(self.compute(), "contract 0")

    def test_export_with_wrong_contract(self):
        self.fc.configure(export_contract=2)
        self.assert_own(self.compute(), "contract 1")

    def test_not_agent_cost(self):
        self.fc.configure(name="other")
        self.assert_own(self.compute(), "not agent-cost")

    def test_cost_source_own_skips_agent_cost(self):
        self.set_config(cost_source="own")
        m = self.compute()
        self.assert_own(m, "cost_source own")
        self.assertEqual([c for c in self.fc.calls() if c[0] == "export"], [])

    def test_cost_source_agent_cost_warns_in_retro_plan(self):
        self.set_config(cost_source="agent-cost")
        self.fc.configure(export_mode="fail")
        agentcost.reset_cache()
        rc, out, _ = self.cli("retro-plan", "--run", R)
        self.assertEqual(rc, 0)
        self.assertIn("warning: cost_source agent-cost, but agent-cost is unavailable (agent-cost export exited 1); "
                      "own numbers used", out)

    def test_auto_does_not_warn(self):
        self.fc.configure(export_mode="fail")
        agentcost.reset_cache()
        rc, out, _ = self.cli("retro-plan", "--run", R)
        self.assertNotIn("warning:", out)


class TestRetroAttribution(AgentCostBase):
    def test_own_fallback_needs_this_runs_task_path(self):
        del os.environ["AGENT_COST_CLI"]
        self.add_keeper("agent-v1", "Operation: search. something unrelated")
        self.add_keeper("agent-v2", "Your task file is /state/runs/other-run/retro/task.md")
        self.add_keeper("agent-v3", f"Your task file is /state/runs/{R}/retro/task.md. Read it.")
        m = self.compute()
        self.assertEqual(m["tokens"]["by_role"]["retro"], 10)
        self.assertEqual(m["tokens"]["by_role"]["other_subagents"], 100 + 20)

    def test_agent_cost_purpose_and_run_id_decide(self):
        self.add_keeper("agent-v1", f"Your task file is /state/runs/{R}/retro/task.md")
        self.add_keeper("agent-v2", "vault search")
        self.add_keeper("agent-v3", "vault update")
        rows = AGENTS + [
            {"session_id": "agent-v1", "kind": "builtin", "purpose": "verify:retro", "run_id": "older-run",
             "usd": 0.01, "tokens": tokens(1), "time": time_(10, 10)},
            {"session_id": "agent-v2", "kind": "builtin", "purpose": "verify:retro", "run_id": R,
             "usd": 0.02, "tokens": tokens(2), "time": time_(10, 10)},
            {"session_id": "agent-v3", "kind": "builtin", "purpose": "vault", "run_id": None,
             "usd": 0.04, "tokens": tokens(4), "time": time_(10, 10)},
        ]
        self.fc.configure(agents=rows)
        m = self.compute()
        retro = self.agent(m, "retro")
        self.assertEqual([(a["total"], a["purpose"]) for a in retro], [(2, "verify:retro")])
        self.assertEqual(m["tokens"]["cost_by_role"]["retro"], 0.02)
        self.assertEqual(m["tokens"]["by_role"]["other_subagents"], 100 + 1 + 4)


class TestRetroKnownFromState(AgentCostBase):
    def test_transport_retro_keeps_role_whatever_agent_cost_says(self):
        ev = self.state / "sessions" / f"{S}.events.jsonl"
        done = {"ts": "2026-10-13T10:50:00.000Z", "sid": S, "event": "retro_done", "repo": "abc123abc123", "run": R,
                "attempt": None, "data": {"transport": "herdr", "herdr_id": "verify-retro-9", "session_id": "retro-sid",
                                          "started_at": "2026-10-13T10:46:00.000Z", "status": "done"}}
        lines = ev.read_text().splitlines()
        ev.write_text("\n".join(lines[:-1] + [json.dumps(done), lines[-1]]) + "\n")
        rows = AGENTS + [{"session_id": "retro-sid", "herdr_id": "verify-retro-9", "kind": "herdr", "purpose": "vault",
                          "run_id": None, "usd": 0.3, "tokens": tokens(7), "time": time_(10, 10)}]
        self.fc.configure(agents=rows)
        m = self.compute()
        retro = self.agent(m, "retro")
        self.assertEqual([(a["total"], a["cost_usd"], a["cost_source"]) for a in retro], [(7, 0.3, "agent-cost")])
        self.assertEqual(m["tokens"]["cost_by_role"]["retro"], 0.3)


RETRO_ROW = {"session_id": "retro-sid", "herdr_id": "verify-retro-9", "kind": "herdr", "purpose": "verify:retro",
             "run_id": R, "usd": 0.3, "tokens": tokens(7), "time": time_(120_000, 90_000)}


class TestRetroAfterClose(AgentCostBase):
    def add_retro_done(self):
        ev = self.state / "sessions" / f"{S}.events.jsonl"
        done = {"ts": "2026-10-13T10:50:00.000Z", "sid": S, "event": "retro_done", "repo": "abc123abc123", "run": R,
                "attempt": None, "data": {"transport": "herdr", "herdr_id": "verify-retro-9", "session_id": "retro-sid",
                                          "started_at": "2026-10-13T10:46:00.000Z", "status": "done"}}
        with open(ev, "a") as f:
            f.write(json.dumps(done) + "\n")

    def exports(self):
        return [c for c in self.fc.calls() if c[0] == "export"]

    def test_retro_after_close_takes_agent_cost_numbers(self):
        self.add_retro_done()
        self.fc.configure(late_agents=[RETRO_ROW])
        m = self.compute()
        retro = self.agent(m, "retro")
        self.assertEqual([(a["total"], a["cost_usd"], a["cost_source"], a["time"]["active_ms"]) for a in retro],
                         [(7, 0.3, "agent-cost", 90_000)])
        self.assertEqual(m["tokens"]["cost_by_role"]["retro"], 0.3)
        self.assertEqual(m["cost_provider"]["after_close"], {"agents": 1, "status": "ok"})
        calls = self.exports()
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][calls[0].index("--until") + 1], CLOSED)
        self.assertNotIn("--until", calls[1])

    def test_main_window_still_ends_at_close(self):
        self.add_retro_done()
        self.fc.configure(late_agents=[RETRO_ROW])
        m = self.compute()
        self.assertEqual(m["tokens"]["main"]["cost_usd"], 1.25)
        self.assertEqual(m["window"]["wall_min"], 45.0)
        self.assertEqual(self.agent(m, "reviewer", 1)[0]["cost_source"], "agent-cost")

    def test_no_second_export_without_late_agents(self):
        self.compute()
        self.assertEqual(len(self.exports()), 1)

    def test_retro_unknown_to_agent_cost_keeps_own_numbers(self):
        self.add_retro_done()
        m = self.compute()
        self.assertEqual([a["cost_source"] for a in self.agent(m, "retro")], ["own"])

    def test_failing_late_export_falls_back_per_agent(self):
        self.add_retro_done()
        self.fc.configure(late_agents=[RETRO_ROW], late_mode="fail")
        m = self.compute()
        self.assertEqual([a["cost_source"] for a in self.agent(m, "retro")], ["own"])
        self.assertEqual(m["cost_provider"]["source"], "agent-cost")
        self.assertEqual(m["cost_provider"]["after_close"]["status"], "agent-cost export exited 1")

    def test_ledger_row_refreshed_after_retro_done(self):
        self.fc.configure(late_agents=[RETRO_ROW])
        rc, _, _ = self.cli("retro-plan", "--run", R)
        self.assertEqual(rc, 0)
        before = json.loads((self.state / "ledger.jsonl").read_text().splitlines()[-1])
        self.assertEqual(before["cost_usd"]["retro"], 0)
        self.add_retro_done()
        agentcost.reset_cache()
        self.assertTrue(metrics.refresh_ledger(R))
        rows = [json.loads(x) for x in (self.state / "ledger.jsonl").read_text().splitlines()]
        self.assertEqual(len([r for r in rows if r["run_id"] == R]), 1)
        row = rows[-1]
        self.assertEqual((row["cost_usd"]["retro"], row["tokens"]["retro"]), (0.3, 7))
        self.assertEqual((row["ts"], row["retro"]), (before["ts"], before["retro"]))
        self.assertEqual(row["cost_usd"]["total"], round(before["cost_usd"]["total"] + 0.3, 4))
        rc, out, _ = self.cli("ledger", "--summary", "--json")
        self.assertEqual(json.loads(out)["rows"], 1)

    def _seed_ledger_row(self):
        self.fc.configure(late_agents=[RETRO_ROW])
        self.assertEqual(self.cli("retro-plan", "--run", R)[0], 0)
        self.add_retro_done()
        agentcost.reset_cache()

    def test_refresh_keeps_rows_appended_during_compute(self):
        self._seed_ledger_row()
        real = metrics.compute_metrics
        late = {"run_id": "late-run", "ts": "2026-01-01T00:00:00Z"}

        def slow(run_id):
            m = real(run_id)
            with open(self.state / "ledger.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(late) + "\n")
            return m

        with mock.patch.object(metrics, "compute_metrics", slow):
            self.assertTrue(metrics.refresh_ledger(R))
        rows = [json.loads(x) for x in (self.state / "ledger.jsonl").read_text().splitlines()]
        self.assertEqual(len([r for r in rows if r["run_id"] == "late-run"]), 1)
        self.assertEqual(len([r for r in rows if r["run_id"] == R]), 1)

    def test_refresh_preserves_malformed_and_foreign_lines(self):
        self._seed_ledger_row()
        p = self.state / "ledger.jsonl"
        p.write_text("not json\n[1, 2]\n" + p.read_text() + "{broken\n")
        self.assertTrue(metrics.refresh_ledger(R))
        lines = p.read_text().splitlines()
        self.assertEqual((lines[0], lines[1], lines[-1]), ("not json", "[1, 2]", "{broken"))
        self.assertEqual(len(lines), 4)

    def test_refresh_without_ledger_row_is_a_noop(self):
        self.assertFalse(metrics.refresh_ledger(R))
        self.assertFalse((self.state / "ledger.jsonl").exists())


class TestLedger(AgentCostBase):
    def test_ledger_row_has_retro_cost_source_and_times(self):
        self.add_keeper("agent-v3", f"Your task file is /state/runs/{R}/retro/task.md")
        rows = AGENTS + [{"session_id": "agent-v3", "kind": "builtin", "purpose": "verify:retro", "run_id": R,
                          "usd": 0.5, "tokens": tokens(2), "time": time_(10, 10)}]
        self.fc.configure(agents=rows)
        rc, _, _ = self.cli("retro-plan", "--run", R)
        self.assertEqual(rc, 0)
        row = json.loads((self.state / "ledger.jsonl").read_text().splitlines()[-1])
        self.assertEqual(row["cost_usd"]["retro"], 0.5)
        self.assertEqual(row["cost_source"], "agent-cost")
        mins = row["minutes"]
        self.assertEqual((mins["wall"], mins["active"], mins["waiting"], mins["idle"]), (45.0, 10.0, 20.0, 15.0))
        rc, out, _ = self.cli("ledger", "--summary", "--project", "toy")
        self.assertIn("wall 45.0 · active 10.0 · waiting 20.0 · idle 15.0 min", out)
        self.assertIn("cost_source: agent-cost 1", out)
        rc, out, _ = self.cli("ledger", "--summary", "--json")
        b = json.loads(out)["by_change_size"]["medium"]
        self.assertEqual((b["minutes_active"], b["minutes_waiting"], b["minutes_idle"]), (10.0, 20.0, 15.0))
        rc, out, _ = self.cli("ledger", "--project", "toy")
        self.assertIn("active 10.0  waiting 20.0  idle 15.0", out)

    def test_own_ledger_row(self):
        self.set_config(cost_source="own")
        self.cli("retro-plan", "--run", R)
        row = json.loads((self.state / "ledger.jsonl").read_text().splitlines()[-1])
        self.assertEqual(row["cost_source"], "own")
        self.assertIn("retro", row["cost_usd"])


if __name__ == "__main__":
    unittest.main()
