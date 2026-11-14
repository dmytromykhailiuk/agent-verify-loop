"""Tests for `vloop setup` / `uninstall` / `doctor` and the Codex agent TOML renderer. Run: python3 -m unittest discover -s tests"""
from __future__ import annotations

import contextlib
import io
import json
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from helpers import AGENTS_SRC, HermeticCase  # noqa: E402
from vlooplib import cli, paths, setup  # noqa: E402
from vlooplib.platforms import codex_agents  # noqa: E402

try:
    import tomllib
except ImportError:
    tomllib = None

AGENT_NAMES = ("verify-qa", "verify-reviewer", "verify-triage")
EVENT_KEYS = ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop", "SubagentStart",
              "SubagentStop")


def read_json(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def vloop_commands(data: dict) -> dict:
    return {key: [h["command"] for g in groups for h in g["hooks"]] for key, groups in data.get("hooks", {}).items()}


class SetupCase(HermeticCase):
    def setUp(self):
        super().setUp()
        self.claude = self.home / ".claude"
        self.codex = self.home / ".codex"
        self.config = Path(os.environ["VERIFY_CONFIG"])
        self.installed = self.home / ".local" / "share" / "agent-verify-loop" / "installed.json"
        self.cli = str(paths.cli())
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "master", str(self.repo)], check=True)
        os.chdir(self.repo)

    def vloop(self, *argv) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = cli.main(list(argv))
            except SystemExit as e:
                rc = e.code
        return rc, out.getvalue(), err.getvalue()

    def ok(self, *argv) -> str:
        rc, out, err = self.vloop(*argv)
        self.assertEqual(rc, 0, f"{argv}: {out}{err}")
        return out

    def settings(self, name="settings.json") -> dict:
        return read_json(self.claude / name)

    def fake_skill(self, home: Path, *, manifest: bool = True) -> Path:
        root = home / "plugins" / "cache" / "market" / "agent-verify-loop" / "1.0.0"
        skill = root / "skills" / "verify-loop"
        (skill / "bin").mkdir(parents=True)
        write(skill / "bin" / "vloop", "#!/bin/sh\n")
        (skill / "bin" / "vloop").chmod(0o755)
        if manifest:
            write(root / ".claude-plugin" / "plugin.json", "{}")
        return skill


class SetupClaudeTest(SetupCase):
    def test_installs_agents_hooks_perms(self):
        out = self.ok("setup", "--agent", "claude")
        self.assertIn("Start a new session", out)
        for name in AGENT_NAMES:
            self.assertTrue((self.claude / "agents" / f"{name}.md").is_file(), name)
        s = self.settings()
        cmds = vloop_commands(s)
        self.assertEqual(set(cmds), set(EVENT_KEYS))
        self.assertEqual(cmds["Stop"], [f"'{self.cli}' hook stop"])
        self.assertEqual(cmds["PreToolUse"], [f"'{self.cli}' hook pre-tool"])
        groups = s["hooks"]
        self.assertEqual(groups["SessionStart"][0]["matcher"], "^(startup|resume|clear|compact)$")
        self.assertEqual(groups["SessionStart"][0]["hooks"][0]["timeout"], 5)
        self.assertTrue(groups["UserPromptSubmit"][0]["hooks"][0]["async"])
        for key in ("PreToolUse", "PostToolUse"):
            self.assertEqual(groups[key][0]["matcher"], "Edit|Write|MultiEdit|NotebookEdit|Bash")
        self.assertEqual(groups["Stop"][0]["hooks"][0]["timeout"], 20)
        self.assertNotIn("matcher", groups["Stop"][0])
        allow = s["permissions"]["allow"]
        self.assertIn(f"Bash({self.cli} *)", allow)
        self.assertTrue(any(r.startswith("Read(") and r.endswith("/skills/verify-loop/**)") for r in allow))
        state = paths.state_dir()
        self.assertTrue(any(r.startswith("Edit(") and str(state).lstrip("/") in r for r in allow))
        self.assertTrue(any(r.startswith("Read(") and str(state).lstrip("/") in r for r in allow))
        self.assertFalse((self.codex).exists())

    def test_agent_paths_are_rewritten_to_this_install(self):
        self.ok("setup", "--agent", "claude", "--only", "agents")
        qa = (self.claude / "agents" / "verify-qa.md").read_text()
        self.assertRegex(qa, rf'command: "?{re.escape(str(paths.pw_mcp()))}"?\n')
        self.assertIn(f'command: "{self.cli} guard qa"', qa)
        reviewer = (self.claude / "agents" / "verify-reviewer.md").read_text()
        self.assertIn(f'command: "{self.cli} guard reviewer"', reviewer)
        triage = (self.claude / "agents" / "verify-triage.md").read_text()
        self.assertEqual(triage, (AGENTS_SRC / "verify-triage.md").read_text())

    def test_rewrite_handles_placeholders_and_foreign_roots(self):
        text = ("a: <skill>/bin/vloop guard x\nb: ~/.claude/skills/verify-loop/bin/pw-mcp\n"
                "c: /opt/some/verify-loop/bin/vloop\nd: /usr/bin/other\n")
        out = setup.rewrite_claude_agent(text)
        self.assertEqual(out, f"a: {self.cli} guard x\nb: {paths.pw_mcp()}\nc: {self.cli}\nd: /usr/bin/other\n")

    def test_preserves_foreign_content(self):
        foreign = {"type": "command", "command": "echo hi"}
        write(self.claude / "settings.json", json.dumps({
            "theme": "dark",
            "hooks": {"SessionStart": [{"matcher": "x", "hooks": [foreign]}], "Stop": [{"hooks": [foreign]}],
                      "Notification": [{"hooks": [foreign]}]},
            "permissions": {"allow": ["Bash(ls)"], "deny": ["Bash(rm *)"]},
        }))
        self.ok("setup", "--agent", "claude")
        s = self.settings()
        self.assertEqual(s["theme"], "dark")
        self.assertEqual(s["hooks"]["Notification"], [{"hooks": [foreign]}])
        self.assertEqual(s["hooks"]["Stop"][0], {"hooks": [foreign]})
        self.assertEqual(len(s["hooks"]["Stop"]), 2)
        self.assertEqual(s["hooks"]["SessionStart"][0], {"matcher": "x", "hooks": [foreign]})
        self.assertEqual(s["permissions"]["deny"], ["Bash(rm *)"])
        self.assertEqual(s["permissions"]["allow"][0], "Bash(ls)")
        self.assertIn(f"Bash({self.cli} *)", s["permissions"]["allow"])

    def test_idempotent(self):
        self.ok("setup", "--agent", "claude")
        before = (self.claude / "settings.json").read_text()
        state = self.installed.read_text()
        out = self.ok("setup", "--agent", "claude")
        self.assertIn("nothing to change", out)
        self.assertEqual((self.claude / "settings.json").read_text(), before)
        self.assertEqual(self.installed.read_text(), state)
        self.assertEqual(sum(len(v) for v in vloop_commands(self.settings()).values()), 7)

    def test_idempotent_keeps_order_when_foreign_hook_added_after(self):
        self.ok("setup", "--agent", "claude")
        s = self.settings()
        s["hooks"]["Stop"].append({"hooks": [{"type": "command", "command": "echo later"}]})
        write(self.claude / "settings.json", json.dumps(s))
        before = (self.claude / "settings.json").read_text()
        self.assertIn("nothing to change", self.ok("setup", "--agent", "claude"))
        self.assertEqual((self.claude / "settings.json").read_text(), before)

    def test_changed_hook_entry_is_replaced_not_duplicated(self):
        self.ok("setup", "--agent", "claude")
        s = self.settings()
        s["hooks"]["Stop"][0]["hooks"][0]["timeout"] = 99
        write(self.claude / "settings.json", json.dumps(s))
        self.ok("setup", "--agent", "claude")
        stop = self.settings()["hooks"]["Stop"]
        self.assertEqual(len(stop), 1)
        self.assertEqual(stop[0]["hooks"][0]["timeout"], 20)

    def test_installed_json_records_hashes_and_hooks(self):
        self.ok("setup", "--agent", "claude")
        state = read_json(self.installed)
        self.assertEqual(sorted(Path(p).name for p in state["files"]), [f"{n}.md" for n in AGENT_NAMES])
        self.assertIn(f"'{self.cli}' hook stop", state["hooks"])
        self.assertTrue(all(len(h) == 64 for h in state["files"].values()))

    def test_user_edited_agent_is_kept_until_force(self):
        self.ok("setup", "--agent", "claude")
        f = self.claude / "agents" / "verify-qa.md"
        f.write_text("my own qa")
        out = self.ok("setup", "--agent", "claude")
        self.assertIn("kept", out)
        self.assertIn("--force", out)
        self.assertEqual(f.read_text(), "my own qa")
        out = self.ok("setup", "--agent", "claude", "--force")
        self.assertIn("(backup verify-qa.md.bak)", out)
        self.assertEqual(f.with_name("verify-qa.md.bak").read_text(), "my own qa")
        self.assertIn("guard qa", f.read_text())
        f.write_text("edited again")
        out = self.ok("setup", "--agent", "claude", "--force")
        self.assertIn("(backup verify-qa.md.bak.1)", out)
        self.assertEqual(f.with_name("verify-qa.md.bak").read_text(), "my own qa")
        self.assertEqual(f.with_name("verify-qa.md.bak.1").read_text(), "edited again")

    def test_foreign_agent_file_is_kept_until_force(self):
        f = self.claude / "agents" / "verify-reviewer.md"
        write(f, "---\nname: verify-reviewer\n---\nhand-written\n")
        out = self.ok("setup", "--agent", "claude", "--only", "agents")
        self.assertIn("kept", out)
        self.assertIn("hand-written", f.read_text())
        self.assertTrue((self.claude / "agents" / "verify-qa.md").is_file())

    def test_previous_install_location_is_updated_not_kept(self):
        self.ok("setup", "--agent", "claude")
        old_hooks = vloop_commands(self.settings())
        moved = self.tmp / "moved" / "skills" / "verify-loop"
        (moved / "bin").mkdir(parents=True)
        with mock.patch.object(paths, "SKILL_DIR", moved):
            out = self.ok("setup", "--agent", "claude")
        self.assertNotIn("kept", out)
        qa = (self.claude / "agents" / "verify-qa.md").read_text()
        self.assertIn(str(moved / "bin" / "pw-mcp"), qa)
        new_hooks = vloop_commands(self.settings())
        self.assertEqual(new_hooks["Stop"], [f"'{moved / 'bin' / 'vloop'}' hook stop"])
        self.assertNotEqual(new_hooks, old_hooks)
        self.assertEqual(sum(len(v) for v in new_hooks.values()), 7)

    def test_legacy_install_is_never_touched(self):
        legacy = f"'{self.claude}/skills/verify-loop/bin/vloop'"
        entry = lambda ev: {"hooks": [{"type": "command", "command": f"{legacy} hook {ev}"}]}
        legacy_settings = {"hooks": {"Stop": [entry("stop")], "PreToolUse": [{"matcher": "Bash", **entry("pre-tool")}]},
                           "permissions": {"allow": [f"Bash({self.claude}/skills/verify-loop/bin/vloop *)"]}}
        write(self.claude / "settings.json", json.dumps(legacy_settings))
        self.ok("setup", "--agent", "claude")
        cmds = vloop_commands(self.settings())
        self.assertIn(f"{legacy} hook stop", cmds["Stop"])
        self.assertIn(f"'{self.cli}' hook stop", cmds["Stop"])
        self.ok("setup", "--agent", "claude", "--uninstall")
        s = self.settings()
        self.assertEqual(s["hooks"], legacy_settings["hooks"])
        self.assertEqual(s["permissions"], legacy_settings["permissions"])

    def test_uninstall_removes_only_ours(self):
        foreign = {"type": "command", "command": "echo hi"}
        original = {"hooks": {"SessionStart": [{"matcher": "x", "hooks": [foreign]}]},
                    "permissions": {"allow": ["Bash(ls)"]}}
        write(self.claude / "settings.json", json.dumps(original))
        write(self.claude / "agents" / "other.md", "---\nname: other\n---\nhi\n")
        self.ok("setup", "--agent", "claude")
        self.ok("setup", "--agent", "claude", "--uninstall")
        for name in AGENT_NAMES:
            self.assertFalse((self.claude / "agents" / f"{name}.md").exists())
        self.assertTrue((self.claude / "agents" / "other.md").exists())
        self.assertEqual(self.settings(), original)

    def test_uninstall_keeps_user_edited_agent_and_force_removes_it(self):
        self.ok("setup", "--agent", "claude")
        f = self.claude / "agents" / "verify-qa.md"
        f.write_text("mine")
        out = self.ok("setup", "--agent", "claude", "--uninstall")
        self.assertIn("kept", out)
        self.assertEqual(f.read_text(), "mine")
        self.ok("setup", "--agent", "claude", "--uninstall", "--force")
        self.assertFalse(f.exists())

    def test_uninstall_on_clean_system_changes_nothing(self):
        out = self.ok("setup", "--agent", "claude", "--uninstall")
        self.assertIn("nothing to change", out)
        self.assertFalse(self.claude.exists())

    def test_uninstall_command_alias(self):
        self.ok("setup", "--agent", "claude")
        self.ok("uninstall", "--agent", "claude")
        self.assertEqual(vloop_commands(self.settings()), {})
        self.assertFalse((self.claude / "agents" / "verify-qa.md").exists())

    def test_full_uninstall_removes_state_file(self):
        self.ok("setup", "--agent", "all")
        self.assertTrue(self.installed.is_file())
        self.ok("setup", "--agent", "all", "--uninstall")
        self.assertFalse(self.installed.exists())

    def test_dry_run_writes_nothing(self):
        out = self.ok("setup", "--agent", "all", "--dry-run", "--mode", "enforce")
        self.assertIn("would write", out)
        self.assertIn("would set gate mode", out)
        self.assertNotIn("Start a new session", out)
        self.assertFalse(self.claude.exists())
        self.assertFalse(self.codex.exists())
        self.assertFalse(self.installed.exists())
        self.assertFalse(self.config.exists())

    def test_dry_run_uninstall_deletes_nothing(self):
        self.ok("setup", "--agent", "claude")
        before = (self.claude / "settings.json").read_text()
        out = self.ok("setup", "--agent", "claude", "--uninstall", "--dry-run")
        self.assertIn("would delete", out)
        self.assertTrue((self.claude / "agents" / "verify-qa.md").is_file())
        self.assertEqual((self.claude / "settings.json").read_text(), before)

    def test_malformed_settings_are_refused(self):
        cases = [("{oops", "not a valid JSON object"), ("[1]", "not a valid JSON object"),
                 (json.dumps({"hooks": []}), "must be a JSON object"),
                 (json.dumps({"hooks": {"Stop": {}}}), "must be a JSON array"),
                 (json.dumps({"permissions": []}), "must be a JSON object"),
                 (json.dumps({"permissions": {"allow": "x"}}), "must be a JSON array")]
        for text, message in cases:
            with self.subTest(text=text):
                write(self.claude / "settings.json", text)
                rc, out, err = self.vloop("setup", "--agent", "claude")
                self.assertEqual(rc, 1)
                self.assertIn(message, err)
                self.assertIn("not changed", err)
                self.assertEqual((self.claude / "settings.json").read_text(), text)
                self.assertFalse((self.claude / "agents").exists())

    def test_only_limits_the_parts(self):
        self.ok("setup", "--agent", "claude", "--only", "hooks")
        self.assertFalse((self.claude / "agents").exists())
        s = self.settings()
        self.assertIn("hooks", s)
        self.assertNotIn("permissions", s)
        self.ok("setup", "--agent", "claude", "--only", "perms")
        self.assertIn("permissions", self.settings())

    def test_unknown_part_is_rejected(self):
        rc, _, err = self.vloop("setup", "--agent", "claude", "--only", "bogus")
        self.assertEqual(rc, 1)
        self.assertIn("unknown part", err)
        self.assertFalse(self.claude.exists())

    def test_project_scope(self):
        self.ok("setup", "--agent", "claude", "--scope", "project")
        local = self.repo / ".claude" / "settings.local.json"
        self.assertEqual(set(vloop_commands(read_json(local))), set(EVENT_KEYS))
        self.assertIn("permissions", read_json(local))
        for name in AGENT_NAMES:
            self.assertTrue((self.repo / ".claude" / "agents" / f"{name}.md").is_file())
        self.assertFalse(self.claude.exists())
        self.assertFalse((self.repo / ".claude" / "settings.json").exists())
        self.ok("setup", "--agent", "claude", "--scope", "project", "--uninstall")
        self.assertEqual(read_json(local), {})
        self.assertFalse((self.repo / ".claude" / "agents" / "verify-qa.md").exists())

    def test_project_uninstall_also_cleans_settings_json(self):
        self.ok("setup", "--agent", "claude", "--scope", "project")
        local = self.repo / ".claude" / "settings.local.json"
        shared = self.repo / ".claude" / "settings.json"
        shared.write_text(local.read_text())
        self.ok("setup", "--agent", "claude", "--scope", "project", "--uninstall")
        self.assertEqual(read_json(shared), {})

    def test_project_scope_needs_a_repo(self):
        outside = self.tmp / "plain"
        outside.mkdir()
        os.chdir(outside)
        rc, _, err = self.vloop("setup", "--agent", "claude", "--scope", "project")
        self.assertEqual(rc, 1)
        self.assertIn("git repo", err)
        self.assertFalse((outside / ".claude").exists())

    def test_mode_is_written_to_user_config(self):
        out = self.ok("setup", "--agent", "claude", "--only", "hooks", "--mode", "enforce")
        self.assertIn("gate mode: warn -> enforce", out)
        self.assertEqual(read_json(self.config)["mode"], "enforce")
        self.ok("setup", "--agent", "claude", "--only", "hooks", "--mode", "warn")
        self.assertEqual(read_json(self.config)["mode"], "warn")

    def test_default_mode_stays_warn(self):
        self.ok("setup", "--agent", "claude")
        self.assertFalse(self.config.exists())

    def test_default_agent_follows_existing_homes(self):
        self.ok("setup")
        self.assertTrue(self.claude.is_dir())
        self.assertFalse(self.codex.exists())
        self.codex.mkdir()
        out = self.ok("setup")
        self.assertIn("codex (user):", out)
        self.assertTrue((self.codex / "hooks.json").is_file())

    def test_platform_flag_selects_default_agent(self):
        self.ok("--platform", "codex", "setup")
        self.assertTrue((self.codex / "hooks.json").is_file())
        self.assertFalse(self.claude.exists())


class PluginBundleTest(SetupCase):
    def test_claude_plugin_skips_agents_and_hooks_but_keeps_perms(self):
        skill = self.fake_skill(self.claude)
        with mock.patch.object(paths, "SKILL_DIR", skill):
            self.assertEqual(setup.bundle_agent(), "claude")
            out = self.ok("setup", "--agent", "claude")
            self.assertIn("--force", out)
            self.assertFalse((self.claude / "agents").exists())
            s = self.settings()
            self.assertNotIn("hooks", s)
            self.assertIn(f"Bash({skill / 'bin' / 'vloop'} *)", s["permissions"]["allow"])

    def test_force_installs_anyway(self):
        skill = self.fake_skill(self.claude)
        with mock.patch.object(paths, "SKILL_DIR", skill):
            self.ok("setup", "--agent", "claude", "--force")
        self.assertTrue((self.claude / "agents" / "verify-qa.md").is_file())
        self.assertEqual(len(vloop_commands(self.settings())), 7)

    def test_uninstall_ignores_bundle_skip(self):
        self.ok("setup", "--agent", "claude")
        skill = self.fake_skill(self.claude)
        with mock.patch.object(paths, "SKILL_DIR", skill):
            self.ok("setup", "--agent", "claude", "--uninstall", "--only", "agents")
        self.assertFalse((self.claude / "agents" / "verify-qa.md").exists())

    def test_cache_dir_without_manifest_is_a_bundle(self):
        skill = self.fake_skill(self.claude, manifest=False)
        with mock.patch.object(paths, "SKILL_DIR", skill):
            self.assertEqual(setup.bundle_agent(), "claude")
            self.assertEqual(setup.plugin_bundle(), self.claude / "plugins" / "cache" / "market" /
                             "agent-verify-loop" / "1.0.0")

    def test_codex_plugin_skips_hooks_keeps_agents(self):
        skill = self.fake_skill(self.codex)
        with mock.patch.object(paths, "SKILL_DIR", skill):
            self.assertEqual(setup.bundle_agent(), "codex")
            out = self.ok("setup", "--agent", "codex")
        self.assertIn("skipped hooks", out)
        self.assertTrue((self.codex / "agents" / "verify-qa.toml").is_file())
        self.assertFalse((self.codex / "hooks.json").exists())

    def test_claude_plugin_does_not_skip_codex_install(self):
        skill = self.fake_skill(self.claude)
        with mock.patch.object(paths, "SKILL_DIR", skill):
            self.ok("setup", "--agent", "codex")
        self.assertTrue((self.codex / "hooks.json").is_file())

    def test_checkout_outside_agent_homes_is_not_owned(self):
        self.assertIsNone(setup.bundle_agent())


class SetupCodexTest(SetupCase):
    def test_agents_and_hooks(self):
        out = self.ok("setup", "--agent", "codex")
        for name in AGENT_NAMES:
            self.assertTrue((self.codex / "agents" / f"{name}.toml").is_file(), name)
        data = read_json(self.codex / "hooks.json")
        self.assertEqual(set(data["hooks"]), set(EVENT_KEYS))
        self.assertEqual(vloop_commands(data)["Stop"], [f"'{self.cli}' --platform codex hook stop"])
        self.assertEqual(data["hooks"]["PreToolUse"][0]["matcher"], "Bash|apply_patch|Edit|Write")
        self.assertEqual(data["hooks"]["PostToolUse"][0]["matcher"], "Bash|apply_patch|Edit|Write")
        self.assertEqual(data["hooks"]["SessionStart"][0]["matcher"], "^(startup|resume|clear|compact)$")
        self.assertIs(data["hooks"]["UserPromptSubmit"][0]["hooks"][0]["async"], True)
        self.assertFalse(self.claude.exists())
        self.assertIn("[sandbox_workspace_write]", out)
        self.assertIn("writable_roots", out)
        self.assertIn(str(paths.state_dir()), out)
        self.assertIn("network_access = true", out)

    def test_no_sandbox_note_without_perms(self):
        out = self.ok("setup", "--agent", "codex", "--only", "agents,hooks")
        self.assertNotIn("writable_roots", out)

    @unittest.skipIf(tomllib is None, "tomllib needs Python 3.11")
    def test_toml_files_match_the_canonical_agents(self):
        self.ok("setup", "--agent", "codex", "--only", "agents")
        for name in AGENT_NAMES:
            data = tomllib.loads((self.codex / "agents" / f"{name}.toml").read_text())
            fields, body = codex_agents.parse((AGENTS_SRC / f"{name}.md").read_text())
            self.assertEqual(data["name"], name)
            self.assertEqual(data["description"], fields["description"])
            self.assertEqual(data["developer_instructions"], body.strip() + "\n")
            self.assertEqual(data["model_reasoning_effort"], fields["effort"])
            for dropped in ("model", "tools", "disallowedTools", "permissionMode", "mcpServers", "hooks"):
                self.assertNotIn(dropped, data)

    def test_idempotent_and_uninstall(self):
        self.ok("setup", "--agent", "codex")
        before = (self.codex / "hooks.json").read_text()
        self.assertIn("nothing to change", self.ok("setup", "--agent", "codex").split("Start")[0])
        self.assertEqual((self.codex / "hooks.json").read_text(), before)
        self.ok("setup", "--agent", "codex", "--uninstall")
        self.assertFalse((self.codex / "agents" / "verify-qa.toml").exists())
        self.assertEqual(read_json(self.codex / "hooks.json"), {})

    def test_foreign_codex_hooks_preserved(self):
        foreign = {"type": "command", "command": "echo codex"}
        write(self.codex / "hooks.json", json.dumps({"hooks": {"Stop": [{"hooks": [foreign]}]}, "x": 1}))
        self.ok("setup", "--agent", "codex")
        data = read_json(self.codex / "hooks.json")
        self.assertEqual(data["x"], 1)
        self.assertEqual(data["hooks"]["Stop"][0], {"hooks": [foreign]})
        self.ok("setup", "--agent", "codex", "--uninstall")
        self.assertEqual(read_json(self.codex / "hooks.json"), {"hooks": {"Stop": [{"hooks": [foreign]}]}, "x": 1})

    def test_user_edited_toml_is_kept(self):
        self.ok("setup", "--agent", "codex")
        f = self.codex / "agents" / "verify-qa.toml"
        f.write_text('name = "mine"\n')
        self.assertIn("kept", self.ok("setup", "--agent", "codex"))
        self.assertEqual(f.read_text(), 'name = "mine"\n')
        self.ok("setup", "--agent", "codex", "--force")
        self.assertEqual(f.with_name("verify-qa.toml.bak").read_text(), 'name = "mine"\n')

    def test_project_scope_uses_repo_codex_dir(self):
        self.ok("setup", "--agent", "codex", "--scope", "project")
        self.assertTrue((self.repo / ".codex" / "agents" / "verify-qa.toml").is_file())
        data = read_json(self.repo / ".codex" / "hooks.json")
        self.assertEqual(len(vloop_commands(data)), 7)
        self.assertFalse(self.codex.exists())

    def test_all_installs_both(self):
        out = self.ok("setup", "--agent", "all")
        self.assertIn("claude (user):", out)
        self.assertIn("codex (user):", out)
        self.assertTrue((self.claude / "agents" / "verify-qa.md").is_file())
        self.assertTrue((self.codex / "agents" / "verify-qa.toml").is_file())


class CodexAgentRenderTest(unittest.TestCase):
    def render(self, front: str, body: str = "body\n") -> str:
        name, text = codex_agents.render_agent(f"---\nname: a\ndescription: d\n{front}---\n{body}")
        self.assertEqual(name, "a.toml")
        return text

    def test_plain_body_uses_literal_string(self):
        text = self.render("model: sonnet\neffort: high\ntools: Read\n", "\nbody \\n\nline2\n\n")
        self.assertIn("developer_instructions = '''\n", text)
        for dropped in ("model =", "tools"):
            self.assertNotIn(dropped, text)
        if tomllib:
            data = tomllib.loads(text)
            self.assertEqual(data["developer_instructions"], "body \\n\nline2\n")
            self.assertEqual(data["model_reasoning_effort"], "high")

    @unittest.skipIf(tomllib is None, "tomllib needs Python 3.11")
    def test_triple_quote_in_body(self):
        body = "use ''' carefully\n\ttab and \x01 ctrl\nend '' ''\n"
        text = self.render("", body)
        self.assertTrue(text.split("developer_instructions =")[1].lstrip().startswith('"'))
        self.assertEqual(tomllib.loads(text)["developer_instructions"], body.strip() + "\n")

    @unittest.skipIf(tomllib is None, "tomllib needs Python 3.11")
    def test_triple_quote_without_control_chars(self):
        body = "a ''' b\n"
        self.assertEqual(tomllib.loads(self.render("", body))["developer_instructions"], body)

    @unittest.skipIf(tomllib is None, "tomllib needs Python 3.11")
    def test_special_characters_in_strings(self):
        md = '---\nname: x\ndescription: "He said \\"hi\\" — ünï \\\\ \x7f"\n---\nb\n'
        data = tomllib.loads(codex_agents.render_agent(md)[1])
        self.assertEqual(data["description"], 'He said "hi" — ünï \\ \x7f')

    @unittest.skipIf(tomllib is None, "tomllib needs Python 3.11")
    def test_crlf_and_trailing_quote_in_body(self):
        md = "---\r\nname: x\r\ndescription: d\r\n---\r\nline 'q'\r\nlast'\r\n"
        data = tomllib.loads(codex_agents.render_agent(md)[1])
        self.assertEqual(data["developer_instructions"], "line 'q'\nlast'\n")

    def test_effort_values(self):
        for effort, expect in (("minimal", True), ("low", True), ("xhigh", True), ("max", False), ("bogus", False)):
            text = self.render(f"effort: {effort}\n")
            self.assertEqual(f'model_reasoning_effort = "{effort}"' in text, expect, effort)
        self.assertNotIn("model_reasoning_effort", self.render(""))

    def test_model_aliases_dropped_real_model_kept(self):
        for alias in ("sonnet", "opus", "haiku", "inherit", "claude-sonnet-5-5"):
            self.assertNotIn("model =", self.render(f"model: {alias}\n"), alias)
        self.assertIn('model = "gpt-5"', self.render("model: gpt-5\n"))

    def test_claude_only_blocks_are_ignored(self):
        front = ("tools: Read, Bash\ndisallowedTools: Edit\npermissionMode: auto\nmcpServers:\n  - playwright:\n"
                 "      command: x\nhooks:\n  PreToolUse:\n    - matcher: Bash\neffort: low\n")
        text = self.render(front)
        for dropped in ("tools", "disallowed", "permissionMode", "mcpServers", "playwright", "PreToolUse"):
            self.assertNotIn(dropped, text)
        self.assertIn('model_reasoning_effort = "low"', text)

    def test_folded_description(self):
        md = "---\nname: a\ndescription: >\n  one\n  two\n---\nb\n"
        self.assertIn('description = "one two"', codex_agents.render_agent(md)[1])

    def test_missing_name_is_an_error(self):
        with self.assertRaises(ValueError):
            codex_agents.render_agent("---\ndescription: d\n---\nb\n")

    def test_platform_delegates(self):
        from vlooplib import platforms

        name, text = platforms.get("codex").render_agent("---\nname: z\ndescription: d\n---\nb\n")
        self.assertEqual(name, "z.toml")
        self.assertIn('name = "z"', text)


class DoctorTest(SetupCase):
    def doctor(self, *extra, expect=0) -> str:
        rc, out, err = self.vloop("doctor", *extra)
        self.assertEqual(rc, expect, out + err)
        return out

    def test_fresh_system_is_ok(self):
        out = self.doctor()
        self.assertIn("Python:", out)
        self.assertIn(self.cli, out)
        self.assertIn("gate mode: warn", out)
        self.assertIn("Transport:", out)
        self.assertIn("agent-vault:", out)
        self.assertIn("user 0/3", out)
        self.assertNotIn("warning", out)

    def test_agent_cost_detection(self):
        import fakeagentcost
        from vlooplib.integrations import agentcost

        agentcost.reset_cache()
        os.environ["PATH"] = "/usr/bin:/bin"
        self.assertIn("agent-cost: not used — agent-cost not found (optional) · cost_source auto", self.doctor())
        fc = fakeagentcost.install(self.tmp / "agent-cost")
        os.environ["AGENT_COST_CLI"] = str(fc.cli)
        out = self.doctor()
        self.assertIn(f"agent-cost: {fc.cli} (v0.3.0, contract 1) · cost_source auto", out)
        fc.configure(contract=0)
        agentcost.reset_cache()
        self.config.parent.mkdir(parents=True, exist_ok=True)
        self.config.write_text(json.dumps({"cost_source": "agent-cost"}))
        report = json.loads(self.doctor("--json"))
        self.assertIn("speaks contract 0, vloop needs 1", report["agent_cost"]["status"])
        self.assertEqual(report["cost_source"], "agent-cost")
        self.assertTrue(any("cost_source is agent-cost" in n for n in report["notes"]))

    def test_after_setup_counts(self):
        self.ok("setup", "--agent", "all")
        out = self.doctor()
        self.assertIn("user 3/3", out)
        self.assertIn("user 7/7", out)
        self.assertNotIn("warning", out)

    def test_json_report(self):
        self.ok("setup", "--agent", "claude", "--mode", "enforce")
        report = json.loads(self.doctor("--json"))
        self.assertEqual(report["problems"], [])
        self.assertEqual(report["config"]["gate_mode"], "enforce")
        claude = report["installed"]["claude"]
        self.assertEqual(claude["agents"]["user"], {"present": 3, "total": 3})
        self.assertEqual(claude["hooks"]["user"]["present"], 7)
        self.assertEqual(report["installed"]["codex"]["hooks"]["user"]["present"], 0)

    def test_project_scope_is_listed_inside_a_repo(self):
        self.ok("setup", "--agent", "claude", "--scope", "project")
        out = self.doctor()
        self.assertIn("project 3/3", out)
        self.assertIn("project 7/7", out)

    def test_user_plus_project_duplicate_warns(self):
        self.ok("setup", "--agent", "claude")
        self.ok("setup", "--agent", "claude", "--scope", "project")
        out = self.doctor(expect=1)
        self.assertIn("warning: claude: vloop hooks are registered more than once", out)

    def test_double_entry_in_one_file_warns(self):
        self.ok("setup", "--agent", "codex")
        path = self.codex / "hooks.json"
        data = read_json(path)
        data["hooks"]["Stop"].append(json.loads(json.dumps(data["hooks"]["Stop"][0])))
        path.write_text(json.dumps(data))
        out = self.doctor(expect=1)
        self.assertIn("warning: codex:", out)
        self.assertIn("Stop", out)

    def test_plugin_plus_settings_hooks_warn(self):
        self.ok("setup", "--agent", "claude")
        skill = self.fake_skill(self.claude)
        with mock.patch.object(paths, "SKILL_DIR", skill):
            out = self.doctor(expect=1)
        self.assertIn("more than once", out)
        self.assertIn("plugin hooks/hooks.json", out)

    def test_plugin_alone_is_ok(self):
        skill = self.fake_skill(self.claude)
        with mock.patch.object(paths, "SKILL_DIR", skill):
            out = self.doctor()
        self.assertIn("plugin provides its own", out)
        self.assertNotIn("more than once", out)

    def test_legacy_install_plus_ours_warns(self):
        legacy = f"'{self.claude}/skills/verify-loop/bin/vloop'"
        write(self.claude / "settings.json", json.dumps({"hooks": {
            "Stop": [{"hooks": [{"type": "command", "command": f"{legacy} hook stop", "timeout": 20}]}]}}))
        self.ok("setup", "--agent", "claude")
        out = self.doctor(expect=1)
        self.assertIn("more than once", out)
        self.assertIn("Stop", out)
        self.assertIn(f"{self.claude}/settings.json".replace(str(self.home), "~"), out)
        self.ok("setup", "--agent", "claude", "--uninstall")
        out = self.doctor()
        self.assertIn("another install", out)
        self.assertNotIn("warning", out)

    def test_two_legacy_entries_warn(self):
        legacy = f"'{self.claude}/skills/verify-loop/bin/vloop' hook stop"
        entry = {"hooks": [{"type": "command", "command": legacy}]}
        write(self.claude / "settings.json", json.dumps({"hooks": {"Stop": [entry, entry]}}))
        self.doctor(expect=1)

    def test_malformed_settings_are_reported(self):
        write(self.claude / "settings.json", "{oops")
        out = self.doctor(expect=1)
        self.assertIn("is not a valid JSON object", out)

    def test_foreign_hooks_do_not_count(self):
        write(self.claude / "settings.json", json.dumps({"hooks": {"Stop": [
            {"hooks": [{"type": "command", "command": "echo vloop hooks"}]}]}}))
        self.ok("setup", "--agent", "claude")
        self.assertNotIn("warning", self.doctor())

    def test_browser_toolbox_status(self):
        out = self.doctor()
        self.assertIn("playwright-mcp:", out)
        self.assertIn("browser:", out)


class CheckBrowserFlagTest(SetupCase):
    def test_check_browser_keeps_old_behaviour(self):
        os.environ["PW_MCP_CHROME_APP"] = str(self.tmp)
        rc, out, _ = self.vloop("setup", "--check-browser")
        self.assertEqual([ln.split(":")[0] for ln in out.strip().splitlines()], ["node", "chrome", "playwright-mcp"])
        self.assertFalse(self.claude.exists())


if __name__ == "__main__":
    unittest.main()
