"""WP1 foundation: paths, version, platform/transport/vault interfaces, CLI stubs."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import SKILL, VLOOP, HermeticCase  # noqa: E402
from vlooplib import CONTRACT, NAME, VERSION, cli, config, paths, platforms, transports  # noqa: E402

PATH_ENV = ("VERIFY_STATE_DIR", "VERIFY_CONFIG", "XDG_STATE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
            "CLAUDE_CONFIG_DIR", "CODEX_HOME", "CLAUDE_PLUGIN_DATA", "CLAUDE_PLUGIN_ROOT")


class PathsCase(HermeticCase):
    def setUp(self):
        super().setUp()
        self._skill_dir = paths.SKILL_DIR
        for k in PATH_ENV:
            os.environ.pop(k, None)

    def tearDown(self):
        paths.SKILL_DIR = self._skill_dir
        super().tearDown()

    def run_vloop(self, *args, exe: Path = VLOOP, stdin: str = "") -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(exe), *args], input=stdin, capture_output=True, text=True,
                              env=dict(os.environ), timeout=60)


class HomeTest(PathsCase):
    def test_home_is_hermetic(self):
        self.assertEqual(Path.home(), self.home)

    def test_claude_home(self):
        self.assertEqual(paths.claude_home(), self.home / ".claude")
        os.environ["CLAUDE_CONFIG_DIR"] = str(self.tmp / "cc")
        self.assertEqual(paths.claude_home(), self.tmp / "cc")

    def test_codex_home(self):
        self.assertEqual(paths.codex_home(), self.home / ".codex")
        os.environ["CODEX_HOME"] = str(self.tmp / "cx")
        self.assertEqual(paths.codex_home(), self.tmp / "cx")

    def test_state_dir(self):
        self.assertEqual(paths.state_dir(), self.home / ".local" / "state" / "agent-verify-loop")
        os.environ["XDG_STATE_HOME"] = str(self.tmp / "xs")
        self.assertEqual(paths.state_dir(), self.tmp / "xs" / "agent-verify-loop")
        os.environ["VERIFY_STATE_DIR"] = str(self.tmp / "vs")
        self.assertEqual(paths.state_dir(), self.tmp / "vs")

    def test_user_config_path(self):
        self.assertEqual(paths.user_config_path(), self.home / ".config" / "agent-verify-loop" / "config.json")
        os.environ["XDG_CONFIG_HOME"] = str(self.tmp / "xc")
        self.assertEqual(paths.user_config_path(), self.tmp / "xc" / "agent-verify-loop" / "config.json")
        os.environ["VERIFY_CONFIG"] = str(self.tmp / "c.json")
        self.assertEqual(paths.user_config_path(), self.tmp / "c.json")
        self.assertEqual(config.user_config_path(), self.tmp / "c.json")

    def test_data_dir(self):
        self.assertEqual(paths.data_dir(), self.home / ".local" / "share" / "agent-verify-loop")
        os.environ["XDG_DATA_HOME"] = str(self.tmp / "xd")
        self.assertEqual(paths.data_dir(), self.tmp / "xd" / "agent-verify-loop")
        os.environ["CLAUDE_PLUGIN_DATA"] = str(self.tmp / "pd")
        self.assertEqual(paths.data_dir(), self.tmp / "xd" / "agent-verify-loop")
        os.environ["CLAUDE_PLUGIN_ROOT"] = str(self.tmp / "elsewhere")
        self.assertEqual(paths.data_dir(), self.tmp / "xd" / "agent-verify-loop")
        os.environ["CLAUDE_PLUGIN_ROOT"] = str(SKILL.parent.parent)
        self.assertEqual(paths.data_dir(), self.tmp / "pd")

    def test_env_read_at_call_time(self):
        os.environ["VERIFY_STATE_DIR"] = str(self.tmp / "a")
        first = paths.state_dir()
        os.environ["VERIFY_STATE_DIR"] = str(self.tmp / "b")
        self.assertNotEqual(first, paths.state_dir())

    def test_tilde(self):
        self.assertEqual(paths.tilde(self.home / "x" / "y"), "~/x/y")
        self.assertEqual(paths.tilde(self.home), "~")
        self.assertEqual(paths.tilde(str(self.home) + "other/z"), str(self.home) + "other/z")
        self.assertEqual(paths.tilde("/opt/vloop"), "/opt/vloop")


class SkillPathsTest(PathsCase):
    def test_bundled_paths(self):
        self.assertEqual(paths.skill_dir(), SKILL)
        self.assertEqual(paths.cli(), SKILL / "bin" / "vloop")
        self.assertEqual(paths.pw_mcp(), SKILL / "bin" / "pw-mcp")
        for fn, name in ((paths.prompts_dir, "prompts"), (paths.schema_dir, "schema"),
                         (paths.config_dir, "config"), (paths.agents_src_dir, "agents")):
            self.assertEqual(fn(), SKILL / name)
            self.assertTrue(fn().is_dir(), name)

    def test_skill_dir_override_moves_cli(self):
        paths.SKILL_DIR = self.tmp / "inst"
        self.assertEqual(paths.cli(), self.tmp / "inst" / "bin" / "vloop")
        self.assertEqual(paths.pw_mcp(), self.tmp / "inst" / "bin" / "pw-mcp")
        self.assertEqual(paths.config_dir(), SKILL / "config")

    def test_repo_config_paths_order(self):
        root = self.tmp / "r"
        self.assertEqual(paths.repo_config_paths(root), [root / ".agent-verify-loop.local.json",
                                                         root / ".agent-verify-loop.json",
                                                         root / ".claude" / "verify.json"])

    def test_repo_config_precedence(self):
        root = self.tmp / "r"
        (root / ".claude").mkdir(parents=True)
        (root / ".claude" / "verify.json").write_text(json.dumps({"notes": "legacy", "app": {"url": "L", "start": "s"}}))
        self.assertEqual(config.repo_config(root)["notes"], "legacy")
        (root / ".agent-verify-loop.json").write_text(json.dumps({"notes": "shared", "app": {"url": "S"}}))
        (root / ".agent-verify-loop.local.json").write_text(json.dumps({"app": {"url": "local"}, "bogus": 1}))
        rc = config.repo_config(root)
        self.assertEqual(rc["notes"], "shared")
        self.assertEqual(rc["app"], {"url": "local", "start": "s"})
        self.assertNotIn("bogus", rc)
        self.assertEqual(config.repo_config_label(root),
                         ".agent-verify-loop.local.json, .agent-verify-loop.json, .claude/verify.json")


class VersionCliTest(PathsCase):
    def test_version_json(self):
        p = self.run_vloop("version", "--json")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(p.stdout), {"name": "agent-verify-loop", "version": VERSION, "contract": 1,
                                                "skill_dir": str(SKILL), "cli": str(VLOOP)})
        self.assertEqual((NAME, CONTRACT), ("agent-verify-loop", 1))

    def test_version_plain(self):
        p = self.run_vloop("version")
        self.assertEqual(p.stdout.strip(), f"agent-verify-loop {VERSION} (contract 1) — {VLOOP}")

    def test_symlinked_invocation_sets_skill_dir(self):
        link = self.home / ".agents" / "skills" / "verify-loop"
        link.parent.mkdir(parents=True)
        os.symlink(SKILL, link)
        p = self.run_vloop("version", "--json", exe=link / "bin" / "vloop")
        self.assertEqual(p.returncode, 0, p.stderr)
        info = json.loads(p.stdout)
        self.assertEqual(info["skill_dir"], str(link))
        self.assertEqual(info["cli"], str(link / "bin" / "vloop"))

    def test_printed_cli_uses_tilde_form(self):
        link = self.home / ".claude" / "skills" / "verify-loop"
        link.parent.mkdir(parents=True)
        os.symlink(SKILL, link)
        repo = self.tmp / "nogit"
        repo.mkdir()
        p = subprocess.run([sys.executable, str(link / "bin" / "vloop"), "auth", "status", "--repo", str(repo)],
                           capture_output=True, text=True, env=dict(os.environ), timeout=60)
        self.assertNotIn(str(SKILL), p.stdout + p.stderr)

    def test_global_platform_option(self):
        p = self.run_vloop("--platform", "codex", "version", "--json")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(self.run_vloop("--platform", "bogus", "version").returncode, 2)
        self.assertEqual(cli.split_platform(["--platform", "codex", "hook", "stop"]), ("codex", ["hook", "stop"]))
        self.assertEqual(cli.split_platform(["--platform=claude", "x"]), ("claude", ["x"]))
        self.assertEqual(cli.split_platform(["hook", "stop"]), (None, ["hook", "stop"]))

    def test_setup_and_doctor_are_implemented(self):
        os.environ["VERIFY_STATE_DIR"] = str(self.tmp / "state")
        for args in (("doctor",), ("setup", "--agent", "claude", "--dry-run"), ("uninstall", "--dry-run")):
            p = self.run_vloop(*args)
            self.assertEqual(p.returncode, 0, (args, p.stderr))
            self.assertNotIn("not implemented", p.stderr)

    def test_subagent_hooks_are_noops(self):
        os.environ["VERIFY_STATE_DIR"] = str(self.tmp / "state")
        for event in ("subagent-start", "subagent-stop"):
            p = self.run_vloop("hook", event, stdin=json.dumps({"session_id": "s1", "agent_type": "x"}))
            self.assertEqual((p.returncode, p.stdout), (0, ""), event)
        self.assertFalse((self.tmp / "state" / "hook-errors.log").exists())


class PlatformTest(PathsCase):
    def test_detect_order(self):
        self.assertEqual(platforms.detect().name, "claude")
        self.assertEqual(platforms.detect({"tool_name": "apply_patch"}).name, "codex")
        os.environ["VLOOP_PLATFORM"] = "claude"
        self.assertEqual(platforms.detect({"tool_name": "apply_patch"}).name, "claude")
        platforms.set_override("codex")
        try:
            self.assertEqual(platforms.detect().name, "codex")
            self.assertEqual(platforms.detect(override="claude").name, "claude")
        finally:
            platforms.set_override(None)
        with self.assertRaises(ValueError):
            platforms.set_override("bogus")

    def test_claude_normalize(self):
        p = platforms.get("claude")
        ev = p.normalize("post-tool", {"session_id": "s", "cwd": "/r", "tool_name": "Edit",
                                       "tool_input": {"file_path": "/r/a.js"}, "agent_type": "verify-qa"})
        self.assertEqual((ev.sid, ev.cwd, ev.tool, ev.edit_paths, ev.agent_type), ("s", "/r", "Edit", ["/r/a.js"],
                                                                                   "verify-qa"))
        ev = p.normalize("post-tool", {"tool_name": "Bash", "tool_input": {"command": "sed -i x a"},
                                       "tool_response": {"bashEditDiff": {"changedFiles": ["/r/a"]}}})
        self.assertEqual((ev.bash_cmd, ev.edit_paths), ("sed -i x a", ["/r/a"]))
        ev = p.normalize("stop", {"stop_hook_active": True, "last_assistant_message": "ok?"})
        self.assertEqual((ev.stop_active, ev.last_msg), (True, "ok?"))

    def test_claude_session_and_prompts(self):
        p = platforms.get("claude")
        self.assertIsNone(p.session_id_from_env())
        os.environ["CLAUDE_CODE_SESSION_ID"] = "abc"
        self.assertEqual(p.session_id_from_env(), "abc")
        import re

        for text in ("/verify-loop", "/agent-verify-loop:verify-qa x", "/retro --proposals", "/verify-retro"):
            self.assertRegex(text, p.skill_prompt_re)
        self.assertRegex("<task-notification>x", p.injected_prompt_re)
        self.assertIsNone(re.match(p.skill_prompt_re, "please /verify-loop"))

    def test_claude_render_agent(self):
        name, text = platforms.get("claude").render_agent("---\nname: verify-qa\n---\nbody\n")
        self.assertEqual((name, text), ("verify-qa.md", "---\nname: verify-qa\n---\nbody\n"))

    def test_codex_normalize(self):
        p = platforms.get("codex")
        self.assertEqual(p.name, "codex")
        ev = p.normalize("stop", {"session_id": "t", "stop_hook_active": True, "turn_id": "1"})
        self.assertEqual((ev.sid, ev.stop_active, ev.edit_paths), ("t", True, []))


class TransportRegistryTest(PathsCase):
    def test_builtins_and_auto(self):
        self.assertIn("agent", transports.names())
        self.assertIn("herdr", transports.names())
        self.assertEqual(transports.select({}).name, "agent")
        os.environ["HERDR_ENV"] = "1"
        os.environ["VERIFY_HERDR_AGENT"] = str(Path(__file__).resolve().parent / "fixtures" / "wp2" / "fake-herdr-agent")
        self.assertEqual(transports.select({"transport": "auto"}).name, "herdr")
        self.assertEqual(transports.select({"transport": "agent"}).name, "agent")
        os.environ["HERDR_AGENT_DEPTH"] = "1"
        self.assertEqual(transports.select({}).name, "agent")

    def test_agent_spawn_plan(self):
        line = transports.get("agent").spawn_plan("qa", {"run_id": "R"}, 2, "/s/q.prompt.md", "haiku", "low")
        self.assertEqual(line, ['Agent(subagent_type="verify-qa", model="haiku", description="verify qa #2", '
                                'prompt="Your task file is /s/q.prompt.md. Read it completely and follow it.")'])

    def test_herdr_hook_context(self):
        h = transports.get("herdr")
        ctx = h.hook_context({"session_id": "child"})
        self.assertEqual((ctx.target_sid, ctx.is_child, ctx.suppress_gate), ("child", False, False))
        os.environ.update({"HERDR_AGENT_DEPTH": "1", "HERDR_PARENT_SESSION_ID": "parent"})
        ctx = h.hook_context({"session_id": "child"})
        self.assertEqual((ctx.target_sid, ctx.is_child, ctx.suppress_gate), ("parent", True, True))
        self.assertIn("HERDR_PARENT_SESSION_ID", h.child_env_strip())

    def test_extra_module_from_env_and_config(self):
        mod = self.tmp / "ext.py"
        mod.write_text(textwrap.dedent("""\
            from vlooplib.transports import Transport

            class Ext(Transport):
                name = "ext-test"
                priority = 500

                def available(self, cfg):
                    return bool(cfg.get("ext_ok"))

                def spawn_plan(self, role, ctx, attempt, prompt_path, model, effort):
                    return ["ext"]

            TRANSPORT = Ext
            """))
        os.environ["VLOOP_TRANSPORTS"] = str(mod)
        self.assertEqual(transports.select({}).name, "agent")
        self.assertEqual(transports.select({"ext_ok": True}).name, "ext-test")
        self.assertEqual(transports.select({"transport": "ext-test"}).name, "ext-test")
        bad = self.tmp / "bad.py"
        bad.write_text("raise RuntimeError('boom')\n")
        self.assertEqual(len(transports.load_modules({"transport_modules": [str(bad)]})), 1)
        transports._REGISTRY.pop("ext-test", None)
