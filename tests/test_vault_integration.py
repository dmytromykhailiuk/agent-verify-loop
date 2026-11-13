"""WP4: agent-vault discovery (contract 1), lessons/covering/resolve calls and the vault root hint."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import fakevault  # noqa: E402
from helpers import HermeticCase  # noqa: E402

from vlooplib.integrations import vault  # noqa: E402

LESSON = {"link": "p/lessons/a", "path": "p/lessons/a.md", "title": "A", "rule": "Do the thing", "occurrences": 2,
          "last_seen": "2026-10-01", "scope": "project", "audience": "reviewer", "category": "process",
          "proposal_status": "none", "proposal_kind": "", "proposal_target": ""}


class VaultCase(HermeticCase):
    def setUp(self):
        super().setUp()
        vault.reset_cache()
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        os.environ["PATH"] = str(self.bin) + os.pathsep + "/usr/bin:/bin"

    def on_path(self, version="v1", **cfg):
        fv = fakevault.install(self.tmp / "onpath", version=version, **cfg)
        (self.bin / "vault").symlink_to(fv.cli)
        return fv


class TestDiscovery(VaultCase):
    def test_nothing_installed(self):
        self.assertIsNone(vault.discover())
        self.assertIsNone(vault.info())
        self.assertFalse(vault.available())
        self.assertEqual(vault.lessons(self.tmp, "reviewer", 3), [])
        self.assertEqual(vault.lessons_bullets(self.tmp, "reviewer", 3), "")
        self.assertEqual(vault.covering(self.tmp, ["a.py"]), "")
        self.assertEqual(vault.covering_notes(self.tmp, ["a.py"]), [])
        self.assertEqual(vault.proposals(self.tmp), [])
        self.assertIsNone(vault.vault_root(self.tmp))
        self.assertEqual(vault.run(["status"]), (127, ""))

    def test_env_var_wins_and_info_shape(self):
        fv = fakevault.install(self.tmp / "envvault")
        other = self.on_path()
        os.environ["AGENT_VAULT_CLI"] = str(fv.cli)
        self.assertEqual(vault.info(), {"cli": str(fv.cli), "skill_dir": str(fv.skill_dir), "ops_retro": str(fv.ops),
                                         "version": "1.4.0", "contract": 1})
        self.assertNotEqual(vault.configured_cli(), other.cli)
        self.assertEqual(vault.configured_cli(), fv.cli)

    def test_verify_env_overrides_agent_env(self):
        a = fakevault.install(self.tmp / "a", v="1.0.0")
        b = fakevault.install(self.tmp / "b", v="2.0.0")
        os.environ["AGENT_VAULT_CLI"] = str(a.cli)
        os.environ["VERIFY_VAULT_CLI"] = str(b.cli)
        self.assertEqual(vault.discover().version, "2.0.0")

    def test_path_vault(self):
        fv = self.on_path()
        self.assertEqual(vault.discover().skill_dir, fv.skill_dir)
        self.assertEqual(vault.discover().contract, 1)

    def test_hashicorp_text_on_path_is_rejected(self):
        self.on_path(version="text")
        self.assertIsNone(vault.discover())
        self.assertFalse(vault.available())

    def test_foreign_json_on_path_is_rejected_and_next_candidate_used(self):
        self.on_path(version="foreign-json")
        good = fakevault.install(self.home / ".agents", version="v1")
        self.assertEqual(vault.discover().cli, good.cli)

    def test_legacy_without_version_only_in_claude_skills_dir(self):
        legacy = fakevault.install(self.home / ".claude", version="legacy")
        info = vault.discover()
        self.assertEqual((info.cli, info.contract, info.version), (legacy.cli, 0, None))
        self.assertEqual(vault.ops_retro_hint(), "~/.claude/skills/vault/ops/retro.md")

    def test_versionless_elsewhere_is_rejected(self):
        self.on_path(version="legacy")
        self.assertIsNone(vault.discover())
        fakevault.install(self.home / ".agents", version="legacy")
        vault.reset_cache()
        self.assertIsNone(vault.discover())

    def test_claude_plugin_cache_newest_wins(self):
        cache = self.home / ".claude" / "plugins" / "cache" / "mkt" / "agent-vault"
        old = fakevault.install(cache / "1.0.0", v="1.0.0")
        new = fakevault.install(cache / "1.1.0", v="1.1.0")
        past = time.time() - 1000
        os.utime(old.cli, (past, past))
        self.assertEqual(vault.discover().cli, new.cli)
        self.assertEqual(vault.discover().version, "1.1.0")

    def test_codex_plugin_dir(self):
        fv = fakevault.install(self.home / ".codex" / "plugins" / "agent-vault")
        self.assertEqual(vault.discover().cli, fv.cli)

    def test_order_path_before_cache_before_legacy(self):
        fakevault.install(self.home / ".claude", version="v1", v="0.1.0")
        cache = fakevault.install(self.home / ".claude" / "plugins" / "cache" / "m" / "agent-vault" / "2", v="2.0.0")
        self.assertEqual(vault.discover().cli, cache.cli)
        vault.reset_cache()
        p = self.on_path(v="3.0.0")
        self.assertEqual((vault.discover().version, vault.discover().skill_dir), ("3.0.0", p.skill_dir))

    def test_non_executable_candidate_skipped(self):
        fv = fakevault.install(self.tmp / "noexec")
        fv.cli.chmod(0o644)
        os.environ["AGENT_VAULT_CLI"] = str(fv.cli)
        self.assertIsNone(vault.discover())

    def test_result_cached_per_process(self):
        fv = fakevault.install(self.tmp / "c")
        os.environ["AGENT_VAULT_CLI"] = str(fv.cli)
        vault.discover()
        vault.discover()
        vault.lessons(self.tmp, "qa", 1)
        self.assertEqual(sum(1 for c in fv.calls() if c[0] == "version"), 1)


class TestCalls(VaultCase):
    def setUp(self):
        super().setUp()
        self.fv = fakevault.install(self.tmp / "v", lessons=[LESSON], covering=[
            {"link": "p/a", "title": "A", "summary": "about a", "type": "module", "stale": False}], vroot="/vr")
        os.environ["AGENT_VAULT_CLI"] = str(self.fv.cli)

    def test_lessons_use_json_format(self):
        self.assertEqual(vault.lessons(self.tmp, "reviewer", 3), [LESSON])
        call = [c for c in self.fv.calls() if c[0] == "lessons"][-1]
        self.assertEqual(call, ["lessons", "--cwd", str(self.tmp), "--audience", "reviewer", "--limit", "3",
                                "--format", "json"])

    def test_lessons_bullets(self):
        self.assertEqual(vault.lessons_bullets(self.tmp, "qa", 2), "- Do the thing (x2)")

    def test_empty_output_means_no_lessons(self):
        self.fv.configure(lessons=[])
        self.assertEqual(vault.lessons(self.tmp, "qa", 2), [])
        self.assertEqual(vault.lessons_bullets(self.tmp, "qa", 2), "")

    def test_garbage_output_means_no_lessons(self):
        self.fv.cli.write_text("#!/bin/sh\nif [ \"$1\" = version ]; then "
                               "echo '{\"name\":\"agent-vault\",\"contract\":1}'; else echo 'not json'; fi\n")
        vault.reset_cache()
        self.assertEqual(vault.lessons(self.tmp, "qa", 2), [])
        self.assertIsNone(vault.vault_root(self.tmp))

    def test_covering(self):
        self.assertEqual(vault.covering(self.tmp, ["a.py", "b.py"]), "- [[p/a]] about a")
        self.assertEqual(vault.covering_notes(self.tmp, ["a.py"])[0]["title"], "A")
        self.assertEqual(vault.covering(self.tmp, []), "")
        call = [c for c in self.fv.calls() if c[0] == "covering"][0]
        self.assertEqual(call[-2:], ["a.py", "b.py"])

    def test_resolve_root(self):
        self.assertEqual(vault.vault_root(self.tmp), Path("/vr/p"))
        self.assertEqual(vault.vault_root(self.tmp, "_harness"), Path("/vr/_harness"))

    def test_proposals(self):
        self.assertEqual(vault.proposals(self.tmp), [])
        self.fv.configure(proposals=[LESSON])
        self.assertEqual(len(vault.proposals(self.tmp)), 1)

    def test_keeper_agent_name(self):
        self.assertEqual(vault.keeper_agent("claude"), "vault-keeper")
        plug = fakevault.install(self.tmp / "plug", plugin="agent-vault")
        os.environ["AGENT_VAULT_CLI"] = str(plug.cli)
        self.assertEqual(vault.keeper_agent("claude"), "agent-vault:vault-keeper")
        self.assertEqual(vault.keeper_agent("codex"), "vault-keeper")


class TestRootHint(VaultCase):
    def test_default_and_env(self):
        self.assertEqual(vault.vault_root_hint(), self.home / "claude-vault")
        os.environ["CLAUDE_VAULT_DIR"] = str(self.tmp / "cv" / "{project}")
        self.assertEqual(vault.vault_root_hint(), self.tmp / "cv")
        os.environ["AGENT_VAULT_DIR"] = str(self.tmp / "av")
        self.assertEqual(vault.vault_root_hint(), self.tmp / "av")

    def test_user_config_and_claude_settings_without_subprocess(self):
        cfg = self.home / ".config" / "agent-vault" / "config.json"
        cfg.parent.mkdir(parents=True)
        cfg.write_text(json.dumps({"vault_dir": str(self.tmp / "fromcfg" / "{project}")}))
        self.assertEqual(vault.vault_root_hint(), self.tmp / "fromcfg")
        cfg.unlink()
        settings = self.home / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text(json.dumps({"env": {"CLAUDE_VAULT_DIR": str(self.tmp / "fromsettings")}}))
        self.assertEqual(vault.vault_root_hint(), self.tmp / "fromsettings")

    def test_relative_template_resolves_against_the_repo_root(self):
        os.environ["AGENT_VAULT_DIR"] = "docs/vault"
        self.assertIsNone(vault.vault_root_hint())
        self.assertIsNone(vault.vault_root_hint(self.tmp))
        repo = self.tmp / "repo"
        (repo / "src").mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        self.assertEqual(vault.vault_root_hint(repo / "src"), Path(os.path.realpath(repo)) / "docs" / "vault")
        os.environ["AGENT_VAULT_DIR"] = "shared/{project}"
        self.assertEqual(vault.vault_root_hint(repo), Path(os.path.realpath(repo)) / "shared")

    def test_relative_templates_match_vault_resolve(self):
        repo = self.tmp / "repo"
        (repo / "src").mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        real = Path(os.path.realpath(repo))
        cases = {
            "./{project}": None,
            "{project}": None,
            "docs/{project}/notes": real / "docs",
            "notes/{project}": real / "notes",
            "vault": real / "vault",
            "..": None,
            "../{project}": None,
        }
        for tpl, want in cases.items():
            os.environ["AGENT_VAULT_DIR"] = tpl
            self.assertEqual(vault.vault_root_hint(repo / "src"), want, tpl)

    def test_absolute_templates_unchanged(self):
        repo = self.tmp / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        os.environ["AGENT_VAULT_DIR"] = str(self.tmp / "abs" / "{project}")
        self.assertEqual(vault.vault_root_hint(repo), self.tmp / "abs")
        os.environ["AGENT_VAULT_DIR"] = str(self.tmp / "{project}")
        self.assertEqual(vault.vault_root_hint(repo), self.tmp)

    def test_hint_never_runs_the_cli(self):
        fv = fakevault.install(self.tmp / "v")
        os.environ["AGENT_VAULT_CLI"] = str(fv.cli)
        vault.vault_root_hint()
        self.assertEqual(fv.calls(), [])


if __name__ == "__main__":
    unittest.main()
