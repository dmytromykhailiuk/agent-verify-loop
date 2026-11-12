"""Packaging: plugin manifests, plugin hooks, agent copies, skill frontmatter and repo hygiene."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from helpers import AGENTS_SRC, REPO, SKILL, SKILLS, HermeticCase  # noqa: E402

import vlooplib  # noqa: E402
from vlooplib import setup  # noqa: E402

PLUGIN_AGENTS = REPO / "agents"
FORBIDDEN_PLUGIN_KEYS = ("hooks", "mcpServers", "permissionMode")
CLI_REL = "skills/verify-loop/bin/vloop"
LEGACY_SKILL_PATH = "~/.claude/skills/verify-loop"
LEGACY_ALLOWED = ("README.md", "tests/", "skills/verify-loop/lib/")
SKIP_DIRS = {".git", "__pycache__", "node_modules"}


def read_json(rel):
    return json.loads((REPO / rel).read_text(encoding="utf-8"))


def frontmatter(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"{path}: no frontmatter"
    end = text.index("\n---\n", 3)
    out = {}
    for line in text[4:end].split("\n"):
        m = re.match(r"^([A-Za-z][\w-]*):[ \t]*(.*)$", line)
        if m:
            out[m.group(1)] = m.group(2)
    return out


def repo_files() -> list[Path]:
    out = []
    for root, dirs, files in os.walk(REPO):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            p = Path(root) / f
            if p.suffix in (".pyc", ".png", ".jpg", ".gif"):
                continue
            out.append(p)
    return out


class Manifests(unittest.TestCase):
    def test_manifests_parse(self):
        for rel in (".claude-plugin/plugin.json", ".claude-plugin/marketplace.json", ".codex-plugin/plugin.json",
                    ".agents/plugins/marketplace.json", "hooks/hooks.json"):
            self.assertIsInstance(read_json(rel), dict, rel)

    def test_names_and_versions_agree(self):
        claude, codex = read_json(".claude-plugin/plugin.json"), read_json(".codex-plugin/plugin.json")
        market = read_json(".claude-plugin/marketplace.json")
        self.assertEqual(claude["name"], vlooplib.NAME)
        self.assertEqual(codex["name"], vlooplib.NAME)
        self.assertEqual(claude["version"], vlooplib.VERSION)
        self.assertEqual(codex["version"], vlooplib.VERSION)
        self.assertEqual(market["name"], vlooplib.NAME)
        entry = next(p for p in market["plugins"] if p["name"] == vlooplib.NAME)
        self.assertEqual(entry["version"], vlooplib.VERSION)
        self.assertEqual(entry["source"], "./")
        self.assertRegex(vlooplib.VERSION, r"^\d+\.\d+\.\d+$")
        self.assertEqual(claude["license"], "MIT")
        self.assertEqual(claude["author"]["name"], "dmytromykhailiuk")
        self.assertIn("MIT", (REPO / "LICENSE").read_text())
        codex_market = read_json(".agents/plugins/marketplace.json")
        self.assertIn(vlooplib.NAME, [p["name"] for p in codex_market["plugins"]])

    def test_codex_paths_exist(self):
        codex = read_json(".codex-plugin/plugin.json")
        for key in ("skills", "hooks"):
            self.assertTrue((REPO / codex[key]).exists(), key)
        self.assertTrue(codex["interface"]["displayName"])
        for p in read_json(".agents/plugins/marketplace.json")["plugins"]:
            self.assertTrue((REPO / p["source"]["path"]).is_dir())

    def test_skills_dir_holds_the_four_skills(self):
        names = sorted(p.parent.name for p in SKILLS.glob("*/SKILL.md"))
        self.assertEqual(names, ["verify-loop", "verify-qa", "verify-retro", "verify-review"])

    def test_version_command_matches_manifest(self):
        p = subprocess.run([sys.executable, str(SKILL / "bin" / "vloop"), "version", "--json"],
                           capture_output=True, text=True)
        doc = json.loads(p.stdout)
        self.assertEqual(doc["name"], vlooplib.NAME)
        self.assertEqual(doc["version"], read_json(".claude-plugin/plugin.json")["version"])


class PluginHooks(HermeticCase):
    def hooks(self):
        return read_json("hooks/hooks.json")["hooks"]

    def commands(self):
        out = {}
        for key, groups in self.hooks().items():
            for grp in groups:
                for h in grp["hooks"]:
                    out.setdefault(key, []).append((grp.get("matcher"), h))
        return out

    def test_commands_point_at_the_plugin_cli(self):
        cli = REPO / CLI_REL
        self.assertTrue(cli.is_file())
        self.assertTrue(os.access(cli, os.X_OK))
        for key, items in self.commands().items():
            for _, h in items:
                self.assertEqual(h["type"], "command")
                self.assertRegex(h["command"], r'^"\$\{CLAUDE_PLUGIN_ROOT\}/' + re.escape(CLI_REL) + r'" hook [a-z-]+$')
                self.assertNotIn("--platform", h["command"], key)

    def test_event_set_equals_what_setup_writes(self):
        written = {key for key, _, _ in setup.hook_defs("claude")}
        self.assertEqual(set(self.hooks()), written)
        self.assertEqual(set(self.hooks()), {key for _, key in setup.HOOK_EVENTS})

    def test_each_event_runs_the_same_subcommand_as_setup(self):
        by_key = self.commands()
        for key, _, entry in setup.hook_defs("claude"):
            plugin = by_key[key]
            self.assertEqual(len(plugin), 1, key)
            sub = entry["command"].rsplit(" hook ", 1)[1]
            self.assertTrue(plugin[0][1]["command"].endswith(f" hook {sub}"), key)

    def test_timeouts_matchers_and_async_match_setup(self):
        by_key = self.commands()
        for key, matcher, entry in setup.hook_defs("claude"):
            plugin_matcher, plugin_entry = by_key[key][0]
            for attr in ("timeout", "async"):
                self.assertEqual(plugin_entry.get(attr), entry.get(attr), f"{key}: {attr}")
            if key == "SessionStart":
                self.assertEqual(plugin_matcher, matcher)
            elif matcher:
                self.assertEqual(set(plugin_matcher.split("|")),
                                 set(setup.TOOL_MATCHER["claude"].split("|")) | set(setup.TOOL_MATCHER["codex"].split("|")),
                                 key)
            else:
                self.assertIsNone(plugin_matcher, key)

    def test_plugin_commands_are_recognised_as_vloop_hooks(self):
        for items in self.commands().values():
            for _, h in items:
                self.assertTrue(setup.HOOK_RE.search(h["command"].replace("${CLAUDE_PLUGIN_ROOT}", "/x")), h)


class AgentCopies(unittest.TestCase):
    def test_copies_are_in_sync(self):
        p = subprocess.run([sys.executable, str(REPO / "scripts" / "sync_agents.py"), "--check"],
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertEqual(sorted(f.name for f in PLUGIN_AGENTS.glob("*.md")),
                         sorted(f.name for f in AGENTS_SRC.glob("*.md")))

    def test_plugin_agents_carry_no_ignored_keys(self):
        for f in PLUGIN_AGENTS.glob("*.md"):
            text = f.read_text(encoding="utf-8")
            head = text[4:text.index("\n---\n", 3)]
            for key in FORBIDDEN_PLUGIN_KEYS:
                self.assertNotRegex(head, rf"(?m)^{key}:", f"{f.name}: {key}")
            self.assertNotIn("{{", text, f.name)
            self.assertNotIn("/bin/vloop", head, f.name)
            self.assertNotIn("pw-mcp", head, f.name)

    def test_plugin_agents_keep_body_and_other_keys(self):
        for src in AGENTS_SRC.glob("*.md"):
            dst = PLUGIN_AGENTS / src.name
            s, d = src.read_text(encoding="utf-8"), dst.read_text(encoding="utf-8")
            self.assertEqual(s.split("\n---\n", 1)[1], d.split("\n---\n", 1)[1], src.name)
            for key in ("name", "description", "model", "effort", "tools"):
                if re.search(rf"(?m)^{key}:", s):
                    self.assertRegex(d, rf"(?m)^{key}:", f"{src.name}: {key}")

    def test_sync_strips_blocks_and_placeholders(self):
        sys.path.insert(0, str(REPO / "scripts"))
        import sync_agents

        src = ("---\nname: x\nhooks:\n  PreToolUse:\n    - matcher: a\n      hooks:\n        - command: \"{{SKILL_DIR}}/b\"\n"
               "mcpServers:\n  - p:\n      command: q\nmodel: sonnet\npermissionMode: auto\ncolor: red\n---\nbody {{keep}}\n")
        self.assertEqual(sync_agents.plugin_copy(src), "---\nname: x\nmodel: sonnet\ncolor: red\n---\nbody {{keep}}\n")


class SkillFiles(unittest.TestCase):
    def test_every_skill_has_name_and_description(self):
        for f in SKILLS.glob("*/SKILL.md"):
            fm = frontmatter(f)
            self.assertEqual(fm.get("name"), f.parent.name, f)
            self.assertTrue(fm.get("description", "").strip(), f)

    def test_agents_have_name_and_description(self):
        for f in AGENTS_SRC.glob("*.md"):
            fm = frontmatter(f)
            self.assertEqual(fm.get("name"), f.stem, f)
            self.assertTrue(fm.get("description", "").strip(), f)


class RepoHygiene(unittest.TestCase):
    def test_no_absolute_user_paths(self):
        bad = []
        for p in repo_files():
            rel = p.relative_to(REPO).as_posix()
            if rel == "tests/test_packaging.py":
                continue
            try:
                text = p.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            if "/Users/" in text:
                bad.append(rel)
        self.assertEqual(bad, [], "hardcoded /Users/ paths")

    def test_legacy_skill_path_only_where_expected(self):
        bad = []
        for p in repo_files():
            rel = p.relative_to(REPO).as_posix()
            if rel == "tests/test_packaging.py" or rel.startswith(LEGACY_ALLOWED):
                continue
            try:
                text = p.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            if LEGACY_SKILL_PATH in text:
                bad.append(rel)
        self.assertEqual(bad, [], "hand-installed skill path outside legacy detection / README migration")

    def test_readme_mentions_legacy_path_only_in_migration_section(self):
        text = (REPO / "README.md").read_text(encoding="utf-8")
        for m in re.finditer(re.escape(LEGACY_SKILL_PATH), text):
            head = text[:m.start()].rsplit("\n## ", 1)[-1].split("\n", 1)[0]
            self.assertTrue(head.lower().startswith("migrating"), head)

    def test_ci_workflow_matrix(self):
        text = (REPO / ".github" / "workflows" / "test.yml").read_text(encoding="utf-8")
        for needle in ("ubuntu-latest", "macos-latest", '"3.10"', '"3.13"', "unittest discover",
                       "sync_agents.py --check", "git config --global user.email"):
            self.assertIn(needle, text)

    def test_docs_and_changelog_exist(self):
        changelog = (REPO / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertIn(f"## {vlooplib.VERSION}", changelog)
        self.assertTrue((REPO / "docs" / "transports.md").is_file())


if __name__ == "__main__":
    unittest.main()
