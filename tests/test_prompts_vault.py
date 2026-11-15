"""WP4: task-file rendering with and without agent-vault: optional blocks, tool paths, agent-neutral wording."""
from __future__ import annotations

import json
import os
import re
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fakevault  # noqa: E402
from helpers import SKILL, VL, HermeticCase  # noqa: E402

from vlooplib import prompts  # noqa: E402

LESSON = {"link": "p/lessons/a", "title": "A", "rule": "Run the tests before saying done", "occurrences": 3}
NOTE = {"link": "p/modules/a", "title": "Module A", "summary": "what module a does", "type": "module", "stale": False}


class RenderCase(HermeticCase):
    def setUp(self):
        super().setUp()
        self.repo = self.tmp / "repo"
        (self.repo / "src").mkdir(parents=True)
        (self.repo / "AGENTS.md").write_text("agents")
        (self.repo / "src" / "CLAUDE.md").write_text("claude")
        self.run_dir = self.tmp / "run"
        self.att = self.run_dir / "attempt-1"
        self.att.mkdir(parents=True)
        (self.att / "diff.numstat.json").write_text(json.dumps({
            "rows": [{"path": "src/a.py", "flags": ["source"]}], "source_files": ["src/a.py"]}))
        (self.run_dir / "prompts.md").write_text("### Prompt 1 (10:00)\nfix it")
        (self.run_dir / "brief.md").write_text("brief")
        self.ctx = {"run_id": "r1", "repo": {"root": str(self.repo), "slug": "toy", "key": "k", "branch": "main"},
                    "target": {"spec": "git", "base_kind": "head", "code_ref": "worktree"}, "session_id": None}
        self.plan = {"roles": {"reviewer": {"mode": "full", "tier": "standard", "model": "sonnet", "timeout_s": 1200},
                               "qa": {"scope": "checks", "timeout_s": 1200}}}

    def render(self, role):
        out = prompts.render_role(role, run_dir=self.run_dir, ctx=self.ctx, attempt=1, plan=self.plan, cfg={},
                                  project={})
        return out.read_text()

    def install_vault(self, **cfg):
        fv = fakevault.install(self.tmp / "av", **cfg)
        os.environ["AGENT_VAULT_CLI"] = str(fv.cli)
        return fv


class TestOptionalBlocks(HermeticCase):
    def test_block_kept_when_value_present_and_dropped_when_empty(self):
        tpl = "A\n<!-- if x -->\nBefore {{x}}\n<!-- endif -->\nB\n"
        self.assertEqual(prompts.render(tpl, {"x": "1"}), "A\nBefore 1\nB\n")
        self.assertEqual(prompts.render(tpl, {"x": ""}), "A\nB\n")
        self.assertEqual(prompts.render(tpl, {"x": "  \n"}), "A\nB\n")

    def test_plain_placeholders_unchanged_and_unknown_still_fails(self):
        self.assertEqual(prompts.render("{{a}} {{b}}", {"a": "1", "b": ""}), "1 (none)")
        with self.assertRaises(KeyError):
            prompts.render("{{nope}}", {})

    def test_two_blocks_are_independent(self):
        tpl = "<!-- if a -->\nA={{a}}\n<!-- endif -->\n<!-- if b -->\nB={{b}}\n<!-- endif -->\n"
        self.assertEqual(prompts.render(tpl, {"a": "", "b": "2"}), "B=2\n")


class TestToolValues(HermeticCase):
    def test_absolute_paths_and_no_vault(self):
        v = prompts.tool_values()
        self.assertEqual(v, {"vloop": VL, "vault_cli": "", "vault_ops_retro": ""})
        self.assertTrue(os.path.isabs(v["vloop"]))

    def test_with_vault(self):
        fv = fakevault.install(self.tmp / "av")
        os.environ["AGENT_VAULT_CLI"] = str(fv.cli)
        v = prompts.tool_values()
        self.assertEqual((v["vault_cli"], v["vault_ops_retro"]), (str(fv.cli), str(fv.ops)))


class TestRoleTasks(RenderCase):
    def test_without_vault_no_lessons_or_notes_blocks(self):
        for role in ("reviewer", "qa"):
            text = self.render(role)
            self.assertNotIn("Lessons from past retros", text, role)
            self.assertNotIn("Vault notes covering", text, role)
            self.assertNotIn("{{", text)
            self.assertNotIn("<!--", text)
            self.assertIn("## Output", text)

    def test_with_vault_lessons_and_notes_included(self):
        fv = self.install_vault(lessons=[{**LESSON, "rule": LESSON["rule"]}], covering=[NOTE])
        for role in ("reviewer", "qa"):
            text = self.render(role)
            self.assertIn("Lessons from past retros (follow them):", text, role)
            self.assertIn("Run the tests before saying done", text, role)
            self.assertIn("[[p/modules/a]]", text, role)
            self.assertNotIn("<!--", text)
        audiences = {c[c.index("--audience") + 1] for c in fv.calls() if c[0] == "lessons"}
        self.assertEqual(audiences, {"reviewer", "qa"})

    def test_vault_without_lessons_drops_only_that_block(self):
        self.install_vault(lessons=[], covering=[NOTE])
        text = self.render("reviewer")
        self.assertNotIn("Lessons from past retros", text)
        self.assertIn("Vault notes covering the changed code", text)

    def test_instruction_files_include_agents_md(self):
        self.assertEqual(prompts.claude_md_files(str(self.repo), ["src/a.py"]).splitlines(),
                         ["AGENTS.md", "src/CLAUDE.md"])
        self.assertIn("src/CLAUDE.md", self.render("reviewer"))


class TestWording(HermeticCase):
    def text(self, name):
        return (SKILL / "prompts" / f"{name}.md").read_text(encoding="utf-8")

    def test_retro_template_declares_tool_lines(self):
        t = self.text("retro")
        self.assertRegex(t, r"(?m)^vloop: \{\{vloop\}\}$")
        self.assertRegex(t, r"(?m)^vault_cli: \{\{vault_cli\}\}$")
        self.assertRegex(t, r"(?m)^ops: \{\{vault_ops_retro\}\}$")

    def test_no_claude_code_only_instructions(self):
        for name in ("retro", "reviewer", "specialist", "qa", "triage"):
            t = self.text(name)
            self.assertNotIn("Claude Code session", t, name)
            self.assertNotRegex(t, r"with the Write tool", name)
            self.assertNotIn("Agent tool", t, name)

    def test_writing_tool_named_per_agent(self):
        for name in ("reviewer", "specialist", "qa"):
            self.assertIn("Write in Claude Code, apply_patch in Codex", self.text(name))

    def test_placeholders_in_retro_are_all_supplied_by_render_task(self):
        used = set(re.findall(r"\{\{\s*([A-Za-z0-9_.-]+)\s*\}\}", self.text("retro")))
        self.assertEqual(used, {"run_id", "platform", "vault_ops_retro", "vloop", "vault_cli", "session_id",
                                "repo_root", "project_slug", "harness_project", "policy", "metrics_path",
                                "digest_path", "judge_paths"})



PREV_JUDGE = {"findings": [
    {"role": "reviewer", "id": "R1", "severity": "major", "category": "correctness", "blocking": True,
     "file": "src/a.py", "line": 3, "title": "reviewer thing"},
    {"role": "qa", "id": "Q1", "severity": "blocker", "category": "acceptance", "blocking": True,
     "check_id": "C1", "title": "qa thing"},
    {"role": "security", "id": "SC1", "severity": "major", "category": "security", "blocking": True,
     "file": "src/a.py", "line": 4, "title": "security thing"},
    {"role": "perf", "id": "PF1", "severity": "minor", "category": "performance", "blocking": False,
     "file": "src/a.py", "line": 5, "title": "perf thing"},
]}


class TestSpecialistTasks(RenderCase):
    def setUp(self):
        super().setUp()
        for role in ("security", "perf", "ux"):
            self.plan["roles"][role] = {"mode": "full", "tier": "standard", "model": "sonnet", "timeout_s": 900}
        (self.att / "triage.json").write_text(json.dumps({"specialists": {
            "security": {"run": True, "reason": "touches the auth middleware", "focus": ["token refresh"]},
            "perf": {"run": True, "reason": "", "focus": []},
            "ux": {"run": True, "ui_spec": "partial", "reason": "new form", "focus": ["error state"]}}}))

    def test_specialist_task_files(self):
        for role, fp, sp in (("security", "SC", "SCS"), ("perf", "PF", "PFS"), ("ux", "UX", "UXS")):
            out = prompts.render_role(role, run_dir=self.run_dir, ctx=self.ctx, attempt=1, plan=self.plan, cfg={},
                                      project={})
            self.assertEqual(out.name, f"{role}.prompt.md")
            text = out.read_text()
            self.assertNotIn("{{", text, role)
            self.assertNotIn("<!--", text, role)
            self.assertTrue(text.startswith(f"# {role} review · run r1 · attempt 1"), role)
            self.assertIn(f'"role": "{role}"', text)
            self.assertIn(f"`{fp}1`", text)
            self.assertIn(f"`{sp}1`", text)
            self.assertIn(prompts.CHARTERS[role], text)
            self.assertIn(str(self.att / f"{role}.json"), text)
            if role == "security":
                self.assertIn("- token refresh", text)
                self.assertIn("Why triage turned you on: touches the auth middleware", text)
                self.assertIn("security evidence rules", text)
            else:
                self.assertIn("never block", text)
            if role == "ux":
                self.assertIn("UI spec in the request or brief (triage): partial.", text)
                self.assertIn("- error state", text)

    def test_specialist_without_triage_entry(self):
        (self.att / "triage.json").unlink()
        text = self.render("perf")
        self.assertIn(prompts.CHARTERS["perf"], text)
        self.assertNotIn("Why triage turned you on", text)

    def test_reviewer_specialist_note(self):
        text = self.render("reviewer")
        self.assertNotIn("Specialist reviewers in this run", text)
        out = prompts.render_role("reviewer", run_dir=self.run_dir, ctx=self.ctx, attempt=1, plan=self.plan, cfg={},
                                  project={}, specialists=["ux", "security"])
        text = out.read_text()
        self.assertIn("## Specialist reviewers in this run", text)
        self.assertIn("Skip §3.5 (Security)", text)
        self.assertIn("A UX specialist reviews", text)
        self.assertNotIn("§3.7", text)
        self.assertLess(text.index("Skip §3.5"), text.index("A UX specialist"))
        self.assertEqual(prompts.specialist_note([]), "")
        self.assertIn("Skip §3.7 (Performance traps)", prompts.specialist_note(["perf"]))

    def test_previous_findings_filtered_by_role(self):
        def ids(text):
            return [ln.split()[1] for ln in text.splitlines() if ln.startswith("- ")]

        self.assertEqual(ids(prompts.previous_findings(PREV_JUDGE)), ["R1", "Q1", "SC1", "PF1"])
        self.assertEqual(ids(prompts.previous_findings(PREV_JUDGE, "reviewer")), ["R1", "Q1"])
        self.assertEqual(ids(prompts.previous_findings(PREV_JUDGE, "qa")), ["R1", "Q1"])
        self.assertEqual(ids(prompts.previous_findings(PREV_JUDGE, "security")), ["SC1"])
        self.assertEqual(ids(prompts.previous_findings(PREV_JUDGE, "ux")), [])

    def test_rerun_task_gets_own_previous_findings(self):
        att2 = self.run_dir / "attempt-2"
        att2.mkdir()
        (att2 / "diff.numstat.json").write_text((self.att / "diff.numstat.json").read_text())
        (self.att / "judge.json").write_text(json.dumps(PREV_JUDGE))
        out = prompts.render_role("security", run_dir=self.run_dir, ctx=self.ctx, attempt=2, plan=self.plan, cfg={},
                                  project={})
        text = out.read_text()
        self.assertIn("SC1 [major·security·blocking]", text)
        self.assertNotIn("R1 [", text)
        text = prompts.render_role("reviewer", run_dir=self.run_dir, ctx=self.ctx, attempt=2, plan=self.plan,
                                   cfg={}, project={}).read_text()
        self.assertIn("R1 [", text)
        self.assertNotIn("SC1 [", text)


if __name__ == "__main__":
    unittest.main()
