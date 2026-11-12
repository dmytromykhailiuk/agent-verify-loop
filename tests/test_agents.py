"""Tests for the agent definitions (skills/verify-loop/agents/verify-*.md) and skills (§2.15.2–§2.15.4, WP5).
Run: python3 -m unittest discover -s tests"""
from __future__ import annotations

import os
import re
import sys
import textwrap
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from helpers import AGENTS_SRC as AGENTS, SKILLS, VLOOP as LIB_VLOOP, HermeticCase  # noqa: E402

FIX = HERE / "fixtures" / "wp5"
GUARD = "{{{{SKILL_DIR}}}}/bin/vloop guard {role}"
LOOP_TOOLS = "Bash(${CLAUDE_SKILL_DIR}/bin/vloop *)"
SIBLING_TOOLS = "Bash(${CLAUDE_SKILL_DIR}/../verify-loop/bin/vloop *)"


def parse_frontmatter(path: Path) -> tuple[dict, str]:
    """Minimal frontmatter parser: `key: value` scalars (quotes stripped); a key with an empty value takes the
    following indented lines verbatim (dedented) as its value. Returns (fields, body)."""
    text = path.read_text(encoding="utf-8")
    lines = text.split("\n")
    assert lines[0] == "---", f"{path}: no frontmatter"
    end = lines.index("---", 1)
    fm, body = lines[1:end], "\n".join(lines[end + 1:])
    out: dict = {}
    i = 0
    while i < len(fm):
        line = fm[i]
        m = re.match(r"^([A-Za-z][\w-]*):\s?(.*)$", line)
        assert m, f"{path}: bad frontmatter line {line!r}"
        key, val = m.group(1), m.group(2)
        assert key not in out, f"{path}: duplicate key {key}"
        if val == "":
            block = []
            i += 1
            while i < len(fm) and (fm[i].startswith(" ") or fm[i] == ""):
                block.append(fm[i])
                i += 1
            out[key] = textwrap.dedent("\n".join(block)).strip("\n")
            continue
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1].replace('\\"', '"')
        out[key] = val
        i += 1
    return out, body


def norm(s: str) -> str:
    return " ".join(s.split())


def guard_hooks(role: str) -> str:
    return textwrap.dedent(f"""\
        PreToolUse:
          - matcher: "Bash|Write|Edit|MultiEdit|NotebookEdit"
            hooks:
              - type: command
                command: "{GUARD.format(role=role)}"
                timeout: 5""")


EXPECTED_AGENTS = {
    "verify-triage": {
        "name": "verify-triage",
        "description": "Cheap, independent triage for the verify loop. Reads the user's request, the acceptance "
                       "criteria and a capped diff, and decides whether QA adds value (none / checks / run / browser) "
                       "and the reviewer tier (standard Sonnet / deep Opus). Returns one JSON object. Run headless by "
                       "vloop (`claude -p --agent verify-triage` or `codex exec`), never as an in-session "
                       "sub-agent.",
        "tools": "Read, Grep, Glob, StructuredOutput",
        "model": "haiku",
        "effort": "low",
        "maxTurns": "6",
        "color": "yellow",
    },
    "verify-reviewer": {
        "name": "verify-reviewer",
        "description": "Independent code reviewer for the verify loop. Reviews a given diff against the user's "
                       "request and acceptance criteria and returns a structured JSON verdict. Blocks only on "
                       "evidenced correctness, security, data, contract, acceptance, demonstrated-convention or build "
                       "problems; everything else is a suggestion. Read-only. Dispatched by the verify-loop / "
                       "verify-review skills with a task file.",
        "tools": "Read, Grep, Glob, Bash, Write",
        "disallowedTools": "Edit, MultiEdit, NotebookEdit, Agent, Skill, WebFetch, WebSearch",
        "model": "sonnet",
        "effort": "medium",
        "permissionMode": "auto",
        "color": "purple",
        "hooks": guard_hooks("reviewer"),
    },
    "verify-qa": {
        "name": "verify-qa",
        "description": "Independent QA agent for the verify loop. Exercises a code change at the scope the triage "
                       "chose — typecheck/lint/build and related tests, running the app/CLI/HTTP endpoints, and a "
                       "real browser via Playwright for web UIs — and returns a structured JSON verdict with evidence "
                       "(screenshots, console/network errors, command output). Never edits project files. Dispatched "
                       "by the verify-loop / verify-qa skills with a task file.",
        "tools": "Read, Grep, Glob, Bash, Write, Skill, mcp__playwright",
        "disallowedTools": "Edit, MultiEdit, NotebookEdit, Agent, WebSearch, WebFetch",
        "model": "sonnet",
        "effort": "medium",
        "permissionMode": "auto",
        "color": "green",
        "mcpServers": textwrap.dedent("""\
            - playwright:
                type: stdio
                command: "{{SKILL_DIR}}/bin/pw-mcp"
                args: []"""),
        "hooks": guard_hooks("qa"),
    },
}

EXPECTED_SKILLS = {
    "verify-loop": {
        "name": "verify-loop",
        "argument-hint": "[status|reset] [--qa none|checks|run|browser] [--review standard|deep] [--no-triage] "
                         "[--repo <path>]",
        "allowed-tools": LOOP_TOOLS,
    },
    "verify-review": {
        "name": "verify-review",
        "argument-hint": "[<A..B>|<branch>|pr:<id>] [--focus \"…\"] [--criteria \"…\"] "
                         "[--review standard|deep] [--model sonnet|opus]",
        "disable-model-invocation": "true",
        "allowed-tools": SIBLING_TOOLS,
    },
    "verify-qa": {
        "name": "verify-qa",
        "argument-hint": "[<url>] [<A..B>|<branch>|pr:<id>] [--focus \"…\"] [--criteria \"…\"] "
                         "[--qa checks|run|browser] [--model haiku|sonnet|opus]",
        "disable-model-invocation": "true",
        "allowed-tools": SIBLING_TOOLS,
    },
    "verify-retro": {
        "name": "verify-retro",
        "argument-hint": "[--run <id>] [--session <id>] [--proposals]",
        "disable-model-invocation": "true",
        "allowed-tools": SIBLING_TOOLS,
    },
}


class AgentFrontmatterTest(HermeticCase):
    def test_frontmatter_exact(self):
        for name, expected in EXPECTED_AGENTS.items():
            fm, _ = parse_frontmatter(AGENTS / f"{name}.md")
            self.assertEqual(fm, expected, name)

    def test_triage_body(self):
        _, body = parse_frontmatter(AGENTS / "verify-triage.md")
        self.assertLessEqual(len(body.strip("\n").split("\n")), 80)
        self.assertIn(norm((FIX / "triage-rubric.txt").read_text()), norm(body))

    def test_reviewer_rubric_verbatim(self):
        _, body = parse_frontmatter(AGENTS / "verify-reviewer.md")
        self.assertIn(norm((FIX / "reviewer-rubric.txt").read_text()), norm(body))

    def test_reviewer_sections_in_order(self):
        _, body = parse_frontmatter(AGENTS / "verify-reviewer.md")
        heads = ["Role & independence", "Procedure", "What to check", "Calibration", "Severity",
                 "Re-review protocol", "Size policy", "Output contract"]
        pos = [body.index(h) for h in heads]
        self.assertEqual(pos, sorted(pos))
        for term in ("rechecked", "dispute_ruling", "files_skipped", "```json"):
            self.assertIn(term, body)

    def test_qa_sections_in_order(self):
        _, body = parse_frontmatter(AGENTS / "verify-qa.md")
        heads = ["Role", "Procedure", "Modes", "Verdict rules", "Output contract"]
        pos = [body.index("## " + str(i + 1) + ". " + h) for i, h in enumerate(heads)]
        self.assertEqual(pos, sorted(pos))
        for term in ("status-before.txt", "status-after.txt", "tree_unchanged", "browser_console_messages",
                     "browser_network_requests", "auth_missing", "auth_expired", "pre_existing",
                     "user declined browser QA", "recheck", "delta", "npm ci", "```json"):
            self.assertIn(term, body)


class AgentRenderTest(HermeticCase):
    def test_placeholders_only_in_frontmatter(self):
        for src in sorted(AGENTS.glob("*.md")):
            _, body = parse_frontmatter(src)
            self.assertNotIn("{{", body, src.name)

    def test_claude_render_resolves_placeholders(self):
        from vlooplib import paths, setup

        out = setup.render_agent_files("claude", self.tmp / "claude")
        self.assertEqual({p.name for p in out}, {"verify-triage.md", "verify-reviewer.md", "verify-qa.md"})
        for path, text in out.items():
            self.assertNotIn("{{", text, path.name)
        reviewer = next(v for k, v in out.items() if k.name == "verify-reviewer.md")
        self.assertIn(f"{paths.cli()} guard reviewer", reviewer)
        qa = next(v for k, v in out.items() if k.name == "verify-qa.md")
        self.assertIn(f"{paths.cli()} guard qa", qa)
        self.assertIn(f'command: "{paths.pw_mcp()}"', qa)

    def test_codex_render_parses(self):
        try:
            import tomllib
        except ImportError:
            self.skipTest("tomllib needs Python 3.11")
        from vlooplib import setup

        out = setup.render_agent_files("codex", self.tmp / "codex")
        self.assertEqual({p.name for p in out}, {"verify-triage.toml", "verify-reviewer.toml", "verify-qa.toml"})
        for path, text in out.items():
            doc = tomllib.loads(text)
            self.assertEqual(doc["name"], path.stem)
            self.assertTrue(doc["description"])
            self.assertNotIn("{{", doc["developer_instructions"])
            self.assertNotIn("hooks", doc)
            self.assertNotIn("mcpServers", doc)

    def test_verifiers_name_their_write_tool_for_both_platforms(self):
        for name in ("verify-reviewer", "verify-qa"):
            _, body = parse_frontmatter(AGENTS / f"{name}.md")
            self.assertIn("apply_patch", body, name)
            self.assertIn("AGENTS.md", body, name)


class SkillTest(HermeticCase):
    def test_frontmatter(self):
        for name, expected in EXPECTED_SKILLS.items():
            fm, _ = parse_frontmatter(SKILLS / name / "SKILL.md")
            for k, v in expected.items():
                self.assertEqual(fm.get(k), v, f"{name}: {k}")
            allowed = set(expected) | {"description"}
            self.assertFalse(set(fm) - allowed, f"{name}: extra keys {set(fm) - allowed}")
            self.assertTrue(fm.get("description", "").strip(), name)

    def test_descriptions_name_both_invocations(self):
        for name in EXPECTED_SKILLS:
            fm, _ = parse_frontmatter(SKILLS / name / "SKILL.md")
            self.assertIn(f"/{name}", fm["description"], name)
            self.assertIn(f"${name}", fm["description"], name)

    def test_verify_qa_description(self):
        fm, _ = parse_frontmatter(SKILLS / "verify-qa" / "SKILL.md")
        d = fm["description"]
        self.assertIn("QA", d)
        self.assertIn("URL", d)

    def test_verify_loop_body(self):
        _, body = parse_frontmatter(SKILLS / "verify-loop" / "SKILL.md")
        self.assertLessEqual(len(body.strip("\n").split("\n")), 160)
        for sub in ("status", "reset", "begin", "brief", "consent", "spawn", "spawn-wait", "collect", "record",
                    "judge", "dispute", "decide", "retro-plan", "skip", "defer"):
            self.assertRegex(body, r"\$VL " + re.escape(sub) + r"\b", sub)
        for term in ("<skill_dir>/bin/vloop", "$ARGUMENTS", "begin --rerun", "--fix-notes -",
                     "## Acceptance criteria", "## How to exercise", "## Out of scope", "CONSENT NEEDED",
                     "Browser QA", "Verify loop", "AskUserQuestion", "--app-url", "## Launch",
                     "agent-verify-loop:verify-reviewer", "Codex", "<skill_dir>/agents/<agent>.md",
                     "not part of this change", "no agent-vault"):
            self.assertIn(term, body)
        steps = [body.index(f"\n{i}. ") for i in range(0, 9)]
        self.assertEqual(steps, sorted(steps))

    def test_no_herdr_in_skills(self):
        for name in EXPECTED_SKILLS:
            text = (SKILLS / name / "SKILL.md").read_text().lower()
            self.assertNotIn("herdr", text, name)

    def test_standalone_skill_commands(self):
        _, review = parse_frontmatter(SKILLS / "verify-review" / "SKILL.md")
        self.assertIn("$VL begin --standalone --roles reviewer", review)
        self.assertIn("$VL judge --run", review)
        self.assertIn("<skill_dir>/../verify-loop/bin/vloop", review)
        _, qa = parse_frontmatter(SKILLS / "verify-qa" / "SKILL.md")
        self.assertIn("$VL begin --standalone --roles qa", qa)
        self.assertIn("CONSENT NEEDED", qa)
        self.assertIn("$VL consent", qa)
        self.assertIn("$VL judge --run", qa)
        self.assertIn("<skill_dir>/../verify-loop/bin/vloop", qa)
        _, retro = parse_frontmatter(SKILLS / "verify-retro" / "SKILL.md")
        for term in ("lessons --proposals", "proposal accept", "proposal reject", "$VL retro-prep",
                     "CODEX_THREAD_ID", "--run <id>", "AskUserQuestion", "no agent-vault", "retro-plan"):
            self.assertIn(term, retro)

    def test_cli_commands_exist(self):
        import subprocess

        known = subprocess.run([sys.executable, str(LIB_VLOOP), "--help"], capture_output=True, text=True,
                               env=os.environ.copy()).stdout
        for name in EXPECTED_SKILLS:
            _, body = parse_frontmatter(SKILLS / name / "SKILL.md")
            for sub in set(re.findall(r"\$VL ([a-z][a-z-]+)", body)):
                self.assertRegex(known, r"(?m)^    " + re.escape(sub) + r"\s", f"{name}: unknown vloop command {sub}")


if __name__ == "__main__":
    unittest.main()
