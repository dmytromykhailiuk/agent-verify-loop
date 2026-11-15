"""Plan builder: prefilter/triage/fallback → per-role plan, floors, flags, re-run modes, browser consent (§2.9)."""
from __future__ import annotations

import copy
from pathlib import Path

from . import state, util
from .roles import ADVISORY_ROLES, ALL_ROLES, SPECIALISTS

SCOPES = ("none", "checks", "run", "browser")
TIERS = ("standard", "deep")
SKIPPED_BY = {"prefilter-trivial": "prefilter", "triage": "triage", "reused": "triage", "fallback": "fallback",
              "user": "user", "standalone": "triage"}


def _vl() -> str:
    return util.vloop_cli()


def _max(order, a: str, b: str) -> str:
    return a if order.index(a) >= order.index(b) else b


def _idx(order, v) -> int:
    return order.index(v) if v in order else 0


# --------------------------------------------------------------------------- synthetic triage results


def change_size(summary: dict) -> str:
    lines, files = int(summary.get("changed_lines") or 0), int(summary.get("files") or 0)
    if lines > 400 or files > 15:
        return "large"
    if lines < 60 and files <= 3:
        return "small"
    return "medium"


def _risk(summary: dict) -> str:
    if summary.get("risky_files"):
        return "high"
    return "medium" if summary.get("source_files") or summary.get("config_files") else "low"


SECURITY_FALLBACK_MIN_LINES = 10


def specialists_off(reason: str) -> dict:
    return {"security": {"run": False, "reason": reason, "focus": []},
            "perf": {"run": False, "reason": reason, "focus": []},
            "ux": {"run": False, "ui_spec": "n/a", "reason": reason, "focus": []}}


def _fallback_specialists(summary: dict, prefix: str) -> dict:
    spec = specialists_off(prefix + "not chosen by the fallback rules")
    sec, lines = summary.get("security_files") or [], int(summary.get("security_changed_lines") or 0)
    if sec and lines >= SECURITY_FALLBACK_MIN_LINES:
        spec["security"] = {"run": True, "focus": list(sec[:5]),
                            "reason": prefix + f"security-sensitive files changed ({util.fmt_paths(sec)}, "
                                               f"{lines} lines)"}
    return spec


def fallback_triage(summary: dict, cfg: dict, project: dict, prefix: str) -> dict:
    """§2.9.2 fallback rules, shaped like a stored triage result (source fallback)."""
    if summary.get("risky_files"):
        tier, rr = "deep", f"risky files changed ({util.fmt_paths(summary['risky_files'])})"
    elif int(summary.get("changed_lines") or 0) > 600:
        tier, rr = "deep", f"{summary.get('changed_lines')} changed lines (> 600)"
    else:
        tier, rr = "standard", "no risky files, ≤ 600 changed lines"
    if summary.get("ui_files") and project.get("app_url"):
        scope, qr = "browser", f"UI files changed ({util.fmt_paths(summary['ui_files'])}) and the app URL is known"
    elif summary.get("source_files") or summary.get("test_files"):
        scope, qr = "checks", "source or test files changed"
    else:
        scope, qr = "none", "no source or test files changed"
    needs_login = bool((project.get("auth") or {}).get("required_for"))
    return {"schema": 1, "role": "triage", "source": "fallback", "change_size": change_size(summary),
            "risk": _risk(summary),
            "qa": {"scope": scope, "reason": prefix + qr, "focus": [], "needs_login": needs_login},
            "review": {"tier": tier, "reason": prefix + rr, "focus": []},
            "specialists": _fallback_specialists(summary, prefix), "confidence": "low", "agent": None}


def _trivial_triage(reason: str) -> dict:
    return {"schema": 1, "role": "triage", "source": "prefilter", "change_size": "small", "risk": "low",
            "qa": {"scope": "none", "reason": f"{reason} (prefilter, no triage)", "focus": [], "needs_login": False},
            "review": {"tier": "standard", "reason": f"{reason} (prefilter)", "focus": []},
            "specialists": specialists_off(f"{reason} (prefilter)"), "confidence": "high", "agent": None}


def _user_triage(summary: dict, qa: str, review: str) -> dict:
    return {"schema": 1, "role": "triage", "source": "user", "change_size": change_size(summary),
            "risk": _risk(summary),
            "qa": {"scope": qa, "reason": f"--qa {qa} (user flag)", "focus": [], "needs_login": False},
            "review": {"tier": review, "reason": f"--review {review} (user flag)", "focus": []},
            "specialists": specialists_off("user flags (no triage)"), "confidence": "high", "agent": None}


def _store_triage(run_dir: Path, ctx: dict, n: int, tri: dict) -> dict:
    from . import triage

    tri = dict(tri)
    tri.update({"run_id": ctx["run_id"], "attempt": n})
    return triage.write(run_dir, ctx, n, tri)


def _run_triage(run_dir, ctx, n, cfg, summary, project, **kw) -> dict:
    from . import classify, triage

    auth = classify.auth_status(ctx["repo"]["root"], cfg)
    return triage.run_triage(run_dir, ctx, n, cfg, summary, project, auth, **kw)


# --------------------------------------------------------------------------- draft → plan


def _draft_spec(tri: dict) -> dict:
    raw = tri.get("specialists") if isinstance(tri.get("specialists"), dict) else {}
    out = {}
    for s in SPECIALISTS:
        o = raw.get(s) if isinstance(raw.get(s), dict) else {}
        out[s] = {"run": o.get("run") is True, "reason": o.get("reason") or "", "focus": list(o.get("focus") or []),
                  "source": "triage"}
        if s == "ux":
            out[s]["ui_spec"] = o.get("ui_spec") or "n/a"
    return out


def _draft(tri: dict) -> dict:
    qa, rv = tri.get("qa") or {}, tri.get("review") or {}
    return {"tier": rv.get("tier") or "standard", "review_reason": rv.get("reason") or "",
            "scope": qa.get("scope") or "none", "qa_reason": qa.get("reason") or "",
            "qa_focus": list(qa.get("focus") or []), "needs_login": bool(qa.get("needs_login")), "skipped_by": None,
            "spec": _draft_spec(tri)}


def _floors_flags(d: dict, cfg: dict, flags: dict, transport: str, notes: list[str]) -> None:
    floors = cfg.get("floors") or {}
    fq, fr = floors.get("qa"), floors.get("review")
    if fq in SCOPES and _idx(SCOPES, fq) > _idx(SCOPES, d["scope"]):
        notes.append(f"repo floor: qa {d['scope']} → {fq}")
        d["qa_reason"] = f"repo floor qa={fq} (was {d['scope']}: {d['qa_reason']})"
        d["scope"] = fq
    if fr in TIERS and _idx(TIERS, fr) > _idx(TIERS, d["tier"]):
        notes.append(f"repo floor: review {d['tier']} → {fr}")
        d["review_reason"] = f"repo floor review={fr} (was {d['tier']}: {d['review_reason']})"
        d["tier"] = fr
    if flags.get("qa") in SCOPES:
        d["scope"] = flags["qa"]
        d["qa_reason"] = f"--qa {flags['qa']} (user flag)"
        if flags["qa"] == "none":
            d["skipped_by"] = "user"
    if flags.get("review") in TIERS:
        d["tier"] = flags["review"]
        d["review_reason"] = f"--review {flags['review']} (user flag)"
    if transport == "agent" and d["scope"] == "browser" and not cfg.get("agent_transport_browser", True):
        d["scope"] = "run"
        notes.append("browser unavailable on the Agent-tool transport")


def _setting(cfg: dict, flags: dict, s: str) -> tuple[str, str, str]:
    """(auto | on | off, source, reason). Precedence: user flag > repo `specialists` > triage."""
    if flags.get(s) in ("on", "off"):
        return flags[s], "user", f"--{s} {flags[s]} (user flag)"
    repo = (cfg.get("specialists") or {}).get(s)
    if repo in ("on", "off"):
        return repo, "repo", f"repo specialists.{s}={repo}"
    return "auto", "triage", ""


def _specialists(d: dict, cfg: dict, flags: dict, summary: dict, notes: list[str],
                 keep_on: dict | None = None) -> None:
    """Final run/off per specialist in d["spec"]. `keep_on`: specialist → focus for those an earlier attempt ran
    (re-runs only ratchet them on). UX on triage's word needs UI files and a UI spec that is not clear."""
    keep_on = keep_on or {}
    for s in SPECIALISTS:
        sp = d["spec"][s]
        setting, source, why = _setting(cfg, flags, s)
        tri_run = sp["run"]
        if setting == "on":
            if not tri_run:
                notes.append(f"{s} on: {why} (triage: off)")
            sp.update(run=True, reason=f"{why}; triage: {sp['reason']}" if tri_run and sp["reason"] else why,
                      source=source)
            continue
        if setting == "off":
            if tri_run or s in keep_on:
                notes.append(f"{s} off: {why} (triage: on)" if tri_run else f"{s} off: {why}")
            sp.update(run=False, reason=why, source=source)
            continue
        if s in keep_on:
            if not tri_run:
                sp.update(run=True, reason="ran in an earlier attempt (specialists only turn on in re-runs)",
                          source="earlier", focus=sp["focus"] or list(keep_on[s]))
            continue
        if s == "ux" and tri_run:
            if not summary.get("ui_files"):
                notes.append("ux off: no UI files changed (triage: on)")
                sp.update(run=False, reason="no UI files changed")
            elif sp.get("ui_spec") == "clear":
                notes.append("ux off: the request gives a clear UI spec (triage: on)")
                sp.update(run=False, reason="clear UI spec in the request")


def _roles(ctx: dict, d: dict, cfg: dict, source: str) -> dict:
    t = cfg.get("timeouts_s") or {}
    roles = {}
    if "reviewer" in ctx.get("roles", []):
        roles["reviewer"] = {"run": True, "mode": "full", "tier": d["tier"], "model": None, "effort": None,
                             "reason": d["review_reason"], "timeout_s": int(t.get("reviewer", 1200)),
                             "carried_from": None}
    if "qa" in ctx.get("roles", []):
        qa = {"run": d["scope"] != "none", "mode": "full", "scope": d["scope"], "model": None, "effort": None,
              "reason": d["qa_reason"], "timeout_s": int(t.get("qa", 1800)), "carried_from": None,
              "focus": d["qa_focus"][:5], "needs_login": d["needs_login"], "auth_status": "not_needed"}
        if d["scope"] == "none":
            qa["skipped_by"] = d["skipped_by"] or SKIPPED_BY.get(source, "triage")
        roles["qa"] = qa
    for s in SPECIALISTS:
        sp = (d.get("spec") or {}).get(s) or {}
        if s in ctx.get("roles", []) and sp.get("run"):
            roles[s] = {"run": True, "mode": "full", "tier": "standard", "model": None, "effort": None,
                        "reason": sp.get("reason") or "", "timeout_s": int(t.get(s, 1200)), "carried_from": None,
                        "focus": list(sp.get("focus") or [])[:5], "source": sp.get("source") or "triage"}
    return roles


def _spec_record(ctx: dict, d: dict) -> dict | None:
    if not any(s in (ctx.get("roles") or []) for s in SPECIALISTS):
        return None
    return {s: {"run": bool(d["spec"][s]["run"]), "reason": d["spec"][s].get("reason") or "",
                "source": d["spec"][s].get("source") or "triage"} for s in SPECIALISTS}


def _skip_qa(qa: dict, reason: str) -> None:
    qa.update({"run": False, "scope": "none", "skipped_by": "user", "reason": reason, "carried_from": None,
               "model": None, "effort": None, "auth_status": "not_needed"})


def finalize(plan: dict, cfg: dict, tri: dict, repo_root: str, *, model_override: str | None = None) -> dict | None:
    """Model/effort per role from tier/scope/mode, and the QA auth status. Returns the auth dict (browser) or None."""
    from . import classify

    models = cfg.get("models") or {}
    rv = plan["roles"].get("reviewer")
    if rv:
        if rv.get("run"):
            mr = models.get("reviewer") or {}
            if rv["mode"] == "delta":
                key = "delta"
            elif rv["tier"] == "deep":
                key = "deep"
            elif plan.get("source") == "prefilter-trivial" or tri.get("source") == "prefilter":
                key = "trivial"
            elif tri.get("change_size") == "small" and tri.get("risk") == "low":
                key = "standard_small"
            else:
                key = "standard"
            m, e = mr.get(key) or ["sonnet", "medium"]
            rv["model"], rv["effort"] = model_override or m, e
        else:
            rv["model"] = rv["effort"] = None
    for s in SPECIALISTS:
        sr = plan["roles"].get(s)
        if not sr:
            continue
        if sr.get("run"):
            ms = models.get(s) or {}
            if sr.get("mode") == "delta":
                key = "delta"
            elif s == "security" and tri.get("risk") == "high" and ms.get("high_risk"):
                key = "high_risk"
            else:
                key = "standard"
            sr["tier"] = "high_risk" if key == "high_risk" else "standard"
            sr["model"], sr["effort"] = ms.get(key) or ms.get("standard") or ["sonnet", "medium"]
        else:
            sr["model"] = sr["effort"] = None
    qa = plan["roles"].get("qa")
    auth = None
    if qa:
        if qa.get("run"):
            mq = models.get("qa") or {}
            key = f"delta_{qa['scope']}" if qa["mode"] == "delta" else qa["scope"]
            qa["model"], qa["effort"] = mq.get(key) or mq.get(qa["scope"]) or ["sonnet", "medium"]
        else:
            qa["model"] = qa["effort"] = None
        if qa.get("run") and qa["scope"] == "browser":
            auth = classify.auth_status(repo_root, cfg)
            qa["auth_status"] = auth.get("status") or "missing"
        else:
            qa["auth_status"] = "not_needed"
    return auth


# --------------------------------------------------------------------------- browser consent (§2.9.4)


def apply_consent(ctx: dict, plan: dict, cfg: dict, *, explicit: bool = False, rule1: bool = False) -> str | None:
    """Mutates plan + ctx.browser_consent. Returns pending | granted | reused | declined | skipped | None."""
    qa = (plan.get("roles") or {}).get("qa")
    if not qa:
        return None
    n = int(plan["attempt"])
    bc = ctx.get("browser_consent") or {}
    wants_browser = bool(qa.get("run")) and qa.get("scope") == "browser"
    earlier = isinstance(bc, dict) and int(bc.get("attempt") or 0) < n
    if bc.get("decision") == "skip-qa" and earlier and not (wants_browser and rule1):
        _skip_qa(qa, "user skipped QA earlier in this run")
        return "skipped"
    if not wants_browser:
        return None
    policy = cfg.get("qa_browser_consent") or "ask"

    def decide(status, source, decision):
        ctx["browser_consent"] = {"status": status, "source": source, "decision": decision, "attempt": n,
                                  "at": util.now_iso(), "note": None}

    def downgrade(reason):
        qa["scope"] = "run"
        qa["reason"] = reason
        qa["auth_status"] = "not_needed"

    if policy == "never":
        downgrade("browser QA disabled by config (qa_browser_consent=never)")
        decide("declined", "config", "never")
        return "declined"
    if policy == "always":
        decide("granted", "config", "always")
        return "granted"
    if explicit:
        decide("granted", "user flags", "flags")
        return "granted"
    if earlier and bc.get("status") == "granted":
        return "reused"
    if earlier and bc.get("status") == "declined" and bc.get("decision") != "skip-qa" and not rule1:
        downgrade("user declined browser QA earlier in this run")
        return "declined"
    decide("pending", None, None)
    return "pending"


def consent_pending(ctx: dict) -> bool:
    bc = ctx.get("browser_consent") or {}
    cur = len(ctx.get("attempts") or [])
    return isinstance(bc, dict) and bc.get("status") == "pending" and int(bc.get("attempt") or 0) == cur


# --------------------------------------------------------------------------- persistence


def save(run_dir: Path, ctx: dict, plan: dict) -> None:
    n = plan["attempt"]
    util.atomic_write_json(Path(run_dir) / f"attempt-{n}" / "plan.json", plan)
    rv, qa = plan["roles"].get("reviewer") or {}, plan["roles"].get("qa") or {}
    if ctx.get("session_id"):
        state.append_event(ctx["session_id"], "plan", repo=ctx["repo"]["key"], run=ctx["run_id"], attempt=n,
                           reviewer={k: rv.get(k) for k in ("run", "mode", "tier", "model", "effort")} if rv else None,
                           qa={k: qa.get(k) for k in ("run", "mode", "scope", "model", "effort")} if qa else None,
                           specialists={s: {k: plan["roles"][s].get(k) for k in ("run", "mode", "model", "effort")}
                                        for s in SPECIALISTS if s in plan["roles"]},
                           notes=plan.get("notes") or [])


def _new_plan(ctx: dict, n: int, source: str, roles: dict, notes: list[str], d: dict | None = None) -> dict:
    plan = {"schema": 1, "run_id": ctx["run_id"], "attempt": n, "source": source,
            "triage_path": f"attempt-{n}/triage.json", "roles": roles, "notes": notes,
            "transport": ctx.get("transport") or "agent"}
    spec = _spec_record(ctx, d) if d else None
    if spec:
        plan["specialists"] = spec
    return plan


# --------------------------------------------------------------------------- builders


def build_initial(run_dir: Path, ctx: dict, n: int, cfg: dict, summary: dict, project: dict, prefilter: dict) -> tuple[dict, dict | None]:
    """Attempt-1 plan (§2.9.2). Returns (plan, auth)."""
    flags = ctx.get("flags") or {}
    notes: list[str] = []
    if flags.get("qa") in SCOPES and flags.get("review") in TIERS:
        tri, source = _store_triage(run_dir, ctx, n, _user_triage(summary, flags["qa"], flags["review"])), "user"
    elif prefilter.get("decision") == "trivial":
        tri, source = _store_triage(run_dir, ctx, n, _trivial_triage(prefilter["reason"])), "prefilter-trivial"
    elif flags.get("no_triage"):
        fb = fallback_triage(summary, cfg, project, "--no-triage: ")
        tri, source = _store_triage(run_dir, ctx, n, fb), "fallback"
    else:
        tri = _run_triage(run_dir, ctx, n, cfg, summary, project)
        source = "triage" if tri.get("source") == "triage" else "fallback"
    d = _draft(tri)
    _floors_flags(d, cfg, flags, ctx.get("transport") or "agent", notes)
    _specialists(d, cfg, flags, summary, notes)
    plan = _new_plan(ctx, n, source, _roles(ctx, d, cfg, source), notes, d)
    apply_consent(ctx, plan, cfg)
    auth = finalize(plan, cfg, tri, ctx["repo"]["root"])
    save(run_dir, ctx, plan)
    return plan, auth


def build_standalone(run_dir: Path, ctx: dict, cfg: dict, summary: dict, project: dict, *, qa_flag: str | None,
                     review_flag: str | None, model: str | None, url: str | None, no_triage: bool) -> tuple[dict, dict | None]:
    n = 1
    roles = ctx.get("roles") or []
    need = ("reviewer" in roles and not (review_flag or model)) or ("qa" in roles and not (qa_flag or url))
    exploratory = bool((ctx.get("standalone") or {}).get("exploratory"))
    if exploratory:  # no diff to triage: browser with a url, else run the app/CLI
        tri = _user_triage(summary, qa_flag or ("browser" if url else "run"), review_flag or "standard")
        tri["qa"]["reason"] = "exploratory check, no code diff"
        tri = _store_triage(run_dir, ctx, n, tri)
    elif need and not no_triage:
        tri = _run_triage(run_dir, ctx, n, cfg, summary, project)
    elif need:
        tri = _store_triage(run_dir, ctx, n, fallback_triage(summary, cfg, project, "--no-triage: "))
    else:
        rv = review_flag or ("deep" if model and "opus" in model else "standard")
        tri = _store_triage(run_dir, ctx, n, _user_triage(summary, qa_flag or ("browser" if url else "checks"), rv))
    d = _draft(tri)
    notes: list[str] = []
    if review_flag:
        d["tier"], d["review_reason"] = review_flag, f"--review {review_flag} (user flag)"
    elif model:
        d["tier"] = "deep" if "opus" in model else "standard"
        d["review_reason"] = f"--model {model} (user flag)"
    if qa_flag:
        d["scope"], d["qa_reason"] = qa_flag, f"--qa {qa_flag} (user flag)"
        if qa_flag == "none":
            d["skipped_by"] = "user"
    elif url:
        d["scope"], d["qa_reason"] = "browser", f"--url {url} (user flag)"
    elif not exploratory and _idx(SCOPES, d["scope"]) < _idx(SCOPES, "checks"):
        d["qa_reason"] = f"standalone QA floor checks (triage: {d['scope']} — {d['qa_reason']})"
        d["scope"] = "checks"
    _floors_flags(d, cfg, {}, ctx.get("transport") or "agent", notes)
    sflags = dict(ctx.get("flags") or {})
    for s in (ctx.get("standalone") or {}).get("roles") or []:
        if s in SPECIALISTS and sflags.get(s) not in ("on", "off"):
            sflags[s] = "on"
    _specialists(d, cfg, sflags, summary, notes)
    plan = _new_plan(ctx, n, "standalone", _roles(ctx, d, cfg, "standalone"), notes, d)
    apply_consent(ctx, plan, cfg, explicit=qa_flag == "browser" or bool(url))
    auth = finalize(plan, cfg, tri, ctx["repo"]["root"], model_override=model)
    save(run_dir, ctx, plan)
    return plan, auth


def _earlier_findings(run_dir: Path, role: str, n: int) -> bool:
    for k in range(1, n):
        j = util.read_json(Path(run_dir) / f"attempt-{k}" / "judge.json", {}) or {}
        if any(f.get("role") == role for f in j.get("findings") or []):
            return True
    return False


def _relevant(role: str, delta: dict, delta_code: bool, prev: dict) -> bool:
    """Does the delta touch anything a passed role should look at again? If not, its verdict is carried forward."""
    if role == "security":
        focus = " ".join(str(f) for f in prev.get("focus") or [])
        paths = [r.get("path") for r in delta.get("rows") or [] if r.get("path")]
        return bool(delta.get("security_files")) or any(p in focus for p in paths)
    if role == "perf":
        return bool(delta.get("source_files"))
    if role == "ux":
        return bool(delta.get("ui_files"))
    return delta_code


def build_rerun(run_dir: Path, ctx: dict, n: int, cfg: dict, delta: dict, project: dict) -> tuple[dict, dict | None]:
    """Attempt N ≥ 2 plan (§2.9.3, §2.9.4)."""
    from . import classify

    run_dir = Path(run_dir)
    prev_plan = util.read_json(run_dir / f"attempt-{n - 1}" / "plan.json", {}) or {}
    prev_judge = util.read_json(run_dir / f"attempt-{n - 1}" / "judge.json", {}) or {}
    prev_tri = util.read_json(run_dir / f"attempt-{n - 1}" / "triage.json", {}) or {}
    first = util.read_json(run_dir / "attempt-1" / "diff.numstat.json", {}) or {}
    summary = util.read_json(run_dir / f"attempt-{n}" / "diff.numstat.json", {}) or {}
    flags = ctx.get("flags") or {}
    notes: list[str] = []
    prev_roles = prev_plan.get("roles") or {}
    prev_scope = (prev_roles.get("qa") or {}).get("scope") or "none"
    prev_tier = (prev_roles.get("reviewer") or {}).get("tier") or "standard"
    material, mreason = classify.material_delta(delta, first, prev_plan, cfg)
    rule1 = bool(delta.get("ui_files")) and prev_scope != "browser"
    prev_d = _draft({"qa": {"scope": prev_scope, "reason": (prev_roles.get("qa") or {}).get("reason"),
                            "focus": (prev_roles.get("qa") or {}).get("focus") or [],
                            "needs_login": (prev_roles.get("qa") or {}).get("needs_login")},
                     "review": {"tier": prev_tier, "reason": (prev_roles.get("reviewer") or {}).get("reason")}})
    prev_on = {s: list(prev_roles[s].get("focus") or []) for s in SPECIALISTS if isinstance(prev_roles.get(s), dict)
               and (prev_roles[s].get("run") or prev_roles[s].get("carried_from") is not None)}
    if flags.get("qa") in SCOPES and flags.get("review") in TIERS:
        tri = _store_triage(run_dir, ctx, n, _user_triage(summary, flags["qa"], flags["review"]))
        source, d = "user", _draft(tri)
    elif material:
        if flags.get("no_triage"):
            tri = _store_triage(run_dir, ctx, n, fallback_triage(summary, cfg, project, "--no-triage: "))
        else:
            tri = _run_triage(run_dir, ctx, n, cfg, summary, project,
                              previous_plan="\n".join(role_lines(prev_plan)), delta=delta)
        source = "triage" if tri.get("source") == "triage" else "fallback"
        notes.append(f"re-triage: {mreason}")
        new = _draft(tri)
        d = copy.deepcopy(new)
        if _idx(TIERS, prev_d["tier"]) > _idx(TIERS, new["tier"]):
            d["tier"], d["review_reason"] = prev_d["tier"], prev_d["review_reason"]
        if _idx(SCOPES, prev_d["scope"]) > _idx(SCOPES, new["scope"]):
            d["scope"], d["qa_reason"], d["qa_focus"] = prev_d["scope"], prev_d["qa_reason"], prev_d["qa_focus"]
        d["needs_login"] = new["needs_login"] or prev_d["needs_login"]
    else:
        tri = copy.deepcopy(prev_tri) or fallback_triage(summary, cfg, project, "")
        tri.update({"source": "reused", "reused_from": n - 1})
        tri = _store_triage(run_dir, ctx, n, tri)
        source, d = "reused", prev_d
        notes.append(f"triage reused: {mreason}")
    _floors_flags(d, cfg, flags, ctx.get("transport") or "agent", notes)
    _specialists(d, cfg, flags, summary, notes, keep_on=prev_on)
    roles = _roles(ctx, d, cfg, source)
    prev_states = {r: (info or {}).get("state") for r, info in (prev_judge.get("roles") or {}).items()}
    delta_code = any("docs" not in (r.get("flags") or []) for r in delta.get("rows") or [])
    for role, pr in roles.items():
        prev = prev_states.get(role)
        if role == "qa" and pr["scope"] == "none":
            if source == "reused":  # the skip decision is the previous attempt's (e.g. prefilter), not triage's
                by = (((prev_judge.get("roles") or {}).get("qa") or {}).get("skipped_by")
                      or (prev_roles.get("qa") or {}).get("skipped_by"))
                if by:
                    pr["skipped_by"] = by
            continue
        if role == "qa" and prev == "skipped":
            if _idx(SCOPES, pr["scope"]) > _idx(SCOPES, prev_scope):
                pr["mode"] = "full"
            else:  # stays skipped unless re-triage raised the scope
                by = ((prev_judge.get("roles") or {}).get("qa") or {}).get("skipped_by")
                pr.update({"run": False, "carried_from": None, "skipped_by": by or SKIPPED_BY.get(source, "triage")})
            continue
        if prev == "fail":
            pr["mode"] = "recheck"
        elif prev == "pass":
            if _relevant(role, delta, delta_code, prev_roles.get(role) or {}):
                pr["mode"] = "delta"
            else:
                pp = prev_roles.get(role) or {}
                k = pp.get("carried_from") if not pp.get("run") and pp.get("carried_from") else n - 1
                pr.update({"run": False, "carried_from": int(k), "mode": pp.get("mode") or "full"})
        else:
            pr["mode"] = "recheck" if _earlier_findings(run_dir, role, n) else "full"
    plan = _new_plan(ctx, n, source, roles, notes, d)
    apply_consent(ctx, plan, cfg, rule1=rule1)
    auth = finalize(plan, cfg, tri, ctx["repo"]["root"])
    save(run_dir, ctx, plan)
    return plan, auth


# --------------------------------------------------------------------------- output (§2.18)


def header(tri: dict, cfg: dict) -> str:
    from . import metrics

    src = tri.get("source")
    ag = tri.get("agent") or {}
    if src == "triage":
        from . import platforms

        alias = (cfg.get("triage") or {}).get("model") or "sonnet"
        if platforms.detect().name == "codex":
            alias = ag.get("model") or "codex"
        return (f"triage {alias} ({util.short_dur(ag.get('duration_s') or 0)}, {metrics.money(ag.get('cost_usd'))}, "
                f"confidence {tri.get('confidence')})")
    if src == "prefilter":
        return "prefilter (no triage)"
    if src == "fallback":
        return f"triage unavailable ({ag.get('error') or '--no-triage'}) → fallback"
    if src == "reused":
        return "triage reused"
    return "user flags"


def role_lines(plan: dict) -> list[str]:
    out = []
    rv = (plan.get("roles") or {}).get("reviewer")
    if rv:
        if rv.get("run"):
            out.append(f"  review: {rv['mode']} · {rv['model']}/{rv['effort']} · {rv['tier']} — {rv.get('reason')}")
        elif rv.get("carried_from"):
            out.append(f"  review: carried from #{rv['carried_from']} (PASS)")
        else:
            out.append(f"  review: none — {rv.get('reason')}")
    for s in SPECIALISTS:
        sr = (plan.get("roles") or {}).get(s)
        if not sr:
            continue
        adv = " (advisory)" if s in ADVISORY_ROLES else ""
        if sr.get("run"):
            out.append(f"  {s}{adv}: {sr['mode']} · {sr['model']}/{sr['effort']} — {sr.get('reason')}")
        elif sr.get("carried_from"):
            out.append(f"  {s}{adv}: carried from #{sr['carried_from']} (PASS)")
    qa = (plan.get("roles") or {}).get("qa")
    if qa:
        if qa.get("run"):
            out.append(f"  qa: {qa['scope']} · {qa['mode']} · {qa['model']}/{qa['effort']} — {qa.get('reason')}")
        elif qa.get("carried_from"):
            out.append(f"  qa: carried from #{qa['carried_from']} (PASS)")
        else:
            out.append(f"  qa: none — {qa.get('reason')}")
    return out


def next_lines(run_dir: Path, ctx: dict, plan: dict, cfg: dict | None = None) -> list[str]:
    """What the main session does next. In-session roles print the transport's spawn lines; the others run through
    `vloop spawn <role>` (Bash, in the background). Mixed plans (in-session reviewer + headless browser QA) put both
    in one message, then `collect`."""
    from . import config, transports

    run_id = ctx["run_id"]
    n = plan["attempt"]
    roles = [r for r in ALL_ROLES if ((plan.get("roles") or {}).get(r) or {}).get("run")]
    if not roles:
        return [f"next: {_vl()} judge --run {run_id}"]
    cfg = cfg if cfg is not None else config.load(ctx["repo"]["root"])
    att = Path(run_dir) / f"attempt-{n}"
    by = {r: transports.for_role(ctx, cfg, r, plan["roles"][r]) for r in roles}
    labels: list[str] = []
    for r in roles:
        if by[r].label not in labels:
            labels.append(by[r].label)
    sctx = {**ctx, "_cfg": cfg}

    def spawn_lines(r):
        pr = plan["roles"][r]
        return by[r].spawn_plan(r, sctx, n, att / f"{r}.prompt.md", pr.get("model"), pr.get("effort"))
    inner = [r for r in roles if by[r].in_session]
    outer = [r for r in roles if not by[r].in_session]
    if not inner:
        return ([f"next ({' + '.join(labels)}, parallel — one Bash call per line in ONE message, "
                 f"run_in_background: true):"] + [f"  {ln}" for r in outer for ln in spawn_lines(r)])
    lines = [f"next ({' + '.join(labels)}, parallel — all calls in ONE message, run_in_background: true):"]
    lines += [f"  {ln}" for r in inner for ln in spawn_lines(r)]
    lines += [f"  Bash: {ln}" for r in outer for ln in spawn_lines(r)]
    lines.append(f"then: {_vl()} collect --run {run_id}")
    footer: list[str] = []
    for t in {id(by[r]): by[r] for r in inner}.values():
        footer += t.plan_footer([r for r in inner if by[r] is t])
    return lines + footer


def block(run_dir: Path, ctx: dict, plan: dict, cfg: dict, auth: dict | None, *, with_next: bool = True) -> list[str]:
    n = plan["attempt"]
    tri = util.read_json(Path(run_dir) / f"attempt-{n}" / "triage.json", {}) or {}
    lines = [f"verify-loop · plan · attempt {n} · {header(tri, cfg)}"] + role_lines(plan)
    qa = (plan.get("roles") or {}).get("qa") or {}
    if qa.get("run") and qa.get("scope") == "browser":
        a = auth or {}
        st = a.get("status") or qa.get("auth_status") or "missing"
        slug = ctx["repo"]["slug"]
        lines.append(f"  auth: {st} ({slug}, saved {a.get('age_days')} days ago)" if a.get("exists")
                     else f"  auth: {st} ({slug})")
        bc = ctx.get("browser_consent") or {}
        if bc.get("status") == "granted":
            if int(bc.get("attempt") or 0) < n:
                lines.append("  consent: browser QA approved earlier in this run")
            else:
                lines.append(f"  consent: browser QA approved ({bc.get('source')})")
    if plan.get("notes"):
        lines.append(f"  notes: {'; '.join(plan['notes'])}")
    if with_next and not consent_pending(ctx):
        lines += next_lines(run_dir, ctx, plan, cfg)
    return lines


def consent_block(run_dir: Path, ctx: dict, plan: dict, project: dict) -> list[str]:
    from . import prompts

    run_id = ctx["run_id"]
    qa = plan["roles"]["qa"]
    url = ctx.get("app_url") or project.get("app_url") or "not set — QA will start the app per recipes"
    checks = list(qa.get("focus") or [])[:5]
    if not checks:
        try:
            brief = (Path(run_dir) / "brief.md").read_text(encoding="utf-8")
        except OSError:
            brief = ""
        checks = prompts.brief_criteria(brief, 5)
    lines = [f"verify-loop · CONSENT NEEDED · browser QA (Playwright, headless Chrome) · run {run_id} · "
             f"attempt {plan['attempt']}",
             f"  why: {qa.get('reason')}",
             f"  url: {url}   auth: {qa.get('auth_status')}",
             "  checks:"]
    lines += [f"    - {c}" for c in checks] or ["    - (none listed)"]
    lines += ['next: ask the user with AskUserQuestion (header "Browser QA"):',
              f"  Run browser QA → {_vl()} consent allow --run {run_id}",
              f"  QA without browser (tests/run only) → {_vl()} consent no-browser --run {run_id}",
              f"  Skip QA for this change → {_vl()} consent skip-qa --run {run_id}"]
    return lines
