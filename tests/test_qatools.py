"""Tests for WP5's QA toolbox: bin/pw-mcp, `vloop setup`, `vloop auth status`, capture-auth.mjs.
Run: python3 -m unittest discover -s tests"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import time
import unittest
from importlib.machinery import SourceFileLoader
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from helpers import PW_MCP, SKILL, HermeticCase  # noqa: E402
from vlooplib import cli, paths, qatools  # noqa: E402

FIX = HERE / "fixtures" / "wp5"
BROWSER = os.environ.get("VLOOP_TEST_BROWSER") == "1"


class EnvCase(HermeticCase):
    def setUp(self):
        super().setUp()
        self.state = self.tmp / "state"
        os.environ["VERIFY_STATE_DIR"] = str(self.state)
        os.environ["VERIFY_CONFIG"] = str(self.tmp / "config.json")

    def vloop(self, *argv) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = cli.main(list(argv))
            except SystemExit as e:
                rc = e.code
        return rc, out.getvalue(), err.getvalue()

    def git_repo(self, name: str) -> Path:
        repo = self.tmp / name
        repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "master", str(repo)], check=True)
        return repo


# --------------------------------------------------------------------------- pw-mcp


class PwMcpTest(EnvCase):
    def setUp(self):
        super().setUp()
        # A private copy of the skill layout: bin/pw-mcp copied (SKILL_DIR = its parent), lib linked, fake cli.js.
        self.skill = self.tmp / "skill"
        (self.skill / "bin").mkdir(parents=True)
        shutil.copy2(PW_MCP, self.skill / "bin" / "pw-mcp")
        os.symlink(SKILL / "lib", self.skill / "lib")
        self.cli_js = self.skill / "qa-toolbox" / "node_modules" / "@playwright" / "mcp" / "cli.js"
        self.cli_js.parent.mkdir(parents=True)
        self.cli_js.write_text("// fake\n")
        self.chrome = self.tmp / "Google Chrome.app"
        self.chrome.mkdir()
        os.environ["PW_MCP_CHROME_APP"] = str(self.chrome)
        self.out = self.tmp / "att" / "qa-artifacts"
        self.storage = self.tmp / "auth.json"
        shutil.copy(FIX / "state-ok.json", self.storage)
        self.cwd = self.tmp / "nogit"
        self.cwd.mkdir()

    def pointer(self, storage: str | None) -> Path:
        text = (FIX / "pointer.json").read_text().replace("@OUT@", str(self.out))
        obj = json.loads(text.replace("@STATE@", storage or ""))
        if storage is None:
            obj["storage_state"] = None
        p = self.tmp / "att" / "pw-pointer.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(obj))
        return p

    def run_pw(self, *args, **env) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(self.skill / "bin" / "pw-mcp"), *args], cwd=self.cwd,
                              capture_output=True, text=True, env={**os.environ, **env}, timeout=30)

    def expected(self, browser: str = "chrome", storage: bool = True, out: str | None = None,
                 viewport: str = "1280x720", ignore: bool = True) -> list[str]:
        cmd = ["node", str(self.cli_js), "--headless", "--isolated", "--browser", browser]
        cmd += ["--output-dir", out or str(self.out), "--caps", "devtools,testing", "--console-level", "warning",
                "--viewport-size", viewport, "--image-responses", "omit", "--file-paths", "absolute"]
        if ignore:
            cmd.append("--ignore-https-errors")
        if storage:
            cmd += ["--storage-state", str(self.storage)]
        return cmd

    def test_print_cmd_with_storage_state(self):
        r = self.run_pw("--pointer", str(self.pointer(str(self.storage))), "--print-cmd")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout), self.expected())
        self.assertEqual(r.stderr, "")

    def test_print_cmd_without_storage_state(self):
        r = self.run_pw("--pointer", str(self.pointer(None)), "--print-cmd")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout), self.expected(storage=False))

    def test_storage_state_file_missing_is_omitted(self):
        r = self.run_pw("--pointer", str(self.pointer(str(self.tmp / "gone.json"))), "--print-cmd")
        self.assertEqual(json.loads(r.stdout), self.expected(storage=False))

    def test_print_cmd_without_chrome(self):
        os.environ["PW_MCP_CHROME_APP"] = str(self.tmp / "no-such.app")
        r = self.run_pw("--pointer", str(self.pointer(str(self.storage))), "--print-cmd")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout), self.expected(browser="chromium", storage=True))
        self.assertIn("Chrome", r.stderr)
        self.assertEqual(len(r.stderr.strip().splitlines()), 1)

    def test_missing_cli_js(self):
        self.cli_js.unlink()
        r = self.run_pw("--pointer", str(self.pointer(None)), "--print-cmd")
        self.assertEqual(r.returncode, 1)
        self.assertEqual(r.stdout, "")
        self.assertEqual(r.stderr.strip(), "pw-mcp: Playwright MCP is not installed; run "
                                           f"{self.skill / 'bin' / 'vloop'} setup --install-browser")

    def test_defaults_without_pointer(self):
        r = self.run_pw("--print-cmd", TMPDIR=str(self.tmp / "tmpdir"))
        self.assertEqual(r.returncode, 0, r.stderr)
        cmd = json.loads(r.stdout)
        out = cmd[cmd.index("--output-dir") + 1]
        self.assertTrue(out.startswith(str(self.tmp / "tmpdir" / "verify-pw") + "/"), out)
        self.assertEqual(cmd, self.expected(storage=False, out=out, viewport="1366x900", ignore=False))

    def test_pointer_from_qa_current_by_repo(self):
        repo = self.git_repo("myapp")
        key = __import__("hashlib").sha1(str(repo).encode()).hexdigest()[:12]
        cur = self.state / "qa-current"
        cur.mkdir(parents=True)
        shutil.copy(self.pointer(str(self.storage)), cur / f"{key}.json")
        os.environ["CLAUDE_PROJECT_DIR"] = str(repo)
        r = self.run_pw("--print-cmd")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout), self.expected())

    def test_newest_recent_pointer_fallback(self):
        cur = self.state / "qa-current"
        cur.mkdir(parents=True)
        old = cur / "aaaaaaaaaaaa.json"
        shutil.copy(self.pointer(None), old)
        t = time.time() - 3 * 3600
        os.utime(old, (t, t))
        r = self.run_pw("--print-cmd")
        self.assertNotIn(str(self.out), json.loads(r.stdout))  # too old → defaults
        shutil.copy(self.pointer(str(self.storage)), cur / "bbbbbbbbbbbb.json")
        r = self.run_pw("--print-cmd")
        self.assertEqual(json.loads(r.stdout), self.expected())


# --------------------------------------------------------------------------- setup


class SetupTest(EnvCase):
    def test_setup_prints_one_line_per_item(self):
        os.environ["PW_MCP_CHROME_APP"] = str(self.tmp)
        rc, out, _ = self.vloop("setup", "--check-browser")
        lines = out.strip().splitlines()
        self.assertEqual([ln.split(":")[0] for ln in lines], ["node", "chrome", "playwright-mcp"])
        self.assertTrue(lines[1].startswith("chrome: ok"), lines[1])
        installed = qatools.mcp_cli().is_file()
        self.assertEqual(rc, 0 if installed and lines[0].startswith("node: ok") else 1)

    def test_setup_reports_missing_chrome(self):
        os.environ["PW_MCP_CHROME_APP"] = str(self.tmp / "missing.app")
        rc, out, _ = self.vloop("setup", "--check-browser")
        self.assertIn("chrome: missing", out)
        self.assertEqual(rc, 1)

    def test_toolbox_check_version(self):
        tb = self.tmp / "tb"
        cli_js = qatools.mcp_cli(tb)
        cli_js.parent.mkdir(parents=True)
        cli_js.write_text("")
        (cli_js.parent / "package.json").write_text(json.dumps({"version": "0.0.83"}))
        self.assertEqual(qatools.check_toolbox(tb), (True, "playwright-mcp: ok (0.0.83)"))
        (cli_js.parent / "package.json").write_text(json.dumps({"version": "0.0.70"}))
        self.assertFalse(qatools.check_toolbox(tb)[0])

    def test_toolbox_manifest_pinned(self):
        pkg = json.loads((SKILL / "qa-toolbox" / "package.json").read_text())
        self.assertEqual(pkg, {"name": "verify-loop-qa-toolbox", "private": True, "version": "1.0.0",
                               "description": "Pinned Playwright MCP for the verify-loop QA agent",
                               "dependencies": {"@playwright/mcp": "0.0.83"}})


@unittest.skipUnless(BROWSER, "browser toolbox tests run with VLOOP_TEST_BROWSER=1")
class CaptureScriptTest(HermeticCase):
    def test_node_check(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node not installed")
        r = subprocess.run([node, "--check", str(SKILL / "qa-toolbox" / "capture-auth.mjs")],
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_usage_error_without_url(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node not installed")
        r = subprocess.run([node, str(SKILL / "qa-toolbox" / "capture-auth.mjs"), "--out", "/dev/null"],
                           capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
        self.assertEqual(r.returncode, 2)
        self.assertIn("--url is required", r.stderr)


# --------------------------------------------------------------------------- auth status (needs WP1)


class AuthStatusTest(EnvCase):
    def setUp(self):
        super().setUp()
        self.repo = self.git_repo("myapp")
        self.auth = self.state / "auth"
        self.auth.mkdir(parents=True, mode=0o700)
        self.path = self.auth / "myapp.json"

    def status(self) -> dict:
        rc, out, err = self.vloop("auth", "status", "--repo", str(self.repo), "--json")
        self.assertEqual(rc, 0, err)
        return json.loads(out)

    def test_missing(self):
        st = self.status()
        self.assertEqual(set(st), {"path", "exists", "saved_at", "age_days", "cookies", "origins",
                                   "earliest_expiry", "status"})
        self.assertEqual((st["status"], st["exists"], st["path"]), ("missing", False, str(self.path)))
        rc, out, _ = self.vloop("auth", "status", "--repo", str(self.repo))
        self.assertEqual(out.strip(), f"auth: missing · {self.path}")

    def test_ok(self):
        shutil.copy(FIX / "state-ok.json", self.path)
        st = self.status()
        self.assertEqual((st["status"], st["cookies"], st["origins"], st["age_days"]), ("ok", 2, 1, 0))
        self.assertEqual(st["earliest_expiry"], "2100-01-01")
        rc, out, _ = self.vloop("auth", "status", "--repo", str(self.repo))
        self.assertEqual(out.strip(), f"auth: ok · {self.path} · saved 0 days ago")

    def test_stale(self):
        shutil.copy(FIX / "state-ok.json", self.path)
        t = time.time() - 30 * 86400
        os.utime(self.path, (t, t))
        st = self.status()
        self.assertEqual((st["status"], st["age_days"]), ("stale", 30))
        rc, out, _ = self.vloop("auth", "status", "--repo", str(self.repo))
        self.assertEqual(out.strip(), f"auth: stale · {self.path} · saved 30 days ago")

    def test_expired(self):
        shutil.copy(FIX / "state-expired.json", self.path)
        st = self.status()
        self.assertEqual((st["status"], st["cookies"], st["origins"]), ("expired", 2, 0))
        self.assertEqual(st["earliest_expiry"], "2000-01-01")

    def test_state_summary_never_values(self):
        shutil.copy(FIX / "state-ok.json", self.path)
        s = qatools.state_summary(self.path)
        self.assertEqual(s, {"cookies": 2, "origins": 1, "earliest_expiry": "2100-01-01"})


# --------------------------------------------------------------------------- find_chrome


def make_exe(path: Path, text: str = "#!/bin/sh\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(0o755)
    return path


class FindChromeTest(EnvCase):
    def setUp(self):
        super().setUp()
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        os.environ["PATH"] = str(self.bin)
        self.apps = self.tmp / "Applications"
        self.user_apps = self.home / "Applications"

    def on(self, system: str):
        return mock.patch.object(sys, "platform", system)

    def mac(self):
        return mock.patch.object(qatools, "_mac_roots", return_value=[self.apps, self.user_apps])

    def test_env_override_directory(self):
        app = self.tmp / "Custom Chrome.app"
        app.mkdir()
        os.environ["PW_MCP_CHROME_APP"] = str(app)
        b = qatools.find_chrome()
        self.assertEqual((b.kind, b.executable, b.system), ("chrome", None, True))

    def test_env_override_resolves_mac_app_executable(self):
        app = self.tmp / "Custom Chrome.app"
        exe = make_exe(app / "Contents" / "MacOS" / "Custom Chrome")
        os.environ["PW_MCP_CHROME_APP"] = str(app)
        self.assertEqual(qatools.find_chrome().executable, str(exe))

    def test_env_override_file(self):
        exe = make_exe(self.tmp / "mychrome")
        os.environ["PW_MCP_CHROME_APP"] = str(exe)
        self.assertEqual(qatools.find_chrome().executable, str(exe))

    def test_env_override_missing_means_bundled_chromium(self):
        os.environ["PW_MCP_CHROME_APP"] = str(self.tmp / "gone.app")
        make_exe(self.bin / "google-chrome")
        with self.on("linux"):
            b = qatools.find_chrome()
        self.assertEqual((b.kind, b.system), ("chromium", False))

    def test_macos_prefers_chrome_in_applications(self):
        (self.apps / "Google Chrome.app").mkdir(parents=True)
        (self.apps / "Chromium.app").mkdir()
        (self.user_apps / "Google Chrome.app").mkdir(parents=True)
        with self.on("darwin"), self.mac():
            b = qatools.find_chrome()
        self.assertEqual((b.kind, b.label), ("chrome", str(self.apps / "Google Chrome.app")))

    def test_macos_user_applications(self):
        (self.user_apps / "Google Chrome.app").mkdir(parents=True)
        with self.on("darwin"), self.mac():
            b = qatools.find_chrome()
        self.assertEqual((b.kind, b.label), ("chrome", str(self.user_apps / "Google Chrome.app")))

    def test_macos_chromium_then_edge(self):
        exe = make_exe(self.apps / "Chromium.app" / "Contents" / "MacOS" / "Chromium")
        (self.apps / "Microsoft Edge.app").mkdir()
        with self.on("darwin"), self.mac():
            b = qatools.find_chrome()
            self.assertEqual((b.kind, b.executable), ("chromium", str(exe)))
            shutil.rmtree(self.apps / "Chromium.app")
            self.assertEqual(qatools.find_chrome().kind, "msedge")

    def test_macos_nothing_found(self):
        with self.on("darwin"), self.mac():
            b = qatools.find_chrome()
        self.assertEqual((b.kind, b.system), ("chromium", False))

    def test_linux_chrome_before_chromium(self):
        chromium = make_exe(self.bin / "chromium")
        stable = make_exe(self.bin / "google-chrome-stable")
        with self.on("linux"):
            b = qatools.find_chrome()
            self.assertEqual((b.kind, b.executable), ("chrome", str(stable)))
            stable.unlink()
            b = qatools.find_chrome()
            self.assertEqual((b.kind, b.executable), ("chromium", str(chromium)))

    def test_linux_chromium_browser_and_none(self):
        with self.on("linux"):
            self.assertFalse(qatools.find_chrome().system)
            exe = make_exe(self.bin / "chromium-browser")
            b = qatools.find_chrome()
        self.assertEqual((b.kind, b.executable), ("chromium", str(exe)))

    def test_windows_program_files(self):
        pf, pf86, local = self.tmp / "pf", self.tmp / "pf86", self.tmp / "local"
        os.environ.update({"PROGRAMFILES": str(pf), "PROGRAMFILES(X86)": str(pf86), "LOCALAPPDATA": str(local)})
        edge = make_exe(pf86 / "Microsoft" / "Edge" / "Application" / "msedge.exe")
        with self.on("win32"):
            b = qatools.find_chrome()
            self.assertEqual((b.kind, b.label), ("msedge", str(edge)))
            chrome = make_exe(local / "Google" / "Chrome" / "Application" / "chrome.exe")
            b = qatools.find_chrome()
        self.assertEqual((b.kind, b.label), ("chrome", str(chrome)))

    def test_windows_nothing_found(self):
        os.environ.update({"PROGRAMFILES": str(self.tmp / "none")})
        with self.on("win32"):
            self.assertFalse(qatools.find_chrome().system)

    def test_os_names(self):
        for plat, name in (("darwin", "macos"), ("linux", "linux"), ("win32", "windows"), ("freebsd14", "linux")):
            with self.on(plat):
                self.assertEqual(qatools._os_name(), name)

    def test_check_chrome_lines(self):
        os.environ["PW_MCP_CHROME_APP"] = str(self.tmp)
        self.assertTrue(qatools.check_chrome()[1].startswith("chrome: ok"))
        os.environ["PW_MCP_CHROME_APP"] = str(self.tmp / "gone")
        ok, line = qatools.check_chrome()
        self.assertFalse(ok)
        self.assertIn("chrome: missing", line)


class PwMcpBuildCmdTest(EnvCase):
    def setUp(self):
        super().setUp()
        loader = SourceFileLoader("pw_mcp_under_test", str(PW_MCP))
        self.mod = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
        loader.exec_module(self.mod)
        self.cli_js = Path("/x/cli.js")

    def build(self, browser, cfg=None):
        return self.mod.build_cmd({"output_dir": "/o"}, cfg or {"playwright": {"browser": "chrome"}}, {}, browser,
                                  self.cli_js)

    def flags(self, cmd, flag):
        return cmd[cmd.index(flag) + 1] if flag in cmd else None

    def test_system_chromium_gets_executable_path(self):
        cmd = self.build(qatools.Browser("chromium", "/usr/bin/chromium", "/usr/bin/chromium"))
        self.assertEqual(self.flags(cmd, "--browser"), "chromium")
        self.assertEqual(self.flags(cmd, "--executable-path"), "/usr/bin/chromium")

    def test_bundled_chromium_has_no_executable_path(self):
        cmd = self.build(qatools.Browser("chromium", None, "", system=False))
        self.assertEqual(self.flags(cmd, "--browser"), "chromium")
        self.assertNotIn("--executable-path", cmd)

    def test_standard_chrome_has_no_executable_path(self):
        cmd = self.build(qatools.Browser("chrome", None, "/Applications/Google Chrome.app"))
        self.assertEqual(self.flags(cmd, "--browser"), "chrome")
        self.assertNotIn("--executable-path", cmd)

    def test_edge(self):
        cmd = self.build(qatools.Browser("msedge", None, "C:/edge"))
        self.assertEqual(self.flags(cmd, "--browser"), "msedge")

    def test_configured_browser_wins_over_detection(self):
        cmd = self.build(qatools.Browser("chrome", None, "x"), {"playwright": {"browser": "firefox"}})
        self.assertEqual(self.flags(cmd, "--browser"), "firefox")
        self.assertNotIn("--executable-path", cmd)


# --------------------------------------------------------------------------- install-browser


FAKE_NPM = """#!/bin/sh
echo "$PWD|$*" >> "$FAKE_NPM_LOG"
case "$1" in
  ci) [ -n "$FAKE_NPM_FAIL_CI" ] && exit 1 ;;
  install) [ -n "$FAKE_NPM_FAIL_INSTALL" ] && exit 1 ;;
esac
mkdir -p node_modules/@playwright/mcp
echo '// fake' > node_modules/@playwright/mcp/cli.js
echo '{"version": "0.0.83"}' > node_modules/@playwright/mcp/package.json
"""
FAKE_NODE = """#!/bin/sh
if [ "$1" = "--version" ]; then echo "${FAKE_NODE_VERSION:-v20.1.0}"; exit 0; fi
echo "$*" >> "$FAKE_NODE_LOG"
out=""
while [ $# -gt 0 ]; do
  if [ "$1" = "--out" ]; then out="$2"; fi
  shift
done
[ -n "$out" ] && echo '{"cookies": [], "origins": []}' > "$out"
exit 0
"""


class InstallBrowserTest(EnvCase):
    def setUp(self):
        super().setUp()
        self.bin = self.tmp / "fakebin"
        make_exe(self.bin / "npm", FAKE_NPM)
        make_exe(self.bin / "node", FAKE_NODE)
        os.environ["PATH"] = f"{self.bin}:/usr/bin:/bin"
        self.log = self.tmp / "npm.log"
        os.environ["FAKE_NPM_LOG"] = str(self.log)
        self.dest = self.home / ".local" / "share" / "agent-verify-loop" / "qa-toolbox"
        os.environ["PW_MCP_CHROME_APP"] = str(self.tmp)

    def calls(self) -> list[str]:
        return self.log.read_text().splitlines() if self.log.exists() else []

    def test_installs_into_data_dir_with_npm_ci(self):
        rc, out, err = self.vloop("setup", "--install-browser", "--only", "browser")
        self.assertEqual(rc, 0, out + err)
        for name in ("package.json", "package-lock.json", "capture-auth.mjs"):
            self.assertEqual((self.dest / name).read_bytes(), (SKILL / "qa-toolbox" / name).read_bytes())
        self.assertEqual([c.split("|")[1] for c in self.calls()], ["ci --no-audit --no-fund"])
        self.assertEqual(Path(self.calls()[0].split("|")[0]).resolve(), self.dest.resolve())
        self.assertIn("playwright-mcp: ok (0.0.83)", out)
        self.assertFalse(self.claude_home().exists())

    def claude_home(self) -> Path:
        return self.home / ".claude"

    def test_falls_back_to_npm_install(self):
        os.environ["FAKE_NPM_FAIL_CI"] = "1"
        rc, out, err = self.vloop("setup", "--install-browser", "--only", "browser")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual([c.split("|")[1].split()[0] for c in self.calls()], ["ci", "install"])
        self.assertTrue(qatools.mcp_cli(self.dest).is_file())

    def test_both_npm_commands_failing_is_an_error(self):
        os.environ["FAKE_NPM_FAIL_CI"] = os.environ["FAKE_NPM_FAIL_INSTALL"] = "1"
        rc, out, err = self.vloop("setup", "--install-browser", "--only", "browser")
        self.assertEqual(rc, 1)
        self.assertIn("not installed", err)

    def test_missing_npm(self):
        (self.bin / "npm").unlink()
        os.environ["PATH"] = str(self.bin)
        rc, out, err = self.vloop("setup", "--install-browser", "--only", "browser")
        self.assertEqual(rc, 1)
        self.assertIn("npm", err)
        self.assertFalse(self.dest.exists())

    def test_missing_node(self):
        (self.bin / "node").unlink()
        os.environ["PATH"] = str(self.bin)
        rc, out, err = self.vloop("setup", "--install-browser", "--only", "browser")
        self.assertEqual(rc, 1)
        self.assertIn("Node.js", err)
        self.assertEqual(self.calls(), [])

    def test_old_node(self):
        os.environ["FAKE_NODE_VERSION"] = "v16.0.0"
        rc, _, err = self.vloop("setup", "--install-browser", "--only", "browser")
        self.assertEqual(rc, 1)
        self.assertIn("too old", err)

    def test_dry_run_installs_nothing(self):
        rc, out, _ = self.vloop("setup", "--install-browser", "--only", "browser", "--dry-run")
        self.assertEqual(rc, 0)
        self.assertIn("would install the browser toolbox", out)
        self.assertEqual(self.calls(), [])
        self.assertFalse(self.dest.exists())

    def test_setup_with_install_browser_also_installs_agents(self):
        rc, out, err = self.vloop("setup", "--agent", "claude", "--install-browser")
        self.assertEqual(rc, 0, out + err)
        self.assertTrue((self.home / ".claude" / "agents" / "verify-qa.md").is_file())
        self.assertTrue(qatools.mcp_cli(self.dest).is_file())

    def test_install_browser_alone_installs_only_the_toolbox(self):
        self.claude_home().mkdir(parents=True, exist_ok=True)
        rc, out, err = self.vloop("setup", "--install-browser")
        self.assertEqual(rc, 0, out + err)
        self.assertTrue(qatools.mcp_cli(self.dest).is_file())
        self.assertEqual(list(self.claude_home().iterdir()), [])
        self.assertNotIn("Start a new session", out)

    def test_toolbox_dir_prefers_data_dir(self):
        self.assertEqual(qatools.toolbox_dir(), self.dest)
        self.vloop("setup", "--install-browser", "--only", "browser")
        self.assertEqual(qatools.toolbox_dir(), self.dest)
        self.assertEqual(qatools.installed_toolbox_dir(), self.dest)

    def test_pw_mcp_uses_the_installed_toolbox(self):
        self.vloop("setup", "--install-browser", "--only", "browser")
        r = subprocess.run([sys.executable, str(PW_MCP), "--print-cmd"], capture_output=True, text=True,
                           cwd=self.tmp, env=dict(os.environ), timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        cmd = json.loads(r.stdout)
        self.assertEqual(cmd[:2], ["node", str(qatools.mcp_cli(self.dest))])

    def test_pw_mcp_without_toolbox_points_at_install_browser(self):
        r = subprocess.run([sys.executable, str(PW_MCP), "--print-cmd"], capture_output=True, text=True,
                           cwd=self.tmp, env=dict(os.environ), timeout=30)
        if qatools.mcp_cli(SKILL / "qa-toolbox").is_file():
            self.skipTest("a toolbox is installed in the skill dir")
        self.assertEqual(r.returncode, 1)
        self.assertIn("setup --install-browser", r.stderr)

    def test_auth_capture_runs_the_installed_script(self):
        self.vloop("setup", "--install-browser", "--only", "browser")
        node_log = self.tmp / "node.log"
        os.environ["FAKE_NODE_LOG"] = str(node_log)
        repo = self.git_repo("app")
        out_file = self.tmp / "auth" / "state.json"
        rc, out, err = self.vloop("auth", "capture", "--url", "http://localhost:3000/login", "--repo", str(repo),
                                  "--out", str(out_file))
        self.assertEqual(rc, 0, out + err)
        args = node_log.read_text().split()
        self.assertEqual(args[0], str(self.dest / "capture-auth.mjs"))
        self.assertEqual(args[args.index("--browser") + 1], "chrome")
        self.assertTrue(out_file.is_file())
        self.assertIn("login state saved", out)

    def test_auth_capture_passes_the_system_browser_executable(self):
        self.vloop("setup", "--install-browser", "--only", "browser")
        node_log = self.tmp / "node.log"
        os.environ["FAKE_NODE_LOG"] = str(node_log)
        exe = make_exe(self.tmp / "chromium-bin")
        os.environ["PW_MCP_CHROME_APP"] = str(exe)
        repo = self.git_repo("app")
        rc, out, err = self.vloop("auth", "capture", "--url", "http://x", "--repo", str(repo), "--out",
                                  str(self.tmp / "s.json"))
        self.assertEqual(rc, 0, out + err)
        args = node_log.read_text().split()
        self.assertEqual(args[args.index("--executable-path") + 1], str(exe))

    def test_auth_capture_without_toolbox(self):
        repo = self.git_repo("app")
        if qatools.mcp_cli(SKILL / "qa-toolbox").is_file():
            self.skipTest("a toolbox is installed in the skill dir")
        rc, _, err = self.vloop("auth", "capture", "--url", "http://x", "--repo", str(repo))
        self.assertEqual(rc, 1)
        self.assertIn("setup --install-browser", err)

    def test_install_flag_alias(self):
        rc, out, err = self.vloop("setup", "--install", "--only", "browser")
        self.assertEqual(rc, 0, out + err)
        self.assertTrue(qatools.mcp_cli(self.dest).is_file())


class WritePointerTest(EnvCase):
    def test_pointer_and_mcp_config(self):
        att = self.tmp / "att"
        auth = self.tmp / "auth.json"
        shutil.copy(FIX / "state-ok.json", auth)
        ctx = {"run_id": "r1", "repo": {"key": "abc123abc123"}}
        ptr = qatools.write_pointer(ctx, 2, att, {"playwright": {"viewport": "800x600"}}, {"ignore_https_errors": False},
                                    {"status": "ok", "exists": True, "path": str(auth)})
        self.assertEqual((ptr["run_id"], ptr["attempt"], ptr["viewport"], ptr["storage_state"],
                          ptr["ignore_https_errors"]), ("r1", 2, "800x600", str(auth), False))
        self.assertEqual(ptr["output_dir"], str(att / "qa-artifacts"))
        self.assertEqual(json.loads((att / "pw-pointer.json").read_text()), ptr)
        self.assertEqual(json.loads((self.state / "qa-current" / "abc123abc123.json").read_text()), ptr)
        mcp = json.loads((att / "mcp.json").read_text())
        self.assertEqual(mcp["mcpServers"]["playwright"], {"type": "stdio", "command": str(paths.pw_mcp()),
                                                           "args": ["--pointer", str(att / "pw-pointer.json")]})

    def test_missing_auth_means_no_storage_state(self):
        ctx = {"run_id": "r1", "repo": {"key": "k"}}
        for auth in (None, {"status": "missing", "exists": False, "path": "/p"},
                     {"status": "expired", "exists": True, "path": "/p"}):
            ptr = qatools.write_pointer(ctx, 1, self.tmp / "att", {}, {}, auth)
            self.assertIsNone(ptr["storage_state"])
            self.assertEqual(ptr["viewport"], "1366x900")
            self.assertTrue(ptr["ignore_https_errors"])


if __name__ == "__main__":
    unittest.main()
