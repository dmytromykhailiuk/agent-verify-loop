"""WP4: metrics across platforms: Codex sessions, USD null for unpriced models, roles by agent suffix, retro_next."""
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

CODEX_FIX = HERE / "fixtures" / "codex" / "sessions"
MAIN = "cccccccc-0000-4000-8000-000000000001"
SUB = "cccccccc-0000-4000-8000-0000000000a1"
RUN = "20261020-100000-toy-cd34"
NOW = "2026-10-20T11:00:00.000Z"
START = "2026-10-20T10:00:00.000Z"
MAIN_TOTAL = (800 + 100) + (1000 + 250) + (1200 + 250)
SUB_TOTAL = (400 + 100) + (300 + 200)


class CodexRunCase(HermeticCase):
    def setUp(self):
        super().setUp()
        self.state = self.tmp / "state"
        self.sessions = self.home / ".codex" / "sessions"
        shutil.copytree(CODEX_FIX, self.sessions)
        self.sub = next(self.sessions.rglob(f"rollout-*{SUB}.jsonl"))
        (self.state / "sessions").mkdir(parents=True)
        self.set_session(platform="codex")
        self.default_events()
        rd = self.state / "runs" / RUN
        rd.mkdir(parents=True)
        (rd / "context.json").write_text(json.dumps({
            "schema": 1, "run_id": RUN, "kind": "loop", "session_id": MAIN, "created_at": "2026-10-20T10:20:00.000Z",
            "repo": {"key": "k", "root": str(self.tmp / "repo"), "slug": "toy"}, "target": {"spec": "session"},
            "transport": "agent", "status": "passed", "failed_attempts": 0,
            "attempts": [{"n": 1, "begin_at": "2026-10-20T10:00:30.000Z", "result": "pass", "agents": {}}]}))
        os.environ["VERIFY_STATE_DIR"] = str(self.state)
        os.environ["VERIFY_CONFIG"] = str(self.tmp / "config.json")

    def set_session(self, **extra):
        (self.state / "sessions" / f"{MAIN}.json").write_text(json.dumps({"session_id": MAIN, **extra}))

    def events(self, rows):
        lines = []
        for t, name, data in rows:
            lines.append(json.dumps({"ts": "2026-10-20T" + t + "Z", "sid": MAIN, "event": name, "repo": "k",
                                     "run": RUN if name != "prompt" else None, "attempt": 1, "data": data}))
        (self.state / "sessions" / f"{MAIN}.events.jsonl").write_text("\n".join(lines) + "\n")

    def default_events(self, stop=True):
        rows = [("10:00:00.000", "prompt", {"task": 1}), ("10:00:30.000", "begin", {})]
        if stop:
            rows.append(("10:04:30.000", "subagent_stop", {"agent_id": "a1", "agent_type": "verify-reviewer",
                                                           "transcript": str(self.sub)}))
        rows += [("10:05:30.000", "judge", {}), ("10:06:00.000", "close", {})]
        self.events(rows)

    def cli(self, *argv):
        p = argparse.ArgumentParser(prog="vloop")
        metrics.register(p.add_subparsers(dest="cmd"))
        args = p.parse_args(list(argv))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = args.fn(args)
        return rc, out.getvalue()


class TestCodexMetrics(CodexRunCase):
    def test_platform_from_session_state(self):
        m = metrics.compute_metrics(RUN, now=NOW)
        self.assertEqual(m["platform"], "codex")
        self.assertEqual(m["tokens"]["main"]["transcript"], str(next(self.sessions.rglob(f"rollout-*{MAIN}.jsonl"))))

    def test_main_tokens_per_model(self):
        main = metrics.compute_metrics(RUN, now=NOW)["tokens"]["main"]
        self.assertEqual(main["total"], MAIN_TOTAL)
        by = main["by_model"]
        self.assertEqual(set(by), {"gpt-5-codex", "gpt-5.1-codex-max"})
        self.assertEqual((by["gpt-5-codex"]["requests"], by["gpt-5.1-codex-max"]["requests"]), (2, 1))
        self.assertEqual(by["gpt-5-codex"]["cache_read"], 1200)
        self.assertEqual(by["gpt-5-codex"]["thinking"], 100)
        self.assertEqual(by["gpt-5.1-codex-max"]["max_context"], 1200 + 1800)

    def test_subagent_from_hook_event_is_a_reviewer(self):
        m = metrics.compute_metrics(RUN, now=NOW)
        ag = [a for a in m["tokens"]["agents"] if a["role"] == "reviewer"]
        self.assertEqual(len(ag), 1)
        self.assertEqual((ag[0]["total"], ag[0]["model"], ag[0]["transport"]), (SUB_TOTAL, "gpt-5-codex", "agent"))
        self.assertEqual((ag[0]["cost_source"], ag[0]["cost_basis"]), ("own", "unpriced"))
        self.assertEqual(m["tokens"]["by_role"]["reviewer"], SUB_TOTAL)
        self.assertEqual(m["tokens"]["total"], MAIN_TOTAL + SUB_TOTAL)

    def test_subagent_from_session_state_only(self):
        self.default_events(stop=False)
        self.set_session(platform="codex", agents={"a1": {"agent_type": "agent-verify-loop:verify-qa",
                                                           "transcript_path": str(self.sub)}})
        m = metrics.compute_metrics(RUN, now=NOW)
        self.assertEqual([a["role"] for a in m["tokens"]["agents"]], ["qa"])

    def test_usd_is_null_not_zero(self):
        m = metrics.compute_metrics(RUN, now=NOW)
        self.assertIsNone(m["tokens"]["cost_usd"])
        self.assertIsNone(m["tokens"]["main"]["cost_usd"])
        self.assertIsNone(m["tokens"]["cost_by_role"]["reviewer"])
        self.assertEqual(m["tokens"]["cost_by_role"]["triage"], 0.0)
        self.assertEqual(m["tokens"]["main"]["cost_basis"], "unpriced")
        for b in m["tokens"]["main"]["by_model"].values():
            self.assertIsNone(b["cost_usd"])

    def test_priced_models_give_usd(self):
        rates = {"input": 1.0, "cache_read": 0.5, "output": 10.0}
        metrics._pricing_cache = {"per_tokens": 1000000, "models": {"gpt-5-codex": rates,
                                                                      "gpt-5.1-codex-max": rates}, "aliases": {}}
        try:
            m = metrics.compute_metrics(RUN, now=NOW)
        finally:
            metrics._pricing_cache = None
        expected_main = ((800 + 1000 + 1200) * 1.0 + (200 + 1000 + 1800) * 0.5 + (100 + 250 + 250) * 10.0) / 1e6
        self.assertAlmostEqual(m["tokens"]["main"]["cost_usd"], expected_main, places=6)
        self.assertIsNotNone(m["tokens"]["cost_usd"])

    def test_mixed_pricing_makes_total_unknown(self):
        rates = {"input": 1.0, "cache_read": 0.5, "output": 10.0}
        metrics._pricing_cache = {"per_tokens": 1000000, "models": {"gpt-5-codex": rates}, "aliases": {}}
        try:
            m = metrics.compute_metrics(RUN, now=NOW)
        finally:
            metrics._pricing_cache = None
        self.assertIsNone(m["tokens"]["main"]["cost_usd"])
        self.assertIsNotNone(m["tokens"]["main"]["by_model"]["gpt-5-codex"]["cost_usd"])

    def test_active_minutes_and_signals_from_rollout(self):
        m = metrics.compute_metrics(RUN, now=NOW)
        self.assertEqual(m["window"]["active_min"], 0.5)
        self.assertEqual(m["signals"]["tool_calls"], {"Bash": 2, "apply_patch": 1, "spawn_agent": 1})
        self.assertEqual(m["signals"]["interrupts"], 1)
        self.assertEqual(m["signals"]["compactions"], 1)

    def test_policy_uses_tokens_and_minutes_when_usd_is_null(self):
        m = metrics.compute_metrics(RUN, now=NOW)
        policy, reason = metrics.decide_policy(m, {"retro_policy": {"full_if_main_tokens_over": 1000}})
        self.assertEqual(policy, "full")
        self.assertIn("main session", reason)
        policy, _ = metrics.decide_policy(m, {})
        self.assertEqual(policy, "light" if m["outcome"]["status"] not in ("accepted", "abandoned") else "full")

    def test_reports_print_na(self):
        m = metrics.compute_metrics(RUN, now=NOW)
        digest = metrics.render_digest(m)
        self.assertIn("| cost | n/a (main n/a) · source own |", digest)
        self.assertEqual(metrics.money(None), "n/a")
        self.assertEqual(metrics.money(0), "≈$0")
        self.assertEqual(metrics.money(0.31), "≈$0.31")

    def test_ledger_row_keeps_nulls(self):
        row = metrics.ledger_row(metrics.compute_metrics(RUN, now=NOW), {"repo": {"key": "k"}}, "light")
        self.assertIsNone(row["cost_usd"]["total"])
        self.assertIsNone(row["cost_usd"]["reviewer"])
        self.assertEqual(row["platform"], "codex")
        self.assertEqual(metrics.ledger_summary([row])["by_change_size"]["unknown"]["cost_total"], None)
        self.assertIn("n/a", metrics.format_ledger_row(row))

    def test_retro_plan_end_to_end_without_vault(self):
        rc, out = self.cli("retro-plan", "--run", RUN)
        self.assertEqual(rc, 0)
        self.assertIn("n/a", out)
        self.assertFalse((self.state / "runs" / RUN / "retro" / "task.md").exists())
        self.assertTrue((self.state / "runs" / RUN / "retro" / "metrics.json").is_file())
        m = json.loads((self.state / "runs" / RUN / "retro" / "metrics.json").read_text())
        self.assertIsNone(m["tokens"]["cost_usd"])

    def test_platform_env_override_without_state(self):
        self.set_session()
        os.environ["VLOOP_PLATFORM"] = "codex"
        self.assertEqual(metrics.compute_metrics(RUN, now=NOW)["platform"], "codex")
        del os.environ["VLOOP_PLATFORM"]
        self.assertEqual(metrics.compute_metrics(RUN, now=NOW)["platform"], "claude")


class TestClaudeUnpricedAndRoles(HermeticCase):
    def test_unpriced_claude_model_has_no_usd(self):
        self.assertIsNone(metrics.request_cost("claude-brand-new-9", {"input_tokens": 1000}))
        self.assertIsNotNone(metrics.request_cost("claude-opus-5-5", {"input_tokens": 1000}))
        self.assertEqual(metrics.resolve_model("claude-opus-5-5-20261101"), "claude-opus-5-5")
        self.assertEqual(metrics.resolve_model("sonnet"), "claude-sonnet-5-5")

    def test_roles_by_agent_suffix(self):
        for kind, role in (("verify-reviewer", "reviewer"), ("agent-verify-loop:verify-reviewer", "reviewer"),
                           ("agent-verify-loop:verify-qa", "qa"), ("verify-qa", "qa"), ("vault-keeper", "retro"),
                           ("agent-vault:vault-keeper", "retro"), ("general-purpose", "other_subagents"),
                           (None, "other_subagents")):
            self.assertEqual(metrics._role_of(kind), role, kind)

    def test_vault_search_detection(self):
        search = {"subagent_type": "vault-keeper", "prompt": "Operation: search\nwhere is X"}
        self.assertTrue(metrics._is_vault_search("Agent", search))
        self.assertTrue(metrics._is_vault_search("Task", {**search, "subagent_type": "agent-vault:vault-keeper"}))
        self.assertFalse(metrics._is_vault_search("Agent", {**search, "subagent_type": "general-purpose"}))
        self.assertFalse(metrics._is_vault_search("Agent", {**search, "prompt": "Operation: retro"}))
        self.assertTrue(metrics._is_vault_search("Bash", {"command": "herdr-agent run vault-search --prompt x"}))
        self.assertFalse(metrics._is_vault_search("Bash", {"command": "ls"}))


class TestRetroNext(CodexRunCase):
    def install(self, **kw):
        fv = fakevault.install(self.tmp / "av", **kw)
        os.environ["AGENT_VAULT_CLI"] = str(fv.cli)
        return fv

    def task(self):
        return self.state / "runs" / RUN / "retro" / "task.md"

    def test_no_vault_is_light_only_and_has_no_proposals_line(self):
        self.assertEqual(metrics.retro_next(RUN, "agent", "claude"), "retro: light only (no agent-vault)")
        self.assertEqual(metrics.retro_next(RUN, "herdr", "codex"), "retro: light only (no agent-vault)")

    def test_agent_claude(self):
        self.install()
        self.assertEqual(metrics.retro_next(RUN, "agent", "claude"),
                         'next (Agent tool, run_in_background: true): Agent(subagent_type="vault-keeper", '
                         f'model="sonnet", description="retro", prompt="Your task file is {self.task()}. '
                         'Read it completely and follow it.")')

    def test_agent_claude_namespaced_when_vault_is_a_plugin(self):
        self.install(plugin="agent-vault")
        self.assertIn('subagent_type="agent-vault:vault-keeper"', metrics.retro_next(RUN, "agent", "claude"))

    def test_agent_codex(self):
        self.install(plugin="agent-vault")
        line = metrics.retro_next(RUN, "agent", "codex")
        self.assertTrue(line.startswith('next (Codex, in the background): spawn the custom agent "vault-keeper"'))
        self.assertIn(f"Your task file is {self.task()}. Read it completely and follow it.", line)
        self.assertNotIn("Agent(", line)

    def test_herdr_and_other_transports(self):
        self.install()
        self.assertEqual(metrics.retro_next(RUN, "herdr", "claude"),
                         f"next (Herdr, run_in_background: true): {VL} spawn retro --run {RUN}")
        self.assertEqual(metrics.retro_next(RUN, "headless", "codex"), metrics.retro_next(RUN, "agent", "codex"))
        self.assertEqual(metrics.retro_next(RUN, "headless", "claude"), metrics.retro_next(RUN, "agent", "claude"))
        self.assertEqual(metrics.retro_next(RUN, "unknown-transport", "claude"),
                         metrics.retro_next(RUN, "agent", "claude"))

    def test_model_from_config(self):
        self.install()
        Path(os.environ["VERIFY_CONFIG"]).write_text(json.dumps({"models": {"retro": ["opus", "high"]}}))
        self.assertIn('model="opus"', metrics.retro_next(RUN, "agent", "claude"))

    def test_proposals_line_only_when_pending(self):
        fv = self.install()
        self.assertEqual(len(metrics.retro_next(RUN, "agent", "claude").splitlines()), 1)
        fv.configure(proposals=[{"link": "p/l", "title": "T"}])
        lines = metrics.retro_next(RUN, "agent", "claude").splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[1].startswith("proposals: 1 pending"))
        self.assertIn(f"{fv.cli} lessons --proposals", lines[1])

    def test_retro_plan_full_with_vault_writes_task_and_prints_next(self):
        fv = self.install()
        self.default_events(stop=False)
        ctx = json.loads((self.state / "runs" / RUN / "context.json").read_text())
        ctx["failed_attempts"] = 1
        (self.state / "runs" / RUN / "context.json").write_text(json.dumps(ctx))
        rc, out = self.cli("retro-plan", "--run", RUN)
        lines = out.strip().splitlines()
        self.assertTrue(lines[0].startswith("retro: full — 1 failed attempt(s) · task: "))
        self.assertTrue(lines[1].startswith("next (Codex, in the background)"))
        task = self.task().read_text()
        self.assertIn(f"vloop: {VL}\nvault_cli: {fv.cli}\nops: {fv.ops}\n", task)
        self.assertIn("Platform: codex", task)
        self.assertNotIn("{{", task)

    def test_retro_prep_without_vault_skips_the_task_file(self):
        rc, out = self.cli("retro-prep", "--run", RUN)
        self.assertEqual(rc, 0)
        self.assertTrue(out.startswith("retro: light only (no agent-vault)\nmetrics: "))
        self.assertTrue((self.state / "runs" / RUN / "retro" / "digest.md").is_file())
        self.assertFalse(self.task().exists())


if __name__ == "__main__":
    unittest.main()
