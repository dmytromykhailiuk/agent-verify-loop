"""WP1 admin subcommands: skip reset defer status pending mode repo-config gc, and the dispatcher."""
from __future__ import annotations

import json
import os
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import helpers  # noqa: E402,F401
sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures" / "wp1"))

from wp1helpers import EnvCase, git, write  # noqa: E402

from vlooplib import util  # noqa: E402

S1 = {"CLAUDE_CODE_SESSION_ID": "s1"}


class AdminTest(EnvCase):
    def setUp(self):
        super().setUp()
        self.root = self.make_repo(files={"a.js": "a\n", "b.js": "b\n", "package.json":
                                          '{"scripts": {"test": "jest", "lint": "eslint .", "typecheck": "tsc"}}'})
        self.slug = util.slugify(self.root.name)

    def touch(self, sid="s1", *rels):
        self.hook("prompt", self.p_prompt(sid, self.root))
        for r in rels or ("a.js",):
            self.edit(sid, self.root, r)

    def rs(self, sid="s1"):
        return next(iter(self.session(sid)["repos"].values()))

    def test_skip(self):
        self.touch("s1", "a.js", "b.js")
        p = self.vloop("skip", "--reason", "user: no need", env=S1)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stdout.strip(),
                         f"verify-loop · skipped by user: user: no need · {self.slug}: 2 files UNVERIFIED")
        self.assertEqual(self.rs()["loop"]["status"], "skipped")
        ev = [e for e in self.events("s1") if e["event"] == "skip"][0]
        self.assertEqual(ev["data"], {"reason": "user: no need", "by": "user"})
        self.assertIsNone(self.hook_json("stop", self.p_stop("s1", self.root), env={"VERIFY_LOOP_MODE": "enforce"}))
        self.assertEqual(self.vloop("skip", "--reason", "x", env=S1).stdout.strip(), "verify-loop · nothing pending")

    def test_skip_with_loop_tree(self):
        from vlooplib import gitsnap, state

        self.touch("s1", "a.js")
        repo = gitsnap.repo_for(self.root)
        tree = gitsnap.snapshot(repo, "s1")["tree"]
        state.run_dir("r1")
        state.save_context({"run_id": "r1", "status": "open", "attempts": [{"n": 1, "tree": tree}]})
        p = self.state / "sessions" / "s1.json"
        st = json.loads(p.read_text())
        next(iter(st["repos"].values()))["loop"].update(run_id="r1", status="open", attempt=1)
        p.write_text(json.dumps(st))
        write(self.root / "b.js", "after begin\n")
        self.edit("s1", self.root, "b.js")
        self.vloop("skip", "--reason", "stop", env=S1)
        rs = self.rs()
        self.assertEqual(rs["baseline"]["tree"], tree)
        self.assertEqual(rs["touched"], ["b.js"])  # edits after the attempt tree stay tracked
        self.assertEqual(state.load_context("r1")["status"], "skipped")

    def test_reset(self):
        self.touch()
        p = self.vloop("reset", env=S1)
        self.assertEqual(p.stdout.strip(), f"verify-loop · reset · {self.slug}")
        self.assertEqual(self.rs()["loop"]["status"], "reset")
        p = self.vloop("reset", "--repo", str(self.root), env=S1)
        self.assertEqual(p.stdout.strip(), f"verify-loop · reset · {self.slug}")
        p = self.vloop("reset", "--repo", str(self.tmp), env=S1)
        self.assertEqual(p.returncode, 2)
        self.assertTrue(p.stderr.startswith("vloop: error: "))

    def test_defer_nothing_pending(self):
        self.assertEqual(self.vloop("defer", "--reason", "x", env=S1).stdout.strip(),
                         "verify-loop · nothing pending to defer")
        p = self.vloop("defer", "--reason", "x", env={"CLAUDE_CODE_SESSION_ID": None})
        self.assertEqual(p.returncode, 2)
        self.assertIn("vloop: error: no session", p.stderr)

    def test_status_and_pending(self):
        p = self.vloop("status", env=S1)
        self.assertEqual(p.stdout.splitlines()[0], "verify-loop · gate: warn (defaults) · session s1")
        self.assertEqual(self.vloop("pending", env=S1).stdout.strip(), "none")
        self.touch()
        self.vloop("mode", "warn")
        lines = self.vloop("status", env=S1).stdout.splitlines()
        self.assertRegex(lines[0], r"^verify-loop · gate: warn \(user config; since \d{4}-\d\d-\d\d, 0 days\) · "
                                   r"session s1$")
        self.assertEqual(lines[1], f"{self.slug}: 1 touched files · pending · loop idle")
        p = self.state / "sessions" / "s1.json"
        st = json.loads(p.read_text())
        next(iter(st["repos"].values()))["loop"].update(
            run_id="r1", status="open", attempt=2, failed_attempts=1, inflight_since=util.now_iso(),
            last_status_line="verify-loop · attempt 1 · failures 1/3 · review FAIL (1 major)")
        p.write_text(json.dumps(st))
        lines = self.vloop("status", "--session", "s1").stdout.splitlines()
        self.assertRegex(lines[1], rf"^{self.slug}: 1 touched files · pending · loop open · run r1 · attempt 2 · "
                                   r"failures 1/3 · inflight since \d\d:\d\d$")
        self.assertEqual(lines[2], "  last: verify-loop · attempt 1 · failures 1/3 · review FAIL (1 major)")
        js = json.loads(self.vloop("status", "--json", env=S1).stdout)
        self.assertEqual(len(js["pending"]), 1)
        self.assertIn(js["pending"][0], js["repos"])
        self.assertEqual(self.vloop("pending", env=S1).stdout.strip(), f"{self.root}\t1\topen")
        self.assertEqual(json.loads(self.vloop("pending", "--json", env=S1).stdout)[0]["touched"], 1)

    def test_mode(self):
        self.assertEqual(self.vloop("mode").stdout.strip(), "gate: warn (source: defaults)")
        self.assertEqual(self.vloop("mode", "enforce").stdout.strip(), "gate: warn → enforce")
        uc = json.loads(self.cfgfile.read_text())
        self.assertEqual(uc["mode"], "enforce")
        out = self.vloop("mode").stdout.strip()
        self.assertRegex(out, r"^gate: enforce \(source: user config\) · installed \d{4}-\d\d-\d\d \(0 days\) · "
                              r"review after \d{4}-\d\d-\d\d$")
        self.assertTrue(self.vloop("mode", env={"VERIFY_LOOP_MODE": "off"}).stdout.startswith("gate: off (source: env)"))
        self.assertEqual(self.vloop("mode", "bogus").returncode, 2)

    def test_repo_config(self):
        p = self.vloop("repo-config", "init", "--repo", str(self.root))
        path = self.root / ".agent-verify-loop.json"
        self.assertEqual(p.stdout.strip(), str(path))
        cfg = json.loads(path.read_text())
        self.assertEqual(cfg["enabled"], True)
        self.assertEqual(cfg["commands"], {"typecheck": "npm run typecheck", "lint": "npm run lint",
                                           "test": "npm test", "build": None})
        self.assertEqual(cfg["app"], {"start": None, "url": None})
        self.vloop("repo-config", "init", "--repo", str(self.root))
        exclude = (self.root / ".git" / "info" / "exclude").read_text()
        self.assertEqual(exclude.splitlines().count(".agent-verify-loop.json"), 1)
        self.assertEqual(git(self.root, "status", "--porcelain"), "")
        shown = json.loads(self.vloop("repo-config", "show", "--repo", str(self.root)).stdout)
        self.assertEqual(shown["commands"]["test"], "npm test")
        self.assertEqual(shown["mode"], "warn")
        self.assertEqual(self.vloop("repo-config", "init", "--repo", str(self.tmp / "nogit")).returncode, 2)

    def test_repo_config_keeps_legacy_file(self):
        legacy = self.root / ".claude" / "verify.json"
        write(legacy, json.dumps({"enabled": True, "notes": "legacy"}))
        p = self.vloop("repo-config", "init", "--repo", str(self.root))
        self.assertEqual(p.stdout.strip(), str(legacy))
        self.assertFalse((self.root / ".agent-verify-loop.json").exists())
        shown = json.loads(self.vloop("repo-config", "show", "--repo", str(self.root)).stdout)
        self.assertEqual(shown["notes"], "legacy")

    def test_gc(self):
        old = time.time() - 30 * 86400

        def age(p: Path, t=old):
            for dirpath, dirs, files in os.walk(p, topdown=False) if p.is_dir() else [(str(p.parent), [], [p.name])]:
                for n in files + dirs:
                    os.utime(os.path.join(dirpath, n), (t, t))
            os.utime(p, (t, t))

        write(self.state / "runs" / "old-run" / "context.json", "{}")
        write(self.state / "runs" / "new-run" / "context.json", "{}")
        for suf in (".json", ".lock", ".events.jsonl"):
            write(self.state / "sessions" / f"olds{suf}", "{}")
            age(self.state / "sessions" / f"olds{suf}")
        write(self.state / "sessions" / "news.json", "{}")
        write(self.state / "snap" / "oldkey" / "objects" / "x", "x")
        write(self.state / "debug" / "prompt-1.json", "{}")
        write(self.state / "auth" / "app.json", "{}")
        write(self.state / "ledger.jsonl", "{}\n")
        for p in (self.state / "runs" / "old-run", self.state / "snap" / "oldkey", self.state / "debug" / "prompt-1.json",
                  self.state / "auth" / "app.json", self.state / "ledger.jsonl"):
            age(p)
        dry = self.vloop("gc", "--dry-run")
        self.assertIn("would remove", dry.stdout)
        self.assertTrue((self.state / "runs" / "old-run").exists())
        p = self.vloop("gc")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue(p.stdout.strip().splitlines()[-1].startswith("gc: removed 6 item(s)"))
        self.assertFalse((self.state / "runs" / "old-run").exists())
        self.assertTrue((self.state / "runs" / "new-run").exists())
        self.assertFalse((self.state / "sessions" / "olds.json").exists())
        self.assertFalse((self.state / "sessions" / "olds.events.jsonl").exists())
        self.assertTrue((self.state / "sessions" / "news.json").exists())
        self.assertFalse((self.state / "snap" / "oldkey").exists())
        self.assertFalse((self.state / "debug" / "prompt-1.json").exists())
        self.assertTrue((self.state / "auth" / "app.json").exists())
        self.assertTrue((self.state / "ledger.jsonl").exists())
        self.assertIn("removed", self.vloop("gc", "--older-than", "0").stdout)

    def test_dispatcher(self):
        p = self.vloop("--help")
        self.assertEqual(p.returncode, 0)
        for cmd in ("hook", "guard", "defer", "skip", "reset", "status", "pending", "mode", "repo-config", "gc"):
            self.assertIn(cmd, p.stdout)
        self.assertEqual(self.vloop("hook", "--help").returncode, 0)
        self.assertEqual(self.vloop("nonsense").returncode, 2)
        import contextlib
        import io

        from vlooplib import cli

        with contextlib.redirect_stdout(io.StringIO()) as buf:
            self.assertEqual(cli.main(["mode"]), 0)
        self.assertTrue(buf.getvalue().startswith("gate: warn"))
        os.environ["VLOOP_STRICT"] = "0"
        cli.build_parser()  # missing WP2/WP4/WP5 modules are skipped silently


if __name__ == "__main__":
    unittest.main()
