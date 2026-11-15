"""Change summary, prefilter, material delta, project facts, login-state status (§2.9, §2.14.5, §2.17)."""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

from . import config, util

QA_SCOPES = ("none", "checks", "run", "browser")
TIERS = ("standard", "deep")

WEB_DEPS = ("@angular/core", "react", "vue", "svelte", "next", "nuxt", "@ionic/angular", "vite")
SERVER_DEPS = ("express", "fastify", "@nestjs/core", "koa", "@hapi/hapi")
PY_SERVER = ("fastapi", "flask", "django")
TEST_DEPS = ("jest", "vitest", "@playwright/test", "cypress", "karma", "mocha")
MANIFESTS = ("package.json", "pyproject.toml", "setup.py", "setup.cfg", "Cargo.toml", "go.mod", "pom.xml",
             "build.gradle", "build.gradle.kts", "Gemfile", "composer.json")


# --------------------------------------------------------------------------- summary (§2.9.1)


def summarize(rows: list[dict], cfg: dict) -> dict:
    m = {k: util.path_matcher(cfg.get(f"{k}_globs") or []) for k in ("docs", "test", "config", "ignore", "ui", "risky",
                                                                     "security")}
    out = {"files": 0, "added": 0, "removed": 0, "changed_lines": 0, "rows": [],
           "ui_files": [], "risky_files": [], "security_files": [], "security_changed_lines": 0, "test_files": [], "config_files": [], "docs_files": [],
           "ignored_files": [], "new_files": [], "deleted_files": [], "renamed_files": [],
           "new_source_files": [], "source_files": [], "docs_only": False}
    for r in rows or []:
        path = r.get("path") or ""
        status = (r.get("status") or "M")[:1]
        flags = []
        is_docs, is_test, is_cfg, is_ign = m["docs"](path), m["test"](path), m["config"](path), m["ignore"](path)
        if is_docs:
            flags.append("docs")
        if is_test:
            flags.append("test")
        if is_cfg:
            flags.append("config")
        if is_ign:
            flags.append("ignored")
        if not is_ign and m["ui"](path):
            flags.append("ui")
        if not is_ign and m["risky"](path):
            flags.append("risky")
        if not is_ign and m["security"](path):
            flags.append("security")
        if status == "A":
            flags.append("new")
        elif status == "D":
            flags.append("deleted")
        elif status == "R":
            flags.append("renamed")
        source = not (is_docs or is_test or is_cfg or is_ign)
        if source:
            flags.append("source")
        added, removed, binary = int(r.get("added") or 0), int(r.get("removed") or 0), bool(r.get("binary"))
        out["rows"].append({"path": path, "old_path": r.get("old_path"), "status": status, "added": added,
                            "removed": removed, "binary": binary, "flags": flags})
        out["files"] += 1
        if not is_ign and not binary:
            out["added"] += added
            out["removed"] += removed
        if "security" in flags and not (is_docs or is_test or binary):
            out["security_changed_lines"] += added + removed
        for flag, key in (("ui", "ui_files"), ("risky", "risky_files"), ("security", "security_files"),
                          ("test", "test_files"),
                          ("config", "config_files"), ("docs", "docs_files"), ("ignored", "ignored_files"),
                          ("new", "new_files"), ("deleted", "deleted_files"), ("renamed", "renamed_files"),
                          ("source", "source_files")):
            if flag in flags:
                out[key].append(path)
        if source and status == "A":
            out["new_source_files"].append(path)
    out["changed_lines"] = out["added"] + out["removed"]
    out["docs_only"] = out["files"] > 0 and all("docs" in r["flags"] for r in out["rows"])
    return out


# --------------------------------------------------------------------------- prefilter (§2.9.2)


def prefilter(summary: dict, whitespace_only: bool, cfg: dict) -> dict:
    pf = cfg.get("prefilter") or {}
    if not summary.get("files"):
        return {"decision": "skip", "reason": "no changes vs baseline (reverted)"}
    if summary.get("docs_only"):
        paths = [r["path"] for r in summary.get("rows") or []]
        return {"decision": "skip", "reason": f"docs-only change ({util.fmt_paths(paths)})"}
    if whitespace_only:
        return {"decision": "skip", "reason": "whitespace-only change"}
    lines, files = int(summary.get("changed_lines") or 0), int(summary.get("files") or 0)
    if (lines <= int(pf.get("trivial_max_lines", 6)) and files <= int(pf.get("trivial_max_files", 2))
            and not any(summary.get(k) for k in ("new_files", "deleted_files", "renamed_files", "ui_files",
                                                  "risky_files", "config_files", "test_files"))):
        return {"decision": "trivial", "reason": f"trivial change: {lines} lines in {files} file(s)"}
    return {"decision": "triage", "reason": "triage"}


def docs_only_paths(paths: list[str], cfg: dict) -> bool:
    match = util.path_matcher(cfg.get("docs_globs") or [])
    return bool(paths) and all(match(p) for p in paths)


# --------------------------------------------------------------------------- material delta (§2.9.3)


def material_delta(delta_summary: dict, first_summary: dict, prev_plan: dict, cfg: dict) -> tuple[bool, str]:
    roles = (prev_plan or {}).get("roles") or {}
    prev_scope = (roles.get("qa") or {}).get("scope") or "none"
    prev_tier = (roles.get("reviewer") or {}).get("tier") or "standard"
    md = cfg.get("material_delta") or {}
    reasons = []
    if delta_summary.get("ui_files") and prev_scope != "browser":
        reasons.append(f"delta touches UI files ({util.fmt_paths(delta_summary['ui_files'])}) "
                       f"and previous QA scope was {prev_scope}")
    if delta_summary.get("risky_files") and prev_tier != "deep":
        reasons.append(f"delta touches risky files ({util.fmt_paths(delta_summary['risky_files'])}) "
                       f"and previous review tier was {prev_tier}")
    sec = roles.get("security") or {}
    sec_ran = sec.get("run") or sec.get("carried_from")
    if delta_summary.get("security_files") and "specialists" in (prev_plan or {}) and not sec_ran:
        reasons.append(f"delta touches security-sensitive files ({util.fmt_paths(delta_summary['security_files'])}) "
                       f"and the security specialist did not run")
    if delta_summary.get("new_source_files"):
        reasons.append(f"delta adds source files ({util.fmt_paths(delta_summary['new_source_files'])})")
    threshold = max(float(md.get("min_lines", 80)),
                    float(md.get("ratio", 0.5)) * float((first_summary or {}).get("changed_lines") or 0))
    lines = int(delta_summary.get("changed_lines") or 0)
    if lines > threshold:
        reasons.append(f"delta changes {lines} lines (> {int(threshold)})")
    if reasons:
        return True, "; ".join(reasons)
    return False, (f"delta {lines} lines in {int(delta_summary.get('files') or 0)} file(s), "
                   f"no new UI/risky/security/source files")


# --------------------------------------------------------------------------- project facts (§2.14.5)


def _read_text(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _py_manifests(root: Path) -> str:
    texts = [_read_text(root / "pyproject.toml")]
    texts += [_read_text(p) for p in sorted(root.glob("requirements*.txt"))]
    return "\n".join(texts).lower()


def _word_in(word: str, text: str) -> bool:
    return re.search(r"(?<![A-Za-z0-9_-])" + re.escape(word) + r"(?![A-Za-z0-9_-])", text) is not None


def detect_project(repo_root: str, cfg: dict) -> dict:
    root = Path(repo_root)
    rc = config.repo_config(root)
    app = {**((cfg or {}).get("app") or {}), **(rc.get("app") or {})}
    auth = {**({k: v for k, v in ((cfg or {}).get("auth") or {}).items() if k != "max_age_days"}),
            **(rc.get("auth") or {})}
    cmds_src = {**((cfg or {}).get("commands") or {}), **(rc.get("commands") or {})}
    pkg = util.read_json(root / "package.json", None)
    pkg = pkg if isinstance(pkg, dict) else None
    deps: dict = {}
    if pkg:
        for k in ("dependencies", "devDependencies", "peerDependencies"):
            if isinstance(pkg.get(k), dict):
                deps.update(pkg[k])
    scripts = pkg.get("scripts") if pkg and isinstance(pkg.get("scripts"), dict) else {}
    py = _py_manifests(root)

    if any(d in deps for d in WEB_DEPS) or any((root / d / "index.html").is_file() for d in ("src", "public", ".")):
        kind = "web"
    elif any(d in deps for d in SERVER_DEPS) or any(_word_in(w, py) for w in PY_SERVER):
        kind = "server"
    elif pkg and pkg.get("bin"):
        kind = "cli"
    elif any((root / m).is_file() for m in MANIFESTS) or list(root.glob("requirements*.txt")):
        kind = "library"
    else:
        kind = "unknown"

    frameworks = [d for d in TEST_DEPS if d in deps]
    if _word_in("pytest", py):
        frameworks.append("pytest")
    if any("node --test" in str(v) for v in scripts.values()):
        frameworks.append("node:test")

    recipes = sorted(str(p.relative_to(root)) for p in root.glob(".claude/skills/run-*/SKILL.md"))
    if (root / ".claude/skills/verify/SKILL.md").is_file():
        recipes.append(".claude/skills/verify/SKILL.md")

    def pick(d, k, default):
        v = d.get(k)
        return default if v is None else v

    storage = auth.get("storage_state") or str(util.state_dir() / "auth" / f"{util.slugify(root.name)}.json")
    return {
        "app_kind": kind,
        "app_url": app.get("url") or None,
        "start_cmd": app.get("start") or None,
        "reuse_running": bool(pick(app, "reuse_running", True)),
        "ignore_https_errors": bool(pick(app, "ignore_https_errors", True)),
        "start_timeout_s": int(pick(app, "start_timeout_s", 240)),
        "commands": {k: cmds_src.get(k) or None for k in ("typecheck", "lint", "test", "build")},
        "scripts": list(scripts.keys())[:20],
        "recipes": recipes,
        "test_frameworks": frameworks,
        "has_verify_json": bool(config.repo_config_files(root)),
        "auth": {"login_url": auth.get("login_url") or None, "required_for": list(auth.get("required_for") or []),
                 "logged_in_check": auth.get("logged_in_check") or None,
                 "storage_state": str(Path(storage).expanduser())},
        "notes": rc.get("notes") or (cfg or {}).get("notes") or "",
    }


# --------------------------------------------------------------------------- login state (§2.17)


def auth_status(repo_root: str, cfg: dict) -> dict:
    rc = config.repo_config(repo_root)
    storage = ((rc.get("auth") or {}).get("storage_state") or ((cfg or {}).get("auth") or {}).get("storage_state")
               or str(util.state_dir() / "auth" / f"{util.slugify(os.path.basename(str(repo_root).rstrip('/')))}.json"))
    path = Path(storage).expanduser()
    out = {"path": str(path), "exists": path.is_file(), "saved_at": None, "age_days": None, "cookies": 0,
           "origins": 0, "earliest_expiry": None, "status": "missing"}
    if not out["exists"]:
        return out
    mtime = path.stat().st_mtime
    out["saved_at"] = datetime.fromtimestamp(mtime, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    out["age_days"] = int((time.time() - mtime) // 86400)
    data = util.read_json(path)
    if not isinstance(data, dict):
        return out
    cookies = [c for c in data.get("cookies") or [] if isinstance(c, dict)]
    origins = [o for o in data.get("origins") or [] if isinstance(o, dict) and o.get("localStorage")]
    out["cookies"] = len(cookies)
    out["origins"] = len(origins)
    exps = []
    for c in cookies:
        try:
            e = float(c.get("expires") or 0)
        except (TypeError, ValueError):
            e = 0
        if e > 0:
            exps.append(e)
    if exps:
        out["earliest_expiry"] = datetime.fromtimestamp(min(exps), timezone.utc).strftime("%Y-%m-%d")
    max_age = int(((cfg or {}).get("auth") or {}).get("max_age_days") or 25)
    now = time.time()
    if exps and all(e < now for e in exps) and not origins:
        out["status"] = "expired"
    elif out["age_days"] > max_age:
        out["status"] = "stale"
    else:
        out["status"] = "ok"
    return out


def dumps(obj) -> str:
    return json.dumps(obj, indent=2, ensure_ascii=False)
