"""Claude Code: hook payloads, `claude -p` commands, Agent-tool lines, ~/.claude/projects transcripts, .md agents."""
from __future__ import annotations

import os
import re
from pathlib import Path

from .base import HookEvent, Platform, task_prompt

EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
SKILL_PROMPT = r"^\s*/(agent-verify-loop:)?(verify-loop|verify-review|verify-qa|verify-retro|retro)(\s.*)?$"
# Non-human prompts injected by Claude Code (subagent hand-backs, background-task notices, reminders)
INJECTED_PROMPT = (r"^\s*(<(agent-message|task-notification|system-reminder|teammate-message|local-command-stdout|"
                   r"local-command-caveat)[\s>]|verify-loop gate:|verify-loop: verification passed)")


def _str(v) -> str | None:
    return v if isinstance(v, str) else None


def bash_edit_paths(tool_response) -> list[str]:
    bed = tool_response.get("bashEditDiff") if isinstance(tool_response, dict) else None
    if not isinstance(bed, dict):
        return []
    if isinstance(bed.get("changedFiles"), list) and bed["changedFiles"]:
        return [p for p in bed["changedFiles"] if isinstance(p, str)]
    if isinstance(bed.get("files"), list):
        return [f["filePath"] for f in bed["files"] if isinstance(f, dict) and f.get("filePath")]
    return []


class ClaudePlatform(Platform):
    name = "claude"
    skill_prompt_re = SKILL_PROMPT
    injected_prompt_re = INJECTED_PROMPT

    def normalize(self, event: str, payload: dict) -> HookEvent:
        tool = _str(payload.get("tool_name")) or ""
        ti = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
        edit_paths: list[str] = []
        bash_cmd = None
        if tool in EDIT_TOOLS:
            p = ti.get("file_path") or ti.get("notebook_path")
            edit_paths = [p] if isinstance(p, str) and p else []
        elif tool == "Bash":
            bash_cmd = _str(ti.get("command"))
            edit_paths = bash_edit_paths(payload.get("tool_response"))
        return HookEvent(
            event=event, sid=_str(payload.get("session_id")) or "", cwd=_str(payload.get("cwd")),
            transcript=_str(payload.get("transcript_path")), tool=tool, edit_paths=edit_paths, bash_cmd=bash_cmd,
            agent_type=_str(payload.get("agent_type")) or "", agent_id=_str(payload.get("agent_id")),
            prompt=_str(payload.get("prompt")), last_msg=_str(payload.get("last_assistant_message")),
            stop_active=bool(payload.get("stop_hook_active")), raw=payload)

    def session_id_from_env(self) -> str | None:
        return os.environ.get("CLAUDE_CODE_SESSION_ID") or None

    def triage_cmd(self, prefix: list[str] | None, cfg: dict, schema_json: str, prompt: str) -> list[str]:
        t = cfg.get("triage") or {}
        prefix = prefix or [t.get("claude_bin") or "claude"]
        return [*prefix, "-p", "--agent", "verify-triage", "--model", str(t.get("model") or "sonnet"),
                "--effort", str(t.get("effort") or "medium"), "--output-format", "json", "--json-schema", schema_json,
                "--tools", "Read,Grep,Glob,StructuredOutput", "--strict-mcp-config",
                "--settings", '{"disableAllHooks":true}',
                "--permission-mode", "default", "--permission-prompts", "none", "--no-session-persistence",
                "--max-budget-usd", str(t.get("max_budget_usd", 0.60)), prompt]

    def headless_cmd(self, role: str, *, agent: str, model: str, effort: str, prompt: str,
                     mcp_config: str | Path | None = None, allowed_tools: list[str] | None = None,
                     permission_mode: str | None = None) -> list[str]:
        cmd = ["claude", "-p", prompt, "--agent", agent, "--model", model, "--effort", effort, "--output-format",
               "json", "--permission-mode", permission_mode or "auto", "--strict-mcp-config"]
        if mcp_config:
            cmd += ["--mcp-config", str(mcp_config)]
        if allowed_tools:
            cmd += ["--allowedTools", *allowed_tools]
        return cmd

    def spawn_instructions(self, role: str, *, agent: str, model: str, attempt: int, prompt_path: str | Path) -> str:
        return (f'Agent(subagent_type="{agent}", model="{model}", description="verify {role} #{attempt}", '
                f'prompt="{task_prompt(prompt_path)}")')

    def transcript_for(self, sid: str | None) -> Path | None:
        from .. import metrics

        return metrics.find_session_transcript(sid)

    def parse_usage(self, path: str | Path) -> list[dict]:
        from .. import metrics

        return metrics.usage_entries(path)

    def render_agent(self, md_text: str) -> tuple[str, str]:
        m = re.search(r"^name:\s*['\"]?([A-Za-z0-9_.:-]+)", md_text, re.M)
        if not m:
            raise ValueError("agent file has no name: in its frontmatter")
        return f"{m.group(1)}.md", md_text
