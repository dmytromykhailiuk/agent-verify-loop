"""Tests for WP5's task templates prompts/{triage,reviewer,qa}.md (§2.15.1).
Run: python3 -m unittest discover -s tests"""
from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from helpers import SKILL, HermeticCase  # noqa: E402

from vlooplib import util  # noqa: E402

PROMPTS = SKILL / "prompts"
PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z0-9_.-]+)\s*\}\}")

COMMON = ("run_id attempt mode repo_root repo_slug branch code_ref_note run_dir attempt_dir verdict_path "
          "time_budget_min user_prompts brief focus diff_summary patch_path delta_path previous_findings fix_notes "
          "disputes user_guidance lessons vault_notes repo_claude_md").split()
ALLOWED = {
    "triage": set("run_id attempt repo_slug user_prompts brief diff_stat changed_files signals project_facts "
                  "auth_status diff_excerpt previous_plan delta_stat".split()),
    "reviewer": set(COMMON + "review_tier baseline_note outside_files".split()),
    "qa": set(COMMON + "qa_scope qa_reason project_facts verify_json recipes app_url auth_status artifacts_dir "
                       "logs_dir browser_note".split()),
}
# Placeholders each template must use (the required blocks are carried by these values).
REQUIRED = {
    "triage": ALLOWED["triage"],
    "reviewer": ALLOWED["reviewer"],
    "qa": ALLOWED["qa"],
}
# Required literal text per template (§2.15.1 "Template structure").
PHRASES = {
    "triage": ["Answer with the JSON object only."],
    "reviewer": ["implementer claims — verify them", "summaries only, do not open the vault",
                 "write the file, then end with a ≤10-line summary and the same JSON in a ```json fence"],
    "qa": ["implementer claims — verify them", "summaries only, do not open the vault",
           "write the file, then end with a ≤10-line summary and the same JSON in a ```json fence"],
}


def text(name: str) -> str:
    return (PROMPTS / f"{name}.md").read_text(encoding="utf-8")


def dummy(names) -> dict:
    return {n: f"<<{n.upper()}>>" for n in names}


class TemplateTest(HermeticCase):
    def test_only_allowed_placeholders(self):
        for name, allowed in ALLOWED.items():
            used = set(PLACEHOLDER.findall(text(name)))
            self.assertFalse(used - allowed, f"{name}.md uses unknown placeholders {sorted(used - allowed)}")

    def test_required_placeholders_present(self):
        for name, req in REQUIRED.items():
            used = set(PLACEHOLDER.findall(text(name)))
            self.assertFalse(req - used, f"{name}.md misses placeholders {sorted(req - used)}")

    def test_required_phrases(self):
        for name, phrases in PHRASES.items():
            t = text(name).lower()
            for ph in phrases:
                self.assertIn(ph.lower(), t, f"{name}.md lacks {ph!r}")

    def test_triage_shows_exact_output_shape(self):
        # live triage never saw the schema and invented flat keys (WP7 bug #1): the template carries the shape
        import json
        m = re.search(r"## Output[^\n]*\n.*?```json\n(.*?)```", text("triage"), re.S)
        self.assertIsNotNone(m)
        shown = json.loads(m.group(1))
        schema = json.loads((SKILL / "schema" / "triage.output.schema.json").read_text())

        def keys(sch):
            return {k: keys(v) if v.get("type") == "object" else None for k, v in sch["properties"].items()}

        def shape(obj):
            return {k: shape(v) if isinstance(v, dict) else None for k, v in obj.items()}

        self.assertEqual(shape(shown), keys(schema))
        for k, v in schema["properties"].items():
            if "enum" in v:
                self.assertEqual(shown[k], " | ".join(v["enum"]))

    def test_triage_ends_with_closing_line(self):
        self.assertEqual(text("triage").rstrip().splitlines()[-1], "Answer with the JSON object only.")

    def test_block_order(self):
        order = {
            "triage": ["user_prompts", "brief", "diff_stat", "changed_files", "signals", "project_facts",
                       "auth_status", "previous_plan", "delta_stat", "diff_excerpt"],
            "reviewer": ["run_id", "repo_root", "code_ref_note", "time_budget_min", "review_tier", "user_prompts",
                         "brief", "focus", "diff_summary", "patch_path", "baseline_note", "outside_files",
                         "delta_path", "previous_findings", "fix_notes", "disputes", "user_guidance",
                         "repo_claude_md", "vault_notes", "lessons", "verdict_path"],
            "qa": ["run_id", "qa_scope", "repo_root", "code_ref_note", "time_budget_min", "qa_reason", "user_prompts", "brief",
                   "focus", "diff_summary", "patch_path", "delta_path", "previous_findings", "fix_notes",
                   "disputes", "user_guidance", "repo_claude_md", "project_facts", "verify_json", "recipes",
                   "app_url", "auth_status", "browser_note", "verdict_path"],
        }
        for name, names in order.items():
            t = text(name)
            pos = [t.index("{{" + n + "}}") for n in names]
            self.assertEqual(pos, sorted(pos), f"{name}.md blocks out of order")

    def test_renders_without_leftovers(self):
        for name, allowed in ALLOWED.items():
            out = util.render_template(text(name), dummy(allowed))
            self.assertNotIn("{{", out, name)
            self.assertNotIn("}}", out, name)
            for n in PLACEHOLDER.findall(text(name)):
                self.assertIn(f"<<{n.upper()}>>", out)

    def test_empty_values_render_none(self):
        out = util.render_template(text("reviewer"), {n: "" for n in ALLOWED["reviewer"]})
        self.assertIn("(none)", out)
        self.assertNotIn("{{", out)

    def test_unknown_placeholder_would_fail(self):
        with self.assertRaises(KeyError):
            util.render_template(text("qa"), dummy(ALLOWED["qa"] - {"browser_note"}))

    def test_qa_mentions_artifacts_and_scope(self):
        t = text("qa")
        for n in ("artifacts_dir", "logs_dir", "qa_reason", "browser_note"):
            self.assertIn("{{" + n + "}}", t)

    def test_optional_blocks_are_balanced_and_documented(self):
        from vlooplib import prompts

        doc = (SKILL.parent.parent / "docs" / "templates.md").read_text(encoding="utf-8")
        self.assertIn("<!-- if name -->", doc)
        self.assertIn("<!-- endif -->", doc)
        for name in ALLOWED:
            body = text(name)
            opens = re.findall(r"<!--\s*if\s+([A-Za-z0-9_.-]+)\s*-->", body)
            self.assertEqual(len(opens), len(re.findall(r"<!--\s*endif\s*-->", body)), name)
            for cond in opens:
                self.assertIn(cond, ALLOWED[name], f"{name}.md: block on unknown placeholder {cond}")
                self.assertIn(cond, doc, f"{cond} is not documented")
                filled = prompts.render(body, dummy(ALLOWED[name]))
                empty = prompts.render(body, {**dummy(ALLOWED[name]), cond: ""})
                self.assertIn(f"<<{cond.upper()}>>", filled)
                self.assertNotIn("<!--", empty)
                self.assertNotIn(f"<<{cond.upper()}>>", empty)
                self.assertLess(len(empty), len(filled))

    def test_no_machine_specific_paths(self):
        for name in ALLOWED:
            self.assertNotIn("~/.claude/skills", text(name), name)


if __name__ == "__main__":
    unittest.main()
