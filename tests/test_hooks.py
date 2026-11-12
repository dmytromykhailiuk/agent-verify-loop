"""WP1 hooks (§2.11): tracking, baselines, the Stop gate — cases (a)–(s) of the WP1 acceptance list."""
from __future__ import annotations

import json
import os
import sys
import time
import unittest
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import helpers  # noqa: E402,F401
sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures" / "wp1"))

from wp1helpers import FIXTURES, EnvCase, git, write  # noqa: E402

from vlooplib import util  # noqa: E402

ENFORCE = {"VERIFY_LOOP_MODE": "enforce"}


class HookCase(EnvCase):
    def setUp(self):
        super().setUp()
        self.root = self.make_repo(files={"a.js": "a\n", "b.js": "b\n", "README.md": "# r\n", ".gitignore": "dist/\n"})
        self.slug = util.slugify(self.root.name)

    def rs(self, sid="s1"):
        st = self.session(sid)
        return next(iter(st["repos"].values())) if st and st["repos"] else None

    def set_loop(self, sid="s1", **kv):
        p = self.state / "sessions" / f"{sid}.json"
        st = json.loads(p.read_text())
        next(iter(st["repos"].values()))["loop"].update(kv)
        p.write_text(json.dumps(st))

    def stop(self, sid="s1", env=None, **extra):
        return self.hook_json("stop", self.p_stop(sid, self.root, **extra), env=env)


class TrackingTest(HookCase):
    def test_a_stop_without_state(self):
        self.assertEqual(self.hook("stop", self.p_stop("none", self.root), env=ENFORCE).stdout, "")
        self.assertIsNone(self.session("none"))

    def test_b_prompt_creates_state_and_baseline(self):
        self.hook("prompt", self.p_prompt("s1", self.root, "add b()"), env={"CLAUDE_CODE_ENTRYPOINT": "cli"})
        st = self.session("s1")
        self.assertEqual(st["task"]["prompts"][0]["text"], "add b()")
        self.assertEqual(st["entrypoint"], "cli")
        self.assertEqual(st["depth"], 0)
        rs = self.rs()
        self.assertEqual(rs["baseline"]["kind"], "snapshot")
        self.assertEqual(rs["baseline"]["trigger"], "prompt")
        self.assertEqual(rs["root"], str(self.root))
        self.assertEqual(self.event_names("s1"), ["prompt", "baseline"])
        # skill invocations are not recorded as task prompts; prompts are capped at 10
        self.hook("prompt", self.p_prompt("s1", self.root, "/verify-loop"))
        self.hook("prompt", self.p_prompt("s1", self.root, "/retro --full"))
        for i in range(12):
            self.hook("prompt", self.p_prompt("s1", self.root, f"p{i}" + "x" * 5000))
        prompts = self.session("s1")["task"]["prompts"]
        self.assertEqual(len(prompts), 10)
        self.assertTrue(prompts[-1]["text"].startswith("p11"))
        self.assertEqual(len(prompts[-1]["text"]), 4000)

    def test_c_pre_tool_snapshots_once_and_bash_fast_path(self):
        f = self.root / "a.js"
        self.hook("pre-tool", self.p_edit("s1", self.root, f, event="PreToolUse"))
        self.hook("pre-tool", self.p_edit("s1", self.root, f, event="PreToolUse"))
        self.assertEqual(self.event_names("s1"), ["baseline"])
        self.assertEqual(self.rs()["baseline"]["trigger"], "pre-edit")
        bash = {"session_id": "s2", "cwd": str(self.root), "hook_event_name": "PreToolUse", "tool_name": "Bash",
                "tool_input": {"command": "echo hi > a.js"}}
        self.assertEqual(self.hook("pre-tool", bash).stdout, "")
        self.assertIsNone(self.session("s2"))
        self.assertEqual(self.event_names("s2"), [])

    def test_h_child_touch_lands_in_parent(self):
        self.hook("prompt", self.p_prompt("parent", self.root))
        child_env = {"HERDR_AGENT_DEPTH": "1", "HERDR_PARENT_SESSION_ID": "parent"}
        self.edit("child", self.root, "a.js", env=child_env)
        rs = self.rs("parent")
        self.assertEqual(rs["touched"], ["a.js"])
        self.assertEqual(rs["touch_sources"]["child"], 1)
        touch = [e for e in self.events("parent") if e["event"] == "touch"][0]
        self.assertEqual(touch["data"]["source"], "child")
        self.assertEqual(touch["data"]["child_session"], "child")
        self.assertIsNone(self.session("child"))
        self.assertIsNone(self.stop("child", env={**child_env, **ENFORCE}))
        # parent with no state yet: minimal parent state is created
        self.edit("child2", self.root, "b.js", env={"HERDR_AGENT_DEPTH": "1", "HERDR_PARENT_SESSION_ID": "p2"})
        self.assertEqual(self.rs("p2")["touched"], ["b.js"])
        self.assertEqual(self.rs("p2")["baseline"]["trigger"], "pre-edit")

    def test_i_verify_agents_untracked(self):
        for agent in ("verify-qa", "verify-reviewer", "vault-keeper", "agent-verify-loop:verify-reviewer",
                      "agent-verify-loop:verify-qa", "agent-vault:vault-keeper", "verify-triage"):
            self.hook("post-tool", self.p_edit("s1", self.root, self.root / "a.js", agent_type=agent))
        self.assertIsNone(self.session("s1"))

    def test_l_excluded_paths(self):
        # vault root (template parent) is excluded even with VERIFY_LOOP_ALLOW_TMP=1
        vault = self.tmp / "vault" / "proj"
        vault.mkdir(parents=True)
        git(vault, "init", "-q", "-b", "master")
        write(vault / "note.md", "x")
        self.hook("post-tool", self.p_edit("s1", vault, vault / "note.md"))
        # a repo with _meta.json {"schema": …} is a vault repo
        meta = self.make_repo("metarepo", {"_meta.json": '{"schema": 1}', "n.md": "x"})
        self.hook("post-tool", self.p_edit("s1", meta, meta / "n.md"))
        # the Claude and Codex homes
        self.hook("post-tool", self.p_edit("s1", self.root, Path(os.environ["CLAUDE_CONFIG_DIR"]) / "skills" / "x.md"))
        self.hook("post-tool", self.p_edit("s1", self.root, Path(os.environ["CODEX_HOME"]) / "skills" / "x.md"))
        # not in a git work tree
        write(self.tmp / "loose" / "x.js", "x")
        self.hook("post-tool", self.p_edit("s1", self.tmp / "loose", self.tmp / "loose" / "x.js"))
        # disabled repo
        off = self.make_repo("offrepo", {"x.js": "x", ".claude/verify.json": '{"enabled": false}'})
        self.hook("post-tool", self.p_edit("s1", off, off / "x.js"))
        self.assertIsNone(self.session("s1"))
        # tmp is excluded without VERIFY_LOOP_ALLOW_TMP
        self.hook("post-tool", self.p_edit("s1", self.root, self.root / "a.js"), env={"VERIFY_LOOP_ALLOW_TMP": None})
        self.assertIsNone(self.session("s1"))
        self.hook("post-tool", self.p_edit("s1", self.root, self.root / "a.js"))
        self.assertEqual(self.rs()["touched"], ["a.js"])
        self.assertEqual(self.errors(), "")

    def test_m_bash_edit_diff(self):
        payload = json.loads((FIXTURES / "bash_edit_diff.json").read_text()
                             .replace("SID", "s1").replace("CWD", str(self.root))
                             .replace("FILE", str(self.root / "b.js")))
        write(self.root / "b.js", "b changed\n")
        self.hook("post-tool", payload)
        rs = self.rs()
        self.assertEqual(rs["touched"], ["b.js"])
        self.assertEqual(rs["touch_sources"]["bash"], 1)
        self.assertEqual(rs["baseline"]["kind"], "head-fallback")
        self.assertEqual(rs["baseline"]["trigger"], "post-bash")
        # files[] fallback when changedFiles is absent
        del payload["tool_response"]["bashEditDiff"]["changedFiles"]
        payload["tool_response"]["bashEditDiff"]["files"] = [{"filePath": str(self.root / "a.js")}]
        self.hook("post-tool", payload)
        self.assertEqual(self.rs()["touched"], ["a.js", "b.js"])
        # without bashEditDiff → nothing
        del payload["tool_response"]["bashEditDiff"]
        payload["session_id"] = "s2"
        self.hook("post-tool", payload)
        self.assertIsNone(self.session("s2"))

    def test_r_malformed_stdin(self):
        p = self.vloop("hook", "post-tool", stdin="{not json")
        self.assertEqual((p.returncode, p.stdout), (0, ""))
        self.assertIn("hook.post-tool", self.errors())
        p = self.vloop("hook", "stop", stdin="[1, 2]")
        self.assertEqual((p.returncode, p.stdout), (0, ""))

    def test_s_gitignored(self):
        write(self.root / "dist" / "x.js", "x")
        self.hook("post-tool", self.p_edit("s1", self.root, self.root / "dist" / "x.js"))
        self.assertIsNone(self.session("s1"))

    def test_symlink_ignored(self):
        os.symlink(self.root / "a.js", self.root / "link.js")
        self.hook("post-tool", self.p_edit("s1", self.root, self.root / "link.js"))
        self.assertIsNone(self.session("s1"))

    def test_mode_off_env_and_debug_payloads(self):
        self.hook("prompt", self.p_prompt("s1", self.root), env={"VERIFY_LOOP_MODE": "off"})
        self.assertIsNone(self.session("s1"))
        self.hook("prompt", self.p_prompt("s1", self.root), env={"VERIFY_LOOP_DEBUG_PAYLOADS": "1"})
        files = list((self.state / "debug").glob("prompt-*.json"))
        self.assertEqual(len(files), 1)
        self.assertEqual(json.loads(files[0].read_text())["prompt"], "do the thing")

    def test_injected_prompts_are_not_user_prompts(self):
        # subagent hand-backs arrive as UserPromptSubmit (WP7 bug #3): no task prompt, no new task after closure
        self.hook("prompt", self.p_prompt("s1", self.root, "fix it"))
        self.edit("s1", self.root, "a.js")
        p = self.vloop("skip", "--reason", "user said so", env={"CLAUDE_CODE_SESSION_ID": "s1"})
        self.assertEqual(p.returncode, 0, p.stderr)
        tree1 = self.rs()["baseline"]["tree"]
        for text in ('<agent-message from="ad21">\n[Subagent hand-back] report', "<task-notification>\ndone",
                     "  <system-reminder>x</system-reminder>"):
            self.hook("prompt", self.p_prompt("s1", self.root, text))
        st = self.session("s1")
        self.assertEqual((st["task"]["n"], st["task"]["prompts"]), (2, []))
        self.assertEqual(self.rs()["baseline"]["tree"], tree1)
        self.assertIsNotNone(self.rs()["closed_fp"])
        self.assertEqual([e["data"].get("injected") for e in self.events("s1") if e["event"] == "prompt"][-3:],
                         [True] * 3)
        self.hook("prompt", self.p_prompt("s1", self.root, "<b>bold</b> is a real prompt"))
        st = self.session("s1")
        self.assertEqual([p["text"] for p in st["task"]["prompts"]], ["<b>bold</b> is a real prompt"])
        self.assertIsNone(self.rs()["closed_fp"])

    def test_rebaseline_on_prompt_after_closure(self):
        self.hook("prompt", self.p_prompt("s1", self.root))
        self.edit("s1", self.root, "a.js")
        tree1 = self.rs()["baseline"]["tree"]
        p = self.vloop("skip", "--reason", "user said so", env={"CLAUDE_CODE_SESSION_ID": "s1"})
        self.assertEqual(p.returncode, 0, p.stderr)
        self.hook("prompt", self.p_prompt("s1", self.root, "next task"))
        rs = self.rs()
        self.assertEqual(rs["touched"], [])
        self.assertIsNone(rs["closed_fp"])
        self.assertNotEqual(rs["baseline"]["tree"], tree1)
        self.assertEqual(rs["loop"]["status"], "idle")
        st = self.session("s1")
        self.assertEqual(st["task"]["n"], 2)
        self.assertEqual([p["text"] for p in st["task"]["prompts"]], ["next task"])
        # pending changes keep the baseline
        self.edit("s1", self.root, "b.js")
        tree2 = self.rs()["baseline"]["tree"]
        self.hook("prompt", self.p_prompt("s1", self.root, "more"))
        self.assertEqual(self.rs()["baseline"]["tree"], tree2)
        self.assertEqual(self.rs()["touched"], ["b.js"])


class GateTest(HookCase):
    def touch(self, sid="s1", rel="a.js"):
        self.hook("prompt", self.p_prompt(sid, self.root))
        self.edit(sid, self.root, rel)

    def test_d_enforce_blocks_twice_then_ignored(self):
        self.touch()
        out = self.stop(env=ENFORCE)
        self.assertEqual(out["decision"], "block")
        self.assertTrue(out["reason"].startswith(
            f"verify-loop gate: this session changed code that hasn't been verified — {self.slug}: 1 files (a.js)."))
        self.assertIn('vloop skip --reason "<the user\'s words>"', out["reason"])
        self.assertEqual(self.stop(env=ENFORCE)["decision"], "block")
        out = self.stop(env=ENFORCE)
        self.assertNotIn("decision", out)
        self.assertEqual(out["systemMessage"],
                         f"⚠ verify-loop: unverified changes in {self.slug} (1 files); the gate stopped blocking after "
                         f"2 attempts. Run /verify-loop or vloop skip --reason \"…\".")
        names = self.event_names("s1")
        self.assertEqual(names.count("gate_block"), 2)
        self.assertEqual(names.count("gate_ignored"), 1)
        # a new change → new fingerprint → blocks again
        self.edit("s1", self.root, "a.js", text="more\n")
        self.assertEqual(self.stop(env=ENFORCE)["decision"], "block")

    def test_e_warn_once_per_fp(self):
        self.touch()
        out = self.stop()
        self.assertEqual(out, {"systemMessage": f"verify-loop (warn mode): unverified changes in {self.slug} (1 files) — "
                                                f"in enforce mode this stop would be blocked until /verify-loop runs."})
        self.assertIsNone(self.stop())
        self.edit("s1", self.root, "b.js")
        self.assertIn("warn mode", self.stop()["systemMessage"])
        self.assertEqual(self.event_names("s1").count("gate_warn"), 3)

    def test_f_question_defers(self):
        self.touch()
        self.assertIsNone(self.stop(env=ENFORCE, msg="Should I also update the docs?**"))
        self.assertEqual(self.events("s1")[-1]["event"], "gate_defer")
        self.assertEqual(self.events("s1")[-1]["data"]["reason"], "question")

    def test_g_defer_command(self):
        self.touch()
        p = self.vloop("defer", "--reason", "asked the user about scope", env={"CLAUDE_CODE_SESSION_ID": "s1"})
        self.assertEqual(p.stdout.strip(), "verify-loop · deferred: asked the user about scope (gate passes once)")
        self.assertIsNone(self.stop(env=ENFORCE))
        self.assertEqual(self.events("s1")[-1]["data"]["reason"], "defer-cmd")
        self.assertEqual(self.stop(env=ENFORCE)["decision"], "block")

    def test_defer_cleared_by_new_touch(self):
        self.touch()
        self.vloop("defer", "--reason", "x", env={"CLAUDE_CODE_SESSION_ID": "s1"})
        self.assertIsNotNone(self.rs()["gate"]["defer"])
        self.edit("s1", self.root, "b.js")
        self.assertIsNone(self.rs()["gate"]["defer"])
        self.assertEqual(self.stop(env=ENFORCE)["decision"], "block")

    def test_j_docs_only(self):
        self.touch(rel="README.md")
        out = self.stop(env=ENFORCE)
        self.assertEqual(out, {"systemMessage": "verify-loop · skipped: docs-only change (README.md)"})
        self.assertEqual(self.rs()["loop"]["status"], "skipped")
        self.assertIn("gate_skip_docs", self.event_names("s1"))
        self.assertIsNone(self.stop(env=ENFORCE))
        self.assertIsNone(self.stop(env=ENFORCE))

    def test_k_reverted_edit(self):
        self.touch()
        write(self.root / "a.js", "a\n")
        self.assertIsNone(self.stop(env=ENFORCE))

    def test_n_background_inflight_escalated(self):
        self.touch()
        self.assertIsNone(self.stop(env=ENFORCE, background_tasks=[{"id": "b1", "status": "running"}]))
        self.assertEqual(self.events("s1")[-1]["data"]["reason"], "background")
        self.set_loop(inflight_since=util.now_iso(), status="open", run_id="r1", attempt=1)
        self.assertIsNone(self.stop(env=ENFORCE))
        self.assertEqual(self.events("s1")[-1]["data"]["reason"], "inflight")
        old = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime(time.time() - 2 * 3600))
        self.set_loop(inflight_since=old, status="escalated")
        out = self.stop(env=ENFORCE)
        self.assertEqual(out, {"systemMessage": "verify-loop: escalated — waiting for your decision (/verify-loop status)"})
        # stale inflight + open loop without a judge → "started but not finished"
        self.set_loop(status="open", last_result=None)
        out = self.stop(env=ENFORCE)
        self.assertIn("run r1 was started but not finished", out["reason"])
        self.set_loop(status="open", last_result="fail")
        out = self.stop(env=ENFORCE)
        self.assertIn("run r1 is still open — attempt 1 was FAIL", out["reason"])

    def test_o_retro_block_once(self):
        self.touch()
        self.set_loop(run_id="r9", retro="pending", status="passed")
        write(self.root / "a.js", "a\n")  # nothing pending
        out = self.stop(env=ENFORCE)
        self.assertEqual(out["decision"], "block")
        self.assertIn("vloop retro-plan --run r9", out["reason"])
        self.assertEqual(self.rs()["loop"]["retro"], "requested")
        self.assertIsNone(self.stop(env=ENFORCE))
        self.set_loop(run_id="r10", retro="pending")
        write(self.state / "runs" / "r10" / "retro" / "plan.json", "{}")
        self.assertIsNone(self.stop(env=ENFORCE))
        self.assertIsNone(self.stop())  # warn mode never asks for the retro

    def test_p_warn_reminder_daily(self):
        past = (date.today() - timedelta(days=1)).isoformat()
        self.write_user_config(mode="warn", installed_at="2026-01-01", enforce_review_after=past)
        payload = {"session_id": "s1", "cwd": str(self.root), "hook_event_name": "SessionStart", "source": "startup"}
        out = self.hook_json("session-start", payload)
        self.assertIn("verify-loop: gate in warn mode since 2026-01-01", out["systemMessage"])
        self.assertIn("`vloop mode enforce`", out["systemMessage"])
        self.assertIsNone(self.hook_json("session-start", payload))
        self.assertEqual(self.event_names("s1"), ["session_start", "session_start"])
        (self.state / "reminder.stamp").unlink()
        future = (date.today() + timedelta(days=3)).isoformat()
        self.write_user_config(mode="warn", installed_at="2026-01-01", enforce_review_after=future)
        self.assertIsNone(self.hook_json("session-start", payload))

    def test_session_start_runs_gc_once_a_day(self):
        (self.state / "gc.stamp").unlink()
        old_run = self.state / "runs" / "old"
        write(old_run / "context.json", "{}")
        t = time.time() - 30 * 86400
        os.utime(old_run / "context.json", (t, t))
        os.utime(old_run, (t, t))
        self.hook("session-start", {"session_id": "s1", "cwd": str(self.root), "source": "startup"})
        self.assertEqual((self.state / "gc.stamp").read_text().strip(), util.today())
        for _ in range(50):
            if not old_run.exists():
                break
            time.sleep(0.05)
        self.assertFalse(old_run.exists())

    def test_q_non_cli_entrypoint(self):
        self.write_user_config(mode="enforce")
        self.touch()
        out = self.stop(env={"CLAUDE_CODE_ENTRYPOINT": "sdk-cli"})
        self.assertIn("warn mode", out["systemMessage"])
        out = self.stop(env={"CLAUDE_CODE_ENTRYPOINT": "sdk-cli", "VERIFY_LOOP_MODE": "enforce"})
        self.assertEqual(out["decision"], "block")
        self.assertEqual(self.stop(env={"CLAUDE_CODE_ENTRYPOINT": "cli"})["decision"], "block")

    def test_mode_off_stop(self):
        self.touch()
        self.write_user_config(mode="off")
        self.assertIsNone(self.stop())

    def test_multi_repo_reason(self):
        other = self.make_repo("other", {"x.py": "x\n"})
        self.touch()
        self.edit("s1", other, "x.py")
        out = self.stop(env=ENFORCE)
        self.assertIn(f"— {self.slug}: 1 files (a.js).", out["reason"])
        self.assertIn("— other: 1 files (x.py).", out["reason"])
        self.assertEqual(out["reason"].count("If the task is finished"), 1)


if __name__ == "__main__":
    unittest.main()
