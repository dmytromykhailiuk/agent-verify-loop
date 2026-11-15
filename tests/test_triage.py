"""WP2 triage tests: exact headless invocation, child env, parsing, fallback on exit/timeout/bad output (§2.8)."""
from __future__ import annotations

import json
import os
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures" / "wp2"))
import wp2helpers as h  # noqa: E402

from vlooplib import prompts, triage, verdict  # noqa: E402


class ReshapeTest(h.HermeticCase):
    def test_nested_output_unchanged(self):
        obj = json.loads((Path(h.FIX) / "triage-checks.json").read_text())
        self.assertEqual(triage.reshape(obj), obj)

    def test_bare_strings_and_missing_fields(self):
        o = triage.reshape({"qa": "RUN", "review": "deep", "reason": "server.js route"},
                           {"changed_lines": 500, "files": 2})
        self.assertEqual(o["qa"], {"scope": "run", "reason": "server.js route", "focus": [], "needs_login": False})
        self.assertEqual(o["review"], {"tier": "deep", "reason": "server.js route", "focus": []})
        self.assertEqual((o["change_size"], o["risk"], o["confidence"]), ("large", "medium", "low"))
        self.assertEqual(verdict.validate("triage-output", o), [])

    def test_list_reason_joined(self):
        o = triage.reshape({"qa": {"scope": "checks", "reason": ["a", "b"], "focus": "x", "needs_login": False},
                            "review": {"tier": "standard", "reason": "r", "focus": []}, "change_size": "small",
                            "risk": "low", "confidence": "high"})
        self.assertEqual((o["qa"]["reason"], o["qa"]["focus"]), ("a; b", ["x"]))

    def test_extract_json_with_triage_keys(self):
        text = 'Decision:\n{"qa_scope": "checks", "review_tier": "standard"} done'
        self.assertIsNone(verdict.extract_json(text))
        self.assertEqual(verdict.extract_json(text, keys=triage.TRIAGE_KEYS)["qa_scope"], "checks")


class TriageCallTest(h.Case):
    herdr = True

    def setUp(self):
        super().setUp()
        self.root = self.make_repo()
        self.prompt(self.root)
        self.code_change(self.root)
        self.rid, _ = self.begin(self.root)
        self.att = self.run_dir(self.rid) / "attempt-1"

    def tri(self) -> dict:
        return json.loads((self.att / "triage.json").read_text())

    def test_exact_invocation_and_env(self):
        os.environ["HERDR_PANE_ID"] = "p9"
        self.triage_output("checks")
        self.brief(self.rid)
        calls = self.claude_calls()
        self.assertEqual(len(calls), 1)
        schema = json.dumps(verdict.load_schema("triage-output"), separators=(",", ":"))
        self.assertEqual(calls[0]["argv"], [
            "-p", "--agent", "verify-triage", "--model", "sonnet", "--effort", "medium", "--output-format", "json",
            "--json-schema", schema, "--tools", "Read,Grep,Glob,StructuredOutput", "--strict-mcp-config",
            "--settings", '{"disableAllHooks":true}', "--permission-mode", "default", "--permission-prompts", "none",
            "--no-session-persistence", "--max-budget-usd", "0.6",
            "Triage the change described on stdin. Answer only with the JSON object."])
        self.assertEqual(calls[0]["env_mode"], "off")
        self.assertIsNone(calls[0]["herdr_env"])
        self.assertIsNone(calls[0]["session_env"])
        self.assertEqual(calls[0]["stdin_chars"], len((self.att / "triage.input.md").read_text()))
        t = self.tri()
        self.assertEqual((t["source"], t["role"], t["run_id"], t["attempt"]), ("triage", "triage", self.rid, 1))
        self.assertEqual(t["agent"], {"model": "claude-haiku-5-5", "duration_s": 6.1, "cost_usd": 0.0042,
                                      "tokens": {"input": 2, "cache_write": 9120, "cache_read": 0, "output": 310},
                                      "error": None})
        self.assertEqual(verdict.validate("triage", t), [])

    def test_child_env_strips_herdr_and_session(self):
        os.environ.update({"HERDR_TAB_ID": "t", "CLAUDE_PID": "1", "HERDR_AGENT_DEPTH": "0"})
        env = triage.child_env()
        from vlooplib import transports

        for k in transports.strip_env():
            self.assertNotIn(k, env)
        self.assertEqual(env["VERIFY_LOOP_MODE"], "off")
        self.assertIn("VERIFY_STATE_DIR", env)

    def test_text_result_is_extracted(self):
        self.triage_output("deep-run")
        os.environ["FAKE_CLAUDE_TEXT"] = "1"
        self.brief(self.rid)
        t = self.tri()
        self.assertEqual((t["source"], t["qa"]["scope"], t["review"]["tier"]), ("triage", "run", "deep"))

    def test_exit_1_falls_back(self):  # (5)
        os.environ["FAKE_CLAUDE_EXIT"] = "1"
        out = self.brief(self.rid)
        t = self.tri()
        self.assertEqual(t["source"], "fallback")
        self.assertEqual(t["agent"]["error"], "exit 1: fake claude: simulated failure")
        self.assertEqual(out.splitlines()[0], "verify-loop · plan · attempt 1 · triage unavailable (exit 1: fake "
                                              "claude: simulated failure) → fallback")
        p = self.plan(self.rid)
        self.assertEqual(p["source"], "fallback")
        self.assertTrue(p["roles"]["qa"]["reason"].startswith("triage unavailable (exit 1: fake claude: simulated "
                                                              "failure): "))
        self.assertEqual((p["roles"]["qa"]["scope"], p["roles"]["reviewer"]["tier"]), ("checks", "standard"))
        ev = [e for e in self.events() if e["event"] == "triage"][-1]["data"]
        self.assertEqual((ev["source"], ev["error"]), ("fallback", "exit 1: fake claude: simulated failure"))

    def test_timeout_falls_back(self):  # (5)
        self.write_config(triage={"timeout_s": 1})
        os.environ["FAKE_CLAUDE_SLEEP"] = "8"
        t0 = time.monotonic()
        out = self.brief(self.rid)
        self.assertLess(time.monotonic() - t0, 6)
        t = self.tri()
        self.assertEqual((t["source"], t["agent"]["error"]), ("fallback", "timeout after 1s"))
        self.assertIn("triage unavailable (timeout after 1s) → fallback", out)
        self.assertTrue(self.plan(self.rid)["roles"]["reviewer"]["reason"].startswith(
            "triage unavailable (timeout after 1s): "))

    def test_flat_text_output_is_reshaped_not_fallback(self):
        # live haiku without StructuredOutput answered a flat object in prose (WP7 bug #1)
        flat = {"qa_scope": "browser", "needs_login": "false", "review_tier": "Standard", "change_size": "small",
                "risk": "low", "reasons": ["public/app.js adds a reset handler", "small client-side change"]}
        bad = self.tmp / "flat.json"
        bad.write_text(json.dumps(flat))
        os.environ["FAKE_CLAUDE_OUTPUT"] = str(bad)
        os.environ["FAKE_CLAUDE_TEXT"] = "1"
        self.brief(self.rid)
        t = self.tri()
        self.assertEqual(t["source"], "triage")
        self.assertEqual((t["qa"]["scope"], t["qa"]["needs_login"], t["review"]["tier"], t["confidence"]),
                         ("browser", False, "standard", "low"))
        self.assertEqual((t["qa"]["reason"], t["review"]["reason"]),
                         ("public/app.js adds a reset handler", "small client-side change"))
        self.assertEqual(verdict.validate("triage", t), [])

    def test_bad_output_falls_back(self):
        bad = self.tmp / "bad.json"
        bad.write_text(json.dumps({"change_size": "huge", "risk": "low"}))
        os.environ["FAKE_CLAUDE_OUTPUT"] = str(bad)
        self.brief(self.rid)
        t = self.tri()
        self.assertEqual(t["source"], "fallback")
        self.assertTrue(t["agent"]["error"].startswith("bad output: "))

    def test_triage_input_contents(self):
        self.triage_output("checks")
        self.brief(self.rid)
        text = (self.att / "triage.input.md").read_text()
        self.assertIn("### Prompt 1 (", text)
        self.assertIn("Make f1 add 100 instead", text)
        self.assertIn("- f1 returns x + 100", text)
        self.assertIn("- src/a.js [source]", text)
        self.assertIn("changed_lines: 30", text)
        self.assertIn('"app_kind": "unknown"', text)
        self.assertIn("Answer with the JSON object only.", text)


FAKE_CODEX = Path(__file__).resolve().parent / "fixtures" / "fake-codex.py"


class CodexTriageTest(h.Case):
    """`codex exec` triage with the fake binary: argv, stdin prompt, `-o` output, usage from `--json` events."""

    def setUp(self):
        super().setUp()
        self.root = self.make_repo()
        self.prompt(self.root)
        self.code_change(self.root)
        self.rid, _ = self.begin(self.root)
        self.att = self.run_dir(self.rid) / "attempt-1"
        self.log = self.tmp / "codex.jsonl"
        os.environ.update({"VERIFY_TRIAGE_CMD": json.dumps([sys.executable, str(FAKE_CODEX)]),
                           "VLOOP_PLATFORM": "codex", "FAKE_CODEX_LOG": str(self.log),
                           "FAKE_CODEX_OUTPUT": str(Path(h.FIX) / "triage-checks.json")})

    def tri(self) -> dict:
        return json.loads((self.att / "triage.json").read_text())

    def call(self) -> dict:
        return json.loads(self.log.read_text().splitlines()[-1])

    def test_invocation_output_and_usage(self):
        self.write_config(platforms={"codex": {"triage": {"model": "gpt-5.5-mini", "effort": "low"}}})
        out = self.brief(self.rid)
        c = self.call()
        argv = c["argv"]
        from vlooplib import paths

        self.assertEqual(argv[:4], ["exec", "-", "--output-schema", str(paths.schema_dir() / "triage.codex.schema.json")])
        self.assertEqual(argv[4], "-o")
        self.assertEqual(argv[6:], ["--json", "-s", "read-only", "--skip-git-repo-check", "--ephemeral", "-m",
                                    "gpt-5.5-mini", "-c", 'model_reasoning_effort="low"'])
        self.assertFalse(Path(argv[5]).exists(), "the -o file is removed after reading")
        self.assertIn("verify-loop triage agent", c["stdin"])
        self.assertIn("Triage the change described on stdin.", c["stdin"])
        self.assertIn("Make f1 add 100 instead", c["stdin"])
        self.assertEqual((c["env"]["VERIFY_LOOP_MODE"], c["env"]["HERDR_ENV"], c["env"]["CLAUDE_CODE_SESSION_ID"]),
                         ("off", None, None))
        t = self.tri()
        self.assertEqual((t["source"], t["qa"]["scope"]), ("triage", "checks"))
        self.assertEqual(t["agent"]["model"], "gpt-5.5-mini")
        self.assertEqual(t["agent"]["tokens"], {"input": 200, "cache_write": 0, "cache_read": 1000, "output": 90})
        self.assertIsNone(t["agent"]["cost_usd"])
        self.assertIsNone(t["agent"]["error"])
        self.assertTrue(out.splitlines()[0].startswith("verify-loop · plan · attempt 1 · triage gpt-5.5-mini ("), out)
        self.assertEqual(verdict.validate("triage", t), [])

    def test_default_model_is_omitted(self):
        self.brief(self.rid)
        argv = self.call()["argv"]
        self.assertNotIn("-m", argv)
        self.assertEqual(self.tri()["agent"]["model"], "codex")

    def test_failure_falls_back(self):
        os.environ["FAKE_CODEX_RC"] = "1"
        self.brief(self.rid)
        t = self.tri()
        self.assertEqual((t["source"], t["agent"]["error"]), ("fallback", "exit 1: fake codex: simulated failure"))

    def test_empty_output_falls_back(self):
        os.environ["FAKE_CODEX_OUTPUT"] = ""
        self.brief(self.rid)
        t = self.tri()
        self.assertEqual((t["source"], t["agent"]["error"]), ("fallback", "no output file"))

    def test_bad_output_falls_back(self):
        bad = self.tmp / "bad.json"
        bad.write_text(json.dumps({"change_size": "huge", "risk": "low"}))
        os.environ["FAKE_CODEX_OUTPUT"] = str(bad)
        self.brief(self.rid)
        t = self.tri()
        self.assertEqual(t["source"], "fallback")
        self.assertTrue(t["agent"]["error"].startswith("bad output: "))

    def test_missing_binary_falls_back(self):
        os.environ["VERIFY_TRIAGE_CMD"] = json.dumps([str(self.tmp / "no-codex")])
        self.brief(self.rid)
        t = self.tri()
        self.assertEqual(t["source"], "fallback")
        self.assertTrue(t["agent"]["error"].startswith("exit 127"))


class CodexSchemaTest(h.HermeticCase):
    def test_file_matches_generator(self):
        from vlooplib import paths

        on_disk = json.loads((paths.schema_dir() / triage.CODEX_SCHEMA_FILE).read_text())
        self.assertEqual(on_disk, triage.codex_schema())

    def test_strict_and_equivalent(self):
        strict = triage.codex_schema()

        def check(node, base):
            if isinstance(node, dict) and "properties" in node:
                self.assertIs(node["additionalProperties"], False)
                self.assertEqual(sorted(node["required"]), sorted(node["properties"]))
                self.assertEqual(sorted(node["properties"]), sorted(base["properties"]))
                for k, sub in node["properties"].items():
                    check(sub, base["properties"][k])
            elif isinstance(node, dict) and isinstance(node.get("items"), dict):
                check(node["items"], base["items"])
        check(strict, verdict.load_schema("triage-output"))
        obj = json.loads((Path(h.FIX) / "triage-checks.json").read_text())
        errors: list[str] = []
        verdict._validate(strict, obj, "", errors)
        self.assertEqual(errors, [])

    def test_optional_properties_become_nullable(self):
        s = triage.strict_schema({"type": "object", "required": ["a"], "properties": {
            "a": {"type": "string"}, "b": {"type": "string", "enum": ["x", "y"]},
            "c": {"type": "array", "items": {"type": "object", "properties": {"d": {"type": "integer"}}}}}})
        self.assertEqual(s["required"], ["a", "b", "c"])
        self.assertEqual(s["properties"]["a"]["type"], "string")
        self.assertEqual(s["properties"]["b"], {"type": ["string", "null"], "enum": ["x", "y", None]})
        self.assertEqual(s["properties"]["c"]["type"], ["array", "null"])
        item = s["properties"]["c"]["items"]
        self.assertEqual((item["required"], item["additionalProperties"], item["properties"]["d"]["type"]),
                         (["d"], False, ["integer", "null"]))


class ParseTest(h.HermeticCase):
    def test_parse_doc(self):
        ok = json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "{}"})
        self.assertIsNone(triage._parse(ok)[1])
        self.assertTrue(triage._parse(json.dumps({"subtype": "success", "is_error": True, "result": "boom"}))[1]
                        .startswith("is_error"))
        self.assertEqual(triage._parse(json.dumps({"subtype": "error_max_budget"}))[1], "subtype error_max_budget")
        self.assertEqual(triage._parse("garbage")[1], "invalid JSON output")


class ExcerptTest(h.HermeticCase):
    PATCH = ("diff --git a/src/a.spec.ts b/src/a.spec.ts\n--- a/src/a.spec.ts\n+++ b/src/a.spec.ts\n@@ -1 +1 @@\n-t\n+T\n"
             "diff --git a/package-lock.json b/package-lock.json\n--- a/package-lock.json\n+++ b/package-lock.json\n"
             "@@ -1 +1 @@\n-x\n+y\n"
             "diff --git a/logo.png b/logo.png\nBinary files a/logo.png and b/logo.png differ\n"
             "diff --git a/src/a.ts b/src/a.ts\n--- a/src/a.ts\n+++ b/src/a.ts\n@@ -1 +1 @@\n-a\n+b\n")
    SUMMARY = {"rows": [{"path": "src/a.spec.ts", "flags": ["test"]}, {"path": "package-lock.json", "flags": ["ignored"]},
                        {"path": "logo.png", "flags": ["source"], "binary": True},
                        {"path": "src/a.ts", "flags": ["source"]}]}

    def test_order_and_filters(self):
        ex = prompts.diff_excerpt(self.PATCH, self.SUMMARY)
        self.assertNotIn("package-lock", ex)
        self.assertNotIn("logo.png", ex)
        self.assertLess(ex.index("src/a.ts"), ex.index("src/a.spec.ts"))

    def test_truncation(self):
        ex = prompts.diff_excerpt(self.PATCH, self.SUMMARY, max_lines=4)
        lines = ex.splitlines()
        self.assertEqual(len(lines), 5)
        self.assertEqual(lines[-1], "[excerpt truncated: 8 more lines]")
        ex = prompts.diff_excerpt(self.PATCH, self.SUMMARY, max_chars=30)
        self.assertTrue(ex.splitlines()[-1].startswith("[excerpt truncated: "))

    def test_prompts_cap_keeps_newest(self):
        text = "\n\n".join(f"### Prompt {i} (10:0{i})\n" + "x" * 3000 for i in range(1, 4))
        capped = prompts.capped_prompts(text)
        self.assertLessEqual(len(capped), 6000)
        self.assertIn("### Prompt 3", capped)
        self.assertNotIn("### Prompt 1", capped)


if __name__ == "__main__":
    unittest.main()
