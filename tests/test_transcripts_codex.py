"""WP4: Codex rollout transcripts: locate, per-request usage, normalized timeline, recorded subagents."""
from __future__ import annotations

import json
import os
import shutil
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from helpers import HermeticCase  # noqa: E402

from vlooplib import metrics  # noqa: E402
from vlooplib.transcripts import claude as tcl  # noqa: E402
from vlooplib.transcripts import codex as tc  # noqa: E402
from vlooplib.transcripts import module_for  # noqa: E402

FIX = HERE / "fixtures" / "codex" / "sessions"
MAIN = "cccccccc-0000-4000-8000-000000000001"
SUB = "cccccccc-0000-4000-8000-0000000000a1"
DAY = "2026/10/20"


class CodexCase(HermeticCase):
    def setUp(self):
        super().setUp()
        self.sessions = self.home / ".codex" / "sessions"
        shutil.copytree(FIX, self.sessions)
        self.main = next((self.sessions / DAY).glob(f"rollout-*{MAIN}.jsonl"))
        self.sub = next((self.sessions / DAY).glob(f"rollout-*{SUB}.jsonl"))

    def write(self, name: str, rows: list[dict]) -> Path:
        p = self.sessions / "2026" / "10" / "21" / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("".join(json.dumps(r) + "\n" for r in rows))
        return p


class TestLocate(CodexCase):
    def test_finds_rollout_by_session_id(self):
        self.assertEqual(tc.transcript_for(MAIN), self.main)
        self.assertEqual(tc.transcript_for(SUB), self.sub)

    def test_unknown_and_empty(self):
        self.assertIsNone(tc.transcript_for("nope"))
        self.assertIsNone(tc.transcript_for(None))
        self.assertIsNone(tc.transcript_for(""))

    def test_newest_wins_for_duplicates(self):
        newer = self.write(f"rollout-2026-10-21T09-00-00-{MAIN}.jsonl", [])
        os.utime(self.main, (1, 1))
        self.assertEqual(tc.transcript_for(MAIN), newer)

    def test_session_meta_fallback_for_odd_names(self):
        odd = self.write("rollout-odd-name.jsonl", [{"timestamp": "2026-10-21T00:00:00Z", "type": "session_meta",
                                                       "payload": {"id": "dddddddd-1"}}])
        self.assertEqual(tc.transcript_for("dddddddd-1"), odd)

    def test_sessions_dir_follows_codex_home_and_override(self):
        self.assertEqual(tc.sessions_root(), self.sessions)
        os.environ["VERIFY_CODEX_SESSIONS_DIR"] = str(self.tmp / "elsewhere")
        self.assertIsNone(tc.transcript_for(MAIN))

    def test_platform_dispatch(self):
        self.assertIs(module_for("codex"), tc)
        self.assertIs(module_for("claude"), tcl)
        self.assertIs(module_for(None), tcl)


class TestUsage(CodexCase):
    def test_repeated_totals_and_empty_info_skipped(self):
        es = tc.usage_entries(self.main)
        self.assertEqual([e["ts"] for e in es],
                         ["2026-10-20T10:00:08.000Z", "2026-10-20T10:00:22.000Z", "2026-10-20T10:05:12.000Z"])

    def test_last_token_usage_mapping(self):
        es = tc.usage_entries(self.main)
        self.assertEqual(es[0]["n"], {"input": 800, "cache_write": 0, "cache_write_5m": 0, "cache_write_1h": 0,
                                      "cache_read": 200, "output": 100, "thinking": 30})
        self.assertEqual((es[1]["n"]["input"], es[1]["n"]["cache_read"], es[1]["n"]["output"]), (1000, 1000, 250))
        self.assertEqual((es[2]["n"]["input"], es[2]["n"]["cache_read"], es[2]["n"]["output"]), (1200, 1800, 250))

    def test_model_comes_from_turn_context_and_switches(self):
        self.assertEqual([e["model"] for e in tc.usage_entries(self.main)],
                         ["gpt-5-codex", "gpt-5-codex", "gpt-5.1-codex-max"])
        self.assertEqual({e["model"] for e in tc.usage_entries(self.sub)}, {"gpt-5-codex"})

    def test_unpriced_models_have_no_cost(self):
        self.assertTrue(all(e["cost"] is None for e in tc.usage_entries(self.main)))

    def test_window(self):
        es = tc.usage_entries(self.main, "2026-10-20T10:00:10.000Z", "2026-10-20T10:05:00.000Z")
        self.assertEqual([e["ts"] for e in es], ["2026-10-20T10:00:22.000Z"])

    def test_delta_of_totals_when_last_usage_missing(self):
        def tok(ts, total):
            return {"timestamp": ts, "type": "event_msg",
                    "payload": {"type": "token_count", "info": {"total_token_usage": total}}}

        def u(i, c, o):
            return {"input_tokens": i, "cached_input_tokens": c, "output_tokens": o, "reasoning_output_tokens": 0}

        p = self.write("rollout-x-delta.jsonl", [
            {"timestamp": "2026-10-21T00:00:00Z", "type": "turn_context", "payload": {"model": "m"}},
            tok("2026-10-21T00:00:01Z", u(100, 40, 10)), tok("2026-10-21T00:00:02Z", u(300, 140, 40)),
            tok("2026-10-21T00:00:03Z", u(50, 0, 5))])
        es = tc.usage_entries(p)
        self.assertEqual([(e["n"]["input"], e["n"]["cache_read"], e["n"]["output"]) for e in es],
                         [(60, 40, 10), (100, 100, 30), (50, 0, 5)])

    def test_missing_model_is_unknown_and_broken_lines_ignored(self):
        p = self.sessions / "2026" / "10" / "21" / "rollout-x-nomodel.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("not json\n" + json.dumps({"timestamp": "2026-10-21T00:00:01Z", "type": "event_msg", "payload": {
            "type": "token_count", "info": {"total_token_usage": {"input_tokens": 5, "output_tokens": 1},
                                             "last_token_usage": {"input_tokens": 5, "output_tokens": 1}}}}) + "\n")
        es = tc.usage_entries(p)
        self.assertEqual([(e["model"], e["n"]["input"]) for e in es], [("unknown", 5)])
        self.assertEqual(tc.usage_entries(self.tmp / "missing.jsonl"), [])

    def test_priced_model_gets_usd(self):
        rates = {"input": 1.0, "cache_read": 0.1, "output": 10.0}
        metrics._pricing_cache = {"per_tokens": 1000000, "models": {"gpt-5-codex": rates}, "aliases": {}}
        try:
            es = tc.usage_entries(self.main)
        finally:
            metrics._pricing_cache = None
        self.assertAlmostEqual(es[0]["cost"], (800 * 1.0 + 200 * 0.1 + 100 * 10.0) / 1e6)
        self.assertIsNone(es[2]["cost"])

    def test_platform_parse_usage_matches(self):
        from vlooplib import platforms

        self.assertEqual(platforms.get("codex").parse_usage(self.main), tc.usage_entries(self.main))
        self.assertEqual(platforms.get("codex").transcript_for(MAIN), self.main)


class TestTimeline(CodexCase):
    def conv(self):
        return metrics.parse_conversation(tc.timeline(self.main))

    def test_prompts_are_user_messages_only(self):
        self.assertEqual([p["text"] for p in self.conv()["prompts"]],
                         ["<prompt 1: implement the change>", "<prompt 2: fix the failing test>"])

    def test_tool_calls_and_errors(self):
        tools = self.conv()["tools"]
        self.assertEqual([t["name"] for t in tools], ["Bash", "Bash", "apply_patch", "spawn_agent"])
        self.assertEqual(tools[0]["input"], {"command": "rg -n pattern src"})
        self.assertEqual((tools[0]["is_error"], tools[1]["is_error"]), (False, True))
        self.assertIn("*** Begin Patch", tools[2]["input"]["command"])

    def test_texts_interrupts_compactions_efforts(self):
        c = self.conv()
        self.assertEqual([t["text"] for t in c["texts"]], ["<assistant text 1>", "<assistant text 2>"])
        self.assertEqual(c["interrupts"], 1)
        self.assertEqual(c["compactions"], 1)
        self.assertEqual(set(c["efforts"]), {"medium", "high"})

    def test_turn_durations_feed_active_minutes(self):
        turns = metrics._turn_durations(tc.timeline(self.main))
        self.assertEqual(turns, [("2026-10-20T10:00:30.000Z", 29000.0)])

    def test_window_and_missing_file(self):
        tl = tc.timeline(self.main, "2026-10-20T10:05:00.000Z", None)
        self.assertTrue(all(r["timestamp"] >= "2026-10-20T10:05:00.000Z" for r in tl))
        self.assertEqual(tc.timeline(self.tmp / "missing.jsonl"), [])

    def test_vault_search_detected_through_spawn_agent(self):
        sig = metrics.compute_signals(self.conv(), [], None, None)
        self.assertTrue(sig["exploration_before_vault"]["vault_search"])
        self.assertEqual(sig["tool_calls"]["Bash"], 2)


class TestSubagents(CodexCase):
    def test_from_events_and_state_deduplicated(self):
        events = [{"event": "subagent_stop", "data": {"agent_id": "a1", "agent_type": "verify-reviewer",
                                                       "transcript": str(self.sub)}},
                  {"event": "subagent_stop", "data": {"agent_transcript_path": str(self.sub)}},
                  {"event": "subagent_stop", "data": {"transcript": str(self.tmp / "gone.jsonl")}},
                  {"event": "prompt", "data": {}}]
        st = {"agents": {"a1": {"agent_type": "verify-reviewer", "transcript_path": str(self.sub)},
                         "a2": {"agent_type": "x", "transcript_path": None}}}
        subs = tc.subagents(self.main, MAIN, events, None, st)
        self.assertEqual(len(subs), 1)
        self.assertEqual((subs[0]["path"], subs[0]["agent_type"], subs[0]["first_ts"]),
                         (self.sub, "verify-reviewer", "2026-10-20T10:02:00.000Z"))

    def test_start_filter_and_state_only(self):
        st = {"agents": {"a1": {"agent_type": "vault-keeper", "transcript_path": str(self.sub)}}}
        self.assertEqual(len(tc.subagents(self.main, MAIN, [], None, st)), 1)
        self.assertEqual(tc.subagents(self.main, MAIN, [], "2026-10-20T11:00:00.000Z", st), [])
        self.assertEqual(tc.subagents(self.main, MAIN, [], None, None), [])


if __name__ == "__main__":
    unittest.main()
