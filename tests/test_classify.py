"""WP1 classify: summary, prefilter table, material delta, project facts, login-state status."""
from __future__ import annotations

import json
import os
import shutil
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import helpers  # noqa: E402,F401
sys.path.insert(0, str(Path(__file__).resolve().parent / "fixtures" / "wp1"))

from wp1helpers import FIXTURES, EnvCase, write  # noqa: E402

from vlooplib import classify, config  # noqa: E402


def row(path, status="M", added=1, removed=0, binary=False, old=None):
    return {"path": path, "old_path": old, "status": status, "added": added, "removed": removed, "binary": binary}


def plan(scope="checks", tier="standard"):
    return {"roles": {"reviewer": {"tier": tier}, "qa": {"scope": scope}}}


class SummaryTest(EnvCase):
    def setUp(self):
        super().setUp()
        self.cfg = config.load()

    def test_summarize_flags(self):
        s = classify.summarize([
            row("src/publisher.ts", added=30, removed=10),
            row("src/publisher.spec.ts", added=10, removed=2),
            row("src/new.ts", "A", 5),
            row("README.md", added=3),
            row("package-lock.json", added=900, removed=800),
            row("src/auth/login.ts", added=2),
            row("app/page.component.html", added=1),
            row("img.png", "A", binary=True),
            row("src/old.ts", "D", removed=7),
            row("src/c.ts", "R", old="src/b.ts"),
            row("tsconfig.json"),
        ], self.cfg)
        flags = {r["path"]: r["flags"] for r in s["rows"]}
        self.assertEqual(flags["src/publisher.ts"], ["source"])
        self.assertEqual(flags["src/publisher.spec.ts"], ["test"])
        self.assertEqual(flags["src/new.ts"], ["new", "source"])
        self.assertEqual(flags["README.md"], ["docs"])
        self.assertEqual(flags["package-lock.json"], ["ignored"])
        self.assertIn("risky", flags["src/auth/login.ts"])
        self.assertIn("ui", flags["app/page.component.html"])
        self.assertIn("config", flags["tsconfig.json"])
        self.assertEqual(s["files"], 11)
        self.assertEqual(s["changed_lines"], s["added"] + s["removed"])
        self.assertEqual(s["changed_lines"], 30 + 10 + 10 + 2 + 5 + 3 + 2 + 1 + (1 + 7) + 1 + 1)  # no lockfile, no binary
        self.assertEqual(s["new_source_files"], ["src/new.ts", "img.png"])
        self.assertEqual(s["deleted_files"], ["src/old.ts"])
        self.assertEqual(s["renamed_files"], ["src/c.ts"])
        self.assertEqual(s["ignored_files"], ["package-lock.json"])
        self.assertFalse(s["docs_only"])
        self.assertTrue(classify.summarize([row("a.md"), row("docs/x.txt")], self.cfg)["docs_only"])
        self.assertFalse(classify.summarize([], self.cfg)["docs_only"])

    def test_prefilter_table(self):
        pf = lambda rows, ws=False: classify.prefilter(classify.summarize(rows, self.cfg), ws, self.cfg)  # noqa: E731
        self.assertEqual(pf([]), {"decision": "skip", "reason": "no changes vs baseline (reverted)"})
        self.assertEqual(pf([row("a.md"), row("b.md"), row("c.md"), row("d.md"), row("e.md")]),
                         {"decision": "skip", "reason": "docs-only change (a.md, b.md, c.md, +2 more)"})
        self.assertEqual(pf([row("a.js", added=50)], ws=True), {"decision": "skip", "reason": "whitespace-only change"})
        self.assertEqual(pf([row("a.js", added=4, removed=2)]),
                         {"decision": "trivial", "reason": "trivial change: 6 lines in 1 file(s)"})
        self.assertEqual(pf([row("a.js", added=10), row("b.js")])["decision"], "triage")  # 11 lines
        # calibration (WP7 note #6): a loop rewrite plus a new test (B3, +9/-1) must reach triage
        self.assertEqual(pf([row("a.js", added=5, removed=2)])["decision"], "triage")  # 7 lines > 6
        self.assertEqual(pf([row("lib/sum.js", added=1, removed=1), row("test/sum.test.js", added=2)])["decision"],
                         "triage")  # touches a test file
        self.assertEqual(pf([row("a.js"), row("b.js"), row("c.js")])["decision"], "triage")  # 3 files
        for special in (row("n.js", "A"), row("d.js", "D"), row("r.js", "R", old="q.js"), row("x.html"),
                        row("src/auth/x.ts"), row("tsconfig.json")):
            self.assertEqual(pf([special]), {"decision": "triage", "reason": "triage"}, special)

    def test_material_delta_rules(self):
        first = {"changed_lines": 100}
        s = lambda rows: classify.summarize(rows, self.cfg)  # noqa: E731
        small = s([row("src/a.ts", added=5)])
        self.assertEqual(classify.material_delta(small, first, plan(), self.cfg)[0], False)
        self.assertIn("delta 5 lines in 1 file(s)", classify.material_delta(small, first, plan(), self.cfg)[1])
        ui = s([row("x.component.html")])
        self.assertTrue(classify.material_delta(ui, first, plan("checks"), self.cfg)[0])  # rule 1
        self.assertFalse(classify.material_delta(ui, first, plan("browser"), self.cfg)[0])
        risky = s([row("src/auth/a.ts")])
        self.assertTrue(classify.material_delta(risky, first, plan(tier="standard"), self.cfg)[0])  # rule 2
        self.assertFalse(classify.material_delta(risky, first, plan(tier="deep"), self.cfg)[0])
        new = s([row("src/n.ts", "A")])
        ok, why = classify.material_delta(new, first, plan(), self.cfg)  # rule 3
        self.assertTrue(ok)
        self.assertIn("src/n.ts", why)
        big = s([row("src/a.ts", added=81)])
        self.assertTrue(classify.material_delta(big, first, plan(), self.cfg)[0])  # rule 4: > max(80, 50)
        self.assertFalse(classify.material_delta(big, {"changed_lines": 400}, plan(), self.cfg)[0])  # > 200 needed
        self.assertTrue(classify.material_delta(s([row("src/a.ts", added=201)]), {"changed_lines": 400}, plan(),
                                                self.cfg)[0])

    def test_docs_only_paths(self):
        self.assertTrue(classify.docs_only_paths(["a.md", "docs/x.png"], self.cfg))
        self.assertFalse(classify.docs_only_paths(["a.md", "b.js"], self.cfg))
        self.assertFalse(classify.docs_only_paths([], self.cfg))


class ProjectTest(EnvCase):
    def proj(self, name) -> Path:
        dst = self.tmp / name
        shutil.copytree(FIXTURES / "projects" / name, dst)
        return dst

    def test_web(self):
        root = self.proj("web")
        write(root / ".claude" / "verify.json", json.dumps({
            "commands": {"test": "npx jest --findRelatedTests {files}"},
            "app": {"url": "https://localhost:8100", "start": "npm run dev"},
            "auth": {"login_url": "https://localhost:8100/login"}, "notes": "tenant t1"}))
        write(root / ".claude" / "skills" / "run-app" / "SKILL.md", "x")
        p = classify.detect_project(str(root), config.load(root))
        self.assertEqual(p["app_kind"], "web")
        self.assertEqual(p["app_url"], "https://localhost:8100")
        self.assertEqual(p["start_cmd"], "npm run dev")
        self.assertEqual(p["commands"], {"typecheck": None, "lint": None,
                                         "test": "npx jest --findRelatedTests {files}", "build": None})
        self.assertEqual(p["scripts"], ["dev", "test", "lint"])
        self.assertEqual(p["test_frameworks"], ["jest", "cypress"])
        self.assertEqual(p["recipes"], [".claude/skills/run-app/SKILL.md"])
        self.assertTrue(p["has_verify_json"])
        self.assertEqual(p["auth"]["login_url"], "https://localhost:8100/login")
        self.assertEqual(p["auth"]["storage_state"], str(self.state / "auth" / "web.json"))
        self.assertEqual(p["notes"], "tenant t1")
        for k in ("app_kind", "app_url", "start_cmd", "reuse_running", "ignore_https_errors", "start_timeout_s",
                  "commands", "scripts", "recipes", "test_frameworks", "has_verify_json", "auth", "notes"):
            self.assertIn(k, p)

    def test_server_cli_library(self):
        p = classify.detect_project(str(self.proj("server")), config.load())
        self.assertEqual(p["app_kind"], "server")
        self.assertEqual(p["test_frameworks"], ["node:test"])
        self.assertIsNone(p["app_url"])
        self.assertEqual(p["commands"]["test"], None)  # no guessing
        self.assertFalse(p["has_verify_json"])
        self.assertEqual(classify.detect_project(str(self.proj("pyserver")), config.load())["app_kind"], "server")
        c = classify.detect_project(str(self.proj("cli")), config.load())
        self.assertEqual((c["app_kind"], c["test_frameworks"]), ("cli", ["vitest"]))
        lib = classify.detect_project(str(self.proj("library")), config.load())
        self.assertEqual((lib["app_kind"], lib["test_frameworks"]), ("library", ["pytest"]))
        empty = self.tmp / "empty"
        empty.mkdir()
        self.assertEqual(classify.detect_project(str(empty), config.load())["app_kind"], "unknown")


class AuthStatusTest(EnvCase):
    def setUp(self):
        super().setUp()
        self.root = self.tmp / "myapp"
        self.root.mkdir()
        self.dst = self.state / "auth" / "myapp.json"

    def put(self, name):
        self.dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(FIXTURES / "auth" / name, self.dst)

    def test_missing(self):
        a = classify.auth_status(str(self.root), config.load())
        self.assertEqual((a["status"], a["exists"], a["path"]), ("missing", False, str(self.dst)))

    def test_ok_no_values(self):
        self.put("ok.json")
        a = classify.auth_status(str(self.root), config.load())
        self.assertEqual(a["status"], "ok")
        self.assertEqual((a["cookies"], a["origins"], a["age_days"]), (2, 1, 0))
        self.assertEqual(a["earliest_expiry"], "2100-01-01")
        self.assertNotIn("SECRET", json.dumps(a))

    def test_stale(self):
        self.put("ok.json")
        old = time.time() - 30 * 86400
        os.utime(self.dst, (old, old))
        a = classify.auth_status(str(self.root), config.load())
        self.assertEqual((a["status"], a["age_days"]), ("stale", 30))

    def test_expired(self):
        self.put("expired.json")
        a = classify.auth_status(str(self.root), config.load())
        self.assertEqual(a["status"], "expired")
        self.assertEqual(a["earliest_expiry"], "2020-09-13")

    def test_repo_storage_state_override(self):
        custom = self.tmp / "custom.json"
        shutil.copy(FIXTURES / "auth" / "ok.json", custom)
        write(self.root / ".claude" / "verify.json", json.dumps({"auth": {"storage_state": str(custom)}}))
        a = classify.auth_status(str(self.root), config.load(self.root))
        self.assertEqual((a["path"], a["status"]), (str(custom), "ok"))


if __name__ == "__main__":
    unittest.main()
