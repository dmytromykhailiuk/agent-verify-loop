"""Loop subcommands: begin brief spawn spawn-wait collect record judge consent dispute decide findings (§2.10.2, WP2).

Verifiers start through the run's transports (`transports.for_role`). Integrity check: the worktree is fingerprinted
right before the verifiers start and again at collect/judge; a change no verifier may make turns that role's verdict
INCONCLUSIVE ("verifier modified files: …")."""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

from . import config, state, util

SCOPES = ("none", "checks", "run", "browser")
TIERS = ("standard", "deep")
BRIEF_MAX = 6000
NARRATIVE = re.compile(r"(?i)^\s*[-*]?\s*(i|we)\s+(fixed|implemented|changed|added|updated|refactored|tested|verified)\b")
BRIEF_TEMPLATE = ("## Acceptance criteria\n- <testable behavior from the user's request>\n## How to exercise\n"
                  "- <routes, commands, test data, user role>\n## Out of scope\n- <…>\nEOF")
HEAD_NOTE = "note: baseline is HEAD (head-fallback) — the diff may include changes made before this task"
NO_CRITERIA = "- (none given) Check the change for correctness and consistency with the project."


def _vl() -> str:
    return util.vloop_cli()


class Fail(Exception):
    def __init__(self, msg: str, code: int = 2):
        super().__init__(msg)
        self.code = code


def _guard(fn):
    def wrapper(args):
        try:
            return fn(args)
        except Fail as e:
            sys.stderr.write(f"vloop: error: {e}\n")
            return e.code
    return wrapper


def _warn(msg: str) -> None:
    sys.stderr.write(f"vloop: warning: {msg}\n")


def _out(lines) -> None:
    print("\n".join(lines) if isinstance(lines, list) else lines)


def _sid(args) -> str | None:
    from . import admin

    return admin.resolve_session(getattr(args, "session", None))


def _read_input(path: str | None) -> str:
    if not path or path == "-":
        return sys.stdin.read()
    try:
        return Path(path).expanduser().read_text(encoding="utf-8")
    except OSError as e:
        raise Fail(f"cannot read {path}: {e.strerror}")


# --------------------------------------------------------------------------- run helpers


def _ctx(run_id: str) -> dict:
    ctx = state.load_context(run_id) if run_id else None
    if not ctx:
        raise Fail(f"unknown run {run_id}")
    return ctx


def _update_ctx(run_id: str, fn) -> dict:
    with state.run_lock(run_id):
        ctx = _ctx(run_id)
        fn(ctx)
        state.save_context(ctx)
    return ctx


def _rd(ctx: dict) -> Path:
    return state.run_dir(ctx["run_id"])


def _n(ctx: dict) -> int:
    return len(ctx.get("attempts") or [])


def _att(ctx: dict, n: int | None = None) -> Path:
    return _rd(ctx) / f"attempt-{n or _n(ctx)}"


def _plan(ctx: dict, n: int | None = None) -> dict:
    return util.read_json(_att(ctx, n) / "plan.json", {}) or {}


def _judge_of(ctx: dict, n: int) -> dict | None:
    j = util.read_json(_att(ctx, n) / "judge.json")
    return j if isinstance(j, dict) else None


def _repo(ctx: dict) -> dict:
    from . import gitsnap

    root = ctx["repo"]["root"]
    r = gitsnap.repo_for(root)
    if not r:
        raise Fail(f"repo {root} is gone", 1)
    return r


def _cfg(ctx_or_root) -> dict:
    root = ctx_or_root["repo"]["root"] if isinstance(ctx_or_root, dict) else ctx_or_root
    return config.load(root)


def _event(ctx: dict, event: str, attempt: int | None = None, **data) -> None:
    if ctx.get("session_id"):
        state.append_event(ctx["session_id"], event, repo=(ctx.get("repo") or {}).get("key"), run=ctx["run_id"],
                           attempt=attempt, **data)


def _max_failed(cfg: dict) -> int:
    return int((cfg.get("loop") or {}).get("max_failed_attempts", 3))


def _project(ctx: dict, cfg: dict) -> dict:
    from . import classify

    return classify.detect_project(ctx["repo"]["root"], cfg)


def _auth_for(ctx: dict, plan_: dict, cfg: dict) -> dict | None:
    from . import classify

    qa = (plan_.get("roles") or {}).get("qa") or {}
    if qa.get("run") and qa.get("scope") == "browser":
        return classify.auth_status(ctx["repo"]["root"], cfg)
    return None


def _set_inflight(ctx: dict, value: str | None) -> None:
    sid, key = ctx.get("session_id"), ctx["repo"]["key"]
    if ctx.get("kind") != "loop" or not sid:
        return
    with state.session_lock(sid):
        st = state.load_session(sid)
        rs = (st or {}).get("repos", {}).get(key)
        if not rs or (rs.get("loop") or {}).get("run_id") != ctx["run_id"]:
            return
        rs["loop"]["inflight_since"] = value
        state.save_session(st)


def _diff_files(repo: dict, a: str, b: str, att: Path, cfg: dict, prefix: str, paths=None) -> dict:
    from . import classify, gitsnap

    rows = gitsnap.diff_numstat(repo, a, b, paths or None)
    summary = classify.summarize(rows, cfg)
    util.atomic_write_text(att / f"{prefix}.patch", gitsnap.diff_patch(repo, a, b, paths or None))
    util.atomic_write_json(att / f"{prefix}.numstat.json", summary)
    return summary


def _snapshot(repo: dict, sid: str | None) -> dict:
    from . import gitsnap

    try:
        with gitsnap.snapshot_lock(repo):
            return gitsnap.snapshot(repo, sid or "vloop")
    except TimeoutError:
        return gitsnap.snapshot(repo, sid or "vloop")


# --------------------------------------------------------------------------- render + print


def _prepare(ctx: dict, plan_: dict, cfg: dict, project: dict, auth: dict | None) -> None:
    """Render task files for the roles that run (QA only when no consent is pending); inflight; agent records."""
    from . import plan as planmod, prompts, qatools, transports

    rd, n = _rd(ctx), plan_["attempt"]
    att = rd / f"attempt-{n}"
    pending = planmod.consent_pending(ctx)
    running = []
    for role in ("reviewer", "qa"):
        pr = (plan_.get("roles") or {}).get(role)
        if not pr or not pr.get("run") or (role == "qa" and pending):
            continue
        if role == "qa":
            (att / "qa-artifacts").mkdir(parents=True, exist_ok=True)
            (att / "qa-logs").mkdir(parents=True, exist_ok=True)
            if pr.get("scope") == "browser":
                qatools.write_pointer(ctx, n, att, cfg, project, auth)
            else:
                for stale in ("mcp.json", "pw-pointer.json"):
                    (att / stale).unlink(missing_ok=True)
        prompts.render_role(role, run_dir=rd, ctx=ctx, attempt=n, plan=plan_, cfg=cfg, project=project, auth=auth)
        running.append(role)
    if pending:
        return
    inner = [r for r in running if transports.for_role(ctx, cfg, r, plan_["roles"][r]).in_session]
    if inner:
        now = util.now_iso()
        _integrity_begin(ctx, n, inner)

        def rec(c):
            a = c["attempts"][n - 1].setdefault("agents", {})
            for role in inner:
                pr = plan_["roles"][role]
                a[role] = {"transport": transports.for_role(ctx, cfg, role, pr).name, "session_id": None,
                           "started_at": now, "ended_at": None, "status": "planned", "model": pr.get("model"),
                           "effort": "medium"}
        _update_ctx(ctx["run_id"], rec)
    _set_inflight(ctx, util.now_iso())


def _plan_output(ctx: dict, plan_: dict, cfg: dict, project: dict, auth: dict | None) -> list[str]:
    from . import plan as planmod

    rd = _rd(ctx)
    lines = planmod.block(rd, ctx, plan_, cfg, auth)
    if planmod.consent_pending(ctx):
        _event(ctx, "consent_pending", plan_["attempt"], reason=plan_["roles"]["qa"].get("reason"),
               app_url=ctx.get("app_url") or project.get("app_url"))
        lines += planmod.consent_block(rd, ctx, plan_, project)
    return lines


def _store_consent(ctx: dict, plan_: dict) -> None:
    bc = ctx.get("browser_consent")

    def upd(c):
        c["browser_consent"] = bc
        c["attempts"][plan_["attempt"] - 1]["plan"] = f"attempt-{plan_['attempt']}/plan.json"
    fresh = _update_ctx(ctx["run_id"], upd)
    ctx.update(fresh)


# --------------------------------------------------------------------------- begin (loop)


def _resolve_repo(args, st: dict) -> tuple[dict | None, list[str], list[str]]:
    """(repo, other pending keys, notes)."""
    from . import gitsnap

    if args.repo:
        repo = gitsnap.repo_for(args.repo)
        if not repo:
            raise Fail(f"not a git repository: {args.repo}")
        if not config.repo_enabled(repo["root"]):
            raise Fail(f"verification is disabled for {repo['root']} "
                       f"({config.repo_config_label(repo['root'])} enabled: false)")
        others = [k for k in state.pending_keys(st) if k != repo["key"]]
        return repo, others, []
    cwd_repo = gitsnap.repo_for(os.getcwd())
    pend = state.pending_keys(st)
    if cwd_repo and cwd_repo["key"] in pend:
        pend.remove(cwd_repo["key"])
        pend.insert(0, cwd_repo["key"])
    if pend:
        return state.repo_from_state(st, pend[0]), pend[1:], []
    if cwd_repo:
        rs = (st.get("repos") or {}).get(cwd_repo["key"])
        if rs and rs.get("baseline") and config.repo_enabled(cwd_repo["root"]):
            return cwd_repo, [], ["note: no tracked edits; verifying the diff vs the task baseline"]
    return None, [], []


def _also_pending(st: dict, keys: list[str]) -> list[str]:
    out = []
    for k in keys:
        rs = st["repos"][k]
        out.append(f"also pending: {rs.get('slug')} ({len(rs.get('touched') or [])} files) — after this loop: "
                   f"{_vl()} begin --repo {rs['root']}")
    return out


@_guard
def cmd_begin(args) -> int:
    if args.standalone:
        return _begin_standalone(args)
    from . import classify, gitsnap, transports

    sid = _sid(args)
    if not sid:
        raise Fail("no session (pass --session or run inside Claude Code)")
    st = state.load_session(sid) or state.new_session(sid, cwd=os.getcwd())
    if args.rerun:
        run_id = args.run
        if not run_id:
            repo = gitsnap.repo_for(args.repo or os.getcwd())
            rs = (st.get("repos") or {}).get(repo["key"]) if repo else None
            run_id = ((rs or {}).get("loop") or {}).get("run_id")
            if not run_id:
                raise Fail("--rerun needs --run ID (no loop for this repo)")
        return _begin_rerun(args, run_id, [])
    repo, others, notes = _resolve_repo(args, st)
    if repo is None:
        print("verify-loop · NOTHING · no tracked changes in this session and no diff vs the task baseline")
        return 0
    rs = (st.get("repos") or {}).get(repo["key"]) or {}
    loop = rs.get("loop") or {}
    if loop.get("status") == "escalated":
        raise Fail(f"run {loop.get('run_id')} is escalated: ask the user, then vloop decide …")
    if loop.get("status") == "open" and loop.get("run_id"):
        return _begin_rerun(args, loop["run_id"], ["note: begin on an open loop → treated as --rerun"])
    cfg = config.load(repo["root"])
    snap = _snapshot(repo, sid)
    tree = snap["tree"]
    if snap["kind"] != "snapshot":
        notes.append(f"note: snapshot failed ({snap['kind']}); verifying HEAD")
    base = rs.get("baseline")
    if not base or not base.get("tree"):
        ht = gitsnap.head_tree(repo)
        base = {"tree": ht or gitsnap.EMPTY_TREE, "kind": "head-fallback" if ht else "empty", "at": util.now_iso(),
                "trigger": "pre-edit"}
        new_base = base
    elif not gitsnap.tree_exists(repo, base["tree"]):
        ht = gitsnap.head_tree(repo)
        notes.append(f"note: baseline tree {base['tree'][:8]} is missing (gc'd?) → using HEAD")
        base = {"tree": ht or gitsnap.EMPTY_TREE, "kind": "head-fallback" if ht else "empty", "at": util.now_iso(),
                "trigger": "pre-edit"}
        new_base = base
    else:
        new_base = None
    if base["kind"] == "head-fallback":
        notes.append(HEAD_NOTE)
    rows = gitsnap.diff_numstat(repo, base["tree"], tree)
    summary = classify.summarize(rows, cfg)
    if not rs.get("touched") and not args.repo and not summary["files"]:
        print("verify-loop · NOTHING · no tracked changes in this session and no diff vs the task baseline")
        return 0
    pf = classify.prefilter(summary, gitsnap.whitespace_only(repo, base["tree"], tree), cfg)
    if pf["decision"] == "skip":
        with state.session_lock(sid):
            st = state.load_session(sid) or state.new_session(sid, cwd=os.getcwd())
            rs2 = state.repo_state(st, repo)
            if new_base and not rs2.get("baseline"):
                rs2["baseline"] = new_base
            lp = rs2.get("loop") or {}
            keep = "pending" if lp.get("retro") == "pending" and lp.get("status") in state.CLOSED else "none"
            state.close_loop(st, repo["key"], "skipped", pf["reason"], tree, retro=keep)
            state.save_session(st)
        state.append_event(sid, "skip", repo=repo["key"], reason=pf["reason"], by="prefilter")
        _out([f"verify-loop · SKIP · {repo['slug']} · {pf['reason']} · loop closed, nothing to verify"]
             + _also_pending(st, others))
        return 0

    run_id = state.new_run_id(repo["slug"])
    rd = state.run_dir(run_id)
    now = util.now_iso()
    ctx = {
        "schema": 1, "run_id": run_id, "kind": "loop", "session_id": sid, "created_at": now,
        "repo": {"key": repo["key"], "root": repo["root"], "slug": repo["slug"],
                 "branch": gitsnap.current_branch(repo), "head": gitsnap.resolve_commit(repo, "HEAD")},
        "target": {"spec": "session", "base_tree": base["tree"], "base_kind": base["kind"], "code_ref": "worktree",
                   "paths": [], "base_at": base.get("at")},
        "roles": ["reviewer", "qa"], "app_url": args.app_url or None, "transport": transports.configured(cfg).name,
        "attempts": [{"n": 1, "begin_at": now, "tree": tree, "prefilter": pf["decision"],
                      "prefilter_reason": pf["reason"], "plan": None, "judge": None, "result": None,
                      "elapsed_s": None, "agents": {}}],
        "failed_attempts": 0, "inconclusive_streak": 0, "status": "open", "waived": [], "disputes": {},
        "user_notes": [], "flags": {"qa": args.qa, "review": args.review, "no_triage": bool(args.no_triage),
                                    "model": None},
        "browser_consent": None, "standalone": None,
    }
    from . import prompts

    att = rd / "attempt-1"
    att.mkdir(parents=True, exist_ok=True)
    util.atomic_write_text(rd / "prompts.md", prompts.prompt_blocks((st.get("task") or {}).get("prompts")) + "\n")
    util.atomic_write_text(att / "tree", tree + "\n")
    _diff_files(repo, base["tree"], tree, att, cfg, "diff")
    with state.run_lock(run_id):
        state.save_context(ctx)
    with state.session_lock(sid):
        st = state.load_session(sid) or state.new_session(sid, cwd=os.getcwd())
        rs2 = state.repo_state(st, repo)
        if new_base and not rs2.get("baseline"):
            rs2["baseline"] = new_base
        rs2["loop"] = {"run_id": run_id, "status": "open", "attempt": 1, "failed_attempts": 0,
                       "inconclusive_streak": 0, "last_result": None, "inflight_since": None, "retro": "none",
                       "closed_at": None, "closed_reason": None, "last_status_line": None}
        state.save_session(st)
    _event(ctx, "begin", 1, kind="loop", files=summary["files"], added=summary["added"], removed=summary["removed"],
           prefilter=pf["decision"], tree=tree, base_kind=base["kind"], target="session")
    pf_line = ("triage" if pf["decision"] == "triage"
               else f"{pf['reason']} — reviewer only, no QA, no triage")
    _out([f"verify-loop · run {run_id} · attempt 1 · {repo['slug']} · {summary['files']} files, "
          f"+{summary['added']}/−{summary['removed']} · baseline: {base['kind']} @ {util.local_hhmm(base.get('at'))}",
          f"prefilter: {pf_line}"] + notes + _also_pending(st, others)
         + [f"next: write the brief → {_vl()} brief --run {run_id} <<'EOF'", BRIEF_TEMPLATE])
    return 0


# --------------------------------------------------------------------------- begin --rerun


def _begin_rerun(args, run_id: str, notes: list[str]) -> int:
    from . import plan as planmod

    ctx = _ctx(run_id)
    if ctx.get("kind") != "loop":
        raise Fail(f"run {run_id} is a {ctx.get('kind')} run; only verification loops have re-runs")
    if ctx.get("status") == "escalated":
        raise Fail(f"run {run_id} is escalated: ask the user, then vloop decide …")
    if ctx.get("status") != "open":
        raise Fail(f"run {run_id} is {ctx.get('status')}")
    prev = _n(ctx)
    j = _judge_of(ctx, prev)
    if not j:
        raise Fail(f"attempt {prev} of run {run_id} has not been judged yet — continue with brief/spawn/judge "
                   f"({_vl()} status shows where it stopped)")
    if j.get("result") not in ("fail", "inconclusive"):
        raise Fail(f"attempt {prev} of run {run_id} was judged {j.get('result')}; nothing to re-run")
    flags = dict(ctx.get("flags") or {})
    for k in ("qa", "review"):
        if getattr(args, k, None):
            flags[k] = getattr(args, k)
    if getattr(args, "no_triage", False):
        flags["no_triage"] = True
    repo = _repo(ctx)
    cfg = _cfg(ctx)
    snap = _snapshot(repo, ctx.get("session_id"))
    tree = snap["tree"]
    n = prev + 1
    rd = _rd(ctx)
    att = rd / f"attempt-{n}"
    att.mkdir(parents=True, exist_ok=True)
    util.atomic_write_text(att / "tree", tree + "\n")
    _diff_files(repo, ctx["target"]["base_tree"], tree, att, cfg, "diff")
    prev_tree = ctx["attempts"][-1]["tree"]
    delta = _diff_files(repo, prev_tree, tree, att, cfg, "delta")
    from . import gitsnap

    util.atomic_write_json(att / "delta.hunks.json", gitsnap.diff_hunks(repo, prev_tree, tree))
    fix = args.fix_notes
    if fix:
        util.atomic_write_text(att / "fix-notes.md", _read_input(fix))
    else:
        _warn("no --fix-notes: the reviewer won't know what you changed")
    new_disputes = [k for k, v in (ctx.get("disputes") or {}).items()
                    if isinstance(v, dict) and int(v.get("attempt") or 0) == prev]
    if tree == prev_tree and not new_disputes:
        _warn(f"no changes since attempt {prev}")
    now = util.now_iso()

    def add(c):
        c["flags"] = flags
        c["attempts"].append({"n": n, "begin_at": now, "tree": tree, "prefilter": "rerun", "plan": None,
                              "judge": None, "result": None, "elapsed_s": None, "agents": {}})
    ctx = _update_ctx(run_id, add)
    sid = ctx.get("session_id")
    if sid:
        with state.session_lock(sid):
            st = state.load_session(sid)
            rs = (st or {}).get("repos", {}).get(ctx["repo"]["key"])
            if rs and (rs.get("loop") or {}).get("run_id") == run_id:
                rs["loop"]["attempt"] = n
                rs["loop"]["status"] = "open"
                state.save_session(st)
    _event(ctx, "begin", n, kind="rerun", files=delta["files"], added=delta["added"], removed=delta["removed"],
           prefilter="rerun", tree=tree, base_kind=ctx["target"].get("base_kind"), target=ctx["target"].get("spec"))
    project = _project(ctx, cfg)
    plan_, auth = planmod.build_rerun(rd, ctx, n, cfg, delta, project)
    _store_consent(ctx, plan_)
    _prepare(ctx, plan_, cfg, project, auth)
    head = (f"verify-loop · run {run_id} · attempt {n} · failures {ctx.get('failed_attempts', 0)}/{_max_failed(cfg)} · "
            f"delta: {delta['files']} files, +{delta['added']}/−{delta['removed']}")
    _out([head] + notes + _plan_output(ctx, plan_, cfg, project, auth))
    return 0


# --------------------------------------------------------------------------- begin --standalone


def _resolve_target(repo: dict, spec: str, sid: str | None) -> dict:
    from . import gitsnap

    if spec == "session":
        st = state.load_session(sid) if sid else None
        rs = ((st or {}).get("repos") or {}).get(repo["key"])
        if rs and rs.get("baseline") and repo["key"] in state.pending_keys(st) \
                and gitsnap.tree_exists(repo, rs["baseline"]["tree"]):
            b = rs["baseline"]
            return {"spec": "session", "base": b["tree"], "kind": b["kind"], "at": b.get("at"), "tree": None,
                    "code_ref": "worktree", "log": None}
        spec = "worktree"
    if spec == "worktree":
        ht = gitsnap.head_tree(repo)
        return {"spec": "worktree", "base": ht or gitsnap.EMPTY_TREE, "kind": "head" if ht else "empty",
                "at": util.now_iso(), "tree": None, "code_ref": "worktree", "log": None}
    if spec.startswith("pr:"):
        rc, out, err = util.run(["aws", "codecommit", "get-pull-request", "--pull-request-id", spec[3:], "--profile",
                                 "ps-mgmt-dev-773814284069", "--region", "eu-west-1"], timeout=60)
        if rc != 0:
            raise Fail(f"aws codecommit get-pull-request failed: {(err or out).strip()[:200]}", 1)
        try:
            tgt = json.loads(out)["pullRequest"]["pullRequestTargets"][0]
            dest, src = tgt["destinationCommit"], tgt["sourceCommit"]
        except (ValueError, KeyError, IndexError, TypeError):
            raise Fail("unexpected get-pull-request output", 1)
        if not (gitsnap.resolve_commit(repo, dest) and gitsnap.resolve_commit(repo, src)):
            raise Fail("commits not found locally; run git fetch")
        mb = gitsnap.merge_base(repo, dest, src) or dest
        return _commit_range(repo, spec, mb, src)
    if ".." in spec:
        a, b = spec.split("..", 1)
        ca, cb = gitsnap.resolve_commit(repo, a), gitsnap.resolve_commit(repo, b or "HEAD")
        if not ca or not cb:
            raise Fail("commits not found locally; run git fetch")
        return _commit_range(repo, spec, ca, cb)
    name = spec[7:] if spec.startswith("branch:") else spec
    if spec.startswith("branch:") or gitsnap.resolve_commit(repo, f"refs/heads/{name}"):
        cb = gitsnap.resolve_commit(repo, name)
        if not cb:
            raise Fail(f"branch {name} not found locally; run git fetch")
        db = gitsnap.default_branch(repo)
        dref = f"origin/{db}" if gitsnap.resolve_commit(repo, f"origin/{db}") else db
        mb = gitsnap.merge_base(repo, dref, cb)
        if not mb:
            raise Fail(f"no merge base between {dref} and {name}")
        return _commit_range(repo, spec, mb, cb)
    raise Fail(f"unknown target {spec!r} (session | worktree | A..B | branch:<name> | pr:<id>)")


def _commit_range(repo: dict, spec: str, a: str, b: str) -> dict:
    from . import gitsnap

    return {"spec": spec, "base": gitsnap.commit_tree(repo, a), "kind": "commit", "at": util.now_iso(),
            "tree": gitsnap.commit_tree(repo, b), "code_ref": b, "log": (a, b)}


def _begin_standalone(args) -> int:
    from . import classify, gitsnap, plan as planmod, prompts, transports

    roles = [r.strip() for r in (args.roles or "").split(",") if r.strip()]
    if not roles or any(r not in ("reviewer", "qa") for r in roles):
        raise Fail("--standalone needs --roles reviewer[,qa]")
    roles = [r for r in ("reviewer", "qa") if r in roles]
    sid = _sid(args)
    repo = gitsnap.repo_for(args.repo or os.getcwd())
    if not repo:
        raise Fail(f"not a git repository: {args.repo or os.getcwd()}")
    cfg = config.load(repo["root"])
    t = _resolve_target(repo, args.target or "session", sid)
    tree = t["tree"] or _snapshot(repo, sid)["tree"]
    paths = list(args.paths or [])
    summary = classify.summarize(gitsnap.diff_numstat(repo, t["base"], tree, paths or None), cfg)
    # no diff + explicit url/focus/criteria → exploratory QA of the running app; a review needs a diff (§2.10.2)
    exploratory = not summary["files"] and "qa" in roles and bool(args.url or args.focus or args.criteria)
    if exploratory:
        roles = ["qa"]
    prefix = {"reviewer": "verify-review", "qa": "verify-qa"}.get(roles[0]) if len(roles) == 1 else "verify"
    if not summary["files"] and not exploratory:
        print(f"{prefix} · NOTHING · target {t['spec']} has no changes")
        return 0
    run_id = state.new_run_id(repo["slug"])
    rd = state.run_dir(run_id)
    att = rd / "attempt-1"
    att.mkdir(parents=True, exist_ok=True)
    now = util.now_iso()
    ctx = {
        "schema": 1, "run_id": run_id, "kind": "standalone", "session_id": sid, "created_at": now,
        "repo": {"key": repo["key"], "root": repo["root"], "slug": repo["slug"],
                 "branch": gitsnap.current_branch(repo), "head": gitsnap.resolve_commit(repo, "HEAD")},
        "target": {"spec": t["spec"], "base_tree": t["base"], "base_kind": t["kind"], "code_ref": t["code_ref"],
                   "paths": paths, "base_at": t["at"]},
        "roles": roles, "app_url": args.url or args.app_url or None, "transport": transports.configured(cfg).name,
        "attempts": [{"n": 1, "begin_at": now, "tree": tree, "prefilter": "standalone", "plan": None,
                      "judge": None, "result": None, "elapsed_s": None, "agents": {}}],
        "failed_attempts": 0, "inconclusive_streak": 0, "status": "open", "waived": [], "disputes": {},
        "user_notes": [], "flags": {"qa": args.qa, "review": args.review, "no_triage": bool(args.no_triage),
                                    "model": args.model},
        "browser_consent": None,
        "standalone": {"roles": roles, "focus": args.focus, "criteria": args.criteria, "url": args.url,
                       "exploratory": exploratory},
    }
    if t["spec"] == "session":
        st = state.load_session(sid) or {}
        ptext = prompts.prompt_blocks((st.get("task") or {}).get("prompts"))
    else:
        ptext = "(standalone run: no user prompts)"
    util.atomic_write_text(rd / "prompts.md", ptext + "\n")
    if args.brief_file:
        brief = _read_input(args.brief_file)
    else:
        crit = [ln.strip() for ln in (args.criteria or "").splitlines() if ln.strip()]
        lines = ["## Acceptance criteria"] + ([ln if ln.startswith("- ") else f"- {ln}" for ln in crit]
                                             or [NO_CRITERIA])
        if t["log"]:
            rc, out, _ = util.run(["git", "-C", repo["root"], "log", "--format=%s", f"{t['log'][0]}..{t['log'][1]}"])
            subjects = [s for s in out.splitlines() if s.strip()][:20] if rc == 0 else []
            if subjects:
                lines += ["## Commit messages"] + [f"- {s}" for s in subjects]
        brief = "\n".join(lines) + "\n"
    util.atomic_write_text(rd / "brief.md", brief)
    util.atomic_write_text(att / "tree", tree + "\n")
    _diff_files(repo, t["base"], tree, att, cfg, "diff", paths)
    with state.run_lock(run_id):
        state.save_context(ctx)
    _event(ctx, "begin", 1, kind="standalone", files=summary["files"], added=summary["added"],
           removed=summary["removed"], prefilter="standalone", tree=tree, base_kind=t["kind"], target=t["spec"])
    project = _project(ctx, cfg)
    plan_, auth = planmod.build_standalone(rd, ctx, cfg, summary, project, qa_flag=args.qa, review_flag=args.review,
                                           model=args.model, url=args.url, no_triage=bool(args.no_triage))
    _store_consent(ctx, plan_)
    _prepare(ctx, plan_, cfg, project, auth)
    size = (f"{summary['files']} files, +{summary['added']}/−{summary['removed']}" if not exploratory else
            "no code diff — exploratory QA against the focus/criteria")
    head = f"{prefix} · run {run_id} · standalone · {repo['slug']} · {t['spec']} · {size}"
    _out([head] + _plan_output(ctx, plan_, cfg, project, auth))
    return 0


# --------------------------------------------------------------------------- brief


def validate_brief(text: str) -> None:
    from . import prompts

    if len(text) > BRIEF_MAX:
        raise Fail(f"brief is too long ({len(text)} chars > {BRIEF_MAX})")
    if not prompts.brief_criteria(text, 1):
        raise Fail('brief must contain "## Acceptance criteria" followed by at least one "- " bullet')
    for ln in text.splitlines():
        if NARRATIVE.search(ln) or "works now" in ln.lower():
            raise Fail(f"brief must hold criteria and facts only, not the implementer's narrative: \"{ln.strip()}\"")


@_guard
def cmd_brief(args) -> int:
    from . import plan as planmod

    ctx = _ctx(args.run)
    if ctx.get("status") != "open":
        raise Fail(f"run {args.run} is {ctx.get('status')}")
    text = _read_input(args.file)
    validate_brief(text)
    n = _n(ctx)
    if _judge_of(ctx, n):
        raise Fail(f"attempt {n} is already judged; next: {_vl()} begin --rerun --run {args.run}")
    rd = _rd(ctx)

    def upd(c):
        if args.app_url:
            c["app_url"] = args.app_url
    util.atomic_write_text(rd / "brief.md", text if text.endswith("\n") else text + "\n")
    ctx = _update_ctx(args.run, upd)
    cfg = _cfg(ctx)
    project = _project(ctx, cfg)
    plan_ = _plan(ctx, n)
    if not plan_:
        summary = util.read_json(_att(ctx, n) / "diff.numstat.json", {}) or {}
        a = ctx["attempts"][n - 1]
        pf = {"decision": a.get("prefilter") or "triage", "reason": a.get("prefilter_reason") or "triage"}
        plan_, auth = planmod.build_initial(rd, ctx, n, cfg, summary, project, pf)
        _store_consent(ctx, plan_)
    else:
        auth = _auth_for(ctx, plan_, cfg)
    _prepare(ctx, plan_, cfg, project, auth)
    _out(_plan_output(ctx, plan_, cfg, project, auth))
    return 0


# --------------------------------------------------------------------------- consent


@_guard
def cmd_consent(args) -> int:
    from . import plan as planmod

    ctx = _ctx(args.run)
    if not planmod.consent_pending(ctx):
        raise Fail(f"no browser consent pending for run {args.run}")
    n = _n(ctx)
    cfg = _cfg(ctx)
    project = _project(ctx, cfg)
    plan_ = _plan(ctx, n)
    qa = plan_["roles"]["qa"]
    note = args.note or None
    decision = args.decision
    bc = {"attempt": n, "at": util.now_iso(), "note": note}
    if decision == "allow":
        bc.update(status="granted", source="user", decision="allow")
    elif decision == "no-browser":
        qa.update(scope="run", reason="user declined browser QA")
        bc.update(status="declined", source="user", decision="no-browser")
    else:
        planmod._skip_qa(qa, "user skipped QA")
        bc.update(status="declined", source="user", decision="skip-qa")

    def upd(c):
        c["browser_consent"] = bc
        if note:
            c.setdefault("user_notes", []).append(note)
    ctx = _update_ctx(args.run, upd)
    if decision != "allow":
        tri = util.read_json(_att(ctx, n) / "triage.json", {}) or {}
        planmod.finalize(plan_, cfg, tri, ctx["repo"]["root"], model_override=(ctx.get("flags") or {}).get("model"))
        planmod.save(_rd(ctx), ctx, plan_)
    _event(ctx, "consent", n, decision=decision, note=note)
    auth = _auth_for(ctx, plan_, cfg)
    _prepare(ctx, plan_, cfg, project, auth)
    if decision == "allow":
        _out(["verify-loop · consent: browser QA approved"] + planmod.next_lines(_rd(ctx), ctx, plan_, cfg))
    elif decision == "no-browser":
        _out(["verify-loop · consent: browser QA declined → QA without browser (run)"]
             + planmod.block(_rd(ctx), ctx, plan_, cfg, auth))
    else:
        _out(["verify-loop · consent: QA skipped by user"] + planmod.block(_rd(ctx), ctx, plan_, cfg, auth))
    return 0


# --------------------------------------------------------------------------- verdict intake


def _accept_verdict(ctx: dict, role: str, n: int, text: str | None, prefer_text: bool = False):
    """Normalize + validate the agent's verdict (file and/or final message). Writes the normalized file."""
    from . import verdict

    att = _att(ctx, n)
    path = att / f"{role}.json"
    mode = ((_plan(ctx, n).get("roles") or {}).get(role) or {}).get("mode")
    cands = []
    from_file = util.read_json(path) if path.exists() else None
    from_text = verdict.extract_json(text) if text else None
    for c in ([from_text, from_file] if prefer_text else [from_file, from_text]):
        if isinstance(c, dict):
            cands.append(c)
    first = None
    for raw in cands:
        v, _ = verdict.normalize(role, raw, run_id=ctx["run_id"], attempt=n, mode=mode)
        errs = verdict.validate(role, v)
        if not errs:
            util.atomic_write_json(path, v)
            (att / f"{role}.error.json").unlink(missing_ok=True)
            return v, None
        first = first or errs[0]
    return None, first or "no verdict JSON found"


def _write_error(ctx: dict, role: str, n: int, status: str, detail: str) -> dict:
    err = {"status": status, "detail": detail}
    util.atomic_write_json(_att(ctx, n) / f"{role}.error.json", err)
    return err


def _record_agent(ctx: dict, role: str, n: int, **fields) -> None:
    def upd(c):
        a = c["attempts"][n - 1].setdefault("agents", {})
        a.setdefault(role, {}).update(fields)
    _update_ctx(ctx["run_id"], upd)


def _duration(info: dict | None, end: str | None = None) -> float | None:
    t0 = util.parse_iso((info or {}).get("started_at"))
    t1 = util.parse_iso(end) if end else None
    if t0 is None:
        return None
    import time

    return max(0.0, (t1 if t1 is not None else time.time()) - t0)


def _next_line(ctx: dict, n: int) -> str:
    att = _att(ctx, n)
    plan_ = _plan(ctx, n)
    missing = [r for r in ("reviewer", "qa") if ((plan_.get("roles") or {}).get(r) or {}).get("run")
               and not (att / f"{r}.json").exists() and not (att / f"{r}.error.json").exists()]
    if missing:
        return f"waiting for: {', '.join(missing)}"
    return f"next: {_vl()} judge --run {ctx['run_id']}"


def _me(info: dict | None, pr: dict) -> str:
    m = (info or {}).get("model") or pr.get("model")
    e = (info or {}).get("effort") or pr.get("effort")
    return f"{m}/{e}" if m and e else None


# --------------------------------------------------------------------------- spawn / spawn-wait (vloop-run transports)


def _finish(ctx: dict, role: str, n: int, res, t, info: dict) -> int:
    from . import verdict

    run_id = ctx["run_id"]
    info = {**info, **(res.info or {})}
    pr = (_plan(ctx, n).get("roles") or {}).get(role) or {}
    now = util.now_iso()
    me = _me(info, pr)
    keep = {k: v for k, v in info.items() if k not in ("started_at", "ended_at", "status")}
    if res.status == "blocked":
        if res.doc is not None or res.screen:
            if res.screen:
                print(res.screen)
            print(f"blocked: answer the dialog in {t.where(info)} yourself only if the user tells you to — show this "
                  f"to the user; then: {_vl()} spawn-wait {role} --run {run_id}")
        else:
            if res.stderr.strip():
                print(res.stderr.rstrip())
            first = (res.stderr.strip().splitlines() or [f"the {t.name} transport could not start the agent"])[0]
            print(f"blocked: {first} — show this to the user; after they fix it: {_vl()} spawn {role} --run {run_id}")
        _record_agent(ctx, role, n, status="blocked", **keep)
        return 3
    if res.status == "timeout":
        print(f"timeout: still running in {t.where(info)}; later: {_vl()} spawn-wait {role} --run {run_id}")
        _record_agent(ctx, role, n, status="timeout", **keep)
        return 4
    v, err = None, None
    if res.status == "done":
        if res.result:
            util.atomic_write_text(_att(ctx, n) / f"{role}.final.md", res.result)
        v, why = _accept_verdict(ctx, role, n, res.result)
        status = "done"
        if v is None:
            err = _write_error(ctx, role, n, "bad_verdict", why)
    elif res.status in ("dead", "no_result"):
        status = res.status
        err = _write_error(ctx, role, n, status, (res.stderr.strip().splitlines() or [""])[-1][:300])
    else:
        status = "error"
        err = _write_error(ctx, role, n, "error", res.stderr.strip()[:2000])
    _record_agent(ctx, role, n, status=status, ended_at=now, **keep)
    dur = _duration(info, now)
    _event(ctx, "agent_done", n, role=role, status=status, duration_s=round(dur or 0, 1),
           session_id=info.get("session_id"), **t.event_fields(info), verdict=(v or {}).get("verdict"))
    print(verdict.role_line(role, v, err, dur, me))
    print(_next_line(ctx, n))
    return 1 if status == "error" else 0


def _role_transport(ctx: dict, role: str, n: int | None = None):
    from . import transports

    pr = (_plan(ctx, n or _n(ctx)).get("roles") or {}).get(role) or {}
    return transports.for_role(ctx, _cfg(ctx), role, pr)


def _in_session_fail(t) -> Fail:
    return Fail(f"this session uses the {t.label.replace(' ', '-')} transport; follow the plan's next: lines")


def _event_fields(info: dict) -> dict:
    from . import transports

    name = (info or {}).get("transport")
    return transports.get(name).event_fields(info) if name in transports.names() else {}


def _check_spawnable(ctx: dict, role: str):
    from . import plan as planmod

    if ctx.get("status") not in ("open",):
        raise Fail(f"run {ctx['run_id']} is {ctx.get('status')}")
    n = _n(ctx)
    pr = (_plan(ctx, n).get("roles") or {}).get(role)
    if not pr:
        raise Fail(f"attempt {n} has no plan yet: run {_vl()} brief --run {ctx['run_id']} first")
    t = _role_transport(ctx, role, n)
    if t.in_session:
        raise _in_session_fail(t)
    if role == "qa" and planmod.consent_pending(ctx):
        raise Fail("waiting for browser QA consent")
    if not pr.get("run"):
        raise Fail(f"{role} does not run in attempt {n}")
    if not (_att(ctx, n) / f"{role}.prompt.md").exists():
        raise Fail(f"no task file for {role}: run {_vl()} brief --run {ctx['run_id']} first")
    return n, pr, t


@_guard
def cmd_spawn(args) -> int:
    ctx = _ctx(args.run)
    if args.role == "retro":
        return _spawn_retro(ctx, None)
    n, pr, t = _check_spawnable(ctx, args.role)
    cfg = _cfg(ctx)
    att = _rd(ctx) / f"attempt-{n}"
    (att / f"{args.role}.error.json").unlink(missing_ok=True)
    _integrity_begin(ctx, n, [args.role])
    info = {"transport": t.name, "session_id": None, "started_at": util.now_iso(), "ended_at": None,
            "status": "running", "model": pr["model"], "effort": pr["effort"], **t.event_fields({})}
    _record_agent(ctx, args.role, n, **info)
    _event(ctx, "spawn", n, role=args.role, transport=t.name, model=pr["model"], effort=pr["effort"],
           **t.event_fields({}))
    res = t.spawn(args.role, ctx, n, att / f"{args.role}.prompt.md", pr["model"], pr["effort"], cfg)
    return _finish(ctx, args.role, n, res, t, info)


@_guard
def cmd_spawn_wait(args) -> int:
    from . import transports

    ctx = _ctx(args.run)
    if args.role == "retro":
        return _spawn_retro(ctx, args.timeout, wait=True)
    n = _n(ctx)
    info = ((ctx["attempts"][n - 1].get("agents") or {}).get(args.role)) or {}
    t = transports.get(info["transport"]) if info.get("transport") in transports.names() \
        else _role_transport(ctx, args.role, n)
    if t.in_session:
        raise _in_session_fail(t)
    if not t.waitable(info):
        raise Fail(f"no {t.name} agent recorded for {args.role} in attempt {n}; run {_vl()} spawn {args.role} "
                   f"--run {args.run}")
    return _finish(ctx, args.role, n, t.wait(info, args.timeout), t, info)


def _spawn_retro(ctx: dict, timeout: float | None, wait: bool = False) -> int:
    from . import transports

    run_id = ctx["run_id"]
    rd = _rd(ctx)
    task = rd / "retro" / "task.md"
    if not task.is_file():
        raise Fail(f"no retro task: run {_vl()} retro-plan --run {run_id} first (full policy) or {_vl()} retro-prep")
    repo = ctx.get("repo") or {}
    cfg = config.load(repo.get("root"))
    prev = ctx.get("retro_agent") or {}
    t = transports.get(prev["transport"]) if wait and prev.get("transport") in transports.names() else \
        transports.for_role(ctx, cfg, "retro")
    if not t.supports_retro:
        raise _in_session_fail(t)
    cwd = repo.get("root")
    if not cwd or not os.path.isdir(cwd):
        st = state.load_session(ctx.get("session_id") or "") or {}
        cwd = st.get("cwd") or os.getcwd()
    if wait:
        if not t.waitable(prev):
            raise Fail(f"no {t.name} retro agent recorded; run {_vl()} spawn retro --run {run_id}")
        info = prev
        res = t.wait(prev, timeout)
    else:
        info = {"transport": t.name, "session_id": None, "started_at": util.now_iso(), "status": "running",
                **t.event_fields({})}
        _event(ctx, "retro_spawn", None, transport=t.name, session_id=None, status="running", **t.event_fields({}))
        res = t.spawn_retro(ctx, rd, cwd, bool(repo.get("root")), cfg)
    info = {**info, **(res.info or {})}
    status = res.status if res.status in ("done", "blocked", "timeout", "dead", "no_result") else "error"
    now = util.now_iso()

    def upd(c):
        c["retro_agent"] = {**info, "status": status,
                            "ended_at": now if status not in ("blocked", "timeout") else None}
    _update_ctx(run_id, upd)
    if status == "blocked":
        if res.screen:
            print(res.screen)
        if res.doc is not None or res.screen:
            print(f"blocked: answer the dialog in {t.where(info)} yourself only if the user tells you to — show this "
                  f"to the user; then: {_vl()} spawn-wait retro --run {run_id}")
        else:
            if res.stderr.strip():
                print(res.stderr.rstrip())
            first = (res.stderr.strip().splitlines() or [f"the {t.name} transport could not start the agent"])[0]
            print(f"blocked: {first} — show this to the user; after they fix it: {_vl()} spawn retro --run {run_id}")
        return 3
    if status == "timeout":
        print(f"timeout: still running in {t.where(info)}; later: {_vl()} spawn-wait retro --run {run_id}")
        return 4
    _event(ctx, "retro_done", None, transport=t.name, **t.event_fields(info), session_id=info.get("session_id"),
           status=status)
    dur = util.short_dur(_duration(info, now) or 0)
    if status == "done":
        result = res.result or ""
        util.atomic_write_text(rd / "retro" / "result.md", result)
        first = next((ln.strip() for ln in result.splitlines() if ln.strip()), "(no message)")
        _out([f"retro: done · {first} · {dur}", result.rstrip()])
        return 0
    detail = (res.stderr.strip().splitlines() or [""])[-1][:200]
    print(f"retro: {status} · {detail} · {dur}" if detail else f"retro: {status} · {dur}")
    return 1 if status == "error" else 0


# --------------------------------------------------------------------------- integrity check

INTEGRITY = "integrity.json"


def _fingerprint(ctx: dict) -> str | None:
    """Tree of tracked + untracked non-ignored files (gitsnap snapshot), or None when it cannot be taken."""
    from . import gitsnap

    if (ctx.get("target") or {}).get("code_ref", "worktree") != "worktree":
        return None
    repo = gitsnap.repo_for(ctx["repo"]["root"])
    if not repo:
        return None
    snap = _snapshot(repo, ctx.get("session_id"))
    return snap["tree"] if snap.get("kind") == "snapshot" else None


def _integrity_begin(ctx: dict, n: int, roles: list[str]) -> None:
    """Fingerprint before the first verifier of the attempt starts; later spawns keep the first fingerprint."""
    path = _att(ctx, n) / INTEGRITY
    d = util.read_json(path, {}) or {}
    if d.get("before"):
        new = [r for r in roles if r not in (d.get("roles") or [])]
        if new:
            d["roles"] = list(d.get("roles") or []) + new
            util.atomic_write_json(path, d)
        return
    fp = _fingerprint(ctx)
    if fp:
        util.atomic_write_json(path, {"before": fp, "at": util.now_iso(), "roles": list(roles)})


def _under(path: str, prefixes: list[str]) -> bool:
    import fnmatch

    for p in prefixes:
        p = p.strip("/")
        if not p:
            continue
        if path == p or path.startswith(p + "/") or fnmatch.fnmatch(path, p):
            return True
    return False


def _main_touched(ctx: dict, since: str) -> set[str]:
    sid = ctx.get("session_id")
    if not sid:
        return set()
    out: set[str] = set()
    for e in state.read_events(sid):
        d = e.get("data") or {}
        if e.get("event") == "touch" and e.get("repo") == ctx["repo"]["key"] and (e.get("ts") or "") >= since \
                and d.get("source") != "child":
            out.update(p for p in d.get("paths") or [] if isinstance(p, str))
    return out


def _integrity_check(ctx: dict, n: int, cfg: dict) -> dict | None:
    """Compare the worktree with the pre-spawn fingerprint. Files the main session touched since then are its own;
    files under the run dir are the verifiers' scratch. The reviewer may change nothing else; QA may also change
    `integrity.qa_allowed_paths`. Changes in QA's allowed paths are not charged to the reviewer when QA ran too."""
    from . import gitsnap

    path = _att(ctx, n) / INTEGRITY
    d = util.read_json(path, {}) or {}
    if not d.get("before") or (cfg.get("integrity") or {}).get("enabled") is False:
        return None
    after = _fingerprint(ctx)
    if not after:
        return d
    repo = gitsnap.repo_for(ctx["repo"]["root"])
    changed: list[str] = []
    if after != d["before"] and repo:
        for row in gitsnap.diff_numstat(repo, d["before"], after):
            for k in ("path", "old_path"):
                if isinstance(row.get(k), str) and row[k] not in changed:
                    changed.append(row[k])
    root = os.path.realpath(ctx["repo"]["root"])
    rd = os.path.realpath(_rd(ctx))
    scratch = [os.path.relpath(rd, root)] if rd.startswith(root + os.sep) else []
    main = _main_touched(ctx, d.get("at") or "")
    foreign = [p for p in changed if p not in main and not _under(p, scratch)]
    allowed = [str(p) for p in ((cfg.get("integrity") or {}).get("qa_allowed_paths") or []) if isinstance(p, str)]
    plan_roles = _plan(ctx, n).get("roles") or {}
    ran = [r for r in ("reviewer", "qa") if (plan_roles.get(r) or {}).get("run")]
    flagged: dict[str, list[str]] = {}
    if "qa" in ran:
        flagged["qa"] = [p for p in foreign if not _under(p, allowed)]
    if "reviewer" in ran:
        flagged["reviewer"] = flagged["qa"] if "qa" in ran else foreign
    d.update({"after": after, "checked_at": util.now_iso(), "changed": changed,
              "main_edits": sorted(set(changed) & main), "flagged": flagged})
    util.atomic_write_json(path, d)
    return d


def _integrity_lines(d: dict | None) -> list[str]:
    if not d:
        return []
    out = []
    for role in ("reviewer", "qa"):
        files = (d.get("flagged") or {}).get(role) or []
        if files:
            out.append(f"{role}: integrity · verifier modified files: {util.fmt_paths(files)} → INCONCLUSIVE")
    return out


# --------------------------------------------------------------------------- collect / record (in-session agents)


@_guard
def cmd_collect(args) -> int:
    from . import plan as planmod, verdict

    ctx = _ctx(args.run)
    if planmod.consent_pending(ctx):
        raise Fail("waiting for browser QA consent")
    n = _n(ctx)
    att = _att(ctx, n)
    plan_ = _plan(ctx, n)
    agents = ctx["attempts"][n - 1].get("agents") or {}
    integ = _integrity_check(ctx, n, _cfg(ctx))
    for role in ("reviewer", "qa"):
        pr = (plan_.get("roles") or {}).get(role) or {}
        if not pr.get("run"):
            continue
        info = agents.get(role) or {}
        path = att / f"{role}.json"
        if path.exists():
            v, why = _accept_verdict(ctx, role, n, None)
            end = util.now_iso()
            dur = _duration(info, end)
            if v:
                if info.get("status") != "recorded":
                    _record_agent(ctx, role, n, status="recorded", ended_at=end)
                    _event(ctx, "agent_done", n, role=role, status="recorded", duration_s=round(dur or 0, 1),
                           session_id=info.get("session_id"), **_event_fields(info),
                           verdict=v.get("verdict"))
                print(verdict.role_line(role, v, None, dur, _me(info, pr)))
            else:
                print(verdict.role_line(role, None, {"status": "bad_verdict", "detail": why}, dur))
                print(f"  fix: {_vl()} record {role} --run {args.run} --file <the agent's final message saved to a file>")
        elif (att / f"{role}.error.json").exists():
            print(verdict.role_line(role, None, util.read_json(att / f"{role}.error.json"), None))
        else:
            print(f"{role}: no verdict yet at {path} — if the agent has finished, save its final message to a file "
                  f"and run: {_vl()} record {role} --run {args.run} --file <file>")
    for ln in _integrity_lines(integ):
        print(ln)
    print(_next_line(ctx, n))
    return 0


@_guard
def cmd_record(args) -> int:
    from . import verdict

    ctx = _ctx(args.run)
    n = _n(ctx)
    pr = (_plan(ctx, n).get("roles") or {}).get(args.role) or {}
    if not pr.get("run"):
        raise Fail(f"{args.role} does not run in attempt {n}")
    text = _read_input(args.file)
    att = _att(ctx, n)
    util.atomic_write_text(att / f"{args.role}.final.md", text)
    v, why = _accept_verdict(ctx, args.role, n, text, prefer_text=True)
    err = None if v else _write_error(ctx, args.role, n, "bad_verdict", why)
    info = (ctx["attempts"][n - 1].get("agents") or {}).get(args.role) or {}
    end = util.now_iso()
    dur = _duration(info, end)
    _record_agent(ctx, args.role, n, status="recorded" if v else "bad_verdict", ended_at=end,
                  transport=info.get("transport") or ctx.get("transport"), model=info.get("model") or pr.get("model"),
                  effort=info.get("effort") or ("medium" if ctx.get("transport") == "agent" else pr.get("effort")))
    _event(ctx, "agent_done", n, role=args.role, status="recorded" if v else "bad_verdict",
           duration_s=round(dur or 0, 1), session_id=info.get("session_id"), **_event_fields(info),
           verdict=(v or {}).get("verdict"))
    print(verdict.role_line(args.role, v, err, dur, _me(info, pr)))
    print(_next_line(ctx, n))
    return 0


# --------------------------------------------------------------------------- judge


def _taint(ctx: dict, n: int, plan_: dict) -> None:
    from . import gitsnap

    if (ctx.get("target") or {}).get("code_ref", "worktree") != "worktree":
        return
    a = ctx["attempts"][n - 1]
    repo = _repo(ctx)
    snap = _snapshot(repo, ctx.get("session_id"))
    changed: list[str] = []
    if snap["kind"] == "snapshot" and snap["tree"] != a["tree"]:
        allowed = [str(p) for p in ((_cfg(ctx).get("integrity") or {}).get("qa_allowed_paths") or [])
                   if isinstance(p, str)]
        changed = [r["path"] for r in gitsnap.diff_numstat(repo, a["tree"], snap["tree"])
                   if not _under(r["path"], allowed)]
    main_edits = False
    if changed and ctx.get("session_id"):
        begin = a.get("begin_at") or ""
        main_edits = any(e.get("event") == "touch" and e.get("repo") == ctx["repo"]["key"]
                         and (e.get("ts") or "") >= begin for e in state.read_events(ctx["session_id"]))
    qa_run = bool(((plan_.get("roles") or {}).get("qa") or {}).get("run"))
    util.atomic_write_json(_att(ctx, n) / "qa.taint.json",
                           {"changed_files": changed, "main_edits": main_edits,
                            "tainted": bool(changed) and not main_edits and qa_run})


def _apply_judge(ctx: dict, j: dict) -> dict:
    """Context + session loop from a judge result (§3 WP2 judge step 4–5)."""
    n = j["attempt"]
    result, esc = j.get("result"), j.get("escalate")

    def upd(c):
        a = c["attempts"][n - 1]
        a.update(result=result, judge=f"attempt-{n}/judge.json", elapsed_s=j.get("elapsed_s"))
        c["failed_attempts"] = j.get("failed_attempts", 0)
        c["inconclusive_streak"] = j.get("inconclusive_streak", 0)
        if c.get("kind") == "loop":
            if esc:
                c["status"] = "escalated"
            elif result == "pass":
                c["status"] = "passed"
            elif c.get("status") == "escalated":
                c["status"] = "open"
        else:
            c["status"] = "done"
    ctx = _update_ctx(ctx["run_id"], upd)
    sid = ctx.get("session_id")
    if ctx.get("kind") == "loop" and sid:
        with state.session_lock(sid):
            st = state.load_session(sid)
            rs = (st or {}).get("repos", {}).get(ctx["repo"]["key"])
            if rs and (rs.get("loop") or {}).get("run_id") == ctx["run_id"]:
                lp = rs["loop"]
                lp.update(attempt=n, failed_attempts=j.get("failed_attempts", 0),
                          inconclusive_streak=j.get("inconclusive_streak", 0), last_result=result,
                          last_status_line=j.get("status_line"), inflight_since=None)
                if esc:
                    lp["status"] = "escalated"
                elif result == "pass":
                    state.close_loop(st, ctx["repo"]["key"], "passed", "verified", tree=ctx["attempts"][n - 1]["tree"],
                                     retro="pending")
                elif lp.get("status") == "escalated":
                    lp["status"] = "open"
                state.save_session(st)
    roles = j.get("roles") or {}
    _event(ctx, "judge", n, result=result,
           blocking={r: (roles.get(r) or {}).get("blocking", 0) for r in ("reviewer", "qa") if r in roles},
           downgraded=sum(int((v or {}).get("downgraded") or 0) for v in roles.values()),
           failed_attempts=j.get("failed_attempts"), inconclusive_streak=j.get("inconclusive_streak"),
           elapsed_s=j.get("elapsed_s"))
    if esc:
        _event(ctx, "escalate", n, reason=esc.get("reason"))
    return ctx


def _report(ctx: dict, j: dict) -> str:
    from . import verdict

    text = verdict.render_report(j, ctx["run_id"])
    if j.get("escalate"):
        text += "\n" + verdict.render_escalation(_rd(ctx))
    return text


def _judge(ctx: dict, n: int, cfg: dict, elapsed: float | None) -> dict:
    from . import verdict

    lp = cfg.get("loop") or {}
    return verdict.judge_attempt(_rd(ctx), n, elapsed_s=elapsed, max_failed=int(lp.get("max_failed_attempts", 3)),
                                 max_inconclusive=int(lp.get("max_inconclusive_streak", 2)))


@_guard
def cmd_judge(args) -> int:
    from . import plan as planmod

    ctx = _ctx(args.run)
    if planmod.consent_pending(ctx):
        raise Fail("waiting for browser QA consent")
    n = _n(ctx)
    existing = _judge_of(ctx, n)
    if existing and ctx.get("status") != "open":
        print(_report(ctx, existing))
        return 0
    if ctx.get("status") != "open":
        raise Fail(f"run {args.run} is {ctx.get('status')}")
    plan_ = _plan(ctx, n)
    if not plan_:
        raise Fail(f"attempt {n} has no plan yet: run {_vl()} brief --run {args.run} first")
    att = _att(ctx, n)
    missing = [r for r in ("reviewer", "qa") if ((plan_.get("roles") or {}).get(r) or {}).get("run")
               and not (att / f"{r}.json").exists() and not (att / f"{r}.error.json").exists()]
    if missing:
        raise Fail(f"waiting for: {', '.join(missing)}")
    cfg = _cfg(ctx)
    _integrity_check(ctx, n, cfg)
    _taint(ctx, n, plan_)
    t0 = util.parse_iso(ctx["attempts"][n - 1].get("begin_at"))
    import time

    elapsed = round(time.time() - t0, 1) if t0 else None
    j = _judge(ctx, n, cfg, elapsed)
    ctx = _apply_judge(ctx, j)
    print(_report(ctx, j))
    return 0


# --------------------------------------------------------------------------- dispute / decide / findings


def _last_judged(ctx: dict) -> tuple[int, dict | None]:
    for n in range(_n(ctx), 0, -1):
        j = _judge_of(ctx, n)
        if j:
            return n, j
    return 0, None


@_guard
def cmd_dispute(args) -> int:
    ctx = _ctx(args.run)
    if ctx.get("status") not in ("open", "escalated"):
        raise Fail(f"run {args.run} is {ctx.get('status')}")
    n, j = _last_judged(ctx)
    if not j:
        raise Fail(f"run {args.run} has no judged attempt yet")
    if not any(f.get("id") == args.finding_id for f in j.get("findings") or []):
        raise Fail(f"unknown finding {args.finding_id} in attempt {n}")

    def upd(c):
        c.setdefault("disputes", {})[args.finding_id] = {"reason": args.reason, "attempt": n}
    ctx = _update_ctx(args.run, upd)
    _event(ctx, "dispute", n, id=args.finding_id)
    print(f"disputed {args.finding_id}: the next reviewer will rule on it")
    return 0


def _close(ctx: dict, status: str, reason: str) -> dict:
    n = _n(ctx)
    tree = ctx["attempts"][n - 1]["tree"]

    def upd(c):
        c["status"] = status
    ctx = _update_ctx(ctx["run_id"], upd)
    sid = ctx.get("session_id")
    if sid:
        with state.session_lock(sid):
            st = state.load_session(sid)
            rs = (st or {}).get("repos", {}).get(ctx["repo"]["key"])
            if rs and (rs.get("loop") or {}).get("run_id") == ctx["run_id"]:
                rs["loop"]["inflight_since"] = None
                state.close_loop(st, ctx["repo"]["key"], status, reason, tree=tree, retro="pending")
                state.save_session(st)
    return ctx


@_guard
def cmd_decide(args) -> int:
    ctx = _ctx(args.run)
    run_id = args.run
    if ctx.get("kind") != "loop":
        raise Fail("decide is only for verification loops")
    n = _n(ctx)
    j = _judge_of(ctx, n)
    st_ = ctx.get("status")
    if not (st_ == "escalated" or (st_ == "open" and j and j.get("result") in ("fail", "inconclusive"))):
        raise Fail(f"nothing to decide: run {run_id} is {st_}"
                   + ("" if j else f" and attempt {n} has not been judged"))
    cfg = _cfg(ctx)
    note = args.note or None
    ids = [i.strip() for i in (args.ids or "").split(",") if i.strip()]
    if args.action == "waive" and not ids:
        raise Fail("--ids is required for waive")

    def notes(c):
        if note:
            c.setdefault("user_notes", []).append(note)
    if args.action == "retry":
        def upd(c):
            notes(c)
            c.update(failed_attempts=0, inconclusive_streak=0, status="open")
        ctx = _update_ctx(run_id, upd)
        jp = _att(ctx, n) / "judge.json"
        if j is not None:  # re-judging this attempt later starts from the reset counters
            j["counters_before"] = {"failed_attempts": 0, "inconclusive_streak": 0}
            util.atomic_write_json(jp, j)
        sid = ctx.get("session_id")
        if sid:
            with state.session_lock(sid):
                st = state.load_session(sid)
                rs = (st or {}).get("repos", {}).get(ctx["repo"]["key"])
                if rs and (rs.get("loop") or {}).get("run_id") == run_id:
                    rs["loop"].update(status="open", failed_attempts=0, inconclusive_streak=0)
                    state.save_session(st)
        _event(ctx, "decide", n, action="retry", ids=ids, note=note)
        _out([f"verify-loop · decision: retry · failures reset to 0/{_max_failed(cfg)}"
              + (" · your note is added for the next agents" if note else ""),
              f"next: fix, then {_vl()} begin --rerun --run {run_id} --fix-notes - <<'EOF' … EOF"])
        return 0
    if args.action == "waive":
        def upd(c):
            notes(c)
            w = c.setdefault("waived", [])
            for i in ids:
                if i not in w:
                    w.append(i)
        ctx = _update_ctx(run_id, upd)
        _event(ctx, "decide", n, action="waive", ids=ids, note=note)
        j2 = _judge(ctx, n, cfg, (j or {}).get("elapsed_s"))
        ctx = _apply_judge(ctx, j2)
        _out([f"verify-loop · decision: waive {','.join(ids)}", _report(ctx, j2)])
        return 0
    open_ids = [f["id"] for f in (j or {}).get("findings") or [] if f.get("blocking")]
    if args.action == "accept":
        def upd(c):
            notes(c)
        _update_ctx(run_id, upd)
        ctx = _close(ctx, "accepted", f"accepted with open findings: {','.join(open_ids) or 'none'}")
        _event(ctx, "decide", n, action="accept", ids=ids or open_ids, note=note)
        _out([f"verify-loop · decision: accept · loop closed (accepted with open findings: "
              f"{', '.join(open_ids) or 'none'})", f"next: {_vl()} retro-plan --run {run_id}"])
        return 0

    def upd(c):
        notes(c)
    _update_ctx(run_id, upd)
    ctx = _close(ctx, "abandoned", "stopped by the user")
    _event(ctx, "decide", n, action="stop", ids=ids or open_ids, note=note)
    _out([f"verify-loop · decision: stop · loop closed (abandoned) · the change is UNVERIFIED — say so in your final "
          f"message and list: {', '.join(open_ids) or 'none'}", f"next: {_vl()} retro-plan --run {run_id}"])
    return 0


@_guard
def cmd_findings(args) -> int:
    ctx = _ctx(args.run)
    if args.attempt:
        n, j = args.attempt, _judge_of(ctx, args.attempt)
    else:
        n, j = _last_judged(ctx)
    if not j:
        raise Fail(f"attempt {n or _n(ctx)} of run {args.run} has not been judged yet")
    if args.json:
        print(json.dumps(j, indent=2, ensure_ascii=False))
    else:
        print(_report(ctx, j))
    return 0


# --------------------------------------------------------------------------- argparse


def register(sub) -> None:
    def common(p):
        p.add_argument("--session", default=None, help="session id (default: the platform's session env)")
        return p

    p = common(sub.add_parser("begin", help="start a verification loop (or a re-run / standalone run)"))
    p.add_argument("--repo", default=None)
    p.add_argument("--rerun", action="store_true")
    p.add_argument("--run", default=None)
    p.add_argument("--fix-notes", default=None, metavar="FILE|-")
    p.add_argument("--qa", choices=SCOPES, default=None)
    p.add_argument("--review", choices=TIERS, default=None)
    p.add_argument("--no-triage", action="store_true")
    p.add_argument("--app-url", default=None)
    p.add_argument("--standalone", action="store_true")
    p.add_argument("--roles", default=None)
    p.add_argument("--target", default=None)
    p.add_argument("--focus", default=None)
    p.add_argument("--criteria", default=None)
    p.add_argument("--url", default=None)
    p.add_argument("--model", default=None)
    p.add_argument("--brief-file", default=None, metavar="FILE|-")
    p.add_argument("paths", nargs="*", help="after --target … --: limit the diff to these paths")
    p.set_defaults(fn=cmd_begin)

    p = common(sub.add_parser("brief", help="store the brief, plan (triage) and render task files"))
    p.add_argument("--run", required=True)
    p.add_argument("--file", default=None)
    p.add_argument("--app-url", default=None)
    p.set_defaults(fn=cmd_brief)

    p = common(sub.add_parser("spawn", help="run a verify agent through its transport (headless, Herdr, …; blocks)"))
    p.add_argument("role", choices=("reviewer", "qa", "retro"))
    p.add_argument("--run", required=True)
    p.set_defaults(fn=cmd_spawn)

    p = common(sub.add_parser("spawn-wait", help="wait for a blocked/timed-out agent and record it"))
    p.add_argument("role", choices=("reviewer", "qa", "retro"))
    p.add_argument("--run", required=True)
    p.add_argument("--timeout", type=float, default=None)
    p.set_defaults(fn=cmd_spawn_wait)

    p = common(sub.add_parser("collect", help="check the verdict files and the worktree integrity after the agents ran"))
    p.add_argument("--run", required=True)
    p.set_defaults(fn=cmd_collect)

    p = common(sub.add_parser("record", help="record a verdict from an agent's final message"))
    p.add_argument("role", choices=("reviewer", "qa"))
    p.add_argument("--run", required=True)
    p.add_argument("--file", default=None)
    p.set_defaults(fn=cmd_record)

    p = common(sub.add_parser("judge", help="judge the current attempt"))
    p.add_argument("--run", required=True)
    p.set_defaults(fn=cmd_judge)

    p = common(sub.add_parser("consent", help="answer the browser-QA consent question"))
    p.add_argument("decision", choices=("allow", "no-browser", "skip-qa"))
    p.add_argument("--run", required=True)
    p.add_argument("--note", default=None)
    p.set_defaults(fn=cmd_consent)

    p = common(sub.add_parser("dispute", help="dispute a finding; the next reviewer rules on it"))
    p.add_argument("finding_id")
    p.add_argument("--run", required=True)
    p.add_argument("--reason", required=True)
    p.set_defaults(fn=cmd_dispute)

    p = common(sub.add_parser("decide", help="the user's decision after an escalation"))
    p.add_argument("action", choices=("retry", "waive", "accept", "stop"))
    p.add_argument("--run", required=True)
    p.add_argument("--ids", default=None)
    p.add_argument("--note", default=None)
    p.set_defaults(fn=cmd_decide)

    p = common(sub.add_parser("findings", help="re-print the report of an attempt"))
    p.add_argument("--run", required=True)
    p.add_argument("--attempt", type=int, default=None)
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_findings)
