"""Codex parity for hooks, guard and gate: payload normalization, platform detection, and end-to-end
`vloop --platform codex hook <event>` runs with recorded Codex payloads (tests/fixtures/codex)."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import helpers  # noqa: E402,F401
sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures" / "wp1"))

from wp1helpers import EnvCase, write  # noqa: E402

from vlooplib import platforms  # noqa: E402

CODEX_FIX = Path(__file__).resolve().parent / "fixtures" / "codex"
ENFORCE = {"VERIFY_LOOP_MODE": "enforce"}


def fixture(name: str, **subs) -> dict:
    text = (CODEX_FIX / f"{name}.json").read_text()
    for k, v in subs.items():
        text = text.replace("{" + k + "}", str(v))
    return json.loads(text)


class NormalizeTest(EnvCase):
    def norm(self, name, event="post-tool"):
        return platforms.get("codex").normalize(event, fixture(name, sid="s", cwd="/r", home="/h"))

    def test_apply_patch_ops(self):
        cases = {"post_tool_apply_patch_add": ["src/new.js"], "post_tool_apply_patch_update": ["a.js"],
                 "post_tool_apply_patch_delete": ["b.js"], "post_tool_apply_patch_move": ["a.js", "lib/a.js"],
                 "post_tool_apply_patch_spaces": ["docs dir/my file.js"], "post_tool_edit_alias": ["a.js"],
                 "pre_tool_apply_patch": ["a.js"]}
        for name, want in cases.items():
            ev = self.norm(name)
            self.assertEqual((ev.edit_paths, ev.bash_cmd), (want, None), name)
            self.assertEqual((ev.sid, ev.cwd), ("s", "/r"))

    def test_bash_and_shell_forms(self):
        ev = self.norm("post_tool_bash")
        self.assertEqual((ev.tool, ev.bash_cmd, ev.edit_paths), ("Bash", "sed -i '' s/a/b/ a.js", []))
        p = platforms.get("codex")
        ev = p.normalize("pre-tool", {"tool_name": "Bash", "tool_input": {"command": ["bash", "-lc", "ls -la"]}})
        self.assertEqual(ev.bash_cmd, "ls -la")
        ev = p.normalize("pre-tool", {"tool_name": "Bash", "tool_input": {
            "command": "apply_patch <<'EOF'\n*** Begin Patch\n*** Add File: x.js\n+x\n*** End Patch\nEOF\n"}})
        self.assertEqual((ev.bash_cmd, ev.edit_paths), (None, ["x.js"]))
        ev = p.normalize("pre-tool", {"tool_name": "Write", "tool_input": {"file_path": "/r/w.js", "content": "x"}})
        self.assertEqual(ev.edit_paths, ["/r/w.js"])

    def test_events(self):
        ev = self.norm("subagent_start", "subagent-start")
        self.assertEqual((ev.agent_id, ev.agent_type), ("agent-rev-1", "verify-reviewer"))
        ev = self.norm("subagent_stop", "subagent-stop")
        self.assertEqual(ev.raw["agent_transcript_path"], "/h/.codex/sessions/2026/11/11/rollout-sub-agent-rev-1.jsonl")
        ev = self.norm("stop_active", "stop")
        self.assertEqual((ev.stop_active, ev.last_msg), (True, "Done."))
        ev = self.norm("user_prompt_submit", "prompt")
        self.assertEqual((ev.prompt, ev.transcript),
                         ("add b()", "/h/.codex/sessions/2026/11/11/rollout-2026-11-11T10-00-00-s.jsonl"))
        ev = self.norm("session_start", "session-start")
        self.assertEqual((ev.sid, ev.tool, ev.edit_paths), ("s", "", []))
        self.assertEqual(platforms.get("codex").normalize("stop", {}).sid, "")

    def test_session_env(self):
        p = platforms.get("codex")
        self.assertIsNone(p.session_id_from_env())
        os.environ["CODEX_THREAD_ID"] = "th-1"
        self.assertEqual(p.session_id_from_env(), "th-1")

    def test_prompt_regexes(self):
        for name in ("codex", "claude"):
            p = platforms.get(name)
            self.assertTrue(re.match(p.injected_prompt_re, "verify-loop gate: this session changed code"), name)
            self.assertTrue(re.match(p.injected_prompt_re, "verify-loop: verification passed but the retro"), name)
            self.assertIsNone(re.match(p.injected_prompt_re, "please run the verify-loop gate: x"), name)
        p = platforms.get("codex")
        for text in ("$verify-loop", "$verify-review", "$verify-qa x", "$verify-retro", "/verify-loop",
                     "$agent-verify-loop:verify-loop", "/agent-verify-loop:verify-qa run"):
            self.assertTrue(re.match(p.skill_prompt_re, text, re.S), text)
        for text in ("run $verify-loop", "$verify-loops", "$vault"):
            self.assertIsNone(re.match(p.skill_prompt_re, text, re.S), text)


class DetectTest(EnvCase):
    def setUp(self):
        super().setUp()
        os.environ.pop("CLAUDE_CODE_ENTRYPOINT", None)

    def test_payload_shape(self):
        self.assertEqual(platforms.detect({"turn_id": "t", "hook_event_name": "Stop"}).name, "codex")
        self.assertEqual(platforms.detect({"tool_name": "apply_patch"}).name, "codex")
        self.assertEqual(platforms.detect({"permission_mode": "default", "hook_event_name": "Stop"}).name, "claude")
        self.assertEqual(platforms.detect({"session_id": "x"}).name, "claude")
        self.assertEqual(platforms.detect(None).name, "claude")

    def test_env_hints(self):
        self.assertEqual(platforms.from_env(), None)
        os.environ["CODEX_HOME"] = str(self.tmp)
        self.assertEqual(platforms.detect({"session_id": "x"}).name, "claude")
        os.environ["CODEX_SANDBOX"] = "seatbelt"
        self.assertEqual(platforms.detect({"session_id": "x"}).name, "codex")
        os.environ["CLAUDECODE"] = "1"
        self.assertEqual(platforms.detect({"session_id": "x"}).name, "claude")
        os.environ["CODEX_THREAD_ID"] = "th"
        self.assertEqual(platforms.detect({"session_id": "x"}).name, "codex")
        self.assertEqual(platforms.detect({"permission_mode": "default"}).name, "claude")

    def test_precedence(self):
        os.environ["VLOOP_PLATFORM"] = "claude"
        self.assertEqual(platforms.detect({"turn_id": "t"}).name, "claude")
        platforms.set_override("codex")
        try:
            self.assertEqual(platforms.detect({"permission_mode": "x"}).name, "codex")
            self.assertEqual(platforms.detect(override="claude").name, "claude")
        finally:
            platforms.set_override(None)
        os.environ["VLOOP_PLATFORM"] = "bogus"
        self.assertEqual(platforms.detect({}).name, "claude")


class CodexHookCase(EnvCase):
    def setUp(self):
        super().setUp()
        self.root = self.make_repo(files={"a.js": "a\n", "b.js": "b\n", "README.md": "# r\n", ".gitignore": "dist/\n"})

    def payload(self, name, sid="c1", **extra):
        d = fixture(name, sid=sid, cwd=self.root, home=self.home)
        d.update(extra)
        return d

    def chook(self, event, name, sid="c1", env=None, **extra):
        p = self.vloop("--platform", "codex", "hook", event, stdin=json.dumps(self.payload(name, sid, **extra)),
                       env=env)
        self.assertEqual(p.returncode, 0, p.stderr)
        return p

    def chook_json(self, *a, **kw):
        out = self.chook(*a, **kw).stdout.strip()
        return json.loads(out) if out else None

    def rs(self, sid="c1"):
        st = self.session(sid)
        return next(iter(st["repos"].values())) if st and st["repos"] else None

    def apply(self, name, mutate, sid="c1", env=None):
        self.chook("pre-tool", name, sid, env=env, hook_event_name="PreToolUse")
        mutate()
        self.chook("post-tool", name, sid, env=env)


class CodexTrackingTest(CodexHookCase):
    def test_apply_patch_touches_and_platform(self):
        self.chook("prompt", "user_prompt_submit")
        st = self.session("c1")
        self.assertEqual(st["platform"], "codex")
        self.assertEqual(self.rs()["baseline"]["trigger"], "prompt")
        self.apply("post_tool_apply_patch_update", lambda: write(self.root / "a.js", "a2\n"))
        self.apply("post_tool_apply_patch_add", lambda: write(self.root / "src" / "new.js", "n\n"))
        self.apply("post_tool_apply_patch_delete", lambda: (self.root / "b.js").unlink())
        self.apply("post_tool_apply_patch_spaces", lambda: write(self.root / "docs dir" / "my file.js", "x\n"))
        self.assertEqual(self.rs()["touched"], ["a.js", "b.js", "docs dir/my file.js", "src/new.js"])
        self.assertEqual(self.rs()["touch_sources"]["edit"], 4)
        self.assertEqual(self.errors(), "")

    def test_move_and_edit_alias(self):
        def move():
            write(self.root / "lib" / "a.js", "a\n")
            (self.root / "a.js").unlink()
        self.apply("post_tool_apply_patch_move", move)
        self.assertEqual(self.rs()["touched"], ["a.js", "lib/a.js"])
        self.assertEqual(self.rs()["baseline"]["trigger"], "pre-edit")
        self.chook("post-tool", "post_tool_edit_alias", sid="c2")
        self.assertEqual(self.rs("c2")["touched"], ["a.js"])

    def test_bash_fast_path_and_untracked_post_bash(self):
        p = self.chook("pre-tool", "pre_tool_bash")
        self.assertEqual(p.stdout, "")
        self.assertIsNone(self.session("c1"))
        self.chook("post-tool", "post_tool_bash")
        self.assertIsNone(self.session("c1"))

    def test_session_start_and_injected_continuation(self):
        self.chook("prompt", "user_prompt_submit")
        self.chook("session-start", "session_start")
        self.assertEqual(self.events("c1")[-1]["data"]["platform"], "codex")
        n = len(self.session("c1")["task"]["prompts"])
        self.chook("prompt", "user_prompt_submit", prompt="verify-loop gate: this session changed code that ...")
        self.chook("prompt", "user_prompt_submit", prompt="$verify-loop")
        self.assertEqual(len(self.session("c1")["task"]["prompts"]), n)
        injected = [e for e in self.events("c1") if e["event"] == "prompt" and e["data"].get("injected")]
        self.assertEqual(len(injected), 1)


class CodexGateTest(CodexHookCase):
    def touch_a(self):
        self.chook("prompt", "user_prompt_submit")
        self.apply("post_tool_apply_patch_update", lambda: write(self.root / "a.js", "a2\n"))

    def test_warn_shape(self):
        self.touch_a()
        out = self.chook_json("stop", "stop", env={"VERIFY_LOOP_MODE": "warn"})
        self.assertEqual(set(out), {"systemMessage"})
        self.assertIn("verify-loop (warn mode)", out["systemMessage"])

    def test_enforce_block_shape_and_stop_hook_active(self):
        self.touch_a()
        out = self.chook_json("stop", "stop", env=ENFORCE)
        self.assertEqual(out["decision"], "block")
        self.assertTrue(out["reason"].startswith("verify-loop gate: "))
        self.assertIn("a.js", out["reason"])
        out = self.chook_json("stop", "stop_active", env=ENFORCE)
        self.assertEqual(out["decision"], "block")
        out = self.chook_json("stop", "stop_active", env=ENFORCE)
        self.assertEqual(set(out), {"systemMessage"})
        self.assertIn("stopped blocking", out["systemMessage"])


class CodexGuardTest(CodexHookCase):
    def setUp(self):
        super().setUp()
        self.tmpdir = self.tmp / "tmpdir"
        self.tmpdir.mkdir()
        os.environ["TMPDIR"] = str(self.tmpdir)

    def deny(self, out):
        return out is not None and out["hookSpecificOutput"]["permissionDecision"] == "deny"

    def test_reviewer_apply_patch_denied(self):
        out = self.chook_json("pre-tool", "pre_tool_apply_patch_reviewer")
        self.assertTrue(self.deny(out), out)
        self.assertIn("reviewer guard", out["hookSpecificOutput"]["permissionDecisionReason"])
        out = self.chook_json("pre-tool", "pre_tool_apply_patch_reviewer",
                              agent_type="agent-verify-loop:verify-reviewer")
        self.assertTrue(self.deny(out))
        self.assertIsNone(self.session("c1"))

    def test_reviewer_runs_dir_patch_allowed_silently(self):
        verdict = self.state / "runs" / "r1" / "attempt-1" / "review.json"
        patch = f"*** Begin Patch\n*** Add File: {verdict}\n+{{}}\n*** End Patch\n"
        p = self.chook("pre-tool", "pre_tool_apply_patch_reviewer", tool_input={"command": patch})
        self.assertEqual(p.stdout, "")

    def test_subagent_attribution_without_agent_type(self):
        self.chook("subagent-start", "subagent_start")
        st = self.session("c1")
        self.assertEqual(st["agents"]["agent-rev-1"]["role"], "reviewer")
        out = self.chook_json("pre-tool", "pre_tool_apply_patch_reviewer", agent_type=None)
        self.assertTrue(self.deny(out), out)
        out = self.chook_json("pre-tool", "pre_tool_bash", agent_id="agent-rev-1",
                              tool_input={"command": "rm -rf src"})
        self.assertTrue(self.deny(out), out)
        self.chook("post-tool", "post_tool_apply_patch_update", agent_id="agent-rev-1")
        self.assertEqual(self.session("c1")["repos"], {})
        self.chook("subagent-stop", "subagent_stop")
        a = self.session("c1")["agents"]["agent-rev-1"]
        self.assertTrue(a["stopped_at"])
        self.assertEqual(a["transcript_path"], f"{self.home}/.codex/sessions/2026/11/11/rollout-sub-agent-rev-1.jsonl")
        stop = [e for e in self.events("c1") if e["event"] == "subagent_stop"][0]
        self.assertEqual(stop["data"]["transcript"], a["transcript_path"])
        self.assertIsNone(self.chook_json("pre-tool", "pre_tool_apply_patch_reviewer", agent_type=None))

    def test_env_role_before_mode_off(self):
        env = {"VLOOP_GUARD_ROLE": "reviewer", "VERIFY_LOOP_MODE": "off"}
        out = self.chook_json("pre-tool", "pre_tool_apply_patch", env=env)
        self.assertTrue(self.deny(out), out)
        self.chook("post-tool", "post_tool_apply_patch_update", env=env)
        self.assertIsNone(self.session("c1"))
        out = self.chook_json("pre-tool", "pre_tool_apply_patch", env={"VLOOP_GUARD_ROLE": "qa"})
        self.assertTrue(self.deny(out), out)
        tmp_patch = f"*** Begin Patch\n*** Add File: {self.tmpdir}/qa-scratch.txt\n+x\n*** End Patch\n"
        p = self.chook("pre-tool", "pre_tool_apply_patch", env={"VLOOP_GUARD_ROLE": "qa"},
                       tool_input={"command": tmp_patch})
        self.assertEqual(p.stdout, "")

    def test_vault_agent_untracked(self):
        self.chook("post-tool", "post_tool_apply_patch_update", agent_type="agent-vault:vault-keeper")
        self.assertIsNone(self.session("c1"))


class StopScanTest(CodexHookCase):
    def test_scan_records_bash_edits(self):
        self.chook("prompt", "user_prompt_submit")
        write(self.root / "a.js", "bash edit\n")
        write(self.root / "dist" / "out.js", "ignored\n")
        write(self.root / "new.js", "untracked new\n")
        self.chook("post-tool", "post_tool_bash")
        self.assertEqual(self.rs()["touched"], [])
        out = self.chook_json("stop", "stop", env=ENFORCE)
        self.assertEqual(out["decision"], "block")
        self.assertIn("a.js", out["reason"])
        rs = self.rs()
        self.assertEqual(rs["touched"], ["a.js", "new.js"])
        self.assertEqual(rs["touch_sources"]["scan"], 1)
        touch = [e for e in self.events("c1") if e["event"] == "touch"][0]
        self.assertEqual(touch["data"]["source"], "scan")

    def test_no_baseline_no_scan(self):
        self.chook("subagent-start", "subagent_start")
        write(self.root / "a.js", "bash edit\n")
        self.assertIsNone(self.chook_json("stop", "stop", env=ENFORCE))
        self.assertEqual(self.session("c1")["repos"], {})

    def test_disabled_by_config_and_claude_default(self):
        self.write_user_config(gate={"scan_on_stop": False})
        self.chook("prompt", "user_prompt_submit")
        write(self.root / "a.js", "bash edit\n")
        self.assertIsNone(self.chook_json("stop", "stop", env=ENFORCE))
        self.write_user_config()
        claude = {"session_id": "k1", "cwd": str(self.root), "hook_event_name": "UserPromptSubmit",
                  "transcript_path": "/dev/null", "prompt": "x", "permission_mode": "default"}
        self.hook("prompt", claude)
        write(self.root / "b.js", "bash edit\n")
        stop = {"session_id": "k1", "cwd": str(self.root), "hook_event_name": "Stop", "permission_mode": "default",
                "stop_hook_active": False, "last_assistant_message": "Done."}
        self.assertIsNone(self.hook_json("stop", stop, env=ENFORCE))
        self.assertEqual(self.session("k1")["platform"], "claude")
        self.write_user_config(gate={"scan_on_stop": {"claude": True}})
        self.assertEqual(self.hook_json("stop", stop, env=ENFORCE)["decision"], "block")


class ScanOnStopShapeTest(unittest.TestCase):
    def test_shapes(self):
        from vlooplib import config, hooks

        claude, codex = platforms.get("claude"), platforms.get("codex")
        self.assertEqual(config.defaults()["gate"]["scan_on_stop"], {"claude": False, "codex": True})
        for v, want in (({"claude": False, "codex": True}, (False, True)), (True, (True, True)),
                        (False, (False, False)), (None, (False, True)), ({"codex": None}, (False, True)),
                        ({"claude": True}, (True, True)), ({}, (False, True))):
            cfg = {"gate": {"scan_on_stop": v}}
            self.assertEqual((hooks.scan_on_stop(cfg, claude), hooks.scan_on_stop(cfg, codex)), want, v)
        self.assertEqual((hooks.scan_on_stop({}, claude), hooks.scan_on_stop({}, codex)), (False, True))


class SessionFallbackTest(CodexHookCase):
    def run_vloop(self, *args, cwd, env=None):
        e = dict(os.environ)
        e.pop("CLAUDE_CODE_ENTRYPOINT", None)
        e.update(env or {})
        return subprocess.run([sys.executable, str(helpers.VLOOP), *args], cwd=cwd, capture_output=True, text=True,
                              env=e, timeout=60)

    def test_skip_uses_newest_matching_codex_session(self):
        self.chook("prompt", "user_prompt_submit", sid="old")
        self.chook("prompt", "user_prompt_submit", sid="c1")
        self.apply("post_tool_apply_patch_update", lambda: write(self.root / "a.js", "a2\n"))
        p = self.run_vloop("--platform", "codex", "skip", "--reason", "user said so", cwd=self.root / "")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("UNVERIFIED", p.stdout)
        self.assertEqual(self.rs("c1")["loop"]["status"], "skipped")
        other = self.make_repo("other")
        p = self.run_vloop("--platform", "codex", "status", cwd=other)
        self.assertEqual(p.returncode, 2)
        p = self.run_vloop("status", cwd=self.root)
        self.assertEqual(p.returncode, 2)
        p = self.run_vloop("status", "--json", cwd=other, env={"CODEX_THREAD_ID": "old"})
        self.assertEqual(json.loads(p.stdout)["session_id"], "old")


if __name__ == "__main__":
    unittest.main()
