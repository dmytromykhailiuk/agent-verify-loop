"""Metrics, retro planning, ledger and gate report (`vloop retro-plan|retro-prep|ledger|gate-report`, §2.13, WP4).

Token accounting is deterministic: it reads the platform's transcripts (Claude Code or Codex: main session,
in-session subagents, transport-launched agent sessions), the session's event log and the run directory. Token totals
count input + cache writes + output; cache reads are reported separately (they would otherwise dominate every total).
USD is priced from pricing.json; a model that is not in it has no USD (None, printed as "n/a").
"""
from __future__ import annotations

import hashlib
import json
import os
import statistics
import sys
import time
from collections import Counter
from pathlib import Path

from . import config, paths, platforms, transports, util
from .integrations import vault
from .transcripts import first_ts, iter_jsonl, module_for
from .transcripts.claude import find_by_agent_name, find_session_transcript, usage_entries
from .transcripts.claude import usage_numbers as _usage_numbers

LEDGER = "ledger.jsonl"
HARNESS_PROJECT = "_harness"
DIGEST_BUDGET = 48000
LARGE_OUTPUT_CHARS = 20000
CLOSED = ("passed", "accepted", "abandoned", "skipped", "reset", "done")
SCOPES = ("none", "checks", "run", "browser")
TIERS = ("standard", "deep")
ROLE_BY_AGENT_TYPE = {"verify-reviewer": "reviewer", "verify-qa": "qa", "vault-keeper": "retro"}
VAULT_KEEPER = "vault-keeper"
ROLES = ("triage", "reviewer", "qa", "retro", "other_subagents")


class CliError(Exception):
    def __init__(self, message: str, code: int = 1):
        super().__init__(message)
        self.code = code


# --------------------------------------------------------------------------- small helpers


def _runs_dir() -> Path:
    return util.state_dir() / "runs"


_iter_jsonl = iter_jsonl


def _minutes(a: str | None, b: str | None) -> float:
    ta, tb = util.parse_iso(a), util.parse_iso(b)
    if ta is None or tb is None:
        return 0.0
    return round(max(0.0, tb - ta) / 60.0, 1)


def _hms(ts: str | None) -> str:
    t = util.parse_iso(ts)
    return time.strftime("%H:%M:%S", time.localtime(t)) if t is not None else "??:??:??"


def money(x: float | None) -> str:
    """`≈$` with 2 significant digits: ≈$0.004 · ≈$0.31 · ≈$2.1; "n/a" when the cost is unknown (unpriced model)."""
    if x is None:
        return "n/a"
    x = float(x or 0)
    if x <= 0:
        return "≈$0"
    import math

    digits = max(0, 1 - int(math.floor(math.log10(x))))
    s = f"{round(x, digits):.{digits}f}"
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return "≈$" + s


def _median(xs):
    xs = [x for x in xs if isinstance(x, (int, float))]
    return round(statistics.median(xs), 4) if xs else None


def _max_ordered(values, order):
    best = None
    for v in values:
        if v in order and (best is None or order.index(v) > order.index(best)):
            best = v
    return best


def _append_event(sid: str | None, event: str, repo=None, run=None, attempt=None, **data) -> None:
    if not sid:
        return
    try:
        from . import state

        state.append_event(sid, event, repo=repo, run=run, attempt=attempt, **data)
        return
    except (ImportError, AttributeError):
        pass
    line = {"ts": util.now_iso(), "sid": sid, "event": event, "repo": repo, "run": run, "attempt": attempt,
            "data": data}
    p = util.state_dir() / "sessions" / f"{sid}.events.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(line, ensure_ascii=False) + "\n")


def _new_retro_run_id() -> str:
    try:
        from . import state

        return state.new_run_id("", kind="retro")
    except (ImportError, AttributeError):
        return time.strftime("retro-%Y%m%d-%H%M%S", time.gmtime()) + "-" + os.urandom(2).hex()


def load_events(sid: str | None) -> list[dict]:
    if not sid:
        return []
    return list(_iter_jsonl(util.state_dir() / "sessions" / f"{sid}.events.jsonl"))


def load_session_state(sid: str | None) -> dict | None:
    if not sid:
        return None
    st = util.read_json(util.state_dir() / "sessions" / f"{sid}.json")
    return st if isinstance(st, dict) else None


# --------------------------------------------------------------------------- pricing


_pricing_cache: dict | None = None


def pricing() -> dict:
    global _pricing_cache
    if _pricing_cache is None:
        _pricing_cache = util.read_json(paths.config_dir() / "pricing.json", {}) or {}
    return _pricing_cache


def resolve_model(model: str | None) -> str | None:
    """The pricing.json model a transcript model name stands for; None when it is not priced."""
    if not model:
        return None
    p = pricing()
    models = p.get("models") or {}
    aliases = p.get("aliases") or {}
    if model in models:
        return model
    if model in aliases:
        return aliases[model]
    for alias, full in aliases.items():
        if model.startswith(f"claude-{alias}-"):
            return full
    for full in models:
        if model.startswith(full + "-"):
            return full
    return None


def request_cost(model: str | None, usage: dict) -> float | None:
    """USD cost of one request (§2.13.4), incl. long-context rates above the threshold; None for an unpriced model."""
    full = resolve_model(model)
    if full is None:
        return None
    n = usage if "cache_write_5m" in usage else _usage_numbers(usage)
    p = pricing()
    rates = (p.get("models") or {}).get(full) or {}
    lc = rates.get("long_context")
    if isinstance(lc, dict) and n["input"] + n["cache_write"] + n["cache_read"] > lc.get("threshold", 10 ** 12):
        rates = lc
    per = float(p.get("per_tokens") or 1_000_000)
    return (n["input"] * rates.get("input", 0) + n["cache_write_5m"] * rates.get("cache_write_5m", 0)
            + n["cache_write_1h"] * rates.get("cache_write_1h", 0) + n["cache_read"] * rates.get("cache_read", 0)
            + n["output"] * rates.get("output", 0)) / per


def _plus(a: float | None, b: float | None) -> float | None:
    return None if a is None or b is None else a + b


def _rounded(x: float | None, digits: int = 6) -> float | None:
    return None if x is None else round(x, digits)


# --------------------------------------------------------------------------- transcript parsing


def aggregate(entries: list[dict], start: str | None = None, end: str | None = None) -> dict:
    by_model: dict[str, dict] = {}
    for e in entries:
        if start and e["ts"] < start:
            continue
        if end and e["ts"] >= end:
            continue
        b = by_model.setdefault(e["model"], {"requests": 0, "input": 0, "cache_write": 0, "cache_read": 0,
                                             "output": 0, "thinking": 0, "max_context": 0, "cost_usd": 0.0})
        n = e["n"]
        b["requests"] += 1
        for k in ("input", "cache_write", "cache_read", "output", "thinking"):
            b[k] += n[k]
        b["max_context"] = max(b["max_context"], n["input"] + n["cache_write"] + n["cache_read"])
        b["cost_usd"] = _plus(b["cost_usd"], e.get("cost"))
    total = sum(b["input"] + b["cache_write"] + b["output"] for b in by_model.values())
    cost: float | None = 0.0
    for b in by_model.values():
        cost = _plus(cost, b["cost_usd"])
        b["cost_usd"] = _rounded(b["cost_usd"])
    return {"by_model": by_model, "total": total, "cost_usd": _rounded(cost)}


def _platform_name(sid: str | None, ctx: dict | None = None) -> str:
    st = load_session_state(sid) or {}
    name = st.get("platform") or (ctx or {}).get("platform")
    if name in platforms.NAMES:
        return name
    return platforms.detect().name


def main_transcript(sid: str | None, platform: str = "claude") -> Path | None:
    st = load_session_state(sid)
    tp = (st or {}).get("transcript_path")
    if tp and Path(tp).expanduser().is_file():
        return Path(tp).expanduser()
    return module_for(platform).transcript_for(sid)


# --------------------------------------------------------------------------- run inputs


def _attempt_dirs(rd: Path) -> list[int]:
    out = []
    for p in rd.glob("attempt-*"):
        try:
            out.append(int(p.name.split("-", 1)[1]))
        except ValueError:
            pass
    return sorted(out)


def _attempt_file(rd: Path, n: int, name: str):
    return util.read_json(rd / f"attempt-{n}" / name)


def _attempt_for_ts(ctx: dict, ts: str | None) -> int | None:
    best = None
    for a in ctx.get("attempts") or []:
        b = a.get("begin_at")
        if b and ts and ts >= b:
            best = a.get("n")
    return best


def _run_events(events: list[dict], run_id: str, name: str) -> list[dict]:
    return [e for e in events if e.get("event") == name and e.get("run") == run_id]


def task_start(events: list[dict], first_begin_ts: str | None) -> str | None:
    """First `prompt` event of the task that the first `begin` belongs to."""
    prompts = [e for e in events if e.get("event") == "prompt"]
    before = [e for e in prompts if not first_begin_ts or (e.get("ts") or "") <= first_begin_ts]
    if not before:
        return None
    task = (before[-1].get("data") or {}).get("task")
    same = [e for e in before if (e.get("data") or {}).get("task") == task]
    return (same or before)[0].get("ts")


def compute_phases(events: list[dict], run_id: str, start: str | None, end: str) -> list[dict]:
    begins = sorted(_run_events(events, run_id, "begin"), key=lambda e: e.get("ts") or "")
    judges = sorted(_run_events(events, run_id, "judge"), key=lambda e: e.get("ts") or "")
    closes = sorted(_run_events(events, run_id, "close"), key=lambda e: e.get("ts") or "")
    phases = []
    if not begins:
        return [{"name": "implement", "start": start, "end": end}] if start else []
    if start and start < begins[0]["ts"]:
        phases.append({"name": "implement", "start": start, "end": begins[0]["ts"]})
    for k, b in enumerate(begins, 1):
        j = judges[k - 1]["ts"] if k - 1 < len(judges) else None
        nxt = begins[k]["ts"] if k < len(begins) else None
        phases.append({"name": f"verify#{k}", "start": b["ts"], "end": j or nxt or (closes[-1]["ts"] if closes else end)})
        if j and nxt:
            phases.append({"name": f"fix#{k}", "start": j, "end": nxt})
    if closes:
        phases.append({"name": "wrap-up", "start": closes[-1]["ts"], "end": end})
    return phases


def _turn_durations(timeline: list[dict]) -> list[tuple[str, float]]:
    return [(d.get("timestamp") or "", float(d.get("durationMs") or 0)) for d in timeline
            if d.get("type") == "system" and d.get("subtype") == "turn_duration"]


IDLE_GAP_S = 600  # gaps between consecutive transcript events longer than this count as idle


def _gap_trimmed_minutes(timeline: list[dict], start: str | None, end: str | None) -> float:
    """Wall minutes of [start, end] minus every gap > IDLE_GAP_S between consecutive transcript events."""
    stamps = [start, end] + [d.get("timestamp") for d in timeline if not end or d.get("timestamp", "") <= end]
    ts = sorted(t for t in map(util.parse_iso, stamps) if t is not None)
    return round(sum(b - a for a, b in zip(ts, ts[1:]) if b - a <= IDLE_GAP_S) / 60.0, 1)


def policy_minutes(window: dict) -> tuple[float, str]:
    """Minutes the retro policy compares: active (turn_duration) time, else gap-trimmed wall time, else wall time."""
    if window.get("active_min"):
        return float(window["active_min"]), "active"
    if window.get("trimmed_min") is not None:
        return float(window["trimmed_min"]), "gap-trimmed"
    return float(window.get("wall_min") or 0.0), "wall"


def load_timeline(path, start: str | None, end: str | None, platform: str = "claude") -> list[dict]:
    """Main-session records inside the window in the Claude record shape (Codex rollouts are converted)."""
    if not path:
        return []
    return module_for(platform).timeline(path, start, end)


# --------------------------------------------------------------------------- agents


def _role_of(agent_type: str | None) -> str:
    return ROLE_BY_AGENT_TYPE.get(str(agent_type or "").rsplit(":", 1)[-1], "other_subagents")


def _transport_transcript(name: str | None, info: dict) -> Path | None:
    try:
        return transports.locate_transcript({**info, "transport": name})
    except Exception as e:
        util.log_error("metrics.locate_transcript", e)
        return None


def collect_agents(ctx: dict, events: list[dict], main_path: Path | None, sid: str | None,
                   start: str | None, end: str, platform: str = "claude") -> list[dict]:
    agents = []
    rd = _runs_dir() / ctx.get("run_id", "")
    tx = module_for(platform)
    for a in ctx.get("attempts") or []:
        n = a.get("n")
        tri = _attempt_file(rd, n, "triage.json") or {}
        ag = tri.get("agent") if isinstance(tri.get("agent"), dict) else None
        if tri.get("source") in ("triage", "fallback") and ag:
            tok = ag.get("tokens") or {}
            total = sum(int(tok.get(k) or 0) for k in ("input", "cache_write", "output"))
            cost = ag.get("cost_usd")
            agents.append({"role": "triage", "attempt": n, "transport": "headless", "session_id": None,
                           "model": ag.get("model"), "total": total,
                           "cost_usd": float(cost) if isinstance(cost, (int, float)) else None,
                           "cost_source": "triage.json", "duration_s": ag.get("duration_s"),
                           "by_model": {}, "first_ts": a.get("begin_at")})
    seen_files: set[str] = set()
    for a in ctx.get("attempts") or []:
        for role, info in (a.get("agents") or {}).items():
            if not isinstance(info, dict) or info.get("transport") in (None, "agent"):
                continue
            path = _transport_transcript(info.get("transport"), {**info, "started_at": info.get("started_at") or start})
            atx = module_for(info["platform"]) if info.get("platform") in platforms.NAMES else tx
            if path is None and isinstance(info.get("usage"), dict):
                agents.append(_agent_from_usage(role, a.get("n"), info.get("transport"), info))
            else:
                agents.append(_agent_from_file(role, a.get("n"), info.get("transport"), info, path, atx))
            if path:
                seen_files.add(str(path))
    for e in _run_events(events, ctx.get("run_id", ""), "retro_done"):
        dd = e.get("data") or {}
        if dd.get("transport") in (None, "agent"):
            continue
        path = _transport_transcript(dd.get("transport"), {**dd, "started_at": dd.get("started_at") or start})
        if path and str(path) in seen_files:
            continue
        agents.append(_agent_from_file("retro", None, dd.get("transport"), dd, path, tx))
    for sub in tx.subagents(main_path, sid, events, start, load_session_state(sid)):
        if str(sub["path"]) in seen_files:
            continue
        ft = sub.get("first_ts")
        role = _role_of(sub.get("agent_type"))
        ag = _agent_from_file(role, _attempt_for_ts(ctx, ft) if role in ("reviewer", "qa") else None, "agent",
                              {"session_id": None}, sub["path"], tx)
        ag["agent_type"] = sub.get("agent_type")
        agents.append(ag)
    return agents


def _agent_from_usage(role, attempt, transport, info: dict) -> dict:
    """A transport-launched agent whose transcript is not on disk (e.g. an ephemeral `codex exec`): the usage the
    transport recorded from the child's own output."""
    u = info["usage"]
    tok = u.get("tokens") if isinstance(u.get("tokens"), dict) else {}
    n = {k: int(tok.get(k) or 0) for k in ("input", "cache_write", "cache_read", "output")}
    model = u.get("model") or info.get("model")
    cost = u.get("cost_usd")
    if isinstance(cost, (int, float)):
        source = "transport"
    else:
        cost = request_cost(model, {**n, "cache_write_5m": n["cache_write"], "cache_write_1h": 0})
        source = "estimate" if cost is not None else "unpriced"
    total = n["input"] + n["cache_write"] + n["output"]
    by_model = {model: {"requests": 0, **n, "thinking": 0, "max_context": 0, "cost_usd": _rounded(cost)}} \
        if model and total else {}
    dur = u.get("duration_s")
    return {"role": role, "attempt": attempt, "transport": transport, "session_id": info.get("session_id"),
            "herdr_id": info.get("herdr_id"), "model": model, "total": total, "cost_usd": _rounded(cost),
            "cost_source": source, "duration_s": dur if isinstance(dur, (int, float)) else None,
            "by_model": by_model, "transcript": None, "first_ts": info.get("started_at")}


def _agent_from_file(role, attempt, transport, info: dict, path: Path | None, tx=None) -> dict:
    tx = tx or module_for("claude")
    entries = tx.usage_entries(path) if path else []
    agg = aggregate(entries)
    read_cost_state = getattr(tx, "cost_state", None)
    cs = read_cost_state(path) if path and read_cost_state else None
    models = sorted(agg["by_model"], key=lambda m: -agg["by_model"][m]["output"])
    dur = None
    st, en = info.get("started_at"), info.get("ended_at")
    if st and en:
        a, b = util.parse_iso(st), util.parse_iso(en)
        dur = round(b - a, 1) if a is not None and b is not None else None
    elif entries:
        a, b = util.parse_iso(entries[0]["ts"]), util.parse_iso(entries[-1]["ts"])
        dur = round(b - a, 1) if a is not None and b is not None else None
    cost = cs if cs is not None else agg["cost_usd"]
    if cs is not None:
        source = "cost-state"
    elif not path:
        source = "missing"
    else:
        source = "estimate" if cost is not None else "unpriced"
    return {"role": role, "attempt": attempt, "transport": transport, "session_id": info.get("session_id"),
            "herdr_id": info.get("herdr_id"), "model": models[0] if models else info.get("model"),
            "total": agg["total"], "cost_usd": _rounded(cost),
            "cost_source": source,
            "duration_s": dur, "by_model": agg["by_model"], "transcript": str(path) if path else None,
            "first_ts": entries[0]["ts"] if entries else (first_ts(path) if path else None)}


# --------------------------------------------------------------------------- signals


def _blocks(d: dict) -> list:
    c = (d.get("message") or {}).get("content")
    return c if isinstance(c, list) else []


def _result_text(block: dict) -> str:
    c = block.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "\n".join(x.get("text", "") for x in c if isinstance(x, dict) and isinstance(x.get("text"), str))
    return ""


def _tool_arg(name: str, inp: dict) -> str:
    if not isinstance(inp, dict):
        return ""
    for k in {"Bash": ("command",), "Read": ("file_path",), "Edit": ("file_path",), "Write": ("file_path",),
              "MultiEdit": ("file_path",), "NotebookEdit": ("notebook_path",), "Grep": ("pattern",),
              "Glob": ("pattern",), "Agent": ("subagent_type", "description"), "Task": ("subagent_type",),
              "Skill": ("skill",), "WebFetch": ("url",), "WebSearch": ("query",)}.get(name, ()):
        if isinstance(inp.get(k), str):
            return inp[k]
    for v in inp.values():
        if isinstance(v, str):
            return v
    return ""


def _is_vault_search(name: str, inp: dict) -> bool:
    if name == "Bash":
        cmd = str(inp.get("command") or "")
        return "run vault-search" in cmd or (VAULT_KEEPER in cmd and "Operation: search" in cmd)
    if name not in ("Agent", "Task", "spawn_agent"):
        return False
    kind = next((str(inp[k]) for k in ("subagent_type", "agent_type", "agent", "name") if inp.get(k)), "")
    if kind.rsplit(":", 1)[-1] != VAULT_KEEPER:
        return False
    return "Operation: search" in " ".join(str(inp.get(k) or "") for k in ("prompt", "message", "task", "input"))


def parse_conversation(timeline: list[dict]) -> dict:
    """Prompts, assistant texts, tool calls (deduped by block id) with their results, compactions, efforts."""
    prompts, texts, tools, interrupts, boundaries, summaries = [], [], [], 0, 0, 0
    tool_index: dict[str, dict] = {}
    seen_text, seen_msg_effort = set(), {}
    for d in timeline:
        t = d.get("type")
        ts = d.get("timestamp")
        if t == "assistant":
            m = d.get("message") or {}
            mid = m.get("id") or d.get("uuid")
            if d.get("effort") and m.get("model") != "<synthetic>":
                seen_msg_effort[mid] = d.get("effort")
            for b in _blocks(d):
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "text" and (b.get("text") or "").strip():
                    key = (mid, b["text"])
                    if key not in seen_text:
                        seen_text.add(key)
                        texts.append({"ts": ts, "text": b["text"]})
                elif b.get("type") == "tool_use" and b.get("id") and b["id"] not in tool_index:
                    tu = {"ts": ts, "id": b["id"], "name": b.get("name") or "?", "input": b.get("input") or {},
                          "is_error": None, "chars": 0, "text": ""}
                    tool_index[b["id"]] = tu
                    tools.append(tu)
        elif t == "user":
            if d.get("isCompactSummary"):
                summaries += 1
                continue
            if d.get("isMeta"):
                continue
            c = (d.get("message") or {}).get("content")
            parts = []
            if isinstance(c, str):
                parts.append(c)
            elif isinstance(c, list):
                for b in c:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "tool_result":
                        tu = tool_index.get(b.get("tool_use_id"))
                        if tu is not None:
                            txt = _result_text(b)
                            tu.update(is_error=bool(b.get("is_error")), chars=len(txt), text=txt[:2000])
                    elif b.get("type") == "text" and isinstance(b.get("text"), str):
                        parts.append(b["text"])
            for p in parts:
                if "[Request interrupted by user" in p:
                    interrupts += 1
                elif p.strip() and not p.lstrip().startswith("<local-command"):
                    prompts.append({"ts": ts, "text": p})
        elif t == "system" and "compact" in str(d.get("subtype") or ""):
            boundaries += 1
    return {"prompts": prompts, "texts": texts, "tools": tools, "interrupts": interrupts,
            "compactions": max(boundaries, summaries), "efforts": dict(Counter(seen_msg_effort.values()))}


def compute_signals(conv: dict, events: list[dict], start: str | None, end: str | None) -> dict:
    tools = conv["tools"]
    by_name = Counter(t["name"] for t in tools)
    failing = Counter()
    for t in tools:
        if t["name"] == "Bash" and t["is_error"]:
            failing[str(t["input"].get("command") or "")[:80]] += 1
    reads = Counter(str(t["input"].get("file_path")) for t in tools if t["name"] == "Read" and t["input"].get("file_path"))
    large = [{"tool": t["name"], "arg": _tool_arg(t["name"], t["input"])[:100], "chars": t["chars"]}
             for t in tools if t["chars"] > LARGE_OUTPUT_CHARS]
    explore, vault_at = 0, None
    for t in tools:
        if _is_vault_search(t["name"], t["input"]):
            vault_at = t["ts"]
            break
        if t["name"] in ("Grep", "Glob", "Read"):
            explore += 1
    denials = sum(1 for t in tools if "permission" in t["text"].lower() and "denied" in t["text"].lower())
    gate = Counter(e.get("event") for e in events
                   if str(e.get("event") or "").startswith(("gate_", "retro_block"))
                   and (not start or (e.get("ts") or "") >= start) and (not end or (e.get("ts") or "") <= end))
    return {
        "tool_calls": dict(by_name.most_common()),
        "tool_calls_total": len(tools),
        "bash_failures": [{"command": c, "count": n} for c, n in failing.most_common()],
        "repeated_failures": [{"command": c, "count": n} for c, n in failing.most_common() if n >= 2],
        "rereads": [{"file": f, "count": n} for f, n in reads.most_common() if n >= 3],
        "large_outputs": large,
        "exploration_before_vault": {"count": explore, "vault_search": vault_at is not None, "vault_search_at": vault_at},
        "user_prompts": len(conv["prompts"]),
        "corrections": max(0, len(conv["prompts"]) - 1),
        "interrupts": conv["interrupts"],
        "permission_denials": denials,
        "compactions": conv["compactions"],
        "efforts": conv["efforts"],
        "gate_events": dict(gate),
    }


# --------------------------------------------------------------------------- findings / triage evaluation


def findings_history(rd: Path, ctx: dict) -> list[dict]:
    out = []
    for n in _attempt_dirs(rd):
        j = _attempt_file(rd, n, "judge.json")
        if not isinstance(j, dict):
            continue
        fs = j.get("findings") or []
        out.append({
            "attempt": n, "result": j.get("result"), "escalate": j.get("escalate"),
            "roles": {r: {k: v.get(k) for k in ("state", "carried", "carried_from", "blocking", "minor", "downgraded",
                                                  "scope", "checks", "tainted") if k in v}
                      for r, v in (j.get("roles") or {}).items() if isinstance(v, dict)},
            "blocking": [f"{f.get('id')}" for f in fs if f.get("blocking")],
            "blocking_by_role": dict(Counter(f.get("role") for f in fs if f.get("blocking"))),
            "downgraded": [{"id": f.get("id"), "from": f.get("downgraded_from"), "reason": f.get("downgrade_reason")}
                           for f in fs if f.get("downgraded_from")],
            "stuck": [f.get("id") for f in fs if f.get("stuck")],
            "late": [f.get("id") for f in fs if f.get("late")],
            "waived": [f.get("id") for f in fs if f.get("waived")],
            "categories": dict(Counter(f.get("category") for f in fs if f.get("blocking"))),
            "unverified": [{"check_id": u.get("check_id"), "need": u.get("need")} for u in j.get("unverified") or []],
            "warnings": j.get("warnings") or [],
        })
    disputes = ctx.get("disputes") or {}
    for h in out:
        h["disputed"] = sorted(k for k, v in disputes.items() if isinstance(v, dict) and v.get("attempt") == h["attempt"])
    return out


def _plans(rd: Path) -> list[dict]:
    return [p for p in (_attempt_file(rd, n, "plan.json") for n in _attempt_dirs(rd)) if isinstance(p, dict)]


def change_info(rd: Path, ctx: dict) -> dict:
    atts = _attempt_dirs(rd)
    last = _attempt_file(rd, atts[-1], "diff.numstat.json") if atts else None
    first_tri = _attempt_file(rd, atts[0], "triage.json") if atts else None
    plans = _plans(rd)
    qa_scopes = [((p.get("roles") or {}).get("qa") or {}).get("scope") for p in plans]
    tiers = [((p.get("roles") or {}).get("reviewer") or {}).get("tier") for p in plans]
    first_att = (ctx.get("attempts") or [{}])[0] if ctx.get("attempts") else {}
    last = last or {}
    first_tri = first_tri or {}
    return {"files": last.get("files", 0), "added": last.get("added", 0), "removed": last.get("removed", 0),
            "prefilter": first_att.get("prefilter"), "change_size": first_tri.get("change_size"),
            "risk": first_tri.get("risk"), "confidence": first_tri.get("confidence"),
            "triage_source": first_tri.get("source"),
            "qa_scope": _max_ordered(qa_scopes, SCOPES) or ("none" if plans else None),
            "review_tier": _max_ordered(tiers, TIERS)}


def triage_eval(change: dict, history: list[dict], outcome: dict) -> dict:
    qa_blocking = any((h["blocking_by_role"].get("qa") or 0) > 0 for h in history)
    rev_blocking = any((h["blocking_by_role"].get("reviewer") or 0) > 0 for h in history)
    acceptance = any(h["categories"].get("acceptance") for h in history)
    unverified = sum(len(h["unverified"]) for h in history)
    notes = []
    scope, tier = change.get("qa_scope"), change.get("review_tier")
    if scope in ("run", "browser") and not qa_blocking and history:
        notes.append(f"QA ({scope}) found no blocking issue")
    if tier == "deep" and not rev_blocking and history:
        notes.append("deep review found nothing blocking")
    if scope in (None, "none") and acceptance:
        notes.append("QA was skipped but blocking acceptance findings came up")
    if change.get("change_size") == "small" and outcome.get("attempts", 0) > 1:
        notes.append(f"triage said small, loop needed {outcome['attempts']} attempts")
    if scope == "browser" and unverified:
        notes.append(f"browser QA left {unverified} criteria unverified")
    return {"source": change.get("triage_source"), "change_size": change.get("change_size"),
            "risk": change.get("risk"), "confidence": change.get("confidence"), "qa_scope": scope,
            "review_tier": tier, "qa_found_blocking": qa_blocking, "reviewer_found_blocking": rev_blocking,
            "unverified": unverified, "mismatches": notes}


# --------------------------------------------------------------------------- ledger


def ledger_path() -> Path:
    return util.state_dir() / LEDGER


def read_ledger() -> list[dict]:
    return list(_iter_jsonl(ledger_path()))


def ledger_summary(rows: list[dict], project: str | None = None, last: int = 20) -> dict:
    if project:
        rows = [r for r in rows if r.get("project") == project]
    rows = rows[-last:]
    out: dict = {"project": project, "rows": len(rows), "by_change_size": {},
                 "qa_scope": dict(Counter(r.get("qa_scope") or "none" for r in rows))}
    groups: dict[str, list] = {}
    for r in rows:
        groups.setdefault(r.get("change_size") or "unknown", []).append(r)
    for cs, rs in groups.items():
        out["by_change_size"][cs] = {
            "n": len(rs),
            "tokens_main": _median([(r.get("tokens") or {}).get("main") for r in rs]),
            "cost_total": _median([(r.get("cost_usd") or {}).get("total") for r in rs]),
            "minutes_wall": _median([(r.get("minutes") or {}).get("wall") for r in rs]),
            "attempts": _median([r.get("attempts") for r in rs]),
        }
    return out


def append_ledger(row: dict) -> bool:
    """Appends once per run_id; returns False when the run is already in the ledger."""
    if any(r.get("run_id") == row.get("run_id") for r in read_ledger()):
        return False
    p = ledger_path()
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return True


def ledger_row(m: dict, ctx: dict, policy: str) -> dict:
    by_role = m["tokens"]["by_role"]
    cost_role = m["tokens"]["cost_by_role"]
    mins = {"wall": m["window"]["wall_min"], "implement": 0.0, "verify": 0.0, "fix": 0.0}
    for ph in m["phases"]:
        key = ph["name"].split("#", 1)[0]
        if key in mins and key != "wall":
            mins[key] = round(mins[key] + ph["wall_min"], 1)
    ch, oc = m["change"], m["outcome"]
    return {"ts": util.now_iso(), "run_id": m["run_id"], "session_id": m["session_id"], "project": m["project"],
            "repo_key": (ctx.get("repo") or {}).get("key"), "kind": ctx.get("kind", "loop"),
            "outcome": oc["status"], "attempts": oc["attempts"], "failed_attempts": oc["failed_attempts"],
            "files": ch.get("files", 0), "added": ch.get("added", 0), "removed": ch.get("removed", 0),
            "prefilter": ch.get("prefilter"), "change_size": ch.get("change_size"), "risk": ch.get("risk"),
            "qa_scope": ch.get("qa_scope"), "review_tier": ch.get("review_tier"),
            "tokens": {"main": m["tokens"]["main"]["total"], "triage": by_role["triage"],
                       "reviewer": by_role["reviewer"], "qa": by_role["qa"], "retro": by_role["retro"]},
            "cost_usd": {"main": _rounded(m["tokens"]["main"]["cost_usd"], 4),
                         "triage": _rounded(cost_role["triage"], 4),
                         "reviewer": _rounded(cost_role["reviewer"], 4), "qa": _rounded(cost_role["qa"], 4),
                         "total": _rounded(m["tokens"]["cost_usd"], 4)},
            "platform": m.get("platform"),
            "minutes": mins, "retro": policy}


# --------------------------------------------------------------------------- metrics


def compute_metrics(run_id: str, *, now: str | None = None) -> dict:
    rd = _runs_dir() / run_id
    ctx = util.read_json(rd / "context.json")
    if not isinstance(ctx, dict):
        raise CliError(f"run {run_id} not found", 2)
    sid = ctx.get("session_id")
    events = load_events(sid)
    end = now or util.now_iso()
    begins = sorted(_run_events(events, run_id, "begin"), key=lambda e: e.get("ts") or "")
    if ctx.get("kind") == "retro":
        start = (events[0].get("ts") if events else None)
    else:
        start = task_start(events, begins[0]["ts"] if begins else None)
    plat = _platform_name(sid, ctx)
    tx = module_for(plat)
    mp = main_transcript(sid, plat)
    if not start:
        start = ctx.get("created_at") if ctx.get("kind") != "retro" else (first_ts(mp) if mp else ctx.get("created_at"))
    entries = tx.usage_entries(mp, start, end, main_only=True) if mp else []
    main = aggregate(entries)
    main["cost_source"] = ("estimate" if main["cost_usd"] is not None else "unpriced") if mp else "missing"
    main["transcript"] = str(mp) if mp else None
    timeline = load_timeline(mp, start, end, plat)
    turns = _turn_durations(timeline)
    agents = collect_agents(ctx, events, mp, sid, start, end, plat)
    by_role = {r: 0 for r in ROLES}
    cost_by_role: dict[str, float | None] = {r: 0.0 for r in ROLES}
    for a in agents:
        by_role[a["role"]] = by_role.get(a["role"], 0) + a["total"]
        cost_by_role[a["role"]] = _plus(cost_by_role.get(a["role"], 0.0), a["cost_usd"])
    agents_cost: float | None = 0.0
    for v in cost_by_role.values():
        agents_cost = _plus(agents_cost, v)

    phases = []
    for ph in compute_phases(events, run_id, start, end):
        k = ph["name"].split("#")
        agent_tok = 0
        for a in agents:
            if a["role"] in ("triage", "reviewer", "qa") and a["attempt"] is not None:
                hit = k[0] == "verify" and len(k) > 1 and str(a["attempt"]) == k[1]
            else:
                ft = a.get("first_ts")
                hit = bool(ft) and ph["start"] <= ft < ph["end"]
            if hit:
                agent_tok += a["total"]
        phases.append({"name": ph["name"], "start": ph["start"], "end": ph["end"],
                       "main_tokens": aggregate(entries, ph["start"], ph["end"])["total"],
                       "agent_tokens": agent_tok, "wall_min": _minutes(ph["start"], ph["end"]),
                       "active_min": round(sum(ms for ts, ms in turns if ph["start"] <= ts < ph["end"]) / 60000, 1)})

    conv = parse_conversation(timeline)
    history = findings_history(rd, ctx)
    statuses = [a.get("result") for a in ctx.get("attempts") or []]
    hist_results = [h["result"] for h in history]
    results = hist_results if len(hist_results) >= len([s for s in statuses if s]) else statuses
    escalated = ctx.get("status") == "escalated" or any(h.get("escalate") for h in history) or bool(
        _run_events(events, run_id, "escalate"))
    outcome = {"status": ctx.get("status"), "attempts": len(ctx.get("attempts") or []) or len(history),
               "failed_attempts": max(int(ctx.get("failed_attempts") or 0), results.count("fail")),
               "escalated": escalated, "inconclusive": results.count("inconclusive"),
               "browser_consent": ctx.get("browser_consent")}
    change = change_info(rd, ctx)
    closes = _run_events(events, run_id, "close")
    closed_at = max(e.get("ts") or "" for e in closes) if closes else None
    project = (ctx.get("repo") or {}).get("slug") or HARNESS_PROJECT
    total_tokens = main["total"] + sum(by_role.values())
    m = {
        "schema": 1, "run_id": run_id, "session_id": sid, "project": project, "platform": plat,
        "generated_at": util.now_iso(),
        "kind": ctx.get("kind", "loop"),
        "window": {"start": start, "end": end, "wall_min": _minutes(start, closed_at or end), "closed_at": closed_at,
                   "active_min": round(sum(ms for _, ms in turns) / 60000, 1),
                   "trimmed_min": _gap_trimmed_minutes(timeline, start, closed_at or end)},
        "outcome": outcome,
        "change": {k: change[k] for k in ("files", "added", "removed", "prefilter", "change_size", "risk",
                                           "qa_scope", "review_tier")},
        "tokens": {"main": main, "agents": [{k: a.get(k) for k in ("role", "attempt", "transport", "session_id",
                                                                    "herdr_id", "model", "total", "cost_usd",
                                                                    "cost_source", "duration_s")} for a in agents],
                   "by_role": by_role, "cost_by_role": {k: _rounded(v) for k, v in cost_by_role.items()},
                   "total": total_tokens,
                   "cost_usd": _rounded(_plus(main["cost_usd"], agents_cost)),
                   "note": "total = input + cache_write + output; cache_read is listed per model and priced; "
                           "cost_usd is null when a model is not in pricing.json"},
        "phases": phases,
        "signals": compute_signals(conv, events, start, end),
        "findings_history": history,
        "triage_eval": triage_eval(change, history, outcome),
        "baselines": ledger_summary(read_ledger(), project),
    }
    m["_conv"] = conv  # stripped before writing; used by the digest
    m["_events"] = [e for e in events if e.get("run") == run_id]
    return m


def decide_policy(m: dict, cfg: dict) -> tuple[str, str]:
    rp = cfg.get("retro_policy") or {}
    oc, ch = m["outcome"], m["change"]
    tok_lim = int(rp.get("full_if_main_tokens_over", 400000))
    min_lim = float(rp.get("full_if_minutes_over", 45))
    main_tok = m["tokens"]["main"]["total"]
    if oc.get("failed_attempts", 0) >= 1:
        return "full", f"{oc['failed_attempts']} failed attempt(s)"
    if oc.get("escalated"):
        return "full", "loop escalated"
    if oc.get("inconclusive", 0) >= 1:
        return "full", f"{oc['inconclusive']} inconclusive attempt(s)"
    if oc.get("status") in ("accepted", "abandoned"):
        return "full", f"outcome {oc['status']}"
    if ch.get("change_size") in ("medium", "large"):
        return "full", f"{ch['change_size']} change"
    if main_tok > tok_lim:
        return "full", f"main session {util.fmt_tokens(main_tok)} tokens > {util.fmt_tokens(tok_lim)}"
    mins, basis = policy_minutes(m["window"])
    if mins > min_lim:
        return "full", f"{basis} {mins:g} min > {min_lim:g} min"
    size = ch.get("change_size") or "small"
    if ch.get("prefilter") == "trivial":
        size = "trivial"
    return "light", f"{size} change, first-pass PASS"


def _public(m: dict) -> dict:
    return {k: v for k, v in m.items() if not k.startswith("_")}


# --------------------------------------------------------------------------- digest


def _consent_text(m: dict) -> str:
    bc = m["outcome"].get("browser_consent")
    if isinstance(bc, dict) and bc.get("status"):
        s = f"{bc.get('status')} ({bc.get('decision')}, {bc.get('source')})"
        return s + (f" — note: {bc['note']}" if bc.get("note") else "")
    evs = [e for e in m.get("_events", []) if e.get("event") in ("consent_pending", "consent")]
    if evs:
        last = evs[-1]
        if last["event"] == "consent":
            d = last.get("data") or {}
            return f"{d.get('decision')} (from events)" + (f" — note: {d['note']}" if d.get("note") else "")
        return "pending (from events)"
    return "not asked"


def anomalies(m: dict) -> list[str]:
    s = m["signals"]
    items: list[tuple[float, str]] = []
    for r in s["repeated_failures"]:
        items.append((10 + r["count"], f"repeated failing command ×{r['count']}: `{r['command']}`"))
    for r in s["rereads"]:
        items.append((5 + r["count"], f"file read ×{r['count']}: {r['file']}"))
    for r in s["large_outputs"]:
        items.append((4 + r["chars"] / 20000, f"large tool output {r['chars'] // 1000}k chars: {r['tool']} {r['arg']}"))
    ex = s["exploration_before_vault"]
    if ex["count"] >= 5:
        where = "before the first vault search" if ex["vault_search"] else "with no vault search at all"
        items.append((3 + ex["count"] / 5, f"{ex['count']} Grep/Glob/Read calls {where}"))
    n_ign = s["gate_events"].get("gate_ignored", 0)
    if n_ign:
        items.append((9, f"gate ignored {n_ign}× (stopped blocking)"))
    for h in m["findings_history"]:
        if h["stuck"]:
            items.append((8, f"attempt {h['attempt']}: stuck findings {', '.join(h['stuck'])}"))
        if h["late"]:
            items.append((6, f"attempt {h['attempt']}: late findings (missed earlier) {', '.join(h['late'])}"))
        needs = Counter(u.get("need") or "?" for u in h["unverified"])
        if needs:
            items.append((5, f"attempt {h['attempt']}: QA blocked/unverified — "
                             + ", ".join(f"{k} ×{v}" for k, v in needs.items())))
    for note in m["triage_eval"]["mismatches"]:
        items.append((4, f"triage: {note}"))
    items.sort(key=lambda x: -x[0])
    return [t for _, t in items[:10]]


def _clip(text: str, n: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _middle_cut(lines: list[str], room: int, label: str) -> list[str]:
    """Keep head and tail lines within `room` chars; one marker line for what's dropped."""
    if sum(len(x) + 1 for x in lines) <= room:
        return lines
    head, tail, used = [], [], 80
    i, j = 0, len(lines) - 1
    while i <= j:
        take_head = len(head) <= len(tail)
        line = lines[i] if take_head else lines[j]
        if used + len(line) + 1 > room:
            break
        used += len(line) + 1
        if take_head:
            head.append(line)
            i += 1
        else:
            tail.append(line)
            j -= 1
    omitted = len(lines) - len(head) - len(tail)
    return head + [f"[… {omitted} {label} omitted …]"] + list(reversed(tail))


def render_digest(m: dict, budget: int = DIGEST_BUDGET) -> str:
    oc, ch, tk = m["outcome"], m["change"], m["tokens"]
    conv = m.get("_conv") or {"prompts": [], "texts": [], "tools": []}
    agents_tok = sum(tk["by_role"].values())
    rows = [
        ("run", m["run_id"]), ("session", m["session_id"]), ("project", m["project"]),
        ("outcome", f"{oc['status']} · {oc['attempts']} attempt(s) · {oc['failed_attempts']} failed · "
                    f"{oc['inconclusive']} inconclusive" + (" · escalated" if oc["escalated"] else "")),
        ("change", f"{ch.get('files')} files, +{ch.get('added')}/−{ch.get('removed')} · prefilter {ch.get('prefilter')}"),
        ("triage", f"{ch.get('change_size')} / risk {ch.get('risk')} · qa {ch.get('qa_scope')} · review {ch.get('review_tier')}"),
        ("browser QA consent", _consent_text(m)),
        ("tokens", f"main {util.fmt_tokens(tk['main']['total'])} · agents {util.fmt_tokens(agents_tok)} · "
                   f"total {util.fmt_tokens(tk['total'])}"),
        ("tokens by role", " · ".join(f"{r} {util.fmt_tokens(v)}" for r, v in tk["by_role"].items() if v) or "none"),
        ("cost", f"{money(tk['cost_usd'])} (main {money(tk['main']['cost_usd'])})"),
        ("time", f"wall {m['window']['wall_min']:g} min · active {m['window']['active_min']:g} min · "
                 "policy {1} {0:g} min".format(*policy_minutes(m["window"]))),
    ]
    for cs, b in (m["baselines"].get("by_change_size") or {}).items():
        rows.append((f"baseline ({cs}, n={b['n']})",
                     f"main {util.fmt_tokens(b['tokens_main'] or 0)} · {money(b['cost_total'])} · "
                     f"{b['minutes_wall']} min · {b['attempts']} attempts"))
    out = [f"# Retro digest — run {m['run_id']}", "", "| metric | value |", "|---|---|"]
    out += [f"| {k} | {str(v).replace('|', '/')} |" for k, v in rows]
    out += ["", "## Phases", "", "| phase | start | end | wall min | active min | main tokens | agent tokens |",
            "|---|---|---|---|---|---|---|"]
    out += [f"| {p['name']} | {_hms(p['start'])} | {_hms(p['end'])} | {p['wall_min']:g} | {p['active_min']:g} | "
            f"{util.fmt_tokens(p['main_tokens'])} | {util.fmt_tokens(p['agent_tokens'])} |" for p in m["phases"]]
    out += ["", "## Verify history", "", "| # | triage | reviewer | qa | blocking | downgraded |", "|---|---|---|---|---|---|"]
    rd = _runs_dir() / m["run_id"]
    for h in m["findings_history"]:
        tri = _attempt_file(rd, h["attempt"], "triage.json") or {}
        tri_s = f"{tri.get('source', '?')}: {(tri.get('qa') or {}).get('scope', '?')}/{(tri.get('review') or {}).get('tier', '?')}"

        def role_s(r):
            v = h["roles"].get(r) or {}
            s = str(v.get("state") or "-")
            return s + (f" (carried #{v.get('carried_from')})" if v.get("carried") else "")

        out.append(f"| {h['attempt']} | {tri_s} | {role_s('reviewer')} | {role_s('qa')} | "
                   f"{', '.join(h['blocking']) or '0'} | {len(h['downgraded'])} |")
    out += ["", "## Anomalies (top 10)", ""] + ([f"- {a}" for a in anomalies(m)] or ["- none"])
    s = m["signals"]
    out += ["", "## Signals", "",
            f"- tool calls ({s['tool_calls_total']}): " + (", ".join(f"{k} {v}" for k, v in s["tool_calls"].items()) or "none"),
            f"- user prompts {s['user_prompts']} (corrections {s['corrections']}), interrupts {s['interrupts']}, "
            f"permission denials {s['permission_denials']}, compactions {s['compactions']}",
            f"- efforts: {', '.join(f'{k} {v}' for k, v in s['efforts'].items()) or 'n/a'}",
            f"- gate events: {', '.join(f'{k} {v}' for k, v in s['gate_events'].items()) or 'none'}"]
    head = "\n".join(out)
    prompts = ["", "## User prompts", ""]
    for p in conv["prompts"]:
        prompts += [f"### {_hms(p['ts'])}", str(p["text"])[:500], ""]
    texts = [f"- [{_hms(t['ts'])}] {_clip(t['text'], 300)}" for t in conv["texts"]]
    tools = []
    for t in conv["tools"]:
        mark = "✗" if t["is_error"] else "✓"
        tools.append(f"[{_hms(t['ts'])}] {t['name']} {mark} {_clip(_tool_arg(t['name'], t['input']), 100)} "
                     f"(out {t['chars'] / 1000:.1f}k chars)")
    prompts_s = "\n".join(prompts)
    fixed = len(head) + len(prompts_s) + 200
    room = max(0, budget - fixed)
    texts_len = sum(len(x) + 1 for x in texts)
    tools_len = sum(len(x) + 1 for x in tools)
    if texts_len + tools_len > room:
        tools = _middle_cut(tools, max(400, room - min(texts_len, room // 2)), "tool calls")
        tools_len = sum(len(x) + 1 for x in tools)
        texts = _middle_cut(texts, max(200, room - tools_len), "messages")
    body = (head + "\n" + prompts_s + "\n## Assistant messages\n\n" + ("\n".join(texts) or "- none")
            + "\n\n## Tool timeline\n\n" + ("\n".join(tools) or "none") + "\n")
    if len(body) > budget:
        body = body[: budget - 40] + "\n[… digest truncated …]\n"
    return body


# --------------------------------------------------------------------------- task file


def render_task(m: dict, ctx: dict, policy: str) -> str:
    from . import prompts

    rd = _runs_dir() / m["run_id"]
    judges = [str(rd / f"attempt-{n}" / "judge.json") for n in _attempt_dirs(rd)
              if (rd / f"attempt-{n}" / "judge.json").is_file()]
    tpl = (paths.prompts_dir() / "retro.md").read_text(encoding="utf-8")
    return prompts.render(tpl, {
        **prompts.tool_values(),
        "run_id": m["run_id"], "session_id": m["session_id"] or "", "platform": m.get("platform") or "claude",
        "repo_root": (ctx.get("repo") or {}).get("root") or "",
        "project_slug": m["project"], "harness_project": HARNESS_PROJECT,
        "metrics_path": str(rd / "retro" / "metrics.json"), "digest_path": str(rd / "retro" / "digest.md"),
        "judge_paths": "\n".join(f"  - {j}" for j in judges), "policy": policy,
    })


def write_artifacts(m: dict, ctx: dict, policy: str, full: bool, task: bool = True) -> Path | None:
    rdir = _runs_dir() / m["run_id"] / "retro"
    rdir.mkdir(parents=True, exist_ok=True)
    util.atomic_write_json(rdir / "metrics.json", _public(m))
    path = rdir / "task.md"
    if full:
        util.atomic_write_text(rdir / "digest.md", render_digest(m))
        if task:
            util.atomic_write_text(path, render_task(m, ctx, policy))
    return path if full and task else None


NO_VAULT_RETRO = "retro: light only (no agent-vault)"


def _spawns_retro(name: str) -> bool:
    try:
        return bool(transports.get(name).supports_retro)
    except Exception:
        return False


def retro_next(run_id: str, transport_name: str | None, platform: str | None) -> str:
    """The line(s) telling the main agent how to run the retro of a closed run.

    Transport `agent` is in-session (Claude Code `Agent(...)` / a Codex custom agent); a transport that supports retro (e.g. herdr) starts it
    through `vloop spawn retro`; the others fall back to the in-session line. Without agent-vault there is nobody to write the retro: light only, and no
    proposals line.
    """
    found = vault.discover()
    if found is None:
        return NO_VAULT_RETRO
    rd = _runs_dir() / run_id
    ctx = util.read_json(rd / "context.json") or {}
    root = (ctx.get("repo") or {}).get("root")
    cfg = config.load(root)
    task = rd / "retro" / "task.md"
    model = ((cfg.get("models") or {}).get("retro") or ["sonnet"])[0]
    prompt = f"Your task file is {task}. Read it completely and follow it."
    if transport_name and transport_name != "agent" and _spawns_retro(transport_name):
        label = "Herdr" if transport_name == "herdr" else transport_name
        line = f"next ({label}, run_in_background: true): {util.vloop_cli()} spawn retro --run {run_id}"
    elif platform == "codex":
        line = (f'next (Codex, in the background): spawn the custom agent "{vault.keeper_agent("codex")}" (model {model}) '
                f'with the prompt: {prompt}')
    else:
        line = (f'next (Agent tool, run_in_background: true): Agent(subagent_type="{vault.keeper_agent(platform)}", '
                f'model="{model}", description="retro", prompt="{prompt}")')
    pending = vault.proposals(root or os.getcwd())
    if pending:
        line += (f"\nproposals: {len(pending)} pending — review: {found.cli} lessons --proposals; "
                 f"then {found.cli} proposal accept|reject <note>")
    return line


def _ran_agents(rd: Path, ctx: dict) -> bool:
    for a in ctx.get("attempts") or []:
        if a.get("agents"):
            return True
    for p in _plans(rd):
        if any(isinstance(r, dict) and r.get("run") for r in (p.get("roles") or {}).values()):
            return True
    return False


# --------------------------------------------------------------------------- commands


def retro_plan(run_id: str) -> tuple[str, dict]:
    rd = _runs_dir() / run_id
    ctx = util.read_json(rd / "context.json")
    if not isinstance(ctx, dict):
        raise CliError(f"run {run_id} not found", 2)
    if ctx.get("status") in ("open", "escalated"):
        raise CliError(f"run {run_id} is still {ctx.get('status')}; retro-plan runs after the loop closes", 2)
    cfg = config.load((ctx.get("repo") or {}).get("root"))
    m = compute_metrics(run_id)
    policy, reason = decide_policy(m, cfg)
    transport = transports.select(cfg).name
    downgraded = policy == "full" and not vault.available()
    if downgraded:
        policy, reason = "light", f"{reason}; no agent-vault to write the retro"
    appended = _ran_agents(rd, ctx) and append_ledger(ledger_row(m, ctx, policy))
    write_artifacts(m, ctx, policy, policy == "full")
    util.atomic_write_json(rd / "retro" / "plan.json", {"policy": policy, "reason": reason, "at": util.now_iso()})
    _append_event(ctx.get("session_id"), "retro_plan", repo=(ctx.get("repo") or {}).get("key"), run=run_id,
                  policy=policy, reason=reason)
    if policy == "full":
        text = (f"retro: full — {reason} · task: {rd / 'retro' / 'task.md'}\n"
                f"{retro_next(run_id, transport, m.get('platform'))}")
    else:
        led = "ledger updated" if appended else "ledger unchanged"
        text = (f"retro: light — {reason} · {led} (≈{util.fmt_tokens(m['tokens']['total'])} tokens, "
                f"{money(m['tokens']['cost_usd'])})")
        if downgraded:
            text += f"\n{retro_next(run_id, transport, m.get('platform'))}"
    return text, m


def _repo_of(cwd: str | None) -> dict | None:
    if not cwd or not os.path.isdir(cwd):
        return None
    rc, out, _ = util.run(["git", "-C", cwd, "rev-parse", "--show-toplevel"], timeout=10)
    if rc != 0 or not out.strip():
        return None
    root = os.path.realpath(out.strip())
    rc, br, _ = util.run(["git", "-C", root, "branch", "--show-current"], timeout=10)
    rc2, head, _ = util.run(["git", "-C", root, "rev-parse", "HEAD"], timeout=10)
    return {"key": hashlib.sha1(root.encode()).hexdigest()[:12], "root": root, "slug": util.slugify(os.path.basename(root)),
            "branch": br.strip() or None if rc == 0 else None, "head": head.strip() or None if rc2 == 0 else None}


def retro_prep(run_id: str | None, sid: str | None) -> tuple[str, str]:
    if run_id:
        rd = _runs_dir() / run_id
        ctx = util.read_json(rd / "context.json")
        if not isinstance(ctx, dict):
            raise CliError(f"run {run_id} not found", 2)
    else:
        if not sid:
            raise CliError("retro-prep needs --run ID or --session SID", 2)
        st = load_session_state(sid)
        if st is None and not load_events(sid) and not module_for(_platform_name(sid)).transcript_for(sid):
            raise CliError(f"session {sid} not found", 2)
        run_id = _new_retro_run_id()
        rd = _runs_dir() / run_id
        rd.mkdir(parents=True, exist_ok=True)
        repo = _repo_of((st or {}).get("cwd"))
        ctx = {"schema": 1, "run_id": run_id, "kind": "retro", "session_id": sid, "created_at": util.now_iso(),
               "repo": repo, "target": None, "roles": ["retro"], "app_url": None,
               "transport": transports.select(config.load((repo or {}).get("root"))).name,
               "platform": _platform_name(sid),
               "attempts": [], "failed_attempts": 0, "inconclusive_streak": 0, "status": "open", "waived": [],
               "disputes": {}, "user_notes": [], "flags": {}, "browser_consent": None, "standalone": None}
        util.atomic_write_json(rd / "context.json", ctx)
    cfg = config.load((ctx.get("repo") or {}).get("root"))
    m = compute_metrics(run_id)
    task = write_artifacts(m, ctx, "full", True, task=vault.available())
    if not (rd / "retro" / "plan.json").exists():
        util.atomic_write_json(rd / "retro" / "plan.json",
                               {"policy": "full", "reason": "requested (retro-prep)", "at": util.now_iso()})
    nxt = retro_next(run_id, transports.select(cfg).name, m.get("platform"))
    if task is None:
        return f"{nxt}\nmetrics: {rd / 'retro' / 'metrics.json'}\ndigest: {rd / 'retro' / 'digest.md'}", run_id
    return f"retro task: {task}\n{nxt}", run_id


def format_ledger_row(r: dict) -> str:
    return (f"{str(r.get('ts', ''))[:16]}  {r.get('project')}  {r.get('run_id')}  {r.get('outcome')}  "
            f"attempts {r.get('attempts')}  {r.get('change_size') or '-'}  qa {r.get('qa_scope') or '-'}  "
            f"review {r.get('review_tier') or '-'}  main {util.fmt_tokens((r.get('tokens') or {}).get('main') or 0)}  "
            f"{money((r.get('cost_usd') or {}).get('total'))}  {(r.get('minutes') or {}).get('wall', 0)} min  "
            f"retro {r.get('retro')}")


def format_summary(s: dict) -> str:
    lines = [f"ledger summary · {s['project'] or 'all projects'} · last {s['rows']} rows"]
    for cs, b in sorted(s["by_change_size"].items()):
        lines.append(f"{cs}: n={b['n']} · main ≈{util.fmt_tokens(b['tokens_main'] or 0)} tokens · "
                     f"{money(b['cost_total'])} · {b['minutes_wall']} min · {b['attempts']} attempts")
    lines.append("qa_scope: " + (", ".join(f"{k} {v}" for k, v in sorted(s["qa_scope"].items())) or "none"))
    return "\n".join(lines)


def gate_report(since: str | None) -> dict:
    since = since or config.user_config().get("installed_at") or "0000-00-00"
    evs: list[dict] = []
    for p in sorted((util.state_dir() / "sessions").glob("*.events.jsonl")):
        evs += [e for e in _iter_jsonl(p) if str(e.get("ts") or "")[:10] >= since]
    by_sid: dict[str, list] = {}
    for e in evs:
        by_sid.setdefault(e.get("sid") or "?", []).append(e)
    warn_verified = warn_never = 0
    for sid, es in by_sid.items():
        begins = [util.parse_iso(e.get("ts")) for e in es if e.get("event") == "begin"]
        for w in (e for e in es if e.get("event") == "gate_warn"):
            t = util.parse_iso(w.get("ts")) or 0
            if any(b is not None and t <= b <= t + 7200 for b in begins):
                warn_verified += 1
            else:
                warn_never += 1
    cnt = Counter(e.get("event") for e in evs)
    defer = Counter((e.get("data") or {}).get("reason") for e in evs if e.get("event") == "gate_defer")
    skips = sum(1 for e in evs if e.get("event") == "skip" and (e.get("data") or {}).get("by") == "user")
    prefilter = Counter((e.get("data") or {}).get("prefilter") for e in evs
                        if e.get("event") == "begin" and (e.get("data") or {}).get("prefilter"))
    tri = [e.get("data") or {} for e in evs if e.get("event") == "triage"]
    tri_src = Counter(d.get("source") for d in tri)
    costs = [float(d.get("cost_usd")) for d in tri if d.get("source") == "triage" and isinstance(d.get("cost_usd"), (int, float))]
    led = [r for r in read_ledger() if str(r.get("ts") or "")[:10] >= since]
    med = {}
    for cs in sorted({r.get("change_size") or "unknown" for r in led}):
        med[cs] = _median([(r.get("cost_usd") or {}).get("total") for r in led if (r.get("change_size") or "unknown") == cs])
    return {
        "since": since, "until": util.today(), "sessions": len(by_sid),
        "gate_warn": cnt.get("gate_warn", 0), "warn_verified": warn_verified, "warn_never_verified": warn_never,
        "gate_block": cnt.get("gate_block", 0), "gate_ignored": cnt.get("gate_ignored", 0),
        "gate_defer": dict(defer), "docs_skips": cnt.get("gate_skip_docs", 0), "user_skips": skips,
        "prefilter": {k: prefilter.get(k, 0) for k in ("skip", "trivial", "triage")},
        "triage_source": dict(tri_src), "fallbacks": tri_src.get("fallback", 0),
        "qa_scopes": dict(Counter(d.get("qa_scope") for d in tri if d.get("qa_scope"))),
        "review_tiers": dict(Counter(d.get("review_tier") for d in tri if d.get("review_tier"))),
        "avg_triage_cost_usd": round(sum(costs) / len(costs), 6) if costs else None,
        "outcomes": dict(Counter(r.get("outcome") for r in led)), "median_cost_by_change_size": med,
    }


def format_gate_report(g: dict) -> str:
    def kv(d):
        return ", ".join(f"{k} {v}" for k, v in d.items()) or "none"

    return "\n".join([
        f"verify-loop gate report · {g['since']} → {g['until']}",
        f"sessions: {g['sessions']}",
        f"would-block (warn): {g['gate_warn']} · would-block → verified: {g['warn_verified']} · "
        f"would-block → never verified: {g['warn_never_verified']}",
        f"blocks: {g['gate_block']} · ignored: {g['gate_ignored']} · defers: {kv(g['gate_defer'])} · "
        f"docs skips: {g['docs_skips']} · user skips: {g['user_skips']}",
        f"prefilter: {kv(g['prefilter'])} · triage sources: {kv(g['triage_source'])} (fallbacks {g['fallbacks']})",
        f"qa scopes: {kv(g['qa_scopes'])} · review tiers: {kv(g['review_tiers'])} · avg triage cost: "
        + (money(g['avg_triage_cost_usd']) if g["avg_triage_cost_usd"] is not None else "n/a"),
        f"loop outcomes: {kv(g['outcomes'])} · median cost per change size: "
        + (", ".join(f"{k} {money(v)}" for k, v in g["median_cost_by_change_size"].items()) or "none"),
        "ready for enforce? decide yourself — switch with: vloop mode enforce",
    ])


# --------------------------------------------------------------------------- CLI


def _guard(fn):
    def wrapped(args) -> int:
        try:
            return fn(args)
        except CliError as e:
            print(f"vloop: error: {e}", file=sys.stderr)
            return e.code
    return wrapped


def cmd_retro_plan(args) -> int:
    text, _ = retro_plan(args.run)
    print(text)
    return 0


def cmd_retro_prep(args) -> int:
    from . import admin

    text, _ = retro_prep(args.run, None if args.run else admin.resolve_session(args.session))
    print(text)
    return 0


def cmd_ledger(args) -> int:
    rows = read_ledger()
    if args.summary:
        s = ledger_summary(rows, args.project, last=args.last or 20)
        print(json.dumps(s, indent=2, ensure_ascii=False) if args.json else format_summary(s))
        return 0
    if args.project:
        rows = [r for r in rows if r.get("project") == args.project]
    if args.last:
        rows = rows[-args.last:]
    if args.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
    else:
        print("\n".join(format_ledger_row(r) for r in rows) if rows else "ledger: empty")
    return 0


def cmd_gate_report(args) -> int:
    g = gate_report(args.since)
    print(json.dumps(g, indent=2, ensure_ascii=False) if args.json else format_gate_report(g))
    return 0


def register(sub) -> None:
    def add(name, fn, help_text):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("--session", default=None, help="session id (default: the platform's session env)")
        p.set_defaults(fn=_guard(fn))
        return p

    p = add("retro-plan", cmd_retro_plan, "metrics, retro policy, ledger line, retro task")
    p.add_argument("--run", required=True)
    p = add("retro-prep", cmd_retro_prep, "full retro artifacts for a run or a whole session")
    p.add_argument("--run", default=None)
    p = add("ledger", cmd_ledger, "ledger rows or medians")
    p.add_argument("--project", default=None)
    p.add_argument("--summary", action="store_true")
    p.add_argument("--last", type=int, default=None)
    p.add_argument("--json", action="store_true")
    p = add("gate-report", cmd_gate_report, "gate/triage statistics since a date")
    p.add_argument("--since", default=None)
    p.add_argument("--json", action="store_true")
