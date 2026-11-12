"""Admin subcommands: defer skip reset status pending mode repo-config gc (§2.10.2, WP1 rows)."""
from __future__ import annotations

import json
import os
import shutil
import sys
import time
from datetime import date
from pathlib import Path

from . import config, paths, state, util


def _err(msg: str, code: int = 2) -> int:
    sys.stderr.write(f"vloop: error: {msg}\n")
    return code


SESSION_HELP = ("session id (default: the platform's session env — $CLAUDE_CODE_SESSION_ID on Claude Code, "
                "$CODEX_THREAD_ID on Codex; on Codex without it: the newest session whose cwd or tracked repo "
                "contains the current directory)")


def resolve_session(explicit: str | None = None, cwd: str | None = None) -> str | None:
    """--session → the platform's session env → (Codex only) newest session state matching the cwd."""
    from . import platforms

    if explicit:
        return explicit
    plat = platforms.detect()
    sid = plat.session_id_from_env()
    if sid:
        return sid
    if plat.name == "codex":
        return state.find_session_by_cwd(cwd or os.getcwd(), platform="codex")
    return None


def _sid(args) -> str | None:
    return resolve_session(getattr(args, "session", None))


def _repo_key(path: str | None) -> dict | None:
    from . import gitsnap

    return gitsnap.repo_for(path or os.getcwd())


def _loop_tree(rs: dict) -> str | None:
    """Tree of the loop's last attempt (the loop 'had a begin'), else None."""
    run_id = (rs.get("loop") or {}).get("run_id")
    ctx = state.load_context(run_id) if run_id else None
    if not ctx:
        return None
    attempts = ctx.get("attempts") or []
    return (attempts[-1] or {}).get("tree") if attempts else None


def _mark_context(rs: dict, status: str) -> None:
    run_id = (rs.get("loop") or {}).get("run_id")
    if not run_id or state.load_context(run_id) is None:
        return
    with state.run_lock(run_id):
        ctx = state.load_context(run_id)
        if ctx and ctx.get("status") in state.OPEN:
            ctx["status"] = status
            state.save_context(ctx)


def _session_or_err(args):
    sid = _sid(args)
    if not sid:
        return None, _err("no session (pass --session, or run inside Claude Code / a Codex session in this repo)")
    return sid, None


# --------------------------------------------------------------------------- defer / skip / reset


def cmd_defer(args) -> int:
    sid, rc = _session_or_err(args)
    if rc is not None:
        return rc
    with state.session_lock(sid):
        st = state.load_session(sid)
        pend = state.pending_keys(st) if st else []
        if not pend:
            print("verify-loop · nothing pending to defer")
            return 0
        fp = state.combined_fp(st, pend)
        targets = pend
        if args.repo:
            r = _repo_key(args.repo)
            targets = [k for k in pend if r and k == r["key"]] or pend
        for k in targets:
            st["repos"][k].setdefault("gate", {})["defer"] = {"fp": fp, "reason": args.reason, "at": util.now_iso()}
        state.save_session(st)
    print(f"verify-loop · deferred: {args.reason} (gate passes once)")
    return 0


def _close_cmd(args, status: str) -> int:
    sid, rc = _session_or_err(args)
    if rc is not None:
        return rc
    lines = []
    with state.session_lock(sid):
        st = state.load_session(sid)
        if not st:
            print("verify-loop · nothing pending")
            return 0
        if args.repo:
            r = _repo_key(args.repo)
            if not r or r["key"] not in st.get("repos", {}):
                return _err(f"repo not tracked in this session: {args.repo}")
            keys = [r["key"]]
        else:
            pend = state.pending_keys(st)
            opened = [k for k, rs in st.get("repos", {}).items() if (rs.get("loop") or {}).get("status") in state.OPEN]
            keys = sorted(set(pend) | (set(opened) if status == "reset" else set()))
            if not keys and status == "reset":
                r = _repo_key(None)
                keys = [r["key"]] if r and r["key"] in st.get("repos", {}) else []
        if not keys:
            print("verify-loop · nothing pending")
            return 0
        for k in keys:
            rs = st["repos"][k]
            n = len(rs.get("touched") or [])
            tree = _loop_tree(rs)
            reason = getattr(args, "reason", None) or "reset by user"
            state.close_loop(st, k, status, reason, tree=tree)
            _mark_context(rs, status)
            if status == "skipped":
                state.append_event(sid, "skip", repo=k, run=(rs.get("loop") or {}).get("run_id"),
                                   reason=args.reason, by="user")
                lines.append(f"verify-loop · skipped by user: {args.reason} · {rs.get('slug')}: {n} files UNVERIFIED")
            else:
                lines.append(f"verify-loop · reset · {rs.get('slug')}")
        state.save_session(st)
    print("\n".join(lines))
    return 0


def cmd_skip(args) -> int:
    return _close_cmd(args, "skipped")


def cmd_reset(args) -> int:
    return _close_cmd(args, "reset")


# --------------------------------------------------------------------------- status / pending


def _days(d: str | None):
    try:
        return (date.today() - date.fromisoformat(str(d))).days
    except (TypeError, ValueError):
        return None


def status_text(st: dict | None, sid: str, pend: list[str], only: str | None = None) -> str:
    mode, source = config.gate_mode(None)
    uc = config.user_config()
    inst = uc.get("installed_at")
    since = f"; since {inst}, {_days(inst)} days" if inst else ""
    lines = [f"verify-loop · gate: {mode} ({source}{since}) · session {sid[:8]}"]
    if not st or not st.get("repos"):
        lines.append("no tracked changes in this session")
        return "\n".join(lines)
    max_failed = int((config.load(st.get("cwd")).get("loop") or {}).get("max_failed_attempts", 3))
    for k in sorted(st["repos"]):
        if only and k != only:
            continue
        rs = st["repos"][k]
        loop = rs.get("loop") or {}
        line = (f"{rs.get('slug')}: {len(rs.get('touched') or [])} touched files · "
                f"{'pending' if k in pend else 'clean'} · loop {loop.get('status') or 'idle'}")
        if loop.get("run_id"):
            line += (f" · run {loop['run_id']} · attempt {loop.get('attempt') or 0} · "
                     f"failures {loop.get('failed_attempts') or 0}/{max_failed}")
        if loop.get("inflight_since"):
            line += f" · inflight since {util.local_hhmm(loop['inflight_since'])}"
        lines.append(line)
        if loop.get("last_status_line"):
            lines.append(f"  last: {loop['last_status_line']}")
    return "\n".join(lines)


def cmd_status(args) -> int:
    sid, rc = _session_or_err(args)
    if rc is not None:
        return rc
    st = state.load_session(sid)
    pend = state.pending_keys(st) if st else []
    only = None
    if args.repo:
        r = _repo_key(args.repo)
        only = r["key"] if r else "-"
    if args.json:
        out = dict(st or {"session_id": sid, "repos": {}})
        if only:
            out["repos"] = {k: v for k, v in (out.get("repos") or {}).items() if k == only}
        out["pending"] = [k for k in pend if not only or k == only]
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return 0
    print(status_text(st, sid, pend, only))
    return 0


def cmd_pending(args) -> int:
    sid, rc = _session_or_err(args)
    if rc is not None:
        return rc
    st = state.load_session(sid)
    pend = state.pending_keys(st) if st else []
    rows = [{"key": k, "root": st["repos"][k]["root"], "touched": len(st["repos"][k].get("touched") or []),
             "loop_status": (st["repos"][k].get("loop") or {}).get("status") or "idle"} for k in pend]
    if args.json:
        print(json.dumps(rows, indent=2, ensure_ascii=False))
    elif not rows:
        print("none")
    else:
        for r in rows:
            print(f"{r['root']}\t{r['touched']}\t{r['loop_status']}")
    return 0


# --------------------------------------------------------------------------- mode


def cmd_mode(args) -> int:
    if args.mode:
        old, new = config.set_user_mode(args.mode)
        print(f"gate: {old} → {new}")
        return 0
    mode, source = config.gate_mode(None)
    uc = config.user_config()
    parts = [f"gate: {mode} (source: {source})"]
    if uc.get("installed_at"):
        parts.append(f"installed {uc['installed_at']} ({_days(uc['installed_at'])} days)")
    if uc.get("enforce_review_after"):
        parts.append(f"review after {uc['enforce_review_after']}")
    print(" · ".join(parts))
    return 0


# --------------------------------------------------------------------------- repo-config


def _detected_commands(root: Path) -> dict:
    pkg = util.read_json(root / "package.json", None)
    scripts = pkg.get("scripts") if isinstance(pkg, dict) and isinstance(pkg.get("scripts"), dict) else {}
    cmds = {"typecheck": None, "lint": None, "test": None, "build": None}
    for name in ("typecheck", "type-check", "tsc"):
        if name in scripts:
            cmds["typecheck"] = f"npm run {name}"
            break
    if "lint" in scripts:
        cmds["lint"] = "npm run lint"
    if "test" in scripts:
        cmds["test"] = "npm test"
    if "build" in scripts:
        cmds["build"] = "npm run build"
    return cmds


def cmd_repo_config(args) -> int:
    r = _repo_key(args.repo)
    if not r:
        return _err(f"not a git work tree: {args.repo or os.getcwd()}")
    root = Path(r["root"])
    if args.action == "show":
        print(config.dumps(config.load(root)))
        return 0
    existing = config.repo_config_files(root)
    path = existing[0] if existing else root / paths.REPO_CONFIG
    if not existing:
        tpl = {"enabled": True, "commands": _detected_commands(root), "app": {"start": None, "url": None},
               "notes": ""}
        util.atomic_write_json(path, tpl)
    rel = str(path.relative_to(root))
    rc, so, _ = util.run(["git", "-C", str(root), "rev-parse", "--git-path", "info/exclude"], timeout=10)
    if rc == 0 and so.strip():
        ex = Path(so.strip())
        ex = ex if ex.is_absolute() else root / ex
        ex.parent.mkdir(parents=True, exist_ok=True)
        try:
            text = ex.read_text(encoding="utf-8")
        except OSError:
            text = ""
        if rel not in [ln.strip() for ln in text.splitlines()]:
            with open(ex, "a", encoding="utf-8") as f:
                if text and not text.endswith("\n"):
                    f.write("\n")
                f.write(rel + "\n")
    print(str(path))
    return 0

# --------------------------------------------------------------------------- gc


def _newest_mtime(p: Path) -> float:
    try:
        newest = p.lstat().st_mtime
    except OSError:
        return 0.0
    if p.is_dir() and not p.is_symlink():
        for dirpath, dirnames, filenames in os.walk(p):
            for name in dirnames + filenames:
                try:
                    newest = max(newest, os.lstat(os.path.join(dirpath, name)).st_mtime)
                except OSError:
                    pass
    return newest


def _remove(p: Path) -> None:
    if p.is_dir() and not p.is_symlink():
        shutil.rmtree(p, ignore_errors=True)
    else:
        try:
            p.unlink()
        except OSError:
            pass


def gc(days: float | None = None, dry_run: bool = False, out=print) -> int:
    if days is None:
        days = float(config.load().get("gc_days", 14))
    sd = util.state_dir()
    now = time.time()
    cutoff = now - days * 86400
    victims: list[Path] = []
    runs = sd / "runs"
    if runs.is_dir():
        victims += [p for p in sorted(runs.iterdir()) if _newest_mtime(p) < cutoff]
    sessions = sd / "sessions"
    if sessions.is_dir():
        groups: dict[str, list[Path]] = {}
        for p in sessions.iterdir():
            name = p.name
            for suf in (".events.jsonl", ".json", ".lock"):
                if name.endswith(suf):
                    groups.setdefault(name[: -len(suf)], []).append(p)
                    break
        for sid in sorted(groups):
            files = groups[sid]
            if max(_newest_mtime(p) for p in files) < cutoff:
                victims += sorted(files)
    snap = sd / "snap"
    if snap.is_dir():
        victims += [p for p in sorted(snap.iterdir()) if p.is_dir() and _newest_mtime(p) < cutoff]
    debug = sd / "debug"
    if debug.is_dir():
        victims += [p for p in sorted(debug.iterdir()) if _newest_mtime(p) < now - 2 * 86400]
    for p in victims:
        out(f"{'would remove' if dry_run else 'removed'} {p}")
        if not dry_run:
            _remove(p)
    out(f"gc: {'would remove' if dry_run else 'removed'} {len(victims)} item(s) older than {days:g} days")
    return 0


def cmd_gc(args) -> int:
    return gc(args.older_than, args.dry_run)


# --------------------------------------------------------------------------- register


def register(sub) -> None:
    def common(p, repo: bool = False):
        p.add_argument("--session", default=None, help=SESSION_HELP)
        if repo:
            p.add_argument("--repo", default=None, help="repo path (default: cwd)")
        return p

    p = common(sub.add_parser("defer", help="let the gate pass once (you stopped to ask the user)"), repo=True)
    p.add_argument("--reason", required=True)
    p.set_defaults(fn=cmd_defer)

    p = common(sub.add_parser(
        "skip", help="close pending loops as skipped (user said not to verify)",
        description="Close the pending loops of this session as skipped (the user said not to verify). The session "
                    "is --session, else the platform's session env ($CLAUDE_CODE_SESSION_ID, $CODEX_THREAD_ID); on "
                    "Codex without either, the newest Codex session whose cwd or tracked repo contains the current "
                    "directory."), repo=True)
    p.add_argument("--reason", required=True)
    p.set_defaults(fn=cmd_skip)

    p = common(sub.add_parser("reset", help="close loops as reset"), repo=True)
    p.set_defaults(fn=cmd_reset)

    p = common(sub.add_parser("status", help="gate and loop status of this session"), repo=True)
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_status)

    p = common(sub.add_parser("pending", help="repos with unverified changes"))
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_pending)

    p = common(sub.add_parser("mode", help="show or set the gate mode"))
    p.add_argument("mode", nargs="?", choices=config.MODES)
    p.set_defaults(fn=cmd_mode)

    p = common(sub.add_parser("repo-config", help=f"init or show the repo config ({paths.REPO_CONFIG})"), repo=True)
    p.add_argument("action", choices=("init", "show"))
    p.set_defaults(fn=cmd_repo_config)

    p = common(sub.add_parser("gc", help="delete old runs, sessions, snapshots and debug payloads"))
    p.add_argument("--older-than", type=float, default=None, metavar="DAYS")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=cmd_gc)
