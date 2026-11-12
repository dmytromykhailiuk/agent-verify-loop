"""Shared by the built-in transports: agent names, task prompts, Claude tool allow-lists, per-platform models, usage."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

PLUGIN = "agent-verify-loop"
AGENT_TYPES = {"reviewer": "verify-reviewer", "qa": "verify-qa", "triage": "verify-triage", "retro": "vault-keeper"}
OWN_AGENTS = ("reviewer", "qa", "triage")

REVIEWER_TOOLS = ["Read", "Grep", "Glob", "Bash(git diff *)", "Bash(git log *)", "Bash(git show *)", "Bash(git grep *)",
                  "Bash(git blame *)", "Bash(git ls-files *)", "Bash(git rev-parse *)", "Bash(git status *)",
                  "Bash(git cat-file *)", "Bash(git merge-base *)", "Bash(rg *)", "Bash(wc *)", "Bash(ls *)",
                  "Bash(head *)", "Bash(tail *)", "Bash(sed -n *)"]
QA_TOOLS = ["Read", "Grep", "Glob", "Write", "Skill", "mcp__playwright", "Bash(npm run *)", "Bash(npm test *)",
            "Bash(npm ci *)", "Bash(npx *)", "Bash(node *)", "Bash(pnpm run *)", "Bash(pnpm test *)",
            "Bash(yarn run *)", "Bash(yarn test *)", "Bash(pytest *)", "Bash(python3 -m pytest *)", "Bash(curl *)",
            "Bash(git status *)", "Bash(git diff *)", "Bash(lsof *)", "Bash(ps *)", "Bash(kill *)", "Bash(sleep *)",
            "Bash(nohup *)"]
CLAUDE_SESSION_ENV = ["CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN",
                      "CLAUDE_CODE_CHILD_SESSION", "CLAUDE_PID"]


def task_prompt(path) -> str:
    return f"Your task file is {path}. Read it completely and follow it."


def plugin_installed() -> bool:
    """True when this vloop runs from an installed Claude plugin (its agents are then `agent-verify-loop:<name>`)."""
    from .. import paths

    if paths.plugin_root() is not None:
        return True
    skill = Path(os.path.realpath(paths.skill_dir()))
    cache = Path(os.path.realpath(paths.claude_home() / "plugins"))
    return cache in skill.parents and (skill.parents[1] / ".claude-plugin" / "plugin.json").is_file()


def agent_names(role: str) -> tuple[str, str | None]:
    """(name to use, the other form or None). Plugin agents are namespaced; user/project installs are not."""
    base = AGENT_TYPES[role]
    if role == "retro":
        from ..integrations import vault

        name = vault.keeper_agent("claude")
        return name, (base if name != base else None)
    if role not in OWN_AGENTS:
        return base, None
    namespaced = f"{PLUGIN}:{base}"
    return (namespaced, base) if plugin_installed() else (base, namespaced)


def agent_name(role: str) -> str:
    return agent_names(role)[0]


def rule(kind: str, path) -> str:
    return f"{kind}(/{str(path).rstrip('/')}/**)"


def tmpdir() -> str:
    return os.path.realpath(os.environ.get("TMPDIR") or "/tmp")


def claude_tools(role: str, run_dir) -> list[str]:
    if role == "reviewer":
        return REVIEWER_TOOLS + [rule("Read", run_dir), rule("Edit", run_dir)]
    tmp = tmpdir()
    return QA_TOOLS + [rule("Read", run_dir), rule("Edit", run_dir), rule("Read", tmp), rule("Edit", tmp)]


def platform_model(cfg: dict, role: str, model: str | None, platform: str) -> str | None:
    """Claude takes the plan's alias; Codex takes `platforms.codex.models.<role>` (None = the session's model)."""
    if platform != "codex":
        return model
    pc = ((cfg.get("platforms") or {}).get("codex") or {})
    if role == "triage":
        return (pc.get("triage") or {}).get("model") or None
    return (pc.get("models") or {}).get(role) or None


def agent_body(name: str) -> str:
    """The instructions of a bundled agent (`agents/<name>.md` without its frontmatter)."""
    from .. import paths

    text = (paths.agents_src_dir() / f"{name}.md").read_text(encoding="utf-8")
    m = re.match(r"^---\n.*?\n---\n", text, re.S)
    return (text[m.end():] if m else text).strip()


# --------------------------------------------------------------------------- usage


def empty_tokens() -> dict:
    return {"input": 0, "cache_write": 0, "cache_read": 0, "output": 0}


def claude_usage(doc: dict | None, alias: str | None, duration_s: float) -> dict:
    """Usage from `claude -p --output-format json`."""
    doc = doc or {}
    usage = doc.get("usage") or {}
    mu = doc.get("modelUsage") or {}
    model = next(iter(mu)) if isinstance(mu, dict) and len(mu) == 1 else alias
    ms = doc.get("duration_ms")
    cost = doc.get("total_cost_usd")
    return {"model": model,
            "duration_s": round(ms / 1000, 1) if isinstance(ms, (int, float)) else round(duration_s, 1),
            "cost_usd": cost if isinstance(cost, (int, float)) else None,
            "tokens": {"input": int(usage.get("input_tokens") or 0),
                       "cache_write": int(usage.get("cache_creation_input_tokens") or 0),
                       "cache_read": int(usage.get("cache_read_input_tokens") or 0),
                       "output": int(usage.get("output_tokens") or 0)}}


def codex_events(text: str) -> list[dict]:
    out = []
    for ln in (text or "").splitlines():
        ln = ln.strip()
        if not ln.startswith("{"):
            continue
        try:
            ev = json.loads(ln)
        except ValueError:
            continue
        if isinstance(ev, dict):
            out.append(ev)
    return out


def codex_usage(text: str, model: str | None, duration_s: float) -> dict:
    """Usage from `codex exec --json`: the sum over `turn.completed` events. USD stays None (priced by metrics)."""
    tokens = empty_tokens()
    reasoning = 0
    session = None
    for ev in codex_events(text):
        if ev.get("type") == "thread.started" and isinstance(ev.get("thread_id"), str):
            session = ev["thread_id"]
        if ev.get("type") != "turn.completed":
            continue
        u = ev.get("usage") or {}
        cached = int(u.get("cached_input_tokens") or 0)
        tokens["input"] += max(0, int(u.get("input_tokens") or 0) - cached)
        tokens["cache_read"] += cached
        tokens["output"] += int(u.get("output_tokens") or 0)
        reasoning += int(u.get("reasoning_output_tokens") or 0)
    return {"model": model, "duration_s": round(duration_s, 1), "cost_usd": None, "tokens": tokens,
            "reasoning_tokens": reasoning, "session_id": session}
