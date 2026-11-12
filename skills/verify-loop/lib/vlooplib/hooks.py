"""`vloop hook <event>`: session tracking, private baselines, the Stop gate (§2.11). Never breaks a session."""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

from . import config, paths, platforms, state, util
from .platforms.base import HookEvent, Platform

EVENTS = ("session-start", "prompt", "pre-tool", "post-tool", "stop", "subagent-start", "subagent-stop")
GUARDED = {"verify-reviewer": "reviewer", "verify-qa": "qa"}
GUARD_ROLES = ("reviewer", "qa")
GUARD_ROLE_ENV = "VLOOP_GUARD_ROLE"
UNTRACKED_PREFIXES = ("verify-", "vault-")


def _vl() -> str:
    return util.vloop_cli()


# --------------------------------------------------------------------------- helpers


def _emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


_cfg_cache: dict | None = None


def _cfg() -> dict:
    global _cfg_cache
    if _cfg_cache is None:
        _cfg_cache = config.load()
    return _cfg_cache


def hook_context(payload: dict):
    from . import transports

    return transports.hook_context(payload, _cfg())


class Call:
    def __init__(self, event: str, payload: dict, plat: Platform, ev: HookEvent):
        self.event, self.payload, self.plat, self.ev = event, payload, plat, ev
        self._ctx = None

    @property
    def ctx(self):
        if self._ctx is None:
            self._ctx = hook_context(self.payload)
        return self._ctx

    @property
    def sid(self) -> str:
        return self.ctx.target_sid

    @property
    def child(self) -> bool:
        return self.ctx.is_child


def agent_suffix(agent_type: str | None) -> str:
    return (agent_type or "").rsplit(":", 1)[-1]


def agent_kind(agent_type: str | None) -> tuple[bool, str | None]:
    """(untracked verifier/vault agent, guard role)."""
    name = agent_suffix(agent_type)
    if not name.startswith(UNTRACKED_PREFIXES):
        return False, None
    return True, GUARDED.get(name)


def _excluded_roots(payload: dict) -> list[str]:
    from .integrations import vault

    roots = [vault.vault_root_hint(), paths.claude_home(), paths.codex_home(), util.state_dir(), paths.data_dir()]
    if os.environ.get("VERIFY_LOOP_ALLOW_TMP") != "1":
        roots += ["/tmp", "/private/tmp", "/var/folders"]
        if os.environ.get("TMPDIR"):
            roots.append(os.environ["TMPDIR"])
        if payload.get("scratchpad_dir"):
            roots.append(payload["scratchpad_dir"])
    return [os.path.realpath(str(r)).rstrip("/") for r in roots if r]


def _is_excluded(real: str, roots: list[str]) -> bool:
    return any(real == r or real.startswith(r + "/") for r in roots)


def _repo_ok(repo: dict) -> bool:
    meta = util.read_json(Path(repo["root"]) / "_meta.json")
    if isinstance(meta, dict) and "schema" in meta:
        return False
    return config.repo_enabled(repo["root"])


def _eligible_repo(path: str, payload: dict, roots: list[str] | None = None) -> dict | None:
    from . import gitsnap

    real = os.path.realpath(path)
    if _is_excluded(real, roots if roots is not None else _excluded_roots(payload)):
        return None
    repo = gitsnap.repo_for(real)
    if not repo or not _repo_ok(repo) or _is_excluded(repo["root"], roots or _excluded_roots(payload)):
        return None
    return repo


def _eligible_path(path: str, payload: dict, roots: list[str]) -> tuple[dict, str] | None:
    """(repo, rel) for a tracked-eligible file path."""
    from . import gitsnap

    if not path:
        return None
    if not os.path.isabs(path):
        path = os.path.join(payload.get("cwd") or os.getcwd(), path)
    path = os.path.normpath(path)
    if os.path.islink(path):
        return None
    real = os.path.realpath(path)
    if _is_excluded(real, roots):
        return None
    repo = gitsnap.repo_for(real)
    if not repo or not _repo_ok(repo) or _is_excluded(repo["root"], roots):
        return None
    rel = os.path.relpath(real, repo["root"])
    if rel.startswith("..") or rel == "." or rel == ".git" or rel.startswith(".git/"):
        return None
    if gitsnap.is_ignored(repo, rel):
        return None
    return repo, rel


def _load_or_new(sid: str, call: Call | None = None, minimal: bool = False) -> dict:
    st = state.load_session(sid)
    if st is None:
        if minimal or call is None:
            st = state.new_session(sid)
        else:
            ctx = call.ctx
            st = state.new_session(sid, transcript_path=call.ev.transcript, cwd=call.ev.cwd,
                                   entrypoint=os.environ.get("CLAUDE_CODE_ENTRYPOINT"),
                                   depth=ctx.depth if ctx.depth is not None else int(ctx.suppress_gate),
                                   parent_session_id=ctx.parent_session_id or (ctx.target_sid if ctx.is_child
                                                                               else None),
                                   platform=call.plat.name)
    if call is not None and not minimal and not st.get("platform"):
        st["platform"] = call.plat.name
    return st


def _baseline_with_snapshot(sid: str, repo: dict, trigger: str, call: Call, timeout: float,
                            lock_wait: float, need, after=None) -> None:
    """Take a private snapshot (under the snapshot lock) and store it when `need(st, rs)` still holds."""
    from . import gitsnap

    try:
        with gitsnap.snapshot_lock(repo, timeout=lock_wait):
            st = state.load_session(sid)
            rs = st["repos"].get(repo["key"]) if st else None
            if st is not None and not need(st, rs):
                return
            snap = gitsnap.snapshot(repo, sid, timeout=timeout)
            with state.session_lock(sid):
                st = _load_or_new(sid, call, minimal=sid != call.ev.sid)
                rs = st["repos"].get(repo["key"])
                if not need(st, rs):
                    return
                rs = state.repo_state(st, repo)
                apply_baseline(st, rs, snap, trigger)
                if after:
                    after(st, rs)
                state.save_session(st)
            state.append_event(sid, "baseline", repo=repo["key"], tree=snap["tree"], kind=snap["kind"],
                               trigger=trigger, ms=snap["ms"])
    except TimeoutError as e:
        util.log_error(f"hooks.{trigger}", e)


def apply_baseline(st: dict, rs: dict, snap: dict, trigger: str) -> None:
    rs["baseline"] = {"tree": snap["tree"], "kind": snap["kind"], "at": util.now_iso(), "trigger": trigger}


# --------------------------------------------------------------------------- session-start


def on_session_start(call: Call) -> None:
    ev, payload = call.ev, call.payload
    sid = ev.sid
    if sid and state.session_path(sid).exists():
        with state.session_lock(sid):
            st = state.load_session(sid)
            if st is not None:
                if ev.transcript:
                    st["transcript_path"] = ev.transcript
                if ev.cwd:
                    st["cwd"] = ev.cwd
                st["platform"] = call.plat.name
                state.save_session(st)
    if sid:
        state.append_event(sid, "session_start", source=payload.get("source"), platform=call.plat.name)
    sd = util.state_dir()
    today = util.today()
    mode, _ = config.gate_mode(os.environ.get("CLAUDE_CODE_ENTRYPOINT"))
    uc = config.user_config()
    after = uc.get("enforce_review_after")
    if mode == "warn" and after and today >= str(after):
        stamp = sd / "reminder.stamp"
        if _read_stamp(stamp) != today:
            util.atomic_write_text(stamp, today + "\n")
            installed = uc.get("installed_at") or "?"
            _emit({"systemMessage": f"verify-loop: gate in warn mode since {installed} ({_days_since(installed)} "
                                    f"days). Review `vloop gate-report`, then switch with `vloop mode enforce`."})
    gc_stamp = sd / "gc.stamp"
    if _read_stamp(gc_stamp) != today:
        util.atomic_write_text(gc_stamp, today + "\n")
        _background_gc()


def _read_stamp(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _days_since(day: str) -> int | str:
    from datetime import date

    try:
        return (date.today() - date.fromisoformat(str(day))).days
    except ValueError:
        return "?"


def _background_gc() -> None:
    sys.stdout.flush()
    sys.stderr.flush()
    try:
        pid = os.fork()
    except OSError as e:
        util.log_error("hooks.gc", e)
        return
    if pid != 0:
        return
    try:
        os.setsid()
        devnull = os.open(os.devnull, os.O_RDWR)
        for fd in (0, 1, 2):
            os.dup2(devnull, fd)
        from . import admin

        admin.gc(days=None, dry_run=False, out=lambda s: None)
    except BaseException as e:
        util.log_error("hooks.gc", e)
    finally:
        os._exit(0)


# --------------------------------------------------------------------------- prompt


def _rebaseline_needed(st: dict | None, rs: dict | None) -> bool:
    if st is None or rs is None or not rs.get("touched"):
        return True
    if rs.get("closed_fp"):
        from . import gitsnap

        key = next(k for k, v in st["repos"].items() if v is rs)
        return gitsnap.fingerprint_worktree(state.repo_from_state(st, key), rs["touched"]) == rs["closed_fp"]
    return False


def on_prompt(call: Call) -> None:
    ev, plat = call.ev, call.plat
    sid = ev.sid
    if not sid:
        return
    text = ev.prompt or ""
    import re

    if re.match(plat.injected_prompt_re, text):
        # not a user turn: no task prompt, no new task, no rebaseline
        state.append_event(sid, "prompt", chars=len(text), injected=True)
        return
    with state.session_lock(sid):
        st = _load_or_new(sid, call)
        if ev.transcript:
            st["transcript_path"] = ev.transcript
        if ev.cwd:
            st["cwd"] = ev.cwd
        task = st.setdefault("task", {"n": 1, "started_at": None, "prompts": []})
        if not re.match(plat.skill_prompt_re, text, re.S):
            now = util.now_iso()
            if not task.get("started_at"):
                task["started_at"] = now
            task["prompts"] = (task.get("prompts") or []) + [{"ts": now, "text": text[:state.PROMPT_MAX]}]
            task["prompts"] = task["prompts"][-state.PROMPTS_KEEP:]
        state.save_session(st)
        n_task = task.get("n")
    state.append_event(sid, "prompt", chars=len(text), task=n_task)

    cwd = ev.cwd
    if not cwd:
        return
    repo = _eligible_repo(cwd, call.payload)
    if not repo:
        return

    _baseline_with_snapshot(sid, repo, "prompt", call, timeout=20.0, lock_wait=20.0,
                            need=_rebaseline_needed, after=_reset_after_prompt)


def _reset_after_prompt(st: dict, rs: dict) -> None:
    rs["touched"] = []
    rs["closed_fp"] = None
    rs["gate"] = {"blocks": {}, "defer": None, "last_warn_fp": None}
    loop = rs.setdefault("loop", {})
    if loop.get("status") in state.CLOSED:
        loop["status"] = "idle"


# --------------------------------------------------------------------------- pre-tool


def on_pre_tool(call: Call) -> None:
    ev = call.ev
    if ev.bash_cmd is not None or not ev.edit_paths:
        return
    sid = call.sid
    if not sid:
        return
    roots = _excluded_roots(call.payload)
    repos: dict[str, dict] = {}
    for path in ev.edit_paths:
        hit = _eligible_path(path, call.payload, roots)
        if hit:
            repos.setdefault(hit[0]["key"], hit[0])
    if not repos:
        return
    known = (state.load_session(sid) or {}).get("repos", {})
    for key, repo in repos.items():
        rs = known.get(key)
        if rs and rs.get("baseline"):
            continue
        _baseline_with_snapshot(sid, repo, "pre-edit", call, timeout=10.0, lock_wait=8.0,
                                need=lambda st, rs: not (rs and rs.get("baseline")))


# --------------------------------------------------------------------------- post-tool


def on_post_tool(call: Call) -> None:
    ev, payload = call.ev, call.payload
    if not ev.edit_paths:
        return
    source = "bash" if ev.bash_cmd is not None else "edit"
    sid = call.sid
    if not sid:
        return
    roots = _excluded_roots(payload)
    hits: dict[str, tuple[dict, list[str]]] = {}
    for p in ev.edit_paths:
        hit = _eligible_path(p, payload, roots)
        if hit:
            repo, rel = hit
            hits.setdefault(repo["key"], (repo, []))[1].append(rel)
    if not hits:
        return
    _record_touches(call, sid, hits, source)


def _record_touches(call: Call, sid: str, hits: dict[str, tuple[dict, list[str]]], source: str) -> None:
    from . import gitsnap

    child = call.child
    known = (state.load_session(sid) or {}).get("repos") or {}
    fallbacks = {key: gitsnap.head_tree(repo) for key, (repo, rels) in hits.items()
                 if not (known.get(key) or {}).get("baseline")}
    with state.session_lock(sid):
        st = _load_or_new(sid, call, minimal=child)
        defer_check = False
        for key, (repo, rels) in hits.items():
            rs = state.repo_state(st, repo)
            if not rs.get("baseline"):
                tree = fallbacks[key] if key in fallbacks else gitsnap.head_tree(repo)
                snap = {"tree": tree, "kind": "head-fallback", "ms": 0} if tree else \
                    {"tree": gitsnap.EMPTY_TREE, "kind": "empty", "ms": 0}
                apply_baseline(st, rs, snap, "post-bash")
                state.append_event(sid, "baseline", repo=key, tree=snap["tree"], kind=snap["kind"],
                                   trigger="post-bash", ms=0)
            before = list(rs.get("touched") or [])
            rs["touched"] = sorted(set(before) | set(rels))
            srcs = rs.setdefault("touch_sources", {"edit": 0, "bash": 0, "child": 0, "scan": 0})
            if child:
                srcs["child"] = int(srcs.get("child") or 0) + 1
            else:
                srcs[source] = int(srcs.get(source) or 0) + 1
            if (rs.get("gate") or {}).get("defer"):
                defer_check = True
        if defer_check:
            pend = state.pending_keys(st)
            fp = state.combined_fp(st, pend) if pend else None
            for rs in st["repos"].values():
                d = (rs.get("gate") or {}).get("defer")
                if d and d.get("fp") != fp:
                    rs["gate"]["defer"] = None
        state.save_session(st)
    for key, (repo, rels) in hits.items():
        state.append_event(sid, "touch", repo=key, paths=sorted(set(rels))[:20], source="child" if child else source,
                           child_session=call.ev.sid if child else None)


# --------------------------------------------------------------------------- stop gate


def _files_piece(rs: dict) -> str:
    t = rs.get("touched") or []
    return f"{rs.get('slug')}: {len(t)} files ({util.fmt_paths(t)})"


def gate_reason(st: dict, keys: list[str]) -> str:
    sentences, need_instr = [], False
    for k in keys:
        rs = st["repos"][k]
        loop = rs.get("loop") or {}
        run_id = loop.get("run_id")
        if loop.get("status") in state.OPEN and run_id:
            if loop.get("last_result") in ("fail", "inconclusive"):
                sentences.append(
                    f"run {run_id} is still open — attempt {loop.get('attempt')} was "
                    f"{str(loop['last_result']).upper()}. Fix the blocking findings and re-run ({_vl()} begin --rerun "
                    f"--run {run_id} --fix-notes - …), or ask the user. Findings: {_vl()} findings --run {run_id}.")
            else:
                sentences.append(
                    f"run {run_id} was started but not finished. Continue the verify-loop skill from the "
                    f"brief/spawn step ({_vl()} status shows where it stopped).")
        else:
            need_instr = True
            sentences.append(f"this session changed code that hasn't been verified — {_files_piece(rs)}.")
    text = "verify-loop gate: " + " ".join(sentences)
    if need_instr:
        text += (f" If the task is finished, run the verify-loop skill now (/verify-loop). If you stopped to ask the "
                 f"user something, end your message with the question, or run {_vl()} defer --reason \"<why>\". If the "
                 f"user said not to verify, run {_vl()} skip --reason \"<the user's words>\" and tell the user the change "
                 f"is unverified.")
    return text


def _slug_files(st: dict, keys: list[str]) -> str:
    return ", ".join(f"{st['repos'][k].get('slug')} ({len(st['repos'][k].get('touched') or [])} files)" for k in keys)


def warn_msg(st: dict, keys: list[str]) -> str:
    return (f"verify-loop (warn mode): unverified changes in {_slug_files(st, keys)} — in enforce mode this stop "
            f"would be blocked until /verify-loop runs.")


def ignored_msg(st: dict, keys: list[str], max_blocks: int) -> str:
    return (f"⚠ verify-loop: unverified changes in {_slug_files(st, keys)}; the gate stopped blocking after "
            f"{max_blocks} attempts. Run /verify-loop or vloop skip --reason \"…\".")


def retro_reason(run_id: str) -> str:
    return (f"verify-loop: verification passed but the retro step was not run. Run {_vl()} retro-plan --run {run_id} "
            f"and follow its next: line (verify-loop skill, step 8), then finish.")


def _gate_event(sid: str, event: str, st: dict, keys: list[str], fp: str | None, n: int) -> None:
    state.append_event(sid, event, repos=[st["repos"][k].get("slug") for k in keys],
                       files=sum(len(st["repos"][k].get("touched") or []) for k in keys), fp=fp, n=n)


def scan_on_stop(cfg: dict, plat: Platform) -> bool:
    v = (cfg.get("gate") or {}).get("scan_on_stop")
    if isinstance(v, dict):
        v = v.get(plat.name)
    return plat.name == "codex" if v is None else bool(v)


def _changed_since(repo: dict, base: str, tree: str) -> list[str]:
    from . import gitsnap

    rc, so, _ = util.run(["git", "-C", repo["root"], "diff-tree", "-r", "-z", "--no-renames", "--name-only", base,
                          tree], env=gitsnap.git_env(repo), timeout=30)
    return [p for p in so.split("\0") if p] if rc == 0 else []


def stop_scan(call: Call, sid: str) -> None:
    """Platforms whose hooks don't report shell edits (Codex): diff each baselined repo of the session against a
    fresh private snapshot of its worktree and record the changed paths as touched (source `scan`)."""
    from . import gitsnap

    st = state.load_session(sid)
    if not st:
        return
    roots = _excluded_roots(call.payload)
    hits: dict[str, tuple[dict, list[str]]] = {}
    for key in sorted(st.get("repos") or {}):
        rs = st["repos"][key]
        base = rs.get("baseline") or {}
        if base.get("kind") != "snapshot" or not base.get("tree"):
            continue
        repo = state.repo_from_state(st, key)
        if not os.path.isdir(repo["root"]) or _is_excluded(os.path.realpath(repo["root"]), roots) or \
                not _repo_ok(repo):
            continue
        try:
            with gitsnap.snapshot_lock(repo, timeout=5.0):
                snap = gitsnap.snapshot(repo, sid, timeout=15.0)
        except TimeoutError as e:
            util.log_error("hooks.stop_scan", e)
            continue
        if snap.get("kind") != "snapshot" or snap["tree"] == base["tree"]:
            continue
        known = set(rs.get("touched") or [])
        new = []
        for rel in _changed_since(repo, base["tree"], snap["tree"]):
            if rel in known or rel == ".git" or rel.startswith(".git/"):
                continue
            if _is_excluded(os.path.realpath(os.path.join(repo["root"], rel)), roots):
                continue
            new.append(rel)
        if new:
            hits[key] = (repo, new)
    if hits:
        _record_touches(call, sid, hits, "scan")


def on_stop(call: Call) -> None:
    payload = call.payload
    sid = call.ev.sid
    if not sid or not state.session_path(sid).exists() or call.ctx.suppress_gate:
        return
    mode, _ = config.gate_mode(os.environ.get("CLAUDE_CODE_ENTRYPOINT"))
    if mode == "off":
        return
    st = state.load_session(sid)
    if st is not None and scan_on_stop(config.load(st.get("cwd")), call.plat):
        stop_scan(call, sid)
    with state.session_lock(sid, timeout=8.0):
        st = state.load_session(sid)
        if st is None:
            return
        out = _gate(st, sid, mode, payload)
    if out is not None:
        _emit(out)


def _gate(st: dict, sid: str, mode: str, payload: dict) -> dict | None:
    cfg = config.load(st.get("cwd"))
    gate_cfg = cfg.get("gate") or {}
    pend = state.pending_keys(st)
    if not pend:
        if mode == "enforce":
            for k in sorted(st.get("repos") or {}):
                loop = st["repos"][k].get("loop") or {}
                run_id = loop.get("run_id")
                if loop.get("retro") == "pending" and run_id and \
                        not (util.state_dir() / "runs" / run_id / "retro" / "plan.json").exists():
                    loop["retro"] = "requested"
                    state.save_session(st)
                    state.append_event(sid, "retro_block", repo=k, run=run_id)
                    return {"decision": "block", "reason": retro_reason(run_id)}
        return None
    tasks = payload.get("background_tasks")
    if isinstance(tasks, list) and any(isinstance(t, dict) and t.get("status") == "running" for t in tasks):
        state.append_event(sid, "gate_wait", reason="background")
        return None
    ttl = float(gate_cfg.get("inflight_ttl_min", 60)) * 60
    now = time.time()
    for k in pend:
        t = util.parse_iso((st["repos"][k].get("loop") or {}).get("inflight_since"))
        if t is not None and now - t < ttl:
            state.append_event(sid, "gate_wait", repo=k, reason="inflight")
            return None
    if all((st["repos"][k].get("loop") or {}).get("status") == "escalated" for k in pend):
        state.append_event(sid, "gate_wait", reason="escalated")
        return {"systemMessage": "verify-loop: escalated — waiting for your decision (/verify-loop status)"}
    msgs, docs_paths = [], []
    for k in list(pend):
        rs = st["repos"][k]
        from . import classify

        rcfg = config.load(rs["root"])
        if classify.docs_only_paths(rs.get("touched") or [], rcfg):
            paths = list(rs["touched"])
            state.close_loop(st, k, "skipped", f"docs-only: {util.fmt_paths(paths)}", tree=None)
            state.append_event(sid, "gate_skip_docs", repo=k, files=paths[:20])
            docs_paths += paths
    if docs_paths:
        state.save_session(st)
        msgs.append(f"verify-loop · skipped: docs-only change ({util.fmt_paths(docs_paths)})")
        pend = state.pending_keys(st)
        if not pend:
            return {"systemMessage": msgs[0]}
    last = payload.get("last_assistant_message")
    if isinstance(last, str) and last.rstrip(" \n\t*_`)").endswith("?"):
        state.append_event(sid, "gate_defer", reason="question")
        return None
    fp = state.combined_fp(st, pend)
    deferred = [k for k in pend if ((st["repos"][k].get("gate") or {}).get("defer") or {}).get("fp") == fp]
    if deferred:
        for k in deferred:
            st["repos"][k]["gate"]["defer"] = None
        state.save_session(st)
        state.append_event(sid, "gate_defer", reason="defer-cmd")
        return None
    n = max(int(((st["repos"][k].get("gate") or {}).get("blocks") or {}).get(fp, 0)) for k in pend)
    max_blocks = int(gate_cfg.get("max_blocks_per_fp", 2))
    if n >= max_blocks:
        _gate_event(sid, "gate_ignored", st, pend, fp, n)
        return {"systemMessage": " ".join(msgs + [ignored_msg(st, pend, max_blocks)])}
    if mode == "warn":
        out = None
        if any((st["repos"][k].get("gate") or {}).get("last_warn_fp") != fp for k in pend):
            for k in pend:
                st["repos"][k].setdefault("gate", {})["last_warn_fp"] = fp
            state.save_session(st)
            out = {"systemMessage": " ".join(msgs + [warn_msg(st, pend)])}
        elif msgs:
            out = {"systemMessage": " ".join(msgs)}
        _gate_event(sid, "gate_warn", st, pend, fp, n)
        return out
    for k in pend:
        st["repos"][k].setdefault("gate", {}).setdefault("blocks", {})[fp] = n + 1
    state.save_session(st)
    _gate_event(sid, "gate_block", st, pend, fp, n + 1)
    out = {"decision": "block", "reason": gate_reason(st, pend)}
    if msgs:
        out["systemMessage"] = " ".join(msgs)
    return out


# --------------------------------------------------------------------------- entry


def on_subagent_start(call: Call) -> None:
    ev = call.ev
    untracked, role = agent_kind(ev.agent_type)
    if not ev.sid or not ev.agent_id or not untracked:
        return
    with state.session_lock(ev.sid):
        st = _load_or_new(ev.sid, call, minimal=call.child)
        state.record_agent_start(st, ev.agent_id, ev.agent_type, role)
        state.save_session(st)
    state.append_event(ev.sid, "subagent_start", agent_id=ev.agent_id, agent_type=ev.agent_type, role=role)


def on_subagent_stop(call: Call) -> None:
    ev = call.ev
    if not ev.sid or not ev.agent_id:
        return
    transcript = call.payload.get("agent_transcript_path")
    transcript = transcript if isinstance(transcript, str) and transcript else None
    untracked, _ = agent_kind(ev.agent_type)
    exists = state.session_path(ev.sid).exists()
    if exists:
        with state.session_lock(ev.sid):
            st = state.load_session(ev.sid)
            if st is not None and (untracked or ev.agent_id in (st.get("agents") or {})):
                state.record_agent_stop(st, ev.agent_id, ev.agent_type, transcript)
                state.save_session(st)
    if exists or untracked:
        state.append_event(ev.sid, "subagent_stop", agent_id=ev.agent_id, agent_type=ev.agent_type,
                           transcript=transcript)


HANDLERS = {"session-start": on_session_start, "prompt": on_prompt, "pre-tool": on_pre_tool,
            "post-tool": on_post_tool, "stop": on_stop, "subagent-start": on_subagent_start,
            "subagent-stop": on_subagent_stop}


def _verifier(ev: HookEvent, event: str) -> tuple[bool, str | None]:
    agent = ev.agent_type
    if not agent and ev.agent_id and ev.sid and event in ("pre-tool", "post-tool"):
        a = state.active_agent(state.load_session(ev.sid), ev.agent_id)
        agent = (a or {}).get("agent_type") or ""
    return agent_kind(agent)


def main(argv: list[str]) -> int:
    event = argv[0] if argv else ""
    try:
        raw = sys.stdin.read()
        if os.environ.get("VERIFY_LOOP_DEBUG_PAYLOADS") == "1":
            d = util.state_dir() / "debug"
            d.mkdir(parents=True, exist_ok=True, mode=0o700)
            util.atomic_write_text(d / f"{event}-{int(time.time() * 1000)}.json", raw, 0o600)
        if event not in HANDLERS:
            raise ValueError(f"unknown hook event: {event!r}")
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            raise ValueError("hook payload is not a JSON object")
        plat = platforms.detect(payload)
        ev = plat.normalize(event, payload)
        env_role = os.environ.get(GUARD_ROLE_ENV)
        if env_role in GUARD_ROLES:
            if event == "pre-tool":
                from . import guard

                return guard.run_guard(env_role, payload, platform=plat.name)
            return 0
        if event in ("subagent-start", "subagent-stop"):
            if os.environ.get("VERIFY_LOOP_MODE") != "off":
                HANDLERS[event](Call(event, payload, plat, ev))
            return 0
        untracked, role = _verifier(ev, event)
        if untracked:
            if event == "pre-tool" and role:
                from . import guard

                return guard.run_guard(role, payload, platform=plat.name)
            return 0
        if os.environ.get("VERIFY_LOOP_MODE") == "off":
            return 0
        if event == "pre-tool" and ev.bash_cmd is not None:
            return 0
        HANDLERS[event](Call(event, payload, plat, ev))
    except BaseException as e:
        util.log_error(f"hook.{event}", e)
    return 0


def register(sub) -> None:
    p = sub.add_parser("hook", help="agent hook entry (stdin = hook JSON)")
    p.add_argument("event", choices=EVENTS)
    p.set_defaults(fn=lambda a: main([a.event]))
    from . import guard

    g = sub.add_parser("guard", help="reviewer/QA guard on a PreToolUse JSON (stdin)")
    g.add_argument("role", choices=("reviewer", "qa"))
    g.set_defaults(fn=guard.cmd_guard)
