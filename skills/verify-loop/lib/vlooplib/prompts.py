"""Task-file rendering for triage, reviewer and QA (§2.15.1). Templates: SKILL_DIR/prompts/<role>.md."""
from __future__ import annotations

import json
import re
from pathlib import Path

from . import config, paths, util
from .integrations import vault
from .roles import ADVISORY_ROLES, PREFIX, REVIEW_ROLES, SPECIALISTS, SUGGESTION_PREFIX

TEMPLATES_DIR: Path | None = None  # tests point this at their own minimal templates
MAX_ROWS = 80
PROMPTS_MAX = 6000
NO_PROMPTS = "(no prompts recorded)"
BASELINE_NOTE = ("Baseline is HEAD (head-fallback): the diff may include changes that existed before this task — "
                 "judge only what the user's request covers.")
DECLINED_NOTE = ("No browser in this scope: the user declined browser QA. List every criterion whose natural check is "
                 "in the browser under unverified with need \"env\" and detail \"user declined browser QA\".")
NO_DIFF_NOTE = ("No code diff: this is an exploratory check of the running app against the focus and the "
                "acceptance criteria. There is nothing to diff; plan checks from the focus/criteria only.")
SCREENSHOT_NOTE = ("IMPORTANT — screenshots: call `mcp__playwright__browser_take_screenshot` with NO `filename` "
                   "argument, exactly `{}` (or `{\"fullPage\": true}`). Never pass `filename` to any screenshot, "
                   "snapshot or PDF tool: files are saved automatically to <artifacts_dir> and the tool result gives "
                   "the path; quote that path as evidence.")


OPTIONAL_BLOCK = re.compile(r"[ \t]*<!--\s*if\s+([A-Za-z0-9_.-]+)\s*-->\n?(.*?)[ \t]*<!--\s*endif\s*-->\n?", re.S)


def template_path(name: str) -> Path:
    return (TEMPLATES_DIR or paths.prompts_dir()) / f"{name}.md"


def tool_values() -> dict[str, str]:
    """Absolute paths of the CLIs a task file points the agent at; the vault ones are empty without agent-vault."""
    found = vault.discover()
    return {"vloop": str(paths.cli()), "vault_cli": str(found.cli) if found else "",
            "vault_ops_retro": str(found.ops_retro) if found else ""}


def render(text: str, values: dict[str, str]) -> str:
    """Fills `{{name}}`; a block wrapped in `<!-- if name -->…<!-- endif -->` is dropped when `name` is empty."""
    def keep(m):
        return m.group(2) if str(values.get(m.group(1)) or "").strip() else ""

    return util.render_template(OPTIONAL_BLOCK.sub(keep, text), values)


def _read(path: Path) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError:
        return ""


# --------------------------------------------------------------------------- shared values


def prompt_blocks(prompts: list[dict]) -> str:
    """`### Prompt <i> (<HH:MM>)` blocks, the content of prompts.md."""
    blocks = [f"### Prompt {i} ({util.local_hhmm(p.get('ts'))})\n{p.get('text') or ''}"
              for i, p in enumerate(prompts or [], 1)]
    return "\n\n".join(blocks) if blocks else NO_PROMPTS


def capped_prompts(text: str, limit: int = PROMPTS_MAX) -> str:
    """Newest prompts kept: cut whole blocks from the front, then the head of the oldest remaining one."""
    if len(text) <= limit:
        return text
    parts = re.split(r"(?m)^(?=### Prompt \d+ )", text)
    while len(parts) > 1 and sum(len(p) for p in parts) > limit:
        parts.pop(0)
    out = "".join(parts)
    if len(out) > limit:
        out = "[…]" + out[-(limit - 3):]
    return out


def _cell(r: dict) -> str:
    return "binary" if r.get("binary") else f"+{r.get('added', 0)}/−{r.get('removed', 0)}"


def summary_line(summary: dict, base_kind: str | None, base_at: str | None) -> str:
    return (f"{summary.get('files', 0)} files, +{summary.get('added', 0)}/−{summary.get('removed', 0)} "
            f"({summary.get('changed_lines', 0)} changed lines excl. ignored) · baseline: {base_kind or '?'} @ "
            f"{util.local_hhmm(base_at)}")


def diff_summary(summary: dict, base_kind: str | None, base_at: str | None) -> str:
    rows = summary.get("rows") or []
    lines = [summary_line(summary, base_kind, base_at), "", "| status | path | +/− | flags |", "|---|---|---|---|"]
    for r in rows[:MAX_ROWS]:
        path = r["path"] if not r.get("old_path") or r.get("old_path") == r["path"] else f"{r['old_path']} → {r['path']}"
        lines.append(f"| {r.get('status', 'M')} | {path} | {_cell(r)} | {', '.join(r.get('flags') or [])} |")
    if len(rows) > MAX_ROWS:
        lines.append(f"… +{len(rows) - MAX_ROWS} more")
    return "\n".join(lines)


def delta_stat(delta: dict) -> str:
    lines = [f"{delta.get('files', 0)} files, +{delta.get('added', 0)}/−{delta.get('removed', 0)} "
             f"({delta.get('changed_lines', 0)} changed lines excl. ignored) since the previous attempt"]
    for r in (delta.get("rows") or [])[:MAX_ROWS]:
        lines.append(f"- {r.get('status', 'M')} {r['path']} {_cell(r)} [{', '.join(r.get('flags') or [])}]")
    return "\n".join(lines)


def changed_files(summary: dict) -> str:
    rows = summary.get("rows") or []
    lines = [f"- {r['path']} [{', '.join(r.get('flags') or [])}]" for r in rows[:MAX_ROWS]]
    if len(rows) > MAX_ROWS:
        lines.append(f"… +{len(rows) - MAX_ROWS} more")
    return "\n".join(lines)


def signals(summary: dict) -> str:
    vals = [("files", summary.get("files", 0)), ("added", summary.get("added", 0)),
            ("removed", summary.get("removed", 0)), ("changed_lines", summary.get("changed_lines", 0))]
    for k, key in (("new", "new_files"), ("deleted", "deleted_files"), ("renamed", "renamed_files"),
                   ("ui", "ui_files"), ("risky", "risky_files"), ("security", "security_files"),
                   ("test", "test_files"), ("config", "config_files"),
                   ("docs", "docs_files"), ("new_source", "new_source_files")):
        vals.append((k, len(summary.get(key) or [])))
    return "\n".join(f"{k}: {v}" for k, v in vals)


HINTS_MAX = 8
NO_HINTS = "(none found)"


def security_files_text(summary: dict) -> str:
    files = summary.get("security_files") or []
    if not files:
        return "(none)"
    by_path = {r["path"]: r for r in summary.get("rows") or []}
    lines = [f"- {p} {_cell(by_path.get(p) or {})}" for p in files[:MAX_ROWS]]
    if len(files) > MAX_ROWS:
        lines.append(f"… +{len(files) - MAX_ROWS} more")
    lines.append(f"changed lines in these files (excl. docs/tests): {int(summary.get('security_changed_lines') or 0)}")
    return "\n".join(lines)


_UI_REF = re.compile(r"(?i)\b(figma|mock-?ups?|wireframes?|screenshots?|zeplin|design spec|макет\w*|скріншот\w*|"
                     r"дизайн\w*)\b|як на|\blike (?:the|in|on)\b")
_FIGMA_URL = re.compile(r"https?://\S*figma\.com\S*", re.I)
_IMAGE_PATH = re.compile(r"(?<![\w/])[\w./~-]*[\w-]\.(?:png|jpe?g|gif|webp|svg)\b", re.I)
_HEX = re.compile(r"(?<![\w&/])#([0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{3,4})\b")
_SIZE = re.compile(r"(?<![\w.])\d+(?:\.\d+)?(?:px|rem|em|vh|vw)\b")
_UI_COPY = re.compile(r"(?i)\b(?:button|label|title|placeholder|heading|tooltip|caption|message|text|link|tab|"
                      r"кнопк\w*|напис\w*|текст\w*|заголов\w*|підказк\w*)\b[^\"\n“«]{0,30}[\"“«]([^\"”»\n]{1,60})[\"”»]")
_UI_WORDS = re.compile(r"(?i)\b(button|click|modal|dialog|page|screen|layout|colou?r|font|align\w*|spacing|margin|"
                       r"padding|responsive|mobile|hover|focus|icon|tooltip|dropdown|menu|visible|shows?|"
                       r"displays?|renders?|кнопк\w*|сторінк\w*|екран\w*|модал\w*|колір|шрифт\w*|відображ\w*)\b")


def _uniq(items) -> list[str]:
    out: list[str] = []
    for x in items:
        if x not in out:
            out.append(x)
    return out


def ui_spec_hints(prompts_text: str, brief: str) -> str:
    """Mechanical evidence of a UI spec in the request and brief: design references, image paths, colours, sizes,
    quoted UI copy, UI-worded acceptance criteria."""
    text = f"{prompts_text or ''}\n{brief or ''}"
    found: list[str] = []
    refs = _uniq(m.group(0).lower() for m in _UI_REF.finditer(text))
    if refs:
        found.append("design references: " + ", ".join(refs[:6]))
    urls = _uniq(_FIGMA_URL.findall(text))
    if urls:
        found.append("figma links: " + ", ".join(urls[:3]))
    imgs = _uniq(_IMAGE_PATH.findall(text))
    if imgs:
        found.append("image paths: " + ", ".join(imgs[:5]))
    hexes = _uniq("#" + h for h in _HEX.findall(text) if not h.isdigit())
    if hexes:
        found.append("colours: " + ", ".join(hexes[:8]))
    sizes = _uniq(_SIZE.findall(text))
    if sizes:
        found.append("sizes: " + ", ".join(sizes[:8]))
    copy_ = _uniq(f'"{c.strip()}"' for c in _UI_COPY.findall(text) if c.strip())
    if copy_:
        found.append("quoted UI copy: " + ", ".join(copy_[:6]))
    crit = _uniq(ln.strip()[:120] for ln in (brief or "").splitlines()
                 if re.match(r"\s*(?:[-*]|\d+[.)])\s+", ln) and _UI_WORDS.search(ln))
    if crit:
        found += ["UI-worded criterion: " + c.lstrip("-*0123456789.) ") for c in crit[:5]]
    return "\n".join(f"- {x}" for x in found) if found else "(none found: no design reference in the request or brief)"


_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")
_LOOP = re.compile(r"(?:^|[^\w.])(?:for|while)\b|\.(?:forEach|map|flatMap|filter|reduce|some|every)\(|"
                   r"\bfor\s+\w+(?:\s*,\s*\w+)*\s+in\b")
_IO_CALL = re.compile(r"\bawait\b|\bfetch\(|\baxios\.|\.query\(|\.execute\(|\.find(?:One|Many|All|Unique)?\(|"
                      r"\.save\(|\.objects\.\w+\(|\brequests\.(?:get|post|put|delete)\(|\bhttp\.(?:get|request)\(|"
                      r"\.findBy\w*\(|\bcursor\.execute\(")
_SYNC_IO = re.compile(r"\b(?:readFileSync|writeFileSync|appendFileSync|readdirSync|statSync|existsSync|execSync|"
                      r"spawnSync)\b|\btime\.sleep\(|\bsubprocess\.(?:run|call|check_output)\(")
_HEAVY_IMPORT = re.compile(r"^\s*import\s+(?:\*\s+as\s+\w+|[\w{},\s]+)\s+from\s+[\'\"](lodash|moment|rxjs|"
                           r"@mui/material|@mui/icons-material|antd|chart\.js|three|d3|firebase|aws-sdk|@aws-sdk/[\w-]+)"
                           r"[\'\"]")


def _indent(s: str) -> int:
    return len(s) - len(s.lstrip(" \t"))


def perf_hints(patch: str, summary: dict) -> str:
    """Regex hints over added lines: IO/await in loops, nested loops, sync IO, heavy client imports."""
    ui = set(summary.get("ui_files") or [])
    rows = {r["path"]: r for r in summary.get("rows") or []}
    hints: list[str] = []
    for path, lines in _patch_sections(patch or ""):
        flags = (rows.get(path) or {}).get("flags") or []
        if "ignored" in flags or "test" in flags or "docs" in flags:
            continue
        lineno, window = 0, []
        for ln in lines:
            m = _HUNK.match(ln)
            if m:
                lineno, window = int(m.group(1)), []
                continue
            if not lineno or ln.startswith("\\"):
                continue
            if ln.startswith("-"):
                continue
            code = ln[1:]
            added = ln.startswith("+")
            ind = _indent(code)
            outer_loops = [w for w in window if _LOOP.search(w[1]) and w[0] < ind]
            if added and code.strip():
                where = f"{path}:{lineno}"
                if _IO_CALL.search(code) and outer_loops:
                    hints.append(f"{where} IO/await inside a loop: {code.strip()[:80]}")
                elif _LOOP.search(code) and outer_loops:
                    hints.append(f"{where} nested loop: {code.strip()[:80]}")
                if _SYNC_IO.search(code):
                    hints.append(f"{where} synchronous IO: {code.strip()[:80]}")
                if (path in ui or path.endswith((".tsx", ".jsx", ".vue", ".svelte"))) and _HEAVY_IMPORT.search(code):
                    hints.append(f"{where} large client import: {code.strip()[:80]}")
            if code.strip():
                window = (window + [(ind, code)])[-8:]
            lineno += 1
            if len(hints) >= HINTS_MAX:
                break
        if len(hints) >= HINTS_MAX:
            break
    hints = _uniq(hints)[:HINTS_MAX]
    return "\n".join(f"- {h}" for h in hints) if hints else NO_HINTS


def calibration_text(project: str | None, cfg: dict) -> str:
    from . import metrics

    try:
        return metrics.triage_calibration(project, last=int((cfg.get("triage") or {}).get("calibration_runs", 30)))
    except Exception as e:  # noqa: BLE001 - calibration is a hint; triage must not fail on a bad ledger
        return f"(unavailable: {type(e).__name__})"


def _patch_sections(patch: str) -> list[tuple[str, list[str]]]:
    out: list[tuple[str, list[str]]] = []
    cur: list[str] | None = None
    for ln in patch.splitlines():
        if ln.startswith("diff --git "):
            cur = [ln]
            out.append(("", cur))
        elif cur is not None:
            cur.append(ln)
    named = []
    for _, lines in out:
        path = ""
        for ln in lines[:8]:
            if ln.startswith("+++ ") and ln[4:] != "/dev/null":
                path = ln[4:].strip('"')
                path = path[2:] if path.startswith("b/") else path
                break
            if ln.startswith("--- ") and ln[4:] != "/dev/null":
                p = ln[4:].strip('"')
                path = p[2:] if p.startswith("a/") else p
        if not path:
            m = re.match(r"diff --git a/(.*) b/(.*)$", lines[0])
            path = m.group(2) if m else ""
        named.append((path, lines))
    return named


def diff_excerpt(patch: str, summary: dict, max_lines: int = 400, max_chars: int = 24000) -> str:
    """diff.patch without ignored/binary hunks, source files first, then others, then tests; capped (§2.8)."""
    flags = {r["path"]: r for r in summary.get("rows") or []}

    def rank(path: str) -> int:
        f = (flags.get(path) or {}).get("flags") or []
        if "source" in f:
            return 0
        if "test" in f:
            return 2
        return 1

    keep = []
    for i, (path, lines) in enumerate(_patch_sections(patch)):
        row = flags.get(path) or {}
        if "ignored" in (row.get("flags") or []) or row.get("binary") or any(
                ln.startswith("Binary files ") or ln == "GIT binary patch" for ln in lines[:10]):
            continue
        keep.append((rank(path), i, lines))
    keep.sort()
    all_lines = [ln for _, _, lines in keep for ln in lines]
    out, chars = [], 0
    for ln in all_lines:
        if len(out) >= max_lines or chars + len(ln) + 1 > max_chars:
            break
        out.append(ln)
        chars += len(ln) + 1
    if len(out) < len(all_lines):
        out.append(f"[excerpt truncated: {len(all_lines) - len(out)} more lines]")
    return "\n".join(out)


def auth_text(scope: str, auth: dict | None, slug: str, login_url: str | None) -> str:
    if scope != "browser" or not auth:
        return "not needed for this scope"
    st, d = auth.get("status"), auth.get("age_days")
    if st == "ok":
        text = f"ok — the browser loads saved login state for {slug} (saved {d} days ago) automatically"
    elif st == "stale":
        text = (f"stale — saved {d} days ago; it may be expired; if you land on the login page mark checks blocked "
                f"with need auth_expired")
    elif st == "expired":
        text = "expired — saved cookies expired; mark logged-in browser checks blocked with need auth_expired"
    else:
        text = "missing — no saved login state; mark logged-in browser checks blocked with need auth_missing"
    return f"{text}\nRefresh: ! {util.vloop_cli()} auth capture --url {login_url or '<login url>'}"


def lessons(repo_root: str, audience: str, limit: int) -> str:
    return vault.lessons_bullets(repo_root, audience, limit)


def vault_notes(repo_root: str, paths: list[str]) -> str:
    return vault.covering(repo_root, paths, 8)


def _repo_config_text(repo_root: str) -> str:
    files = config.repo_config_files(repo_root)
    if len(files) == 1:
        return _read(files[0])
    return "\n".join(f"{p.relative_to(Path(repo_root))}:\n{_read(p)}" for p in files)


def claude_md_files(repo_root: str, paths: list[str]) -> str:
    root = Path(repo_root)
    found: list[str] = []
    cands = ["CLAUDE.md", ".claude/CLAUDE.md", "AGENTS.md"]
    for p in paths:
        parts = Path(p).parts[:-1]
        for i in range(1, len(parts) + 1):
            cands += [str(Path(*parts[:i]) / "CLAUDE.md"), str(Path(*parts[:i]) / "AGENTS.md")]
    for c in cands:
        if c not in found and (root / c).is_file():
            found.append(c)
    return "\n".join(found)


def previous_findings(judge: dict | None, role: str | None = None) -> str:
    """Open findings of the previous judge. A specialist sees only its own; the reviewer and QA see theirs."""
    keep = None if role is None else {role} if role in SPECIALISTS else {"reviewer", "qa"}
    out = []
    for f in (judge or {}).get("findings") or []:
        if f.get("waived"):
            continue
        if keep is not None and (f.get("role") or "reviewer") not in keep:
            continue
        anchor = (f"{f.get('file')}:{f.get('line')}" if f.get("file") and f.get("line") else f.get("file")) or (
            f"check {f['check_id']}" if f.get("check_id") else "(no anchor)")
        out.append(f"- {f.get('id')} [{f.get('severity')}·{f.get('category')}·"
                   f"{'blocking' if f.get('blocking') else 'non-blocking'}] {anchor} — {f.get('title')}")
        if f.get("problem"):
            out.append(f"  problem: {f['problem']}")
        if f.get("scenario"):
            out.append(f"  scenario: {f['scenario']}")
        if f.get("expected") or f.get("actual"):
            out.append(f"  expected/actual: {f.get('expected') or '?'} / {f.get('actual') or '?'}")
    return "\n".join(out)


def focus_lines(items: list[str], extra: str | None = None) -> str:
    lines = [f"- {x}" for x in items or [] if x]
    if extra:
        lines += [f"- {x.strip()}" for x in extra.splitlines() if x.strip()]
    return "\n".join(lines)


# --------------------------------------------------------------------------- triage input (§2.8)


def render_triage(*, run_dir: Path, ctx: dict, attempt: int, summary: dict, cfg: dict, project: dict,
                  auth: dict | None, previous_plan: str = "", delta: dict | None = None) -> str:
    att = Path(run_dir) / f"attempt-{attempt}"
    t = cfg.get("triage") or {}
    a = auth or {}
    auth_line = a.get("status") or "missing"
    if a.get("exists"):
        auth_line += f" (saved {a.get('age_days')} days ago)"
    prompts_text = _read(Path(run_dir) / "prompts.md")
    brief = _read(Path(run_dir) / "brief.md")
    patch = _read(att / "diff.patch")
    values = {
        "run_id": ctx["run_id"], "attempt": str(attempt), "repo_slug": ctx["repo"]["slug"],
        "user_prompts": capped_prompts(prompts_text), "brief": brief,
        "security_files": security_files_text(summary), "ui_spec_hints": ui_spec_hints(prompts_text, brief),
        "perf_hints": perf_hints(patch, summary), "calibration": calibration_text(ctx["repo"].get("slug"), cfg),
        "diff_stat": diff_summary(summary, ctx["target"].get("base_kind"), ctx["target"].get("base_at")),
        "changed_files": changed_files(summary), "signals": signals(summary),
        "project_facts": json.dumps(project, indent=2, ensure_ascii=False), "auth_status": auth_line,
        "diff_excerpt": diff_excerpt(patch, summary, int(t.get("diff_excerpt_max_lines", 900)),
                                     int(t.get("diff_excerpt_max_chars", 60000))),
        "previous_plan": previous_plan, "delta_stat": delta_stat(delta) if delta else "",
    }
    text = render(_read(template_path("triage")), {**tool_values(), **values})
    util.atomic_write_text(att / "triage.input.md", text)
    return text


# --------------------------------------------------------------------------- reviewer / QA task files


def _outside(ctx: dict, summary: dict) -> str:
    if ctx["target"].get("spec") != "session" or not ctx.get("session_id"):
        return ""
    from . import state

    st = state.load_session(ctx["session_id"]) or {}
    rs = (st.get("repos") or {}).get(ctx["repo"]["key"]) or {}
    touched = set(rs.get("touched") or [])
    return "\n".join(f"- {r['path']}" for r in summary.get("rows") or [] if r["path"] not in touched)


SPECIALIST_NOTES = {
    "security": "A security specialist reviews this change in parallel. Skip §3.5 (Security) of your checklist: "
                "raise a security problem only when it is also a correctness or data bug you traced yourself.",
    "perf": "A performance specialist reviews this change in parallel. Skip §3.7 (Performance traps).",
    "ux": "A UX specialist reviews this change in parallel. Leave accessibility, UI states, visual consistency and "
          "UI copy to it; still block on UI logic that is functionally broken.",
}

CHARTERS = {
    "security": ("Security of the change: injection (SQL, shell, template, path), authentication and authorization "
                 "bypass, session and token handling, secrets in code or logs, crypto misuse, unsafe "
                 "deserialization, SSRF, XSS, open redirects, permissive CORS or CSP, CI workflow injection, "
                 "dependency or permission changes. Trace untrusted input from its entry point to the sink."),
    "perf": ("Performance of the change on hot paths: N+1 queries, IO or await inside loops, unbounded loops or "
             "result sets, quadratic work on growing data, synchronous IO in request handlers, missing pagination "
             "or caching where the code already caches, memory growth, heavy client bundles and re-render storms. "
             "Advisory only: your findings never block."),
    "ux": ("User experience of the UI change, read from the code (no browser): labels, alt text and aria, focus "
           "order and keyboard access, loading / empty / error / disabled states, consistency with sibling "
           "components and design tokens, responsive CSS, and UI copy. Advisory only: your findings never block."),
}


def specialist_note(running: list[str] | None) -> str:
    return "\n".join(f"- {SPECIALIST_NOTES[r]}" for r in SPECIALISTS if r in (running or []))


def _charter_focus(role: str, spec: dict) -> str:
    lines = [CHARTERS[role]]
    if role == "ux" and spec.get("ui_spec") not in (None, "", "n/a"):
        lines.append(f"UI spec in the request or brief (triage): {spec['ui_spec']}.")
    if spec.get("reason"):
        lines.append(f"Why triage turned you on: {spec['reason']}")
    return "\n\n".join(lines)


def render_role(role: str, *, run_dir: Path, ctx: dict, attempt: int, plan: dict, cfg: dict, project: dict,
                auth: dict | None = None, specialists: list[str] | None = None) -> Path:
    run_dir = Path(run_dir)
    att = run_dir / f"attempt-{attempt}"
    pr = plan["roles"][role]
    summary = util.read_json(att / "diff.numstat.json", {}) or {}
    triage = util.read_json(att / "triage.json", {}) or {}
    prev_judge = util.read_json(run_dir / f"attempt-{attempt - 1}" / "judge.json") if attempt >= 2 else None
    repo = ctx["repo"]
    tgt = ctx["target"]
    code_ref = tgt.get("code_ref") or "worktree"
    code_note = ("Code under review is the working tree." if code_ref == "worktree" else
                 f"Review the code at commit {code_ref}: read files with git show {code_ref}:<path> "
                 f"(the working tree may differ).")
    timeout = int(pr.get("timeout_s") or (cfg.get("timeouts_s") or {}).get(role, 1200))
    standalone = ctx.get("standalone") or {}
    spec = ((triage.get("specialists") or {}).get(role) or {}) if role in SPECIALISTS else {}
    tri_part = spec if role in SPECIALISTS else triage.get("review" if role == "reviewer" else "qa") or {}
    focus_items = pr.get("focus") if role == "qa" and pr.get("focus") else tri_part.get("focus") or []
    source_paths = list(summary.get("source_files") or [])
    all_paths = [r["path"] for r in summary.get("rows") or []]
    values = {
        "run_id": ctx["run_id"], "attempt": str(attempt), "mode": pr.get("mode") or "full",
        "repo_root": repo["root"], "repo_slug": repo["slug"], "branch": repo.get("branch") or "(detached HEAD)",
        "code_ref_note": code_note, "run_dir": str(run_dir), "attempt_dir": str(att),
        "verdict_path": str(att / f"{role}.json"), "time_budget_min": str(max(1, int(timeout / 60 - 5))),
        "user_prompts": _read(run_dir / "prompts.md"), "brief": _read(run_dir / "brief.md"),
        "focus": focus_lines(focus_items, standalone.get("focus")),
        "diff_summary": (NO_DIFF_NOTE if standalone.get("exploratory") else
                         diff_summary(summary, tgt.get("base_kind"), tgt.get("base_at"))),
        "patch_path": "(no code diff)" if standalone.get("exploratory") else str(att / "diff.patch"),
        "delta_path": str(att / "delta.patch") if attempt >= 2 and (att / "delta.patch").exists() else "",
        "previous_findings": previous_findings(prev_judge, role),
        "fix_notes": _read(att / "fix-notes.md"),
        "disputes": "\n".join(f"- {k}: {(v or {}).get('reason') if isinstance(v, dict) else v}"
                              for k, v in (ctx.get("disputes") or {}).items()),
        "user_guidance": "\n".join(f"- {n}" for n in ctx.get("user_notes") or []),
        "lessons": lessons(repo["root"], role, int(cfg.get("lessons_limit") or 5)),
        "vault_notes": vault_notes(repo["root"], source_paths),
        "repo_claude_md": claude_md_files(repo["root"], all_paths),
    }
    if role in REVIEW_ROLES:
        tier = pr.get("tier") or "standard"
        values.update({
            "review_tier": f"{tier} ({pr.get('model')})" + (f" — {pr['reason']}" if pr.get("reason") else ""),
            "baseline_note": BASELINE_NOTE if tgt.get("base_kind") == "head-fallback" else "",
            "outside_files": _outside(ctx, summary),
        })
        if role == "reviewer":
            values["specialist_note"] = specialist_note(specialists)
        else:
            values.update({
                "role": role, "charter_focus": _charter_focus(role, spec), "id_prefix": PREFIX[role],
                "suggestion_prefix": SUGGESTION_PREFIX[role],
                "advisory_note": ("Advisory role: your findings are reported but never block the loop. Still use "
                                  "honest severities." if role in ADVISORY_ROLES else
                                  "Blocking findings must meet the security evidence rules in your system prompt."),
            })
    else:
        scope = pr.get("scope") or "checks"
        vj = _repo_config_text(repo["root"])
        app_url = ctx.get("app_url") or project.get("app_url") or ""
        login = (project.get("auth") or {}).get("login_url") or app_url
        consent = ctx.get("browser_consent") or {}
        if scope == "browser":
            note = ("Playwright tools (mcp__playwright__*) are available.\n\n" + SCREENSHOT_NOTE.replace(
                "<artifacts_dir>", str(att / "qa-artifacts")))
        elif consent.get("status") == "declined":
            note = DECLINED_NOTE
        else:
            note = "No browser in this scope."
        values.update({
            "qa_scope": scope, "qa_reason": pr.get("reason") or "",
            "project_facts": json.dumps(project, indent=2, ensure_ascii=False),
            "verify_json": vj,
            "recipes": "\n".join(project.get("recipes") or []), "app_url": app_url,
            "auth_status": auth_text(scope, auth, repo["slug"], login),
            "artifacts_dir": str(att / "qa-artifacts"), "logs_dir": str(att / "qa-logs"), "browser_note": note,
        })
    text = render(_read(template_path("specialist" if role in SPECIALISTS else role)), {**tool_values(), **values})
    out = att / f"{role}.prompt.md"
    util.atomic_write_text(out, text)
    return out


def brief_criteria(brief: str, n: int = 5) -> list[str]:
    """First `- ` bullets under `## Acceptance criteria`."""
    out, inside = [], False
    for ln in brief.splitlines():
        s = ln.strip()
        if s.startswith("## "):
            inside = s[3:].strip().lower() == "acceptance criteria"
            continue
        if inside and s.startswith("- "):
            out.append(s[2:].strip())
            if len(out) >= n:
                break
    return out

