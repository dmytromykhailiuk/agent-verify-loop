"""The agent platform vloop runs under (Claude Code, Codex): hook payloads, commands, transcripts, agent files."""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class HookEvent:
    event: str
    sid: str = ""
    cwd: str | None = None
    transcript: str | None = None
    tool: str = ""
    edit_paths: list[str] = field(default_factory=list)
    bash_cmd: str | None = None
    agent_type: str = ""
    agent_id: str | None = None
    prompt: str | None = None
    last_msg: str | None = None
    stop_active: bool = False
    raw: dict = field(default_factory=dict)


class Platform(abc.ABC):
    name: str = ""
    skill_prompt_re: str = ""
    injected_prompt_re: str = ""

    @abc.abstractmethod
    def normalize(self, event: str, payload: dict) -> HookEvent: ...

    @abc.abstractmethod
    def session_id_from_env(self) -> str | None: ...

    @abc.abstractmethod
    def triage_cmd(self, prefix: list[str] | None, cfg: dict, schema_json: str, prompt: str) -> list[str]: ...

    @abc.abstractmethod
    def headless_cmd(self, role: str, *, agent: str, model: str, effort: str, prompt: str,
                     mcp_config: str | Path | None = None, allowed_tools: list[str] | None = None,
                     permission_mode: str | None = None) -> list[str]: ...

    @abc.abstractmethod
    def spawn_instructions(self, role: str, *, agent: str, model: str, attempt: int, prompt_path: str | Path) -> str: ...

    @abc.abstractmethod
    def transcript_for(self, sid: str | None) -> Path | None: ...

    @abc.abstractmethod
    def parse_usage(self, path: str | Path) -> list[dict]: ...

    @abc.abstractmethod
    def render_agent(self, md_text: str) -> tuple[str, str]: ...


def task_prompt(prompt_path: str | Path) -> str:
    return f"Your task file is {prompt_path}. Read it completely and follow it."
