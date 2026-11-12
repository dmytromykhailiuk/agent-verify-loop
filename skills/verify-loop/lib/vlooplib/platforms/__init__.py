"""Platform registry and detection: `--platform` / override → `VLOOP_PLATFORM` → hook payload shape → env hints →
claude. `CODEX_HOME` is not a hint: users export it globally, also inside Claude Code."""
from __future__ import annotations

import os

from .base import HookEvent, Platform
from .claude import ClaudePlatform
from .codex import CodexPlatform

NAMES = ("claude", "codex")
ENV = "VLOOP_PLATFORM"
_REGISTRY: dict[str, Platform] = {"claude": ClaudePlatform(), "codex": CodexPlatform()}
_override: str | None = None


def set_override(name: str | None) -> None:
    global _override
    if name is not None and name not in _REGISTRY:
        raise ValueError(f"unknown platform: {name!r} (one of {', '.join(NAMES)})")
    _override = name


def get_override() -> str | None:
    return _override


def get(name: str) -> Platform:
    if name not in _REGISTRY:
        raise ValueError(f"unknown platform: {name!r} (one of {', '.join(NAMES)})")
    return _REGISTRY[name]


CODEX_KEYS = ("turn_id",)
CLAUDE_KEYS = ("permission_mode",)
CODEX_TOOLS = ("apply_patch",)
CLAUDE_ENV = ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID")
CODEX_ENV = ("CODEX_SANDBOX", "CODEX_SANDBOX_NETWORK_DISABLED")


def from_payload(payload: dict | None) -> str | None:
    if not isinstance(payload, dict) or not payload:
        return None
    if payload.get("tool_name") in CODEX_TOOLS or any(k in payload for k in CODEX_KEYS):
        return "codex"
    if any(k in payload for k in CLAUDE_KEYS):
        return "claude"
    return None


def from_env() -> str | None:
    env = os.environ
    if env.get("CODEX_THREAD_ID"):
        return "codex"
    if any(env.get(k) for k in CLAUDE_ENV):
        return "claude"
    if any(env.get(k) for k in CODEX_ENV):
        return "codex"
    return None


def detect(payload: dict | None = None, override: str | None = None) -> Platform:
    name = override or _override or os.environ.get(ENV) or from_payload(payload) or from_env() or "claude"
    return get(name if name in _REGISTRY else "claude")


__all__ = ["HookEvent", "Platform", "NAMES", "detect", "get", "set_override", "get_override", "from_payload",
           "from_env"]
