"""In-session transport: the main agent spawns the verifiers itself (Claude Agent tool / Codex custom agent) from
the printed lines, then runs `collect`. It cannot attach an MCP server, so browser QA goes headless."""
from __future__ import annotations

from pathlib import Path

from . import common
from .base import Transport


class AgentTransport(Transport):
    name = "agent"
    priority = 0
    in_session = True
    supports_browser = False

    @property
    def label(self) -> str:
        from .. import platforms

        return "Codex subagents" if platforms.detect().name == "codex" else "Agent tool"

    def available(self, cfg: dict) -> bool:
        return True

    def spawn_plan(self, role: str, ctx: dict, attempt: int, prompt_path: str | Path, model: str,
                   effort: str) -> list[str]:
        from .. import config, platforms

        p = platforms.detect()
        cfg = ctx.get("_cfg") if isinstance(ctx.get("_cfg"), dict) else config.load((ctx.get("repo") or {}).get("root"))
        m = common.platform_model(cfg, role, model, p.name)
        agent = common.AGENT_TYPES[role] if p.name == "codex" else common.agent_name(role)
        return [p.spawn_instructions(role, agent=agent, model=m or "", attempt=attempt, prompt_path=prompt_path)]

    def plan_footer(self, roles: list[str]) -> list[str]:
        from .. import platforms

        if platforms.detect().name == "codex":
            return []
        pairs = [common.agent_names(r) for r in roles]
        alts = [f'"{alt}"' for _, alt in pairs if alt]
        if not alts:
            return []
        return [f"  if a subagent_type above is not in your agent list, use its other form: {', '.join(alts)}"]

    def where(self, info: dict) -> str:
        return "this session"

    def locate_transcript(self, info: dict) -> Path | None:
        from .. import platforms

        return platforms.detect().transcript_for((info or {}).get("session_id"))
