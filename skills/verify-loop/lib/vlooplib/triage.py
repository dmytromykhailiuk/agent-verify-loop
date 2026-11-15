"""Headless triage call: `claude -p --agent verify-triage` or `codex exec` with a JSON schema; mechanical fallback on
any failure (§2.8)."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path

from . import platforms, roles, state, util

PROMPT = "Triage the change described on stdin. Answer only with the JSON object."
CODEX_SCHEMA_FILE = "triage.codex.schema.json"


def command(cfg: dict) -> list[str]:
    from . import verdict

    prefix = None
    raw = os.environ.get("VERIFY_TRIAGE_CMD")
    if raw:
        try:
            v = json.loads(raw)
            if isinstance(v, list) and v and all(isinstance(x, str) for x in v):
                prefix = v
        except ValueError:
            prefix = None
    schema = json.dumps(verdict.load_schema("triage-output"), separators=(",", ":"), ensure_ascii=False)
    return platforms.detect().triage_cmd(prefix, cfg, schema, PROMPT)


def child_env() -> dict:
    from . import transports

    return transports.child_env({"VERIFY_LOOP_MODE": "off"})


def stdin_text(platform: str, text: str) -> str:
    """Claude gets the agent via `--agent` and the task on stdin; Codex gets the agent's instructions + task."""
    if platform != "codex":
        return text
    from .platforms import codex_cmds

    return codex_cmds.agent_prompt("verify-triage", f"{PROMPT}\n\n{text}")


def strict_schema(schema: dict) -> dict:
    """The Codex `--output-schema` variant: every property required, no extra properties, optional ones nullable."""
    s = copy.deepcopy(schema)

    def walk(node):
        if not isinstance(node, dict):
            return node
        if node.get("type") == "object" or "properties" in node:
            props = node.get("properties") or {}
            req = set(node.get("required") or [])
            for k, sub in props.items():
                walk(sub)
                if k not in req:
                    t = sub.get("type")
                    types = t if isinstance(t, list) else [t] if t else []
                    if "null" not in types:
                        sub["type"] = [*types, "null"]
                    if "enum" in sub and None not in sub["enum"]:
                        sub["enum"] = [*sub["enum"], None]
            node["required"] = list(props)
            node["additionalProperties"] = False
        if isinstance(node.get("items"), dict):
            walk(node["items"])
        return node

    return walk(s)


def codex_schema() -> dict:
    from . import verdict

    return strict_schema(verdict.load_schema("triage-output"))


def _execute(cmd: list[str], cwd: str, stdin_text: str, timeout: float) -> tuple[int | None, str, str]:
    """(rc or None on timeout, stdout, stderr). Kills the whole process group on timeout."""
    import signal
    import subprocess

    try:
        p = subprocess.Popen(cmd, cwd=cwd, env=child_env(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, start_new_session=True)
    except OSError as e:
        return 127, "", str(e)
    try:
        out, err = p.communicate(stdin_text, timeout=timeout)
        return p.returncode, out, err
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            p.communicate(timeout=5)
        except Exception:  # noqa: BLE001 - best effort
            pass
        return None, "", ""


def _parse(out: str) -> tuple[dict | None, str | None]:
    out = (out or "").strip()
    doc = None
    for cand in (out, out.splitlines()[-1] if out else ""):
        try:
            doc = json.loads(cand)
            break
        except ValueError:
            continue
    if not isinstance(doc, dict):
        return None, "invalid JSON output"
    if doc.get("is_error"):
        return doc, f"is_error: {str(doc.get('result') or '')[:80]}"
    if doc.get("subtype") != "success":
        return doc, f"subtype {doc.get('subtype')}"
    return doc, None


def _parse_codex(out_path: str | None, events: str) -> tuple[dict | None, str | None]:
    """(the answer object, error). The answer is the `-o` file; a failed turn shows up as `turn.failed`/`error`."""
    from .transports import common

    for ev in common.codex_events(events):
        if ev.get("type") in ("turn.failed", "error"):
            msg = (ev.get("error") or {}).get("message") if isinstance(ev.get("error"), dict) else ev.get("message")
            return None, f"codex {ev.get('type')}: {str(msg or '')[:80]}"
    try:
        text = Path(out_path).read_text(encoding="utf-8") if out_path else ""
    except OSError:
        text = ""
    if not text.strip():
        return None, "no output file"
    try:
        obj = json.loads(text)
    except ValueError:
        from . import verdict

        obj = verdict.extract_json(text, keys=TRIAGE_KEYS)
    return (obj, None) if isinstance(obj, dict) else (None, "invalid JSON output")


def _agent_info(doc: dict | None, alias: str, duration_s: float, error: str | None) -> dict:
    from .transports import common

    return {**common.claude_usage(doc, alias, duration_s), "error": error}


def _codex_agent_info(events: str, model: str | None, duration_s: float, error: str | None) -> dict:
    from .transports import common

    u = common.codex_usage(events, model or "codex", duration_s)
    return {"model": u["model"], "duration_s": u["duration_s"], "cost_usd": None, "tokens": u["tokens"],
            "error": error}


TRIAGE_KEYS = ("qa", "review", "qa_scope", "scope", "review_tier", "tier", "change_size", "specialists")
_SIZE = ("small", "medium", "large")


def _text(v) -> str:
    if isinstance(v, list):
        return "; ".join(str(x) for x in v if str(x).strip())
    return "" if v is None else str(v)


def _list(v) -> list:
    if isinstance(v, list):
        return [str(x) for x in v]
    return [str(v)] if isinstance(v, str) and v.strip() else []


def _size_from(summary: dict | None) -> str:
    s = summary or {}
    lines, files = int(s.get("changed_lines") or 0), int(s.get("files") or 0)
    if lines > 400 or files > 15:
        return "large"
    return "small" if lines < 60 and files <= 3 else "medium"


# flat key the model may use -> (sub-object, key in it)
_FLAT = {"qa_scope": ("qa", "scope"), "scope": ("qa", "scope"), "needs_login": ("qa", "needs_login"),
         "qa_needs_login": ("qa", "needs_login"), "qa_reason": ("qa", "reason"), "qa_focus": ("qa", "focus"),
         "review_tier": ("review", "tier"), "tier": ("review", "tier"), "review_reason": ("review", "reason"),
         "review_focus": ("review", "focus")}


_TRUE = ("true", "yes", "on", "1", "run")


def _flag(v) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in _TRUE
    return bool(v)


def _specialists(o: dict) -> dict:
    raw = o.pop("specialists", None)
    if isinstance(raw, list):
        base = {str(x).strip().lower(): {"run": True} for x in raw}
    elif isinstance(raw, dict):
        base = raw
    else:
        base = {}
    out = {}
    for role in roles.SPECIALISTS:
        v = base.get(role)
        if v is None and role in o:
            v = o.pop(role)
        sub = dict(v) if isinstance(v, dict) else ({} if v is None else {"run": v})
        for key in ("run", "reason", "focus"):
            flat = f"{role}_{key}"
            if flat in o:
                sub.setdefault(key, o.pop(flat))
        if role == "ux":
            for flat in ("ui_spec", "ux_ui_spec", "ux_spec"):
                if flat in o:
                    sub.setdefault("ui_spec", o.pop(flat))
        sub["run"] = _flag(sub.get("run", False))
        sub["reason"] = _text(sub.get("reason"))
        sub["focus"] = _list(sub.get("focus"))
        if role == "ux":
            spec = str(sub.get("ui_spec") or "").strip().lower()
            sub["ui_spec"] = spec if spec in roles.UI_SPEC else "n/a"
        out[role] = sub
    return out


def reshape(obj: dict, summary: dict | None = None) -> dict:
    """Repair near-miss triage output into the schema shape: flat keys (`qa_scope`, `review_tier`, `needs_login`,
    `reasons`, `focus`), a bare string for `qa`/`review`, list reasons, missing size/risk/confidence (§2.8)."""
    o = dict(obj)
    reasons = _list(o.pop("reasons", None) or o.pop("reason", None))
    focus = _list(o.pop("focus", None))
    subs = {}
    for part, key in (("qa", "scope"), ("review", "tier")):
        v = o.get(part)
        subs[part] = dict(v) if isinstance(v, dict) else ({key: v} if isinstance(v, str) else {})
    for flat, (part, key) in _FLAT.items():
        if flat in o:
            v = o.pop(flat)
            subs[part].setdefault(key, v)
    qa, rv = subs["qa"], subs["review"]
    qa.setdefault("reason", reasons[0] if reasons else "")
    rv.setdefault("reason", reasons[1] if len(reasons) > 1 else (reasons[0] if reasons else ""))
    for sub in (qa, rv):
        sub["reason"] = _text(sub.get("reason"))
        sub["focus"] = _list(sub.get("focus", focus))
    if isinstance(qa.get("needs_login"), str):
        qa["needs_login"] = qa["needs_login"].strip().lower() in ("true", "yes", "1")
    qa.setdefault("needs_login", False)
    for sub, key in ((qa, "scope"), (rv, "tier")):
        if isinstance(sub.get(key), str):
            sub[key] = sub[key].strip().lower()
    o["qa"], o["review"] = qa, rv
    o["specialists"] = _specialists(o)
    if o.get("change_size") not in _SIZE:
        o["change_size"] = _size_from(summary)
    o.setdefault("risk", "medium")
    o.setdefault("confidence", "low")
    for key in ("change_size", "risk", "confidence"):
        if isinstance(o.get(key), str):
            o[key] = o[key].strip().lower()
    return o


def write(run_dir: Path, ctx: dict, attempt: int, tri: dict) -> dict:
    """Normalize, store attempt-N/triage.json and log the `triage` event."""
    from . import verdict

    tri, _ = verdict.normalize("triage", tri, run_id=ctx["run_id"], attempt=attempt)
    util.atomic_write_json(Path(run_dir) / f"attempt-{attempt}" / "triage.json", tri)
    ag = tri.get("agent") or {}
    if ctx.get("session_id"):
        state.append_event(ctx["session_id"], "triage", repo=ctx["repo"]["key"], run=ctx["run_id"], attempt=attempt,
                           source=tri.get("source"), model=ag.get("model"), duration_s=ag.get("duration_s"),
                           cost_usd=ag.get("cost_usd"), qa_scope=(tri.get("qa") or {}).get("scope"),
                           review_tier=(tri.get("review") or {}).get("tier"), change_size=tri.get("change_size"),
                           risk=tri.get("risk"), confidence=tri.get("confidence"), error=ag.get("error"),
                           specialists=[r for r, v in (tri.get("specialists") or {}).items()
                                        if isinstance(v, dict) and v.get("run")])
    return tri


def run_triage(run_dir: Path, ctx: dict, attempt: int, cfg: dict, summary: dict, project: dict, auth: dict | None, *,
               previous_plan: str = "", delta: dict | None = None) -> dict:
    """Render triage.input.md, call the headless agent, store triage.json (source triage or fallback)."""
    import time

    from . import plan, prompts, verdict

    text = prompts.render_triage(run_dir=run_dir, ctx=ctx, attempt=attempt, summary=summary, cfg=cfg,
                                 project=project, auth=auth, previous_plan=previous_plan, delta=delta)
    t = cfg.get("triage") or {}
    timeout = float(t.get("timeout_s") or 300)
    plat = platforms.detect().name
    codex = plat == "codex"
    if codex:
        from .transports import common

        alias = common.platform_model(cfg, "triage", None, "codex") or "codex"
    else:
        alias = str(t.get("model") or "sonnet")
    doc, error, obj, out, out_path = None, None, None, "", None
    t0 = time.monotonic()
    try:
        cmd = command(cfg)
    except Exception as e:  # noqa: BLE001 - any failure falls back to mechanical triage
        cmd, error = None, f"no command: {e}"
    if cmd is not None:
        if codex:
            from .platforms import codex_cmds

            out_path = codex_cmds.out_file(cmd)
        rc, out, err = _execute(cmd, ctx["repo"]["root"], stdin_text(plat, text), timeout)
        if rc is None:
            error = f"timeout after {int(timeout)}s"
        elif rc != 0:
            first = (err or out).strip().splitlines()
            error = f"exit {rc}" + (f": {first[0][:80]}" if first else "")
        elif codex:
            obj, error = _parse_codex(out_path, out)
        else:
            doc, error = _parse(out)
            if error is None:
                so = doc.get("structured_output")
                obj = so if isinstance(so, dict) else verdict.extract_json(str(doc.get("result") or ""),
                                                                           keys=TRIAGE_KEYS)
        if error is None:
            obj = reshape(obj, summary) if isinstance(obj, dict) else None
            if not isinstance(obj, dict):
                error = "no JSON object in the result"
        if out_path:
            Path(out_path).unlink(missing_ok=True)
    dur = time.monotonic() - t0

    def info(err_):
        return _codex_agent_info(out, alias, dur, err_) if codex else _agent_info(doc, alias, dur, err_)
    if error is None:
        tri = {"schema": 1, "role": "triage", "run_id": ctx["run_id"], "attempt": attempt, "source": "triage",
               **{k: obj.get(k) for k in ("change_size", "risk", "qa", "review", "specialists", "confidence")},
               "agent": info(None)}
        norm, _ = verdict.normalize("triage", tri, run_id=ctx["run_id"], attempt=attempt)
        errs = verdict.validate("triage", norm)
        if not errs:
            return write(run_dir, ctx, attempt, norm)
        error = f"bad output: {errs[0]}"
    fb = plan.fallback_triage(summary, cfg, project, f"triage unavailable ({error}): ")
    fb.update({"run_id": ctx["run_id"], "attempt": attempt, "source": "fallback", "agent": info(error)})
    return write(run_dir, ctx, attempt, fb)
