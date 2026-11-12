"""Claude Code transcripts: ~/.claude/projects/<project>/<session id>.jsonl and its subagents/ directory."""
from __future__ import annotations

import glob
import os
from pathlib import Path

from .. import paths, util
from . import first_ts, int_of, iter_jsonl, with_cost


def transcripts_root() -> Path:
    env = os.environ.get("VERIFY_TRANSCRIPTS_DIR")
    return Path(env).expanduser() if env else paths.claude_home() / "projects"


def usage_numbers(u: dict) -> dict:
    inp, cw, cr, out = (int_of(u.get("input_tokens")), int_of(u.get("cache_creation_input_tokens")),
                        int_of(u.get("cache_read_input_tokens")), int_of(u.get("output_tokens")))
    cc = u.get("cache_creation")
    if isinstance(cc, dict) and ("ephemeral_5m_input_tokens" in cc or "ephemeral_1h_input_tokens" in cc):
        w5, w1 = int_of(cc.get("ephemeral_5m_input_tokens")), int_of(cc.get("ephemeral_1h_input_tokens"))
    else:
        w5, w1 = cw, 0
    details = u.get("output_tokens_details")
    thinking = int_of(details.get("thinking_tokens")) if isinstance(details, dict) else 0
    return {"input": inp, "cache_write": cw, "cache_write_5m": w5, "cache_write_1h": w1, "cache_read": cr,
            "output": out, "thinking": thinking}


def find_session_transcript(sid: str | None) -> Path | None:
    if not sid:
        return None
    hits = glob.glob(str(transcripts_root() / "*" / f"{glob.escape(sid)}.jsonl"))
    if not hits:
        return None
    return Path(max(hits, key=lambda h: os.path.getmtime(h)))


transcript_for = find_session_transcript


def find_by_agent_name(herdr_id: str | None, since: str | None = None) -> Path | None:
    """Fallback for a launcher that does not record the session id: the transcript holding the agent-name record."""
    if not herdr_id:
        return None
    needles = (f'{{"type":"agent-name","agentName":"{herdr_id}"', f'"agentName": "{herdr_id}"')
    t0 = (util.parse_iso(since) or 0) - 86400
    for h in sorted(glob.glob(str(transcripts_root() / "*" / "*.jsonl")), key=os.path.getmtime, reverse=True):
        try:
            if os.path.getmtime(h) < t0:
                continue
            with open(h, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            continue
        if any(n in text for n in needles):
            return Path(h)
    return None


def usage_entries(path, start: str | None = None, end: str | None = None, main_only: bool = False) -> list[dict]:
    """Deduplicated assistant requests in file order."""
    entries: dict[str, dict] = {}
    for idx, d in enumerate(iter_jsonl(path)):
        if d.get("type") != "assistant":
            continue
        m = d.get("message") or {}
        u, model = m.get("usage"), m.get("model")
        if not isinstance(u, dict) or model in (None, "<synthetic>"):
            continue
        if main_only and d.get("isSidechain") is True:
            continue
        ts = d.get("timestamp") or ""
        if start and ts < start:
            continue
        if end and ts > end:
            continue
        key = m.get("id") or d.get("requestId") or d.get("uuid") or f"#{idx}"
        entries[key] = {"ts": ts, "model": model, "n": usage_numbers(u)}
    return with_cost(list(entries.values()))


def cost_state(path) -> float | None:
    last = None
    for d in iter_jsonl(path):
        if d.get("type") == "cost-state" and isinstance(d.get("totalCostUSD"), (int, float)):
            last = float(d["totalCostUSD"])
    return last


def timeline(path, start: str | None, end: str | None) -> list[dict]:
    """Main-session records (isSidechain != true) inside the window, in file order."""
    if not path:
        return []
    out = []
    for d in iter_jsonl(path):
        if d.get("isSidechain") is True:
            continue
        ts = d.get("timestamp")
        if not ts:
            continue
        if start and ts < start:
            continue
        if end and ts > end:
            continue
        out.append(d)
    return out


def subagents(main_path: Path | None, sid: str | None, events: list[dict], start: str | None,
              st: dict | None = None) -> list[dict]:
    """Agent-tool subagent transcripts of the main session: [{"path", "agent_type", "first_ts"}]."""
    if not main_path or not sid:
        return []
    out = []
    for f in sorted((Path(main_path).parent / sid / "subagents").glob("agent-*.jsonl")):
        ft = first_ts(f)
        if start and ft and ft < start:
            continue
        meta = util.read_json(f.with_name(f.name[: -len(".jsonl")] + ".meta.json"), {}) or {}
        out.append({"path": f, "agent_type": meta.get("agentType"), "first_ts": ft})
    return out
