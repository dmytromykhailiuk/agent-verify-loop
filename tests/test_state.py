"""WP1 state/config/util: session files, locks, pending, closure, events, run dirs, config merge."""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import helpers  # noqa: E402,F401
sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures" / "wp1"))

from wp1helpers import EnvCase, write  # noqa: E402

from vlooplib import config, gitsnap, state, util  # noqa: E402


class UtilTest(EnvCase):
    def test_now_iso_and_formatting(self):
        self.assertRegex(util.now_iso(), r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")
        self.assertEqual(util.short_dur(45), "45s")
        self.assertEqual(util.short_dur(220), "3m40s")
        self.assertEqual(util.short_dur(3720), "1h02m")
        self.assertEqual(util.fmt_tokens(950), "950")
        self.assertEqual(util.fmt_tokens(82000), "82k")
        self.assertEqual(util.fmt_tokens(1_400_000), "1.4M")
        self.assertEqual(util.fmt_paths(["a", "b", "c", "d", "e"]), "a, b, c, +2 more")
        self.assertEqual(util.slugify("Scenarios Manager!"), "scenarios-manager")
        self.assertEqual(util.slugify("..."), "project")

    def test_render_template(self):
        self.assertEqual(util.render_template("x {{a}} {{ b }} {{c}}", {"a": "1", "b": "", "c": None}),
                         "x 1 (none) (none)")
        with self.assertRaises(KeyError):
            util.render_template("{{missing}}", {})

    def test_globs(self):
        m = util.path_matcher(["*.md", "docs/**", "**/auth/**", "**/package.json"])
        self.assertTrue(m("README.md"))
        self.assertTrue(m("a/b/c.md"))
        self.assertTrue(m("docs/x/y.txt"))
        self.assertTrue(m("src/auth/login.ts"))
        self.assertTrue(m("auth/login.ts"))
        self.assertTrue(m("package.json"))
        self.assertTrue(m("apps/x/package.json"))
        self.assertFalse(m("src/main.ts"))

    def test_atomic_and_read_json(self):
        p = self.tmp / "x" / "y.json"
        util.atomic_write_json(p, {"a": 1})
        self.assertEqual(util.read_json(p), {"a": 1})
        p.write_text("{bad")
        self.assertEqual(util.read_json(p, "dflt"), "dflt")
        self.assertEqual(util.read_json(self.tmp / "nope.json"), None)

    def test_run_and_log_error(self):
        self.assertEqual(util.run(["true"])[0], 0)
        self.assertEqual(util.run(["/nonexistent/bin"])[0], 127)
        self.assertEqual(util.run(["sleep", "5"], timeout=0.2)[0], 124)
        util.log_error("comp", ValueError("boom"))
        line = (self.state / "hook-errors.log").read_text().strip()
        self.assertRegex(line, r"^\S+Z\tcomp\tValueError\('boom'\)$")

    def test_state_dir_mode(self):
        os.environ["VERIFY_STATE_DIR"] = str(self.tmp / "fresh")
        d = util.state_dir()
        self.assertTrue(d.is_dir())
        self.assertEqual(d.stat().st_mode & 0o777, 0o700)
        self.assertTrue((util.skill_dir() / "config" / "defaults.json").is_file())


class ConfigTest(EnvCase):
    def test_merge_and_globs_appended(self):
        root = self.make_repo()
        write(root / ".claude" / "verify.json", json.dumps({
            "ui_globs": ["*.twig"], "floors": {"qa": "checks"}, "mode": "enforce", "unknown": 1,
            "app": {"url": "https://localhost:8100"}}))
        self.write_user_config(mode="enforce", gate={"max_blocks_per_fp": 5})
        cfg = config.load(root)
        self.assertIn("*.twig", cfg["ui_globs"])
        self.assertIn("*.html", cfg["ui_globs"])
        self.assertEqual(cfg["gate"]["max_blocks_per_fp"], 5)
        self.assertEqual(cfg["gate"]["inflight_ttl_min"], 60)
        self.assertEqual(cfg["floors"], {"qa": "checks"})
        self.assertNotIn("unknown", cfg)
        self.assertEqual(cfg["mode"], "enforce")
        self.assertEqual(config.repo_config(root).get("mode"), None)  # not an allowed repo key

    def test_gate_mode_sources(self):
        self.assertEqual(config.gate_mode("cli"), ("warn", "defaults"))
        self.write_user_config(mode="enforce")
        self.assertEqual(config.gate_mode("cli"), ("enforce", "user config"))
        self.assertEqual(config.gate_mode("sdk-cli"), ("warn", "user config"))  # capped
        os.environ["VERIFY_LOOP_MODE"] = "enforce"
        self.assertEqual(config.gate_mode("sdk-cli"), ("enforce", "env"))
        os.environ["VERIFY_LOOP_MODE"] = "off"
        self.assertEqual(config.gate_mode("cli"), ("off", "env"))

    def test_set_user_mode_creates_file(self):
        old, new = config.set_user_mode("enforce")
        self.assertEqual((old, new), ("warn", "enforce"))
        uc = json.loads(self.cfgfile.read_text())
        self.assertEqual(uc["installed_at"], util.today())
        self.assertRegex(uc["enforce_review_after"], r"^\d{4}-\d\d-\d\d$")
        self.assertEqual(config.set_user_mode("off"), ("enforce", "off"))
        with self.assertRaises(ValueError):
            config.set_user_mode("bogus")

    def test_repo_enabled(self):
        root = self.make_repo()
        self.assertTrue(config.repo_enabled(root))
        write(root / ".claude" / "verify.json", '{"enabled": false}')
        self.assertFalse(config.repo_enabled(root))


class StateTest(EnvCase):
    def setUp(self):
        super().setUp()
        self.root = self.make_repo(files={"a.js": "a\n", "b.js": "b\n", "README.md": "# r\n"})
        self.repo = gitsnap.repo_for(self.root)

    def _state_with_touch(self, rels=("a.js",)):
        st = state.new_session("s1", cwd=str(self.root))
        rs = state.repo_state(st, self.repo)
        snap = gitsnap.snapshot(self.repo, "s1")
        rs["baseline"] = {"tree": snap["tree"], "kind": snap["kind"], "at": util.now_iso(), "trigger": "prompt"}
        for r in rels:
            write(self.root / r, "changed\n")
        rs["touched"] = sorted(rels)
        return st, rs

    def test_new_save_load(self):
        st = state.new_session("s1", cwd="/x")
        self.assertEqual(st["task"], {"n": 1, "started_at": None, "prompts": []})
        state.save_session(st)
        self.assertEqual(state.load_session("s1")["cwd"], "/x")
        self.assertEqual(state.session_path("s1").stat().st_mode & 0o777, 0o600)
        self.assertIsNone(state.load_session("nope"))
        rs = state.repo_state(st, self.repo)
        self.assertEqual(rs["loop"]["status"], "idle")
        self.assertEqual(rs["gate"], {"blocks": {}, "defer": None, "last_warn_fp": None})
        self.assertIsNone(state.repo_state(st, {"key": "zz", "root": "/", "slug": "z"}, create=False))
        self.assertEqual(state.repo_from_state(st, self.repo["key"])["common_dir"], self.repo["common_dir"])

    def test_session_lock_timeout(self):
        with state.session_lock("s1"):
            errs = []

            def other():
                try:
                    with state.session_lock("s1", timeout=0.2):
                        pass
                except TimeoutError as e:
                    errs.append(e)

            t = threading.Thread(target=other)
            t.start()
            t.join()
        self.assertEqual(len(errs), 1)
        with state.session_lock("s1", timeout=0.2):
            pass

    def test_pending_and_revert(self):
        st, rs = self._state_with_touch(["a.js"])
        self.assertEqual(state.pending_keys(st), [self.repo["key"]])
        write(self.root / "a.js", "a\n")
        self.assertEqual(state.pending_keys(st), [])
        rs["touched"] = []
        self.assertEqual(state.pending_keys(st), [])

    def test_combined_fp(self):
        st, rs = self._state_with_touch(["a.js"])
        fp1 = state.combined_fp(st, [self.repo["key"]])
        self.assertEqual(fp1, state.combined_fp(st, [self.repo["key"]]))
        write(self.root / "a.js", "again\n")
        self.assertNotEqual(fp1, state.combined_fp(st, [self.repo["key"]]))

    def test_close_without_tree(self):
        st, rs = self._state_with_touch(["a.js"])
        rs["gate"]["blocks"] = {"x": 2}
        rs["gate"]["defer"] = {"fp": "x"}
        st["task"]["prompts"] = [{"ts": "t", "text": "p"}]
        state.close_loop(st, self.repo["key"], "skipped", "user", tree=None)
        self.assertEqual(state.pending_keys(st), [])
        self.assertEqual(rs["gate"]["blocks"], {})
        self.assertIsNone(rs["gate"]["defer"])
        self.assertEqual(rs["loop"]["status"], "skipped")
        self.assertEqual(st["task"]["n"], 2)
        self.assertEqual(st["task"]["prompts"], [])
        write(self.root / "a.js", "later\n")  # a new change after the closure is pending again
        self.assertEqual(state.pending_keys(st), [self.repo["key"]])
        self.assertEqual(state.read_events("s1")[-1]["event"], "close")
        self.assertEqual(state.read_events("s1")[-1]["data"]["with_tree"], False)

    def test_close_with_tree(self):
        st, rs = self._state_with_touch(["a.js", "b.js"])
        t = gitsnap.snapshot(self.repo, "s1")["tree"]
        write(self.root / "b.js", "after attempt\n")
        state.close_loop(st, self.repo["key"], "passed", "PASS", tree=t, retro="pending")
        self.assertEqual(rs["baseline"]["tree"], t)
        self.assertEqual(rs["baseline"]["trigger"], "closure")
        self.assertEqual(rs["touched"], ["b.js"])  # only paths changed since the verified tree
        self.assertIsNone(rs["closed_fp"])
        self.assertEqual(rs["loop"]["retro"], "pending")
        self.assertEqual(state.pending_keys(st), [self.repo["key"]])

    def test_events(self):
        state.append_event("s1", "touch", repo="k", paths=["a"], source="edit", child_session=None)
        line = (self.state / "sessions" / "s1.events.jsonl").read_text().strip()
        ev = json.loads(line)
        self.assertEqual(set(ev), {"ts", "sid", "event", "repo", "run", "attempt", "data"})
        self.assertEqual(ev["data"]["paths"], ["a"])

    def test_runs(self):
        rid = state.new_run_id("Scenarios Manager With A Long Name")
        self.assertRegex(rid, r"^\d{8}-\d{6}-scenarios-manager-wi-[0-9a-f]{4}$")
        self.assertRegex(state.new_run_id("x", kind="retro"), r"^retro-\d{8}-\d{6}-[0-9a-f]{4}$")
        d = state.run_dir(rid)
        self.assertTrue(d.is_dir())
        self.assertIsNone(state.load_context(rid))
        with state.run_lock(rid):
            state.save_context({"run_id": rid, "status": "open"})
        self.assertEqual(state.load_context(rid)["status"], "open")


if __name__ == "__main__":
    unittest.main()
