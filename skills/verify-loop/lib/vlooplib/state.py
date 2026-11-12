"""Session and run state: session files, locks, pending/fingerprints, closure, events, run dirs (§2.5, §2.6)."""
from __future__ import annotations

import fcntl
import json
import os
import time
from contextlib import contextmanager
from pathlib import Path

from . import util

CLOSED = ("passed", "accepted", "abandoned", "skipped", "reset")
OPEN = ("open", "escalated")
PROMPTS_KEEP = 10
PROMPT_MAX = 4000


# --------------------------------------------------------------------------- locks


@contextmanager
def _flock(path: Path, timeout: float):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"lock timeout: {path}")
                time.sleep(0.02)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


# --------------------------------------------------------------------------- sessions


def _sessions_dir() -> Path:
    d = util.state_dir() / "sessions"
    if not d.is_dir():
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
    return d


def session_path(sid: str) -> Path:
    return _sessions_dir() / f"{sid}.json"


def load_session(sid: str) -> dict | None:
    if not sid:
        return None
    st = util.read_json(session_path(sid))
    return st if isinstance(st, dict) else None


def new_session(sid: str, **fields) -> dict:
    now = util.now_iso()
    st = {
        "schema": 1,
        "session_id": sid,
        "created_at": now,
        "updated_at": now,
        "transcript_path": None,
        "cwd": None,
        "entrypoint": None,
        "depth": 0,
        "parent_session_id": None,
        "platform": None,
        "task": {"n": 1, "started_at": None, "prompts": []},
        "repos": {},
    }
    st.update(fields)
    return st


def save_session(st: dict) -> None:
    st["updated_at"] = util.now_iso()
    util.atomic_write_json(session_path(st["session_id"]), st, 0o600)


@contextmanager
def session_lock(sid: str, timeout: float = 5.0):
    with _flock(_sessions_dir() / f"{sid}.lock", timeout):
        yield


def _new_sources() -> dict:
    return {"edit": 0, "bash": 0, "child": 0, "scan": 0}


def _new_gate() -> dict:
    return {"blocks": {}, "defer": None, "last_warn_fp": None}


def _new_loop() -> dict:
    return {
        "run_id": None,
        "status": "idle",
        "attempt": 0,
        "failed_attempts": 0,
        "inconclusive_streak": 0,
        "last_result": None,
        "inflight_since": None,
        "retro": "none",
        "closed_at": None,
        "closed_reason": None,
        "last_status_line": None,
    }


def repo_state(st: dict, repo: dict, create: bool = True) -> dict | None:
    repos = st.setdefault("repos", {})
    rs = repos.get(repo["key"])
    if rs is None:
        if not create:
            return None
        rs = repos[repo["key"]] = {
            "root": repo["root"],
            "slug": repo["slug"],
            "common_dir": repo.get("common_dir"),
            "baseline": None,
            "touched": [],
            "touch_sources": _new_sources(),
            "closed_fp": None,
            "gate": _new_gate(),
            "loop": _new_loop(),
        }
    else:
        if repo.get("common_dir") and not rs.get("common_dir"):
            rs["common_dir"] = repo["common_dir"]
        rs.setdefault("gate", _new_gate())
        rs.setdefault("loop", _new_loop())
        rs.setdefault("touched", [])
        rs.setdefault("touch_sources", _new_sources())
    return rs


def repo_from_state(st: dict, key: str) -> dict:
    rs = st["repos"][key]
    common = rs.get("common_dir")
    if not common:
        from . import gitsnap

        r = gitsnap.repo_for(rs["root"])
        common = r["common_dir"] if r else os.path.join(rs["root"], ".git")
    return {"root": rs["root"], "key": key, "slug": rs.get("slug") or util.slugify(os.path.basename(rs["root"])),
            "common_dir": common}


def _under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def find_session_by_cwd(cwd: str | Path, platform: str | None = None) -> str | None:
    """Newest session state whose cwd (or a tracked repo root) contains `cwd`: the session id fallback for platforms
    that expose no session id to shell commands."""
    real = os.path.realpath(str(cwd))
    d = util.state_dir() / "sessions"
    try:
        files = [e for e in os.scandir(d) if e.name.endswith(".json") and not e.name.endswith(".events.jsonl")]
    except OSError:
        return None
    files.sort(key=lambda e: e.stat().st_mtime, reverse=True)
    for e in files:
        st = util.read_json(Path(e.path))
        if not isinstance(st, dict) or not st.get("session_id"):
            continue
        if platform and st.get("platform") not in (platform, None):
            continue
        roots = [st.get("cwd")] + [rs.get("root") for rs in (st.get("repos") or {}).values() if isinstance(rs, dict)]
        if any(isinstance(r, str) and r and _under(real, os.path.realpath(r)) for r in roots):
            return str(st["session_id"])
    return None


# --------------------------------------------------------------------------- subagents

AGENTS_KEEP = 50


def record_agent_start(st: dict, agent_id: str, agent_type: str, role: str | None) -> None:
    agents = st.setdefault("agents", {})
    agents[agent_id] = {"agent_type": agent_type, "role": role, "started_at": util.now_iso(), "stopped_at": None,
                        "transcript_path": None}
    if len(agents) > AGENTS_KEEP:
        for k in sorted(agents, key=lambda k: agents[k].get("started_at") or "")[:len(agents) - AGENTS_KEEP]:
            agents.pop(k, None)


def record_agent_stop(st: dict, agent_id: str, agent_type: str, transcript_path: str | None) -> None:
    a = st.setdefault("agents", {}).setdefault(agent_id, {"agent_type": agent_type, "role": None, "started_at": None})
    a["stopped_at"] = util.now_iso()
    a["transcript_path"] = transcript_path


def active_agent(st: dict | None, agent_id: str | None) -> dict | None:
    if not st or not agent_id:
        return None
    a = (st.get("agents") or {}).get(agent_id)
    return a if isinstance(a, dict) and not a.get("stopped_at") else None


# --------------------------------------------------------------------------- pending and fingerprints (§2.5.2)


def _fp_wt(st: dict, key: str, paths: list[str]) -> str:
    from . import gitsnap

    return gitsnap.fingerprint_worktree(repo_from_state(st, key), paths)


def is_pending(st: dict, key: str) -> bool:
    from . import gitsnap

    rs = st["repos"][key]
    touched = rs.get("touched") or []
    if not touched or not os.path.isdir(rs["root"]):
        return False
    repo = repo_from_state(st, key)
    fpw = gitsnap.fingerprint_worktree(repo, touched)
    if rs.get("closed_fp") and fpw == rs["closed_fp"]:
        return False
    base = rs.get("baseline")
    if base and base.get("tree"):
        if fpw == gitsnap.fingerprint_tree(repo, base["tree"], touched):
            return False
    return True


def pending_keys(st: dict) -> list[str]:
    return [k for k in sorted(st.get("repos") or {}) if is_pending(st, k)]


def combined_fp(st: dict, keys: list[str]) -> str:
    import hashlib

    lines = [f"{k}:{_fp_wt(st, k, st['repos'][k].get('touched') or [])}" for k in sorted(keys)]
    return hashlib.sha1("\n".join(lines).encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- closure


def close_loop(st: dict, key: str, status: str, reason: str, tree: str | None, retro: str = "none") -> None:
    from . import gitsnap

    rs = st["repos"][key]
    repo = repo_from_state(st, key)
    touched = rs.get("touched") or []
    if tree:
        rs["baseline"] = {"tree": tree, "kind": "snapshot", "at": util.now_iso(), "trigger": "closure"}
        rs["touched"] = [p for p in touched
                         if gitsnap.fingerprint_worktree(repo, [p]) != gitsnap.fingerprint_tree(repo, tree, [p])]
        rs["closed_fp"] = None
    else:
        rs["closed_fp"] = gitsnap.fingerprint_worktree(repo, touched)
    gate = rs.setdefault("gate", _new_gate())
    gate["blocks"] = {}
    gate["defer"] = None
    loop = rs.setdefault("loop", _new_loop())
    loop["status"] = status
    loop["closed_at"] = util.now_iso()
    loop["closed_reason"] = reason
    loop["inflight_since"] = None
    loop["retro"] = retro
    task = st.setdefault("task", {"n": 1, "started_at": None, "prompts": []})
    task["n"] = int(task.get("n") or 1) + 1
    task["started_at"] = None
    task["prompts"] = []
    append_event(st["session_id"], "close", repo=key, run=loop.get("run_id"), attempt=loop.get("attempt") or None,
                 status=status, reason=reason, with_tree=bool(tree))


# --------------------------------------------------------------------------- events


def append_event(sid: str, event: str, repo: str | None = None, run: str | None = None,
                 attempt: int | None = None, **data) -> None:
    try:
        line = json.dumps({"ts": util.now_iso(), "sid": sid, "event": event, "repo": repo, "run": run,
                           "attempt": attempt, "data": data}, ensure_ascii=False)
        fd = os.open(str(_sessions_dir() / f"{sid}.events.jsonl"), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, (line + "\n").encode("utf-8"))
        finally:
            os.close(fd)
    except BaseException as e:
        util.log_error("state.append_event", e)


def read_events(sid: str) -> list[dict]:
    out = []
    try:
        with open(_sessions_dir() / f"{sid}.events.jsonl", encoding="utf-8") as f:
            for ln in f:
                try:
                    out.append(json.loads(ln))
                except ValueError:
                    pass
    except OSError:
        pass
    return out


# --------------------------------------------------------------------------- runs (§2.6)


def new_run_id(slug: str, kind: str = "loop") -> str:
    import secrets

    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime())
    if kind == "retro":
        return f"retro-{stamp}-{secrets.token_hex(2)}"
    return f"{stamp}-{util.slugify(slug)[:20]}-{secrets.token_hex(2)}"


def _runs_dir() -> Path:
    d = util.state_dir() / "runs"
    if not d.is_dir():
        d.mkdir(parents=True, exist_ok=True, mode=0o700)
    return d


def run_dir(run_id: str) -> Path:
    d = _runs_dir() / run_id
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    return d


@contextmanager
def run_lock(run_id: str, timeout: float = 10.0):
    with _flock(run_dir(run_id) / ".lock", timeout):
        yield


def load_context(run_id: str) -> dict | None:
    if not run_id:
        return None
    ctx = util.read_json(_runs_dir() / run_id / "context.json")
    return ctx if isinstance(ctx, dict) else None


def save_context(ctx: dict) -> None:
    util.atomic_write_json(run_dir(ctx["run_id"]) / "context.json", ctx)
