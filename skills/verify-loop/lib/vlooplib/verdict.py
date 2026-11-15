"""Verdict schemas, validation, normalization, the judge and reports (spec §2.7, §2.14.6, §2.18).

Pure functions over run-dir files. `judge_attempt` reads only the run dir, `the repo config files` and the repo
working tree (rule 4); it writes only `attempt-N/judge.json`. Stdlib only; the one vlooplib import is `paths`.
"""
from __future__ import annotations

import copy
import json
import os
import re
import tempfile
from pathlib import Path

from . import paths
from .roles import ADVISORY_ROLES, ALL_ROLES, PREFIX, REVIEW_ROLES, SPECIALISTS, SUGGESTION_PREFIX

ROLES = ALL_ROLES
SCHEMA_FILES = {
    "triage-output": "triage.output.schema.json",
    "triage": "triage.schema.json",
    "reviewer": "reviewer.schema.json",
    "security": "reviewer.schema.json",
    "perf": "reviewer.schema.json",
    "ux": "reviewer.schema.json",
    "qa": "qa.schema.json",
    "judge": "judge.schema.json",
}


def _vl() -> str:
    return paths.tilde(paths.cli())


REVIEW_BLOCKING_CATEGORIES = {"correctness", "security", "data", "contract", "acceptance", "convention", "build"}
SECURITY_BLOCKING_CATEGORIES = {"security", "data"}
SECURITY_SCENARIO_MIN = 40
ADVISORY_REASON = "advisory role"
QA_BLOCKING_CATEGORIES = {"acceptance", "regression", "crash", "build", "data", "security"}
CLOSED_STATUSES = {"fixed", "withdrawn"}
AUTH_NEEDS = {"auth_missing", "auth_expired"}
DECLINE_DETAIL = "user declined browser QA"
ERROR_TEXT = {"dead": "agent died", "no_result": "no result", "error": "agent error", "timeout": "timed out",
              "bad_verdict": "bad verdict"}

_schemas: dict[str, dict] = {}


# ---------------------------------------------------------------- small helpers

def _short_dur(seconds) -> str:
    s = int(round(max(0.0, float(seconds or 0))))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"


def _fmt_paths(paths, n: int = 3) -> str:
    paths = list(paths or [])
    head = ", ".join(paths[:n])
    if len(paths) > n:
        head += f", +{len(paths) - n} more"
    return head


def _clip(text, n: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def _read_json(path: Path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(obj, indent=2, ensure_ascii=False) + "\n")
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _is_int(v) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _plural_counts(counts: dict, order=("blocker", "major", "minor")) -> str:
    return ", ".join(f"{counts[k]} {k}" for k in order if counts.get(k))


# ---------------------------------------------------------------- schemas and validation

def _schema_dir() -> Path:
    return paths.schema_dir()


def load_schema(kind: str) -> dict:
    if kind not in SCHEMA_FILES:
        raise ValueError(f"unknown schema kind: {kind}")
    if kind not in _schemas:
        _schemas[kind] = json.loads((_schema_dir() / SCHEMA_FILES[kind]).read_text(encoding="utf-8"))
    return _schemas[kind]


def _type_ok(value, t: str) -> bool:
    if t == "object":
        return isinstance(value, dict)
    if t == "array":
        return isinstance(value, list)
    if t == "string":
        return isinstance(value, str)
    if t == "integer":
        return _is_int(value)
    if t == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if t == "boolean":
        return isinstance(value, bool)
    if t == "null":
        return value is None
    return True


def _join(path: str, key) -> str:
    if isinstance(key, int):
        return f"{path}[{key}]"
    return f"{path}.{key}" if path else str(key)


def _validate(schema: dict, value, path: str, errors: list[str]) -> None:
    label = path or "(root)"
    types = schema.get("type")
    if types is not None:
        tl = types if isinstance(types, list) else [types]
        if not any(_type_ok(value, t) for t in tl):
            errors.append(f"{label}: expected {' or '.join(tl)}")
            return
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{label}: not in enum")
        return
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            errors.append(f"{label}: shorter than {schema['minLength']}")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errors.append(f"{label}: longer than {schema['maxLength']}")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            errors.append(f"{label}: does not match {schema['pattern']}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{label}: less than {schema['minimum']}")
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errors.append(f"{label}: fewer than {schema['minItems']} items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errors.append(f"{label}: more than {schema['maxItems']} items")
        if "items" in schema:
            for i, item in enumerate(value):
                _validate(schema["items"], item, _join(path, i), errors)
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                errors.append(f"{_join(path, key)}: required")
        for key, item in value.items():
            if key in props:
                _validate(props[key], item, _join(path, key), errors)
            elif schema.get("additionalProperties") is False:
                errors.append(f"{_join(path, key)}: unexpected field")


def validate(kind: str, obj) -> list[str]:
    errors: list[str] = []
    _validate(load_schema(kind), obj, "", errors)
    return errors


# ---------------------------------------------------------------- normalization

def _coerce(schema: dict, value, path: str, warnings: list[str]):
    """Schema-driven safe repairs: type coercion, enum case, truncation, maxItems."""
    types = schema.get("type")
    tl = (types if isinstance(types, list) else [types]) if types else []
    if tl and not any(_type_ok(value, t) for t in tl):
        new = _MISSING = object()
        if isinstance(value, str):
            s = value.strip()
            if ("integer" in tl or "number" in tl) and re.fullmatch(r"-?\d+", s):
                new = int(s)
            elif "number" in tl and re.fullmatch(r"-?\d+\.\d*", s):
                new = float(s)
            elif "boolean" in tl and s.lower() in ("true", "false", "yes", "no"):
                new = s.lower() in ("true", "yes")
            elif "null" in tl and s.lower() in ("", "null", "none"):
                new = None
            elif "array" in tl:
                new = [value] if s else []
        elif isinstance(value, float) and "integer" in tl and value.is_integer():
            new = int(value)
        elif _is_int(value) and "string" in tl:
            new = str(value)
        elif isinstance(value, list) and "integer" in tl:
            new = len(value)  # e.g. coverage.files_reviewed given as the list of files
        elif isinstance(value, list) and "string" in tl and all(isinstance(v, (str, int, float)) for v in value):
            new = "; ".join(str(v) for v in value)
        elif isinstance(value, dict) and "array" in tl:
            new = [value]
        elif isinstance(value, dict) and "string" in tl:
            new = json.dumps(value, ensure_ascii=False, separators=(", ", ": "))
        if new is not _MISSING:
            warnings.append(f"{path or '(root)'}: coerced {value!r} to {new!r}")
            value = new
    if "enum" in schema and isinstance(value, str) and value not in schema["enum"]:
        for opt in schema["enum"]:
            if isinstance(opt, str) and opt.lower() == value.strip().lower():
                value = opt
                break
        else:
            if value.strip() == "" and None in schema["enum"]:
                value = None
    if isinstance(value, str) and "maxLength" in schema and len(value) > schema["maxLength"]:
        warnings.append(f"{path}: truncated to {schema['maxLength']} chars")
        value = value[: schema["maxLength"] - 1] + "…"
    if isinstance(value, list):
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            warnings.append(f"{path}: kept the first {schema['maxItems']} of {len(value)} items")
            value = value[: schema["maxItems"]]
        if "items" in schema:
            value = [_coerce(schema["items"], v, _join(path, i), warnings) for i, v in enumerate(value)]
    if isinstance(value, dict):
        props = schema.get("properties", {})
        value = {k: (_coerce(props[k], v, _join(path, k), warnings) if k in props else v) for k, v in value.items()}
    return value


def _renumber(items: list, prefix: str, path: str, warnings: list[str]) -> dict:
    """Give every item a unique `<prefix><n>` id; returns {old_id: new_id} for changed ones."""
    pat = re.compile(rf"^{prefix}\d+$")
    used: set[str] = set()
    bad: list[int] = []
    mapping: dict = {}
    for i, it in enumerate(items):
        if not isinstance(it, dict):
            continue
        raw = it.get("id")
        cand = raw.strip().upper() if isinstance(raw, str) else raw
        if isinstance(cand, str) and pat.match(cand) and cand not in used:
            if cand != raw:
                mapping[raw] = cand
            it["id"] = cand
            used.add(cand)
        else:
            bad.append(i)
    n = 0
    for i in bad:
        n += 1
        while f"{prefix}{n}" in used:
            n += 1
        new = f"{prefix}{n}"
        old = items[i].get("id")
        warnings.append(f"{path}[{i}].id: re-numbered {old!r} to {new}")
        if isinstance(old, (str, int)) and old not in mapping:
            mapping[old] = new
        items[i]["id"] = new
        used.add(new)
    return mapping


def _defaults(obj: dict, values: dict) -> None:
    for k, v in values.items():
        if k not in obj or (obj[k] is None and v is not None):
            obj[k] = copy.deepcopy(v)


def _fix(obj: dict, key: str, expected, warnings: list[str]) -> None:
    if expected is None:
        obj.setdefault(key, None)
        return
    if key in obj and obj[key] not in (None, expected):
        warnings.append(f"{key}: {obj[key]!r} replaced with {expected!r}")
    obj[key] = expected


REVIEW_FINDING_DEFAULTS = {"file": "", "line": None, "end_line": None, "scenario": "", "suggestion": "",
                           "criterion": "", "convention_ref": "", "command": "", "status": "new",
                           "missed_earlier": False, "missed_reason": ""}
SPECIALIST_FINDING_DEFAULTS = {"evidence_ref": "", "measurement": "", "wcag": ""}
QA_CHECK_DEFAULTS = {"evidence": [], "notes": "", "need": None, "logged_in": False}
QA_FINDING_DEFAULTS = {"check_id": "", "expected": "", "actual": "", "repro": [], "evidence": [], "file": "",
                       "line": None, "url": "", "command": "", "pre_existing": "no", "status": "new"}


def _fix_line(obj: dict, key: str, path: str, warnings: list[str]) -> None:
    v = obj.get(key)
    if v is None or _is_int(v) and v >= 1:
        return
    if isinstance(v, str):
        m = re.match(r"^\s*L?(\d+)", v)
        if m and int(m.group(1)) >= 1:
            obj[key] = int(m.group(1))
            warnings.append(f"{path}.{key}: coerced {v!r} to {obj[key]}")
            return
    warnings.append(f"{path}.{key}: invalid {v!r} replaced with null")
    obj[key] = None


def _norm_suggestions(o: dict, prefix: str, warnings: list[str]) -> None:
    sug = o.get("suggestions")
    if sug is None:
        o["suggestions"] = []
        return
    if not isinstance(sug, list):
        return
    out = []
    for s in sug:
        if isinstance(s, str):
            s = {"id": None, "text": s}
        out.append(s)
    if len(out) > 5:
        warnings.append(f"suggestions: kept the first 5 of {len(out)} items")
        out = out[:5]
    _renumber(out, prefix, "suggestions", warnings)
    for i, s in enumerate(out):
        if isinstance(s, dict) and "line" in s:
            _fix_line(s, "line", f"suggestions[{i}]", warnings)
    o["suggestions"] = out


def _norm_reviewer(o: dict, warnings: list[str], role: str = "reviewer") -> None:
    findings = o.get("findings")
    if isinstance(findings, list):
        for i, f in enumerate(findings):
            if not isinstance(f, dict):
                continue
            if isinstance(f.get("severity"), str) and f["severity"].strip().lower() == "nit":
                warnings.append(f"findings[{i}].severity: 'nit' mapped to 'minor'")
                f["severity"] = "minor"
            _defaults(f, REVIEW_FINDING_DEFAULTS)
            if role != "reviewer":
                _defaults(f, SPECIALIST_FINDING_DEFAULTS)
            for key in ("line", "end_line"):
                _fix_line(f, key, f"findings[{i}]", warnings)
            for key in ("file", "scenario", "suggestion", "criterion", "convention_ref", "command", "missed_reason",
                        "evidence_ref", "measurement", "wcag"):
                if key in f and f.get(key) is None:
                    f[key] = ""
        _renumber(findings, PREFIX[role], "findings", warnings)
    _norm_suggestions(o, SUGGESTION_PREFIX[role], warnings)
    if o.get("rechecked") is None:
        o["rechecked"] = []
    if isinstance(o.get("rechecked"), list):
        for r in o["rechecked"]:
            if isinstance(r, dict):
                r.setdefault("note", "")
                if r.get("dispute_ruling") in (None, ""):
                    r["dispute_ruling"] = "n/a"
    if not isinstance(o.get("coverage"), dict):
        o["coverage"] = {}
    _defaults(o["coverage"], {"files_reviewed": 0, "files_skipped": [], "note": ""})


def _norm_qa(o: dict, warnings: list[str]) -> None:
    mapping: dict = {}
    checks = o.get("checks")
    if isinstance(checks, list):
        for i, c in enumerate(checks):
            if isinstance(c, dict):
                _defaults(c, QA_CHECK_DEFAULTS)
                if c.get("need") == "":
                    c["need"] = None
        mapping = _renumber(checks, "C", "checks", warnings)
    findings = o.get("findings")
    if isinstance(findings, list):
        for i, f in enumerate(findings):
            if not isinstance(f, dict):
                continue
            _defaults(f, QA_FINDING_DEFAULTS)
            _fix_line(f, "line", f"findings[{i}]", warnings)
            for key in ("check_id", "expected", "actual", "file", "url", "command"):
                if f.get(key) is None:
                    f[key] = ""
            if isinstance(f.get("check_id"), str):
                cid = f["check_id"].strip()
                f["check_id"] = mapping.get(cid, cid.upper() if re.fullmatch(r"[cC]\d+", cid) else cid)
        _renumber(findings, "Q", "findings", warnings)
    _norm_suggestions(o, "QS", warnings)
    for key in ("unverified", "skipped"):
        if o.get(key) is None:
            o[key] = []
    for u in o.get("unverified") or []:
        if isinstance(u, dict):
            _defaults(u, {"check_id": None, "need": None, "detail": "", "how_to_fix": ""})
            if u.get("need") == "":
                u["need"] = None
            cid = u.get("check_id")
            if isinstance(cid, str):
                u["check_id"] = mapping.get(cid, cid)
    if not isinstance(o.get("env"), dict):
        o["env"] = {}


def _norm_triage(o: dict, warnings: list[str]) -> None:
    if o.get("source") in (None, ""):
        o["source"] = "triage"
    for part in ("qa", "review"):
        sub = o.get(part)
        if isinstance(sub, dict):
            sub.setdefault("reason", "")
            sub.setdefault("focus", [])
            if part == "qa":
                sub.setdefault("needs_login", False)
    spec = o.get("specialists")
    if not isinstance(spec, dict):
        spec = {}
    for role in ("security", "perf", "ux"):
        sub = spec.get(role)
        if not isinstance(sub, dict):
            sub = {"run": bool(sub)} if isinstance(sub, bool) else {}
        sub.setdefault("run", False)
        sub.setdefault("reason", "")
        sub.setdefault("focus", [])
        if role == "ux":
            sub.setdefault("ui_spec", "n/a")
        spec[role] = sub
    o["specialists"] = spec
    o.setdefault("agent", None)


def normalize(kind: str, obj: dict, *, run_id: str | None = None, attempt: int | None = None,
              mode: str | None = None) -> tuple[dict, list[str]]:
    warnings: list[str] = []
    if not isinstance(obj, dict):
        return obj, ["not an object"]
    o = copy.deepcopy(obj)
    verdict_kinds = (*REVIEW_ROLES, "qa", "triage")
    if kind in verdict_kinds:
        if o.get("schema") is None:
            o["schema"] = 1 if kind == "triage" else 2
        _fix(o, "role", kind, warnings)
        _fix(o, "run_id", run_id, warnings)
        if attempt is not None and isinstance(o.get("attempt"), str) and o["attempt"].strip() == str(attempt):
            o["attempt"] = attempt
        _fix(o, "attempt", attempt, warnings)
    if kind in (*REVIEW_ROLES, "qa") and mode is not None:
        if o.get("mode") not in ("full", "recheck", "delta"):
            if o.get("mode") is not None:
                warnings.append(f"mode: {o['mode']!r} replaced with {mode!r}")
            o["mode"] = mode
    schema = load_schema(kind)
    o = _coerce(schema, o, "", warnings)
    if kind in REVIEW_ROLES:
        _norm_reviewer(o, warnings, kind)
    elif kind == "qa":
        _norm_qa(o, warnings)
    elif kind == "triage":
        _norm_triage(o, warnings)
    if kind in verdict_kinds:
        o = _coerce(schema, o, "", warnings)
    return o, warnings


# ---------------------------------------------------------------- JSON extraction

_FENCE = re.compile(r"```[ \t]*json[ \t]*\r?\n(.*?)```", re.S | re.I)


def _balanced_end(text: str, start: int) -> int | None:
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i
    return None


def extract_json(text: str, keys=("role", "verdict", "qa")) -> dict | None:
    if not isinstance(text, str) or not text.strip():
        return None
    for m in reversed(list(_FENCE.finditer(text))):
        try:
            obj = json.loads(m.group(1))
        except ValueError:
            continue
        if isinstance(obj, dict):
            return obj
    try:
        obj = json.loads(text.strip())
        if isinstance(obj, dict):
            return obj
    except ValueError:
        pass
    best = None
    i = 0
    skip_until = -1
    while True:
        i = text.find("{", i)
        if i < 0:
            break
        if i <= skip_until:
            i += 1
            continue
        end = _balanced_end(text, i)
        if end is not None:
            try:
                obj = json.loads(text[i:end + 1])
            except ValueError:
                obj = None
            if isinstance(obj, dict) and any(k in obj for k in keys):
                if best is None or end + 1 - i > best[0]:
                    best = (end + 1 - i, obj)
                skip_until = end
        i += 1
    return best[1] if best else None


# ---------------------------------------------------------------- judge

def _load_verdict(run_dir: Path, role: str, att: int, ctx: dict) -> tuple[dict | None, str | None]:
    """(normalized verdict, None) or (None, inconclusive reason)."""
    adir = run_dir / f"attempt-{att}"
    path = adir / f"{role}.json"
    if not path.exists():
        err = _read_json(adir / f"{role}.error.json")
        if isinstance(err, dict) and err.get("status"):
            return None, f"agent {err['status']}"
        return None, "no verdict"
    raw = _read_json(path)
    if not isinstance(raw, dict):
        return None, "bad verdict: not a JSON object"
    v, _ = normalize(role, raw, run_id=ctx.get("run_id"), attempt=att)
    errors = validate(role, v)
    if errors:
        return None, f"bad verdict: {errors[0]}"
    return v, None


def _deleted_files(run_dir: Path, att: int) -> set[str]:
    summary = _read_json(run_dir / f"attempt-{att}" / "diff.numstat.json", {}) or {}
    out = set(summary.get("deleted_files") or [])
    for row in summary.get("rows") or []:
        if isinstance(row, dict) and (row.get("status") == "D" or "deleted" in (row.get("flags") or [])):
            out.add(row.get("path"))
    return out


def _line_count(root: str, rel: str) -> int | None:
    try:
        with open(os.path.join(root, rel), "rb") as fh:
            data = fh.read()
    except OSError:
        return None
    if not data:
        return 0
    return data.count(b"\n") + (0 if data.endswith(b"\n") else 1)


def _anchor_changed(hunks: dict, file: str, line) -> bool:
    ranges = hunks.get(file)
    if not ranges:
        return False
    if not _is_int(line):
        return True
    for r in ranges:
        try:
            start, count = int(r[0]), int(r[1])
        except (TypeError, ValueError, IndexError):
            continue
        if start - 3 <= line <= start + max(count, 1) + 3:
            return True
    return False


_HUNK_HEAD = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def _patch_hunks(patch: str | None) -> dict | None:
    """New-side hunk ranges per file from a unified diff: {path: [[start, count], …]}; None without a patch."""
    if patch is None:
        return None
    out: dict = {}
    path = None
    for ln in patch.splitlines():
        if ln.startswith("diff --git "):
            m = re.match(r"diff --git a/(.*) b/(.*)$", ln)
            path = m.group(2) if m else None
        elif ln.startswith("+++ "):
            target = ln[4:].strip().strip('"')
            if target != "/dev/null":
                path = target[2:] if target.startswith("b/") else target
        else:
            m = _HUNK_HEAD.match(ln)
            if m and path:
                out.setdefault(path, []).append([int(m.group(1)), int(m.group(2) if m.group(2) is not None else 1)])
    return out


def _anchor_reason(f: dict, env: dict, *, need_anchor: bool = True) -> str | None:
    file = f.get("file") or ""
    line = f.get("line")
    if need_anchor and (not file or not (_is_int(line) and line >= 1)):
        return "no file:line anchor"
    if env["code_ref"] == "worktree" and file and env["root"]:
        if not os.path.isfile(os.path.join(env["root"], file)):
            if file not in env["deleted"]:
                return "anchor file not found"
        elif _is_int(line):
            n = _line_count(env["root"], file)
            if n is not None and line > n:
                return "line out of range"
    return None


def _late_reason(f: dict, env: dict) -> str | None:
    file = f.get("file") or ""
    if env["attempt"] >= 2 and f.get("status") == "new" and file and env["hunks"] is not None:
        if not _anchor_changed(env["hunks"], file, f.get("line")):
            if f.get("missed_earlier") is True and len((f.get("missed_reason") or "").strip()) >= 20:
                f["_late"] = True
            else:
                return f"new finding on code unchanged since attempt {env['attempt'] - 1}"
    return None


_EVIDENCE_REF = re.compile(r"([^\s,;:()\[\]`'\"]+):L?(\d+)")


def _security_rules(f: dict, env: dict) -> str | None:
    cat = f.get("category")
    if cat not in SECURITY_BLOCKING_CATEGORIES:
        return f"category {cat} is never blocking for security"
    if f.get("confidence") not in ("high", "medium"):
        return "low confidence"
    reason = _anchor_reason(f, env)
    if reason:
        return reason
    if len((f.get("scenario") or "").strip()) < SECURITY_SCENARIO_MIN:
        return "no concrete exploit scenario (input → sink → impact)"
    m = _EVIDENCE_REF.search(f.get("evidence_ref") or "")
    if not m:
        return "no evidence_ref (path:line of the untrusted entry point)"
    patch = env.get("patch_hunks")
    if patch is not None:
        if not (_anchor_changed(patch, f.get("file") or "", f.get("line"))
                or _anchor_changed(patch, m.group(1), int(m.group(2)))):
            return "pre-existing: neither the anchor nor the evidence is in the diff"
    return _late_reason(f, env)


def _advisory_rules(f: dict, env: dict) -> str | None:
    return ADVISORY_REASON


def _review_rules(f: dict, env: dict) -> str | None:
    cat = f.get("category")
    if cat not in REVIEW_BLOCKING_CATEGORIES:
        return f"category {cat} is never blocking"
    if f.get("confidence") == "low":
        return "low confidence"
    reason = _anchor_reason(f, env, need_anchor=cat != "build")
    if reason:
        return reason
    if cat == "build" and not (f.get("command") or "").strip():
        return "build finding without the failing command"
    if len((f.get("scenario") or "").strip()) < 30:
        return "no concrete failing scenario"
    if cat == "convention" and not (f.get("convention_ref") or "").strip():
        return "convention finding without proof (convention_ref)"
    if cat == "acceptance" and not (f.get("criterion") or "").strip():
        return "acceptance finding without the criterion"
    return _late_reason(f, env)


RULES = {"reviewer": _review_rules, "security": _security_rules, "perf": _advisory_rules, "ux": _advisory_rules}


def _qa_rules(f: dict, failing_checks: set[str]) -> str | None:
    cat = f.get("category")
    if cat not in QA_BLOCKING_CATEGORIES:
        return f"category {cat} is never blocking"
    if f.get("confidence") == "low":
        return "low confidence"
    if not f.get("check_id") or f.get("check_id") not in failing_checks:
        return "not tied to a failing check"
    if not f.get("repro") and not (f.get("command") or "").strip():
        return "no reproduction"
    if not f.get("evidence"):
        return "no evidence"
    if f.get("pre_existing") == "confirmed":
        return "pre-existing failure"
    return None


def _waived_ids(ctx: dict) -> set[str]:
    out = set()
    for w in ctx.get("waived") or []:
        if isinstance(w, str):
            out.add(w)
        elif isinstance(w, dict) and w.get("id"):
            out.add(w["id"])
    return out


def _finding_record(role: str, f: dict, checks_by_id: dict) -> dict:
    check = checks_by_id.get(f.get("check_id") or "") if role == "qa" else None
    review = role in REVIEW_ROLES
    rec = {
        "role": role, "id": f.get("id"), "severity": f.get("severity"), "blocking": False, "late": False,
        "stuck": False, "waived": False, "downgraded_from": None, "downgrade_reason": None,
        "category": f.get("category"), "confidence": f.get("confidence"),
        "file": f.get("file") or "", "line": f.get("line"),
        "title": f.get("title") or "", "problem": f.get("problem") if review else None,
        "scenario": f.get("scenario") if review else None,
        "suggestion": f.get("suggestion") if review else None,
        "check_id": f.get("check_id") or None if role == "qa" else None,
        "expected": f.get("expected") if role == "qa" else None,
        "actual": f.get("actual") if role == "qa" else None,
        "repro": list(f.get("repro") or []) if role == "qa" else [],
        "evidence": list(f.get("evidence") or []) if role == "qa" else [],
        "dispute": None,
        "status": f.get("status") or "new",
        "criterion": (check or {}).get("criterion") if role == "qa" else (f.get("criterion") or ""),
        "command": f.get("command") or "",
        "url": f.get("url") or "" if role == "qa" else "",
        "missed_earlier": bool(f.get("missed_earlier")) if review else False,
        "carried": False,
        "duplicate_of": None,
    }
    if role in SPECIALISTS:
        for key in ("evidence_ref", "measurement", "wcag"):
            rec[key] = f.get(key) or ""
    return rec


def _evaluate(role: str, v: dict, env: dict) -> tuple[list[dict], dict]:
    """Apply the blocking rules to one verdict. Returns (finding records, role counters)."""
    checks = v.get("checks") or [] if role == "qa" else []
    checks_by_id = {c.get("id"): c for c in checks if isinstance(c, dict)}
    failing = {c.get("id") for c in checks if isinstance(c, dict) and c.get("result") == "fail"}
    records = []
    for f in v.get("findings") or []:
        if not isinstance(f, dict) or f.get("status") in CLOSED_STATUSES:
            continue
        rec = _finding_record(role, f, checks_by_id)
        if f.get("severity") in ("blocker", "major"):
            f = dict(f)
            reason = RULES[role](f, env) if role in RULES else _qa_rules(f, failing)
            if reason:
                rec["downgraded_from"] = rec["severity"]
                rec["severity"] = "minor"
                rec["downgrade_reason"] = reason
            else:
                rec["blocking"] = True
                rec["late"] = bool(f.get("_late"))
        if rec["id"] in env["waived"]:
            rec["blocking"] = False
            rec["waived"] = True
        dispute = env["disputes"].get(rec["id"])
        if isinstance(dispute, dict):
            rec["dispute"] = dispute.get("reason")
        elif isinstance(dispute, str):
            rec["dispute"] = dispute
        records.append(rec)
    return records, checks_by_id


def _counts(records: list[dict], v: dict | None) -> dict:
    blocking = [r for r in records if r["blocking"]]
    return {
        "blocking": len(blocking),
        "minor": sum(1 for r in records if not r["blocking"] and not r["waived"]),
        "suggestions": len((v or {}).get("suggestions") or []),
        "downgraded": sum(1 for r in records if r["downgraded_from"]),
        "blocking_by_severity": {s: sum(1 for r in blocking if r["severity"] == s) for s in ("blocker", "major")},
    }


def _unverified(v: dict, checks_by_id: dict, how_url: str | None) -> list[dict]:
    out = []
    seen = set()
    for u in v.get("unverified") or []:
        if not isinstance(u, dict):
            continue
        cid = u.get("check_id")
        seen.add(cid)
        out.append({"check_id": cid, "criterion": (checks_by_id.get(cid) or {}).get("criterion", ""),
                    "need": u.get("need"), "how_to_fix": u.get("how_to_fix") or "", "detail": u.get("detail") or ""})
    for c in v.get("checks") or []:
        if isinstance(c, dict) and c.get("result") == "blocked" and c.get("id") not in seen:
            out.append({"check_id": c.get("id"), "criterion": c.get("criterion", ""), "need": c.get("need"),
                        "how_to_fix": "", "detail": c.get("notes") or ""})
    for u in out:
        if u["need"] in AUTH_NEEDS and not u["how_to_fix"]:
            u["how_to_fix"] = f"! {_vl()} auth capture --url {how_url or '<login url>'}"
    return out


def _how_url(ctx: dict) -> str | None:
    root = (ctx.get("repo") or {}).get("root")
    if root:
        for path in paths.repo_config_paths(root):
            cfg = _read_json(path, {}) or {}
            auth = cfg.get("auth") if isinstance(cfg, dict) else None
            if isinstance(auth, dict) and auth.get("login_url"):
                return auth["login_url"]
    return ctx.get("app_url")


def _role_base(state: str, carried_from, verdict_path) -> dict:
    return {"state": state, "carried": carried_from is not None, "carried_from": carried_from,
            "verdict_path": verdict_path, "blocking": 0, "minor": 0, "suggestions": 0, "downgraded": 0,
            "reasons": [], "blocking_by_severity": {"blocker": 0, "major": 0}, "notes": []}


def _carried_from_judge(run_dir: Path, role: str, k: int, waived: set[str]) -> tuple[dict, list, list, list] | None:
    prev = _read_json(run_dir / f"attempt-{k}" / "judge.json")
    if not isinstance(prev, dict) or role not in (prev.get("roles") or {}):
        return None
    info = copy.deepcopy(prev["roles"][role])
    findings = [copy.deepcopy(f) for f in prev.get("findings") or [] if f.get("role") == role]
    for f in findings:
        f["carried"] = True
        if f.get("id") in waived and f.get("blocking"):
            f["blocking"] = False
            f["waived"] = True
    sugg = [s for s in prev.get("suggestions") or [] if s.get("role") == role]
    unver = [u for u in prev.get("unverified") or [] if role == "qa"]
    info["carried"] = True
    info["carried_from"] = k
    blocking = [f for f in findings if f.get("blocking")]
    info["blocking"] = len(blocking)
    info["blocking_by_severity"] = {s: sum(1 for f in blocking if f.get("severity") == s) for s in ("blocker", "major")}
    if info.get("state") == "fail" and not blocking:
        info["state"] = "pass"
        info.setdefault("notes", []).append("normalized: no blocking findings survived")
    return info, findings, sugg, unver


def judged_roles(ctx: dict, plan: dict) -> list[str]:
    """Roles the judge reports, in display order: the context's reviewer/QA as before, plus every specialist the
    plan ran or carried. A specialist missing from the plan did not run and is omitted."""
    listed = set(ctx.get("roles") or [])
    planned = plan.get("roles") if isinstance(plan.get("roles"), dict) else {}
    out = []
    for role in ALL_ROLES:
        if role in SPECIALISTS:
            pr = planned.get(role)
            if isinstance(pr, dict) and (pr.get("run") or pr.get("carried_from") is not None):
                out.append(role)
        elif role in listed:
            out.append(role)
    return out


def _near(a, b) -> bool:
    return _is_int(a) and _is_int(b) and abs(a - b) <= 3


def _dedupe(roles: dict, findings: list[dict]) -> None:
    """A reviewer finding that repeats a blocking specialist finding (same file, line ±3, same category) is non-blocking."""
    spec = [f for f in findings if f.get("role") in SPECIALISTS and f.get("file")
            and f.get("blocking") and not f.get("waived")]
    if not spec:
        return
    changed = False
    for f in findings:
        if f.get("role") != "reviewer" or not f.get("file"):
            continue
        for s in spec:
            if s["file"] == f["file"] and s.get("category") == f.get("category") and _near(s.get("line"), f.get("line")):
                f["duplicate_of"] = s["id"]
                if f.get("blocking"):
                    f["blocking"] = False
                    changed = True
                break
    info = roles.get("reviewer")
    if not changed or not info:
        return
    recs = [f for f in findings if f.get("role") == "reviewer"]
    blocking = [f for f in recs if f.get("blocking")]
    info["blocking"] = len(blocking)
    info["minor"] = sum(1 for f in recs if not f.get("blocking") and not f.get("waived"))
    info["blocking_by_severity"] = {s: sum(1 for f in blocking if f.get("severity") == s) for s in ("blocker", "major")}
    if info.get("state") == "fail" and not blocking:
        info["state"] = "pass"
        info.setdefault("notes", []).append("normalized: blocking findings duplicate specialist findings")


def modified_reason(paths) -> str:
    """Integrity check: a verifier changed the worktree (§ integrity). Always INCONCLUSIVE, never PASS."""
    return f"verifier modified files: {_fmt_paths(paths)}"


def judge_attempt(run_dir: Path, attempt: int, *, elapsed_s: float | None = None, max_failed: int = 3,
                  max_inconclusive: int = 2) -> dict:
    run_dir = Path(run_dir)
    ctx = _read_json(run_dir / "context.json", {}) or {}
    adir = run_dir / f"attempt-{attempt}"
    plan = _read_json(adir / "plan.json", {}) or {}
    kind = ctx.get("kind") or "loop"
    waived = _waived_ids(ctx)
    disputes = ctx.get("disputes") or {}
    taint = _read_json(adir / "qa.taint.json", {}) or {}
    integrity = (_read_json(adir / "integrity.json", {}) or {}).get("flagged") or {}
    prev_judge = _read_json(run_dir / f"attempt-{attempt - 1}" / "judge.json", {}) if attempt >= 2 else {}
    prev_judge = prev_judge or {}
    consent = ctx.get("browser_consent")
    how_url = _how_url(ctx)
    warnings: list[str] = []

    roles: dict = {}
    findings: list[dict] = []
    suggestions: list[dict] = []
    unverified: list[dict] = []

    for role in judged_roles(ctx, plan):
        pr = (plan.get("roles") or {}).get(role) or {}
        if pr.get("run"):
            att, carried_from = attempt, None
        elif pr.get("carried_from") is not None:
            att, carried_from = int(pr["carried_from"]), int(pr["carried_from"])
            got = _carried_from_judge(run_dir, role, att, waived)
            if got:
                info, fl, sl, ul = got
                info.setdefault("verdict_path", f"attempt-{att}/{role}.json")
                if role in ADVISORY_ROLES:
                    info["advisory"] = True
                roles[role] = info
                findings += fl
                suggestions += sl
                unverified += ul
                continue
        else:
            info = _role_base("skipped", None, None)
            if role == "qa":
                info["skipped_by"] = pr.get("skipped_by")
                info["scope"] = pr.get("scope") or "none"
            roles[role] = info
            continue

        vpath = f"attempt-{att}/{role}.json"
        info = _role_base("pass", carried_from, vpath)
        if role in ADVISORY_ROLES:
            info["advisory"] = True
        modified = list(integrity.get(role) or []) if att == attempt else []
        if modified:
            info["modified_files"] = modified
        v, why = _load_verdict(run_dir, role, att, ctx)
        if role == "qa":
            info["scope"] = (v or {}).get("scope") or pr.get("scope")
            info["checks"] = {"pass": 0, "fail": 0, "skip": 0, "blocked": 0}
            info["tainted"] = False
        if v is None:
            info["state"] = "inconclusive"
            if modified:
                info["reasons"].append(modified_reason(modified))
            info["reasons"].append(why)
            if why == "no verdict" and not (run_dir / vpath).exists():
                info["verdict_path"] = None
            roles[role] = info
            continue
        info["verdict"] = v.get("verdict")
        info["mode"] = v.get("mode")
        hunks = None
        if att >= 2:
            hunks = _read_json(run_dir / f"attempt-{att}" / "delta.hunks.json")
            if not isinstance(hunks, dict):
                hunks = None
                if role in ("reviewer", "security"):
                    warnings.append(f"attempt-{att}/delta.hunks.json missing: late rule not applied")
        patch_hunks = None
        if role == "security":
            patch_path = run_dir / f"attempt-{att}" / "diff.patch"
            patch_hunks = _patch_hunks(patch_path.read_text(encoding="utf-8", errors="replace")
                                       if patch_path.is_file() else None)
        env = {"code_ref": (ctx.get("target") or {}).get("code_ref") or "worktree",
               "root": (ctx.get("repo") or {}).get("root"), "deleted": _deleted_files(run_dir, att),
               "attempt": att, "hunks": hunks, "waived": waived, "disputes": disputes, "patch_hunks": patch_hunks}
        recs, checks_by_id = _evaluate(role, v, env)
        if carried_from is not None:
            for r in recs:
                r["carried"] = True
        c = _counts(recs, v)
        info.update({k: c[k] for k in ("blocking", "minor", "suggestions", "downgraded", "blocking_by_severity")})
        findings += recs
        for s in v.get("suggestions") or []:
            if isinstance(s, dict):
                suggestions.append({"role": role, "id": s.get("id"), "text": s.get("text", ""),
                                    "file": s.get("file"), "line": s.get("line")})
        state = None
        if role == "qa":
            for chk in v.get("checks") or []:
                if isinstance(chk, dict) and chk.get("result") in info["checks"]:
                    info["checks"][chk["result"]] += 1
            unv = _unverified(v, checks_by_id, how_url)
            unverified += unv
            verified = info["checks"]["pass"] + info["checks"]["fail"]
            role_taint = taint if att == attempt else (_read_json(run_dir / f"attempt-{att}" / "qa.taint.json", {}) or {})
            info["tainted"] = bool(role_taint.get("tainted"))
            declined = (isinstance(consent, dict) and consent.get("status") == "declined"
                        and consent.get("source") == "user" and unv
                        and all(u.get("detail") == DECLINE_DETAIL for u in unv))
            decline_ids = {u.get("check_id") for u in unv}
            if modified:
                state = "inconclusive"
                info["reasons"].append(modified_reason(modified))
            elif info["tainted"]:
                state = "inconclusive"
                info["reasons"].append(f"side effects: {_fmt_paths(role_taint.get('changed_files') or [])}")
            elif declined and (
                    (v["verdict"] != "inconclusive" and verified == 0)
                    or (v["verdict"] == "inconclusive" and info["checks"]["fail"] == 0
                        and all(chk.get("id") in decline_ids for chk in v.get("checks") or []
                                if isinstance(chk, dict) and chk.get("result") == "blocked"))):
                state = "skipped"
                info["skipped_by"] = "user"
                info["notes"].append("nothing verifiable without the browser (user declined)")
            elif v["verdict"] == "inconclusive":
                state = "inconclusive"
                info["reasons"].append(_clip(v.get("summary") or "inconclusive", 80))
            elif verified == 0:
                state = "inconclusive"
                info["reasons"].append("verified nothing")
            info["unverified"] = len(unv)
        elif modified:
            state = "inconclusive"
            info["reasons"].append(modified_reason(modified))
        elif v["verdict"] == "inconclusive":
            state = "inconclusive"
            info["reasons"].append(_clip(v.get("summary") or "inconclusive", 80))
        if state is None:
            if info["blocking"]:
                state = "fail"
            else:
                state = "pass"
                if v["verdict"] == "fail":
                    info["notes"].append("normalized: no blocking findings survived")
        info["state"] = state
        roles[role] = info

    if taint.get("main_edits"):
        warnings.append(f"tree changed during verification (main agent edited "
                        f"{_fmt_paths(taint.get('changed_files') or [])})")

    # stuck: still_present now and open in the previous attempt
    prev_status = {}
    for pf in prev_judge.get("findings") or []:
        prev_status[(pf.get("role"), pf.get("id"))] = pf.get("status", "new")
    for f in findings:
        if f.get("carried"):
            continue
        if f.get("status") == "still_present" and prev_status.get((f["role"], f["id"])) in ("still_present", "new"):
            f["stuck"] = True

    _dedupe(roles, findings)

    states = []
    for role, info in roles.items():
        if info.get("advisory") and info["state"] == "inconclusive" and not info.get("modified_files"):
            warnings.append(f"{role} inconclusive (advisory, not counted): "
                            f"{(info.get('reasons') or ['inconclusive'])[0]}")
            continue
        states.append(info["state"])
    result = "fail" if "fail" in states else "inconclusive" if "inconclusive" in states else "pass"

    old = _read_json(adir / "judge.json")
    before = (old or {}).get("counters_before") if isinstance(old, dict) else None
    if not isinstance(before, dict):
        before = {"failed_attempts": int(ctx.get("failed_attempts") or 0),
                  "inconclusive_streak": int(ctx.get("inconclusive_streak") or 0)}
    failed, streak = before["failed_attempts"], before["inconclusive_streak"]
    if result == "fail":
        failed, streak = failed + 1, 0
    elif result == "inconclusive":
        streak += 1
    else:
        streak = 0
    escalate = None
    if kind == "loop":
        if result == "fail" and failed >= max_failed:
            escalate = {"reason": f"{failed} failed attempts"}
        elif result == "inconclusive" and streak >= max_inconclusive:
            escalate = {"reason": f"{streak} inconclusive attempts in a row"}

    judge = {
        "schema": 1, "run_id": ctx.get("run_id") or run_dir.name, "attempt": attempt, "kind": kind,
        "result": result, "escalate": escalate, "roles": roles, "findings": findings,
        "suggestions": suggestions, "unverified": unverified, "warnings": warnings,
        "browser_consent": copy.deepcopy(consent) if isinstance(consent, dict) else None,
        "failed_attempts": failed, "inconclusive_streak": streak, "max_failed": max_failed,
        "max_inconclusive": max_inconclusive, "elapsed_s": elapsed_s, "counters_before": before,
        "roles_order": list(roles), "status_line": "",
    }
    judge["status_line"] = status_line(judge)
    _write_json(adir / "judge.json", judge)
    return judge


# ---------------------------------------------------------------- rendering

def _role_piece(role: str, info: dict, judge: dict) -> str:
    name = "review" if role == "reviewer" else role
    state = info.get("state")
    inner: list[str] = []
    if info.get("carried") and info.get("carried_from") is not None:
        inner.append(f"carried from #{info['carried_from']}")
    if state == "skipped":
        if role == "qa" and info.get("skipped_by") == "user" and info.get("notes"):
            return "qa SKIPPED (user declined browser)"
        if role == "qa":
            return f"qa SKIPPED ({info.get('skipped_by') or 'plan'}: {info.get('scope') or 'none'})"
        return f"{name} SKIPPED"
    if state == "inconclusive":
        inner.append((info.get("reasons") or ["inconclusive"])[0])
    elif state == "fail":
        inner.append(_plural_counts(info.get("blocking_by_severity") or {}) or f"{info.get('blocking', 0)} blocking")
    elif state == "pass" and role in ADVISORY_ROLES:
        if info.get("minor"):
            inner.append(f"{info['minor']} advisory")
    elif state == "pass" and role == "qa":
        ch = info.get("checks") or {}
        p, f = ch.get("pass", 0), ch.get("fail", 0)
        inner.append(f"{p}/{p + f} checks")
        n_unv = info.get("unverified")
        if n_unv is None:
            n_unv = len(judge.get("unverified") or [])
        if n_unv:
            inner.append(f"{n_unv} unverified")
        if info.get("scope"):
            inner.append(info["scope"])
    head = f"{name} {state.upper()}"
    return f"{head} ({', '.join(inner)})" if inner else head


def _prefix(judge: dict) -> str:
    if judge.get("kind") != "standalone":
        return "verify-loop"
    active = [r for r in (judge.get("roles") or {})]
    if active and "reviewer" in active and all(r in REVIEW_ROLES for r in active):
        return "verify-review"
    if active == ["qa"]:
        return "verify-qa"
    return "verify"


def status_line(judge: dict) -> str:
    parts = [_prefix(judge), f"attempt {judge.get('attempt')}"]
    if judge.get("kind") != "standalone":
        parts.append(f"failures {judge.get('failed_attempts', 0)}/{judge.get('max_failed', 3)}")
    roles = judge.get("roles") or {}
    for role in ROLES:
        if role in roles:
            parts.append(_role_piece(role, roles[role], judge))
    if judge.get("elapsed_s") is not None:
        parts.append(_short_dur(judge["elapsed_s"]))
    return " · ".join(parts)


def _anchor(f: dict) -> str:
    file = f.get("file") or ""
    if not file:
        return ""
    return f"{file}:{f['line']}" if _is_int(f.get("line")) else file


def _blocking_lines(f: dict) -> list[str]:
    if f["role"] in REVIEW_ROLES:
        anchor = _anchor(f)
        head = f"  {f['id']} [{f['severity']}·{f.get('category')}·{f.get('confidence')}]"
        head += f" {anchor} — {f.get('title')}" if anchor else f" — {f.get('title')}"
        if f.get("late"):
            head += " (missed earlier)"
        if f.get("stuck"):
            head += " (stuck)"
        out = [head]
        if f.get("scenario"):
            out.append(f"     scenario: {_clip(f['scenario'], 160)}")
        if f.get("evidence_ref"):
            out.append(f"     entry point: {_clip(f['evidence_ref'], 120)}")
        if f.get("suggestion"):
            out.append(f"     fix: {_clip(f['suggestion'], 160)}")
    else:
        head = f"  {f['id']} [{f['severity']}·{f.get('category')}]"
        if f.get("check_id"):
            head += f" {f['check_id']}"
            if f.get("criterion"):
                head += f" \"{_clip(f['criterion'], 80)}\""
        if f.get("expected") or f.get("actual"):
            head += f" — expected: {_clip(f.get('expected'), 80)} · actual: {_clip(f.get('actual'), 80)}"
        else:
            head += f" — {f.get('title')}"
        if f.get("stuck"):
            head += " (stuck)"
        out = [head]
        bits = []
        if f.get("repro"):
            bits.append("repro: " + " ".join(f"{i}) {_clip(s, 80)}" for i, s in enumerate(f["repro"], 1)))
        elif f.get("command"):
            bits.append(f"command: {_clip(f['command'], 120)}")
        if f.get("evidence"):
            ev = f"evidence: {_clip(f['evidence'][0], 160)}"
            if len(f["evidence"]) > 1:
                ev += f" (+{len(f['evidence']) - 1} more)"
            bits.append(ev)
        if bits:
            out.append("     " + " · ".join(bits))
    if f.get("dispute"):
        out.append(f"     disputed: \"{_clip(f['dispute'], 120)}\"")
    return out


def _consent_line(consent) -> str | None:
    if not isinstance(consent, dict):
        return None
    status = consent.get("status")
    source = consent.get("source") or "unknown"
    if status == "granted":
        line = f"Browser QA: approved ({source})"
    elif status == "declined":
        line = f"Browser QA: declined ({consent.get('decision') or 'unknown'}, {source})"
    elif status == "pending":
        line = "Browser QA: pending"
    else:
        return None
    if consent.get("note"):
        line += f" — note: {consent['note']}"
    return line


def render_report(judge: dict, run_id: str, *, next_hint: bool = True) -> str:
    escalated = bool(judge.get("escalate"))
    result = "ESCALATE" if escalated else str(judge.get("result", "inconclusive")).upper()
    lines = [f"RESULT: {result}", judge.get("status_line") or status_line(judge)]
    findings = judge.get("findings") or []
    blocking = [f for f in findings if f.get("blocking")]
    if blocking:
        lines.append("Blocking:")
        for f in blocking:
            lines += _blocking_lines(f)
    down = [f for f in findings if f.get("downgraded_from") and not f.get("waived")
            and f.get("downgrade_reason") != ADVISORY_REASON]
    if down:
        lines.append("Not blocking (downgraded): " + " · ".join(
            f"{f['id']} {f['downgraded_from']}→minor ({f.get('downgrade_reason')})" for f in down))
    dups = [f for f in findings if f.get("duplicate_of") and not f.get("waived")]
    if dups:
        lines.append("Duplicates (not blocking): " + " · ".join(f"{f['id']} = {f['duplicate_of']}" for f in dups))
    advisory = [f for f in findings if f.get("downgrade_reason") == ADVISORY_REASON and not f.get("waived")
                or f.get("role") in ADVISORY_ROLES and not f.get("downgraded_from") and not f.get("waived")]
    if advisory:
        bits = []
        for f in advisory:
            anchor = _anchor(f)
            sev = f.get("downgraded_from") or f.get("severity")
            bits.append(f"{f['id']} [{sev}] {anchor + ' — ' if anchor else '— '}{_clip(f.get('title'), 80)}")
        lines.append("Advisory (never blocking): " + " · ".join(bits))
    minor = [f for f in findings if not f.get("blocking") and not f.get("downgraded_from") and not f.get("waived")
             and not f.get("duplicate_of") and f.get("role") not in ADVISORY_ROLES]
    if minor:
        bits = []
        for f in minor:
            anchor = _anchor(f)
            bits.append(f"{f['id']} {anchor} — {_clip(f.get('title'), 80)}" if anchor
                        else f"{f['id']} — {_clip(f.get('title'), 80)}")
        lines.append("Minor: " + " · ".join(bits))
    waived = [f for f in findings if f.get("waived")]
    if waived:
        lines.append("Waived: " + " · ".join(f"{f['id']} — {_clip(f.get('title'), 60)}" for f in waived))
    sugg = judge.get("suggestions") or []
    if sugg:
        shown = sugg[:3]
        lines.append(f"Suggestions ({len(shown)} of {len(sugg)}): "
                     + " · ".join(f"{s.get('id')} {_clip(s.get('text'), 80)}" for s in shown))
    unv = judge.get("unverified") or []
    if unv:
        bits = []
        for u in unv:
            b = f"{u.get('check_id') or '?'}"
            if u.get("criterion"):
                b += f" \"{_clip(u['criterion'], 80)}\""
            b += f" — {u.get('need') or _clip(u.get('detail') or 'unverified', 60)}"
            if u.get("how_to_fix"):
                b += f" → {u['how_to_fix']}"
            bits.append(b)
        lines.append("Unverified: " + " · ".join(bits))
    if judge.get("warnings"):
        lines.append("Warnings: " + "; ".join(judge["warnings"]))
    cl = _consent_line(judge.get("browser_consent"))
    if cl:
        lines.append(cl)
    if next_hint and not escalated:
        standalone = judge.get("kind") == "standalone"
        if standalone:
            lines.append("next: report this to the user; fix only if asked")
        elif judge.get("result") == "pass":
            lines.append(f"next: {_vl()} retro-plan --run {run_id}")
        elif judge.get("result") == "fail":
            lines.append(f"next: fix every blocking finding (or dispute one: {_vl()} dispute <id> --run {run_id} "
                         f"--reason \"<evidence>\"), then:")
            lines.append(f"  {_vl()} begin --rerun --run {run_id} --fix-notes - <<'EOF'")
            for f in blocking or [{"id": "<id>"}]:
                lines.append(f"  {f['id']}: <what changed, where>")
            lines.append("  EOF")
        else:
            lines.append(f"next: fix the environment if it's simple, then {_vl()} begin --rerun --run {run_id}")
    return "\n".join(lines)


def _judges(run_dir: Path) -> list[dict]:
    out = []
    for p in run_dir.glob("attempt-*/judge.json"):
        j = _read_json(p)
        if isinstance(j, dict) and _is_int(j.get("attempt")):
            out.append(j)
    return sorted(out, key=lambda j: j["attempt"])


def render_escalation(run_dir: Path) -> str:
    run_dir = Path(run_dir)
    ctx = _read_json(run_dir / "context.json", {}) or {}
    run_id = ctx.get("run_id") or run_dir.name
    judges = _judges(run_dir)
    last = judges[-1] if judges else {}
    reason = (last.get("escalate") or {}).get("reason") or (
        f"{last.get('failed_attempts', 0)} failed attempts" if last.get("result") == "fail"
        else f"{last.get('inconclusive_streak', 0)} inconclusive attempts in a row")
    total = sum(float(j.get("elapsed_s") or 0) for j in judges)
    if not total:
        total = sum(float(a.get("elapsed_s") or 0) for a in ctx.get("attempts") or [] if isinstance(a, dict))
    head = f"verify-loop · ESCALATION · {reason} · {_short_dur(total)} · run {run_id}"
    lines = [head]
    disputes = ctx.get("disputes") or {}

    rulings: dict = {}
    if last:
        for role in REVIEW_ROLES:
            rv = _read_json(run_dir / f"attempt-{last['attempt']}" / f"{role}.json", {}) or {}
            for r in rv.get("rechecked") or []:
                if isinstance(r, dict) and r.get("dispute_ruling") == "upheld":
                    rulings[r.get("id")] = r.get("note") or ""

    still = [f for f in last.get("findings") or [] if f.get("blocking")]
    body: list[str] = []
    for f in still:
        seen = [j["attempt"] for j in judges
                if any(g.get("id") == f["id"] and g.get("role") == f["role"] for g in j.get("findings") or [])]
        tag = f"{f['severity']}·stuck" if f.get("stuck") else f["severity"]
        if f["role"] in REVIEW_ROLES:
            anchor = _anchor(f)
            text = f"  {f['id']} [{tag}] {anchor + ' — ' if anchor else '— '}{_clip(f.get('title'), 70)}"
        else:
            where = f.get("check_id") or ""
            if f.get("url"):
                where = f"{where} {f['url']}".strip()
            text = f"  {f['id']} [{tag}] {where + ' — ' if where else '— '}{_clip(f.get('title'), 70)}"
            if f.get("evidence"):
                text += f" … evidence: {_clip(f['evidence'][0], 80)}"
        hist = []
        if seen:
            hist.append(f"raised #{seen[0]}" + (", still " + ",".join(f"#{a}" for a in seen[1:]) if len(seen) > 1 else ""))
        d = disputes.get(f["id"])
        if d:
            hist.append(f"disputed: \"{_clip(d.get('reason') if isinstance(d, dict) else d, 60)}\"")
        if f["id"] in rulings:
            hist.append(f"reviewer upheld: \"{_clip(rulings[f['id']], 60)}\"")
        if hist:
            text += f" ({'; '.join(hist)})"
        body.append(text)
    if body:
        lines.append("Still failing:")
        if len(body) > 10:
            body = body[:9] + [f"  +{len(body) - 9} more"]
        lines += body

    if last.get("result") == "inconclusive":
        env = []
        for j in judges[-max(1, int(last.get("inconclusive_streak") or 1)):]:
            for role, info in (j.get("roles") or {}).items():
                if info.get("state") == "inconclusive":
                    for r in info.get("reasons") or []:
                        env.append(f"  #{j['attempt']} {role}: {_clip(r, 100)}")
        if env:
            lines.append("Environment:")
            lines += env[:6]

    tried = []
    for p in sorted(run_dir.glob("attempt-*/fix-notes.md"), key=lambda p: int(p.parent.name.split("-")[1])):
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            continue
        if text.strip():
            tried.append(f"#{p.parent.name.split('-')[1]} {_clip(text, 80)}")
    if tried:
        lines.append("Tried: " + " · ".join(tried))
    unv = last.get("unverified") or []
    if unv:
        lines.append("Unverified: " + ", ".join(f"{u.get('check_id') or '?'} ({u.get('need') or 'unverified'})"
                                                for u in unv))
    ids = [f["id"] for f in still if f.get("stuck") or f["id"] in disputes] or [f["id"] for f in still]
    tail = [
        'next: show this report to the user and ask with AskUserQuestion (header "Verify loop"):',
        f"  Try 3 more attempts → {_vl()} decide retry --run {run_id} [--note \"<user text>\"]",
        f"  Waive disputed/stuck findings → {_vl()} decide waive --run {run_id} --ids {','.join(ids) or '<ids>'} "
        "[--note …]",
        f"  Accept as is → {_vl()} decide accept --run {run_id}",
        f"  Stop, I'll take over → {_vl()} decide stop --run {run_id}",
    ]
    room = 25 - len(tail)
    if len(lines) > room:
        lines = lines[: room - 1] + ["  …"]
    return "\n".join(lines + tail)


def role_line(role: str, verdict: dict | None, error: dict | None, duration_s: float | None,
              model_effort: str | None = None) -> str:
    dur = [_short_dur(duration_s)] if duration_s is not None else []
    me = [model_effort] if model_effort else []
    if not isinstance(verdict, dict) or verdict.get("verdict") not in ("pass", "fail", "inconclusive"):
        if isinstance(error, dict) and error.get("status"):
            text = ERROR_TEXT.get(error["status"], f"agent {error['status']}")
            if error.get("detail"):
                text += f" ({_clip(error['detail'], 80)})"
        else:
            text = "no verdict"
        return " · ".join([f"{role}: INCONCLUSIVE", text] + dur)
    v = verdict["verdict"]
    head = f"{role}: {v.upper()}" + (" (advisory)" if role in ADVISORY_ROLES else "")
    open_f = [f for f in verdict.get("findings") or []
              if isinstance(f, dict) and f.get("status") not in CLOSED_STATUSES]
    sev = {s: sum(1 for f in open_f if f.get("severity") == s) for s in ("blocker", "major", "minor")}
    if v == "inconclusive":
        return " · ".join([head, _clip(verdict.get("summary") or "inconclusive", 60)] + me + dur)
    if role in REVIEW_ROLES:
        n_sugg = len(verdict.get("suggestions") or [])
        if v == "pass":
            mid = f"{sev['blocker'] + sev['major']} blocking, {sev['minor']} minor, {n_sugg} suggestions"
        else:
            mid = ", ".join([f"{sev[s]} {s}" for s in ("blocker", "major") if sev[s]] + [f"{sev['minor']} minor"])
        return " · ".join([head, mid] + me + dur)
    checks = [c for c in verdict.get("checks") or [] if isinstance(c, dict)]
    p = sum(1 for c in checks if c.get("result") == "pass")
    f = sum(1 for c in checks if c.get("result") == "fail")
    listed = {u.get("check_id") for u in verdict.get("unverified") or [] if isinstance(u, dict)}
    n_unv = len(listed) + sum(1 for c in checks if c.get("result") == "blocked" and c.get("id") not in listed)
    ch = f"{p}/{p + f} checks"
    parts = [head]
    if v == "pass":
        parts.append(ch + (f" ({n_unv} unverified)" if n_unv else ""))
    else:
        parts.append(_plural_counts(sev, ("blocker", "major")) or "0 blocking")
        parts.append(ch)
    if verdict.get("scope"):
        parts.append(verdict["scope"])
    return " · ".join(parts + me + dur)
