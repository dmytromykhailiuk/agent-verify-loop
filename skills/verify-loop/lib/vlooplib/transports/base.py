"""How verifier agents are started and waited for. Contract for built-in and extension transports.

In-session transports (`in_session = True`) only print instructions (`spawn_plan`) and the main session spawns the
agents itself, then runs `vloop collect`. The others run the agent from `vloop spawn <role>` (`spawn`, blocking up to
the role's timeout) and `vloop spawn-wait <role>` (`wait`).
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from pathlib import Path

STATUSES = ("done", "manual", "blocked", "timeout", "dead", "no_result", "api_error", "error")


@dataclass
class SpawnResult:
    status: str
    rc: int = 0
    doc: dict | None = None
    stderr: str = ""
    info: dict = field(default_factory=dict)
    result: str | None = None
    screen: str | None = None


@dataclass
class HookContext:
    target_sid: str
    is_child: bool = False
    suppress_gate: bool = False
    depth: int | None = None
    parent_session_id: str | None = None


class Transport(abc.ABC):
    name: str = ""
    priority: int = 0
    label: str = ""
    in_session: bool = False
    supports_browser: bool = True
    supports_retro: bool = False
    auto: bool = True

    @abc.abstractmethod
    def available(self, cfg: dict) -> bool: ...

    @abc.abstractmethod
    def spawn_plan(self, role: str, ctx: dict, attempt: int, prompt_path: str | Path, model: str,
                   effort: str) -> list[str]: ...

    def spawn(self, role: str, ctx: dict, attempt: int, prompt_path: str | Path, model: str, effort: str,
              cfg: dict) -> SpawnResult:
        return SpawnResult(status="manual", info={"transport": self.name})

    def wait(self, info: dict, timeout: float | None) -> SpawnResult:
        return SpawnResult(status="manual", info=dict(info or {}))

    def spawn_retro(self, ctx: dict, run_dir: Path, cwd: str, has_repo: bool, cfg: dict) -> SpawnResult:
        return SpawnResult(status="manual", info={"transport": self.name})

    def hook_context(self, payload: dict) -> HookContext:
        return HookContext(target_sid=str((payload or {}).get("session_id") or ""))

    def child_env_strip(self) -> list[str]:
        return []

    def locate_transcript(self, info: dict) -> Path | None:
        return None

    def plan_footer(self, roles: list[str]) -> list[str]:
        return []

    def waitable(self, info: dict) -> bool:
        return False

    def where(self, info: dict) -> str:
        return self.name

    def event_fields(self, info: dict) -> dict:
        return {}
