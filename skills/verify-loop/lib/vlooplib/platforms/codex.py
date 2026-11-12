"""Codex CLI platform."""
from __future__ import annotations

import os
from pathlib import Path

from . import applypatch
from .base import HookEvent, Platform

PATCH_TOOLS = ("apply_patch", "Edit", "Write", "MultiEdit")
SHELL_TOOLS = ("Bash", "shell", "exec_command", "local_shell")
SHELLS = ("bash", "sh", "zsh")
SKILL_PROMPT = r"^\s*[/$](agent-verify-loop:)?(verify-loop|verify-review|verify-qa|verify-retro)(\s.*)?$"
INJECTED_PROMPT = r"^\s*(verify-loop gate:|verify-loop: verification passed)"


def _str(v) -> str | None:
    return v if isinstance(v, str) else None


def shell_command(cmd) -> str | None:
    if isinstance(cmd, str):
        return cmd
    if isinstance(cmd, list) and all(isinstance(x, str) for x in cmd) and cmd:
        if len(cmd) >= 3 and os.path.basename(cmd[0]) in SHELLS and cmd[1] in ("-c", "-lc", "-ic"):
            return cmd[2]
        return " ".join(cmd)
    return None


def patch_text(tool_input) -> str | None:
    if isinstance(tool_input, str):
        return tool_input if applypatch.looks_like_patch(tool_input) else None
    if not isinstance(tool_input, dict):
        return None
    for key in ("command", "input", "patch"):
        v = tool_input.get(key)
        if isinstance(v, list):
            v = "\n".join(x for x in v if isinstance(x, str))
        if applypatch.looks_like_patch(v):
            return v
    return None


class CodexPlatform(Platform):
    name = "codex"
    skill_prompt_re = SKILL_PROMPT
    injected_prompt_re = INJECTED_PROMPT

    def normalize(self, event: str, payload: dict) -> HookEvent:
        payload = payload if isinstance(payload, dict) else {}
        tool = _str(payload.get("tool_name")) or ""
        ti = payload.get("tool_input")
        edit_paths: list[str] = []
        bash_cmd = None
        patch = patch_text(ti) if tool in PATCH_TOOLS else None
        if patch is not None:
            edit_paths = applypatch.paths(patch)
        elif tool in PATCH_TOOLS and isinstance(ti, dict):
            p = ti.get("file_path") or ti.get("path")
            edit_paths = [p] if isinstance(p, str) and p else []
        elif tool in SHELL_TOOLS:
            cmd = shell_command(ti.get("command") if isinstance(ti, dict) else ti)
            if cmd is not None and applypatch.looks_like_patch(cmd) and cmd.lstrip().startswith("apply_patch"):
                edit_paths = applypatch.paths(cmd)
            else:
                bash_cmd = cmd if cmd is not None else ""
        return HookEvent(
            event=event, sid=_str(payload.get("session_id")) or "", cwd=_str(payload.get("cwd")),
            transcript=_str(payload.get("transcript_path")), tool=tool, edit_paths=edit_paths, bash_cmd=bash_cmd,
            agent_type=_str(payload.get("agent_type")) or "", agent_id=_str(payload.get("agent_id")),
            prompt=_str(payload.get("prompt")), last_msg=_str(payload.get("last_assistant_message")),
            stop_active=bool(payload.get("stop_hook_active")), raw=payload)

    def session_id_from_env(self) -> str | None:
        return os.environ.get("CODEX_THREAD_ID") or None

    def triage_cmd(self, prefix: list[str] | None, cfg: dict, schema_json: str, prompt: str) -> list[str]:
        from . import codex_cmds

        return codex_cmds.triage_cmd(prefix, cfg, schema_json, prompt)

    def headless_cmd(self, role: str, *, agent: str, model: str, effort: str, prompt: str,
                     mcp_config: str | Path | None = None, allowed_tools: list[str] | None = None,
                     permission_mode: str | None = None) -> list[str]:
        from . import codex_cmds

        return codex_cmds.headless_cmd(role, agent=agent, model=model, effort=effort, prompt=prompt,
                                       mcp_config=mcp_config, allowed_tools=allowed_tools,
                                       permission_mode=permission_mode)

    def spawn_instructions(self, role: str, *, agent: str, model: str, attempt: int, prompt_path: str | Path) -> str:
        from . import codex_cmds

        return codex_cmds.spawn_instructions(role, agent=agent, model=model, attempt=attempt, prompt_path=prompt_path)

    def transcript_for(self, sid: str | None) -> Path | None:
        from ..transcripts import codex as tc

        return tc.transcript_for(sid)

    def parse_usage(self, path: str | Path) -> list[dict]:
        from ..transcripts import codex as tc

        return tc.usage_entries(path)

    def render_agent(self, md_text: str) -> tuple[str, str]:
        from . import codex_agents

        return codex_agents.render_agent(md_text)
