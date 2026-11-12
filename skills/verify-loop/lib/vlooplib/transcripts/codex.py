"""Codex rollout transcripts (~/.codex/sessions/YYYY/MM/DD/rollout-*-<session id>.jsonl): locate, token usage, timeline."""
from __future__ import annotations

import glob
import json
import os
import re
from pathlib import Path

from .. import paths, util
from . import first_ts, int_of, iter_jsonl, with_cost

SHELL_TOOLS = ("shell", "exec_command", "shell_command", "container.exec", "local_shell", "unified_exec")
SCAN_LIMIT = 300


def sessions_root() -> Path:
    env = os.environ.get("VERIFY_CODEX_SESSIONS_DIR")
    return Path(env).expanduser() if env else paths.codex_home() / "sessions"


def _meta_id(path: str) -> str | None:
    for d in iter_jsonl(path):
        if d.get("type") == "session_meta":
            p = d.get("payload")
            return str(p.get("id")) if isinstance(p, dict) and p.get("id") else None
        return None
    return None


def transcript_for(sid: str | None) -> Path | None:
    if not sid:
        return None
    root = sessions_root()
    hits = glob.glob(str(root / "**" / f"rollout-*{glob.escape(str(sid))}.jsonl"), recursive=True)
    if hits:
        return Path(max(hits, key=lambda h: os.path.getmtime(h)))
    recent = sorted(glob.glob(str(root / "**" / "rollout-*.jsonl"), recursive=True), key=os.path.getmtime,
                    reverse=True)[:SCAN_LIMIT]
    for h in recent:
        if _meta_id(h) == sid:
            return Path(h)
    return None


def _payload(d: dict) -> dict:
    p = d.get("payload")
    return p if isinstance(p, dict) else {}


def _numbers(u: dict) -> dict:
    inp_all, out = int_of(u.get("input_tokens")), int_of(u.get("output_tokens"))
    cached = min(int_of(u.get("cached_input_tokens")), inp_all)
    return {"input": inp_all - cached, "cache_write": 0, "cache_write_5m": 0, "cache_write_1h": 0,
            "cache_read": cached, "output": out, "thinking": min(int_of(u.get("reasoning_output_tokens")), out)}


_KEYS = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_output_tokens")


def _signature(u: dict) -> tuple:
    return tuple(int_of(u.get(k)) for k in _KEYS) + (int_of(u.get("total_tokens")),)


def _delta(total: dict, prev: dict | None) -> dict:
    if prev is None or any(int_of(total.get(k)) < int_of(prev.get(k)) for k in _KEYS):
        return total
    return {k: int_of(total.get(k)) - int_of(prev.get(k)) for k in _KEYS}


def usage_entries(path, start: str | None = None, end: str | None = None, main_only: bool = False) -> list[dict]:
    """One entry per model request: `last_token_usage` of each `token_count` event whose running total changed.

    Codex repeats a token_count event with an unchanged total (and emits info-less rate-limit events), so a repeated
    total is skipped. The model is the latest `turn_context` model at that point; there is no per-request id.
    """
    entries: list[dict] = []
    model: str | None = None
    prev_total: dict | None = None
    prev_sig: tuple | None = None
    for d in iter_jsonl(path):
        t, p = d.get("type"), _payload(d)
        if t == "turn_context":
            model = p.get("model") or model
            continue
        if t == "session_meta":
            model = p.get("model") or model
            continue
        if t != "event_msg" or p.get("type") != "token_count":
            continue
        info = p.get("info")
        if not isinstance(info, dict):
            continue
        total, last = info.get("total_token_usage"), info.get("last_token_usage")
        if isinstance(total, dict):
            sig = _signature(total)
            if sig == prev_sig:
                continue
            usage = last if isinstance(last, dict) else _delta(total, prev_total)
            prev_sig, prev_total = sig, total
        elif isinstance(last, dict):
            usage = last
        else:
            continue
        ts = d.get("timestamp") or ""
        if (start and ts < start) or (end and ts > end):
            continue
        n = _numbers(usage)
        if not (n["input"] or n["cache_read"] or n["output"]):
            continue
        entries.append({"ts": ts, "model": model or "unknown", "n": n})
    return with_cost(entries)


def _command_text(cmd) -> str:
    if isinstance(cmd, list):
        parts = [str(x) for x in cmd]
        if len(parts) >= 3 and parts[1] in ("-lc", "-c") and os.path.basename(parts[0]) in ("bash", "sh", "zsh"):
            return parts[2]
        return " ".join(parts)
    return str(cmd or "")


def _args(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        v = json.loads(raw) if isinstance(raw, str) else None
    except ValueError:
        return {"input": raw} if isinstance(raw, str) else {}
    return v if isinstance(v, dict) else ({"input": raw} if isinstance(raw, str) else {})


def _tool_use(p: dict) -> tuple[str, dict]:
    name = str(p.get("name") or "")
    if p.get("type") == "local_shell_call":
        action = p.get("action") if isinstance(p.get("action"), dict) else {}
        return "Bash", {"command": _command_text(action.get("command"))}
    if p.get("type") == "custom_tool_call":
        raw = p.get("input")
        return name or "custom", {"command": raw} if name == "apply_patch" else {"input": raw}
    args = _args(p.get("arguments"))
    if name in SHELL_TOOLS:
        return "Bash", {"command": _command_text(args.get("command") if "command" in args else args.get("cmd"))}
    return name or "tool", args


def _output(p: dict) -> tuple[str, bool]:
    raw = p.get("output")
    if isinstance(raw, dict):
        text = str(raw.get("content") if raw.get("content") is not None else json.dumps(raw))
        return text, raw.get("success") is False
    text = raw if isinstance(raw, str) else json.dumps(raw)
    err = False
    try:
        v = json.loads(text)
    except ValueError:
        v = None
    if isinstance(v, dict):
        meta = v.get("metadata") if isinstance(v.get("metadata"), dict) else {}
        if isinstance(v.get("output"), str):
            text = v["output"]
        if isinstance(meta.get("exit_code"), int):
            err = meta["exit_code"] != 0
        return text, err
    m = re.search(r"(?:^|\n)(?:Exit code|exit code|Process exited with code):?\s*(-?\d+)", text)
    return text, bool(m and int(m.group(1)) != 0)


def _assistant_text(p: dict) -> str:
    parts = p.get("content")
    if isinstance(parts, str):
        return parts
    return "\n".join(x.get("text", "") for x in parts or [] if isinstance(x, dict) and isinstance(x.get("text"), str))


def timeline(path, start: str | None = None, end: str | None = None) -> list[dict]:
    """Rollout lines converted to the Claude record shape (user / assistant with tool_use blocks / turn_duration), so
    prompts, tool calls, interrupts, compactions and active time are read by the same code for both platforms."""
    out: list[dict] = []
    effort: str | None = None
    model: str | None = None
    started: str | None = None
    for i, d in enumerate(iter_jsonl(path)):
        ts, t, p = d.get("timestamp"), d.get("type"), _payload(d)
        if not ts:
            continue
        rec: dict | None = None
        uid = f"cx-{i}"
        if t == "turn_context":
            effort, model = p.get("effort") or effort, p.get("model") or model
        elif t == "compacted":
            rec = {"type": "system", "subtype": "compact_boundary"}
        elif t == "event_msg":
            et = p.get("type")
            if et == "user_message" and isinstance(p.get("message"), str):
                rec = {"type": "user", "message": {"role": "user", "content": p["message"]}}
            elif et == "task_started":
                started = ts
            elif et in ("task_complete", "turn_complete"):
                ms = p.get("duration_ms")
                if not isinstance(ms, (int, float)):
                    a, b = util.parse_iso(started), util.parse_iso(ts)
                    ms = (b - a) * 1000 if a is not None and b is not None else 0
                started = None
                if ms:
                    rec = {"type": "system", "subtype": "turn_duration", "durationMs": ms}
            elif et == "turn_aborted":
                started = None
                rec = {"type": "user", "message": {"role": "user",
                                                    "content": "[Request interrupted by user for tool use]"}}
        elif t == "response_item":
            rt = p.get("type")
            if rt == "message" and p.get("role") == "assistant":
                text = _assistant_text(p)
                if text.strip():
                    rec = {"type": "assistant", "effort": effort,
                           "message": {"role": "assistant", "model": model,
                                       "content": [{"type": "text", "text": text}]}}
            elif rt in ("function_call", "custom_tool_call", "local_shell_call"):
                name, inp = _tool_use(p)
                rec = {"type": "assistant", "effort": effort,
                       "message": {"role": "assistant", "model": model, "content": [
                           {"type": "tool_use", "id": p.get("call_id") or uid, "name": name, "input": inp}]}}
            elif rt in ("function_call_output", "custom_tool_call_output"):
                text, err = _output(p)
                rec = {"type": "user", "message": {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": p.get("call_id"), "content": text, "is_error": err}]}}
        if rec is None or (start and ts < start) or (end and ts > end):
            continue
        rec.update({"timestamp": ts, "uuid": uid, "isSidechain": False})
        out.append(rec)
    return out


def _recorded_subagents(events: list[dict], st: dict | None) -> list[tuple[str, str | None]]:
    out: list[tuple[str, str | None]] = []
    for e in events or []:
        data = e.get("data") if isinstance(e.get("data"), dict) else {}
        raw = data.get("agent_transcript_path") or data.get("transcript") or e.get("agent_transcript_path")
        if isinstance(raw, str) and raw:
            kind = data.get("agent_type") or e.get("agent_type")
            out.append((raw, kind if isinstance(kind, str) else None))
    agents = (st or {}).get("agents")
    for a in (agents.values() if isinstance(agents, dict) else []):
        raw = a.get("transcript_path") if isinstance(a, dict) else None
        if isinstance(raw, str) and raw:
            out.append((raw, a.get("agent_type") if isinstance(a.get("agent_type"), str) else None))
    return out


def subagents(main_path: Path | None, sid: str | None, events: list[dict], start: str | None,
              st: dict | None = None) -> list[dict]:
    """Subagent rollouts recorded by the SubagentStop hook, from the session events (`agent_transcript_path`, or
    `transcript` as vloop's hook writes it) and the session state's `agents` map."""
    seen: set[str] = set()
    out = []
    for raw, agent_type in _recorded_subagents(events, st):
        f = Path(raw).expanduser()
        if str(f) in seen or not f.is_file():
            continue
        seen.add(str(f))
        ft = first_ts(f)
        if start and ft and ft < start:
            continue
        out.append({"path": f, "agent_type": agent_type, "first_ts": ft})
    return out
