"""agent-cost integration (export contract 1): discovering the agent-cost CLI and reading `export --json`.

Every failure degrades to "no agent-cost" (None): vloop then uses its own transcript accounting.
"""
from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from .. import paths, util

NAME = "agent-cost"
CONTRACT = 1
CLI_ENV = ("AGENT_COST_CLI",)
EXPORT_TIMEOUT_S = 10
PROBE_TIMEOUT_S = 3
REL = Path("skills") / "agent-cost" / "bin" / "agent-cost"
SOURCES = ("auto", "own", "agent-cost")


@dataclass
class CostInfo:
    cli: Path
    version: str | None = None
    contract: int | None = None

    def to_dict(self) -> dict:
        return {"cli": str(self.cli), "version": self.version, "contract": self.contract}


_cache: dict[tuple, tuple[CostInfo | None, str]] = {}


def reset_cache() -> None:
    _cache.clear()


def _cache_key() -> tuple:
    return (tuple(os.environ.get(n) for n in CLI_ENV), os.environ.get("PATH"), str(paths.claude_home()),
            str(Path.home()))


def _executable(p: Path) -> bool:
    return p.is_file() and os.access(p, os.X_OK)


def _candidates() -> list[Path]:
    out: list[Path] = []
    for name in CLI_ENV:
        v = os.environ.get(name)
        if v:
            out.append(Path(v).expanduser())
    on_path = shutil.which(NAME)
    if on_path:
        out.append(Path(on_path))
    cache = paths.claude_home() / "plugins" / "cache"
    if cache.is_dir():
        out += sorted((p for p in cache.glob("*/agent-cost/*/" + REL.as_posix()) if p.is_file()),
                      key=lambda p: p.stat().st_mtime, reverse=True)
    out.append(paths.claude_home() / REL)
    return out


def _probe(cli: Path) -> tuple[CostInfo | None, str]:
    rc, out, _ = util.run([str(cli), "version", "--json"], timeout=PROBE_TIMEOUT_S)
    try:
        data = json.loads(out)
    except ValueError:
        return None, f"{cli}: `version --json` is not JSON"
    if not isinstance(data, dict) or data.get("name") != NAME:
        return None, f"{cli}: not agent-cost"
    try:
        contract = int(data.get("contract"))
    except (TypeError, ValueError):
        contract = None
    info = CostInfo(cli=cli, version=str(data.get("version") or "") or None, contract=contract)
    if contract != CONTRACT:
        return None, f"{cli}: agent-cost {info.version or '?'} speaks contract {contract}, vloop needs {CONTRACT}"
    return info, "ok"


def _find() -> tuple[CostInfo | None, str]:
    seen: set[str] = set()
    reason = "agent-cost not found"
    for cand in _candidates():
        key = str(cand)
        if key in seen or not _executable(cand):
            continue
        seen.add(key)
        info, why = _probe(cand)
        if info:
            return info, "ok"
        reason = why
    return None, reason


def status() -> tuple[CostInfo | None, str]:
    """(info, "ok") for a usable agent-cost, else (None, why not)."""
    key = _cache_key()
    if key not in _cache:
        _cache[key] = _find()
    return _cache[key]


def discover() -> CostInfo | None:
    return status()[0]


def _num(v) -> bool:
    return v is None or (isinstance(v, (int, float)) and not isinstance(v, bool))


def _valid_numbers(x: dict) -> bool:
    for key in ("tokens", "time"):
        block = x.get(key)
        if block is not None and not (isinstance(block, dict) and all(_num(v) for v in block.values())):
            return False
    models = x.get("models")
    if isinstance(models, dict) and not all(isinstance(m, dict) and _valid_numbers(m) for m in models.values()):
        return False
    return _num(x.get("usd"))


def _valid_export(data, sid: str) -> bool:
    if not isinstance(data, dict) or data.get("contract") != CONTRACT:
        return False
    if data.get("session_id") not in (None, sid):
        return False
    if not (isinstance(data.get("main"), dict) and isinstance(data.get("agents", []), list)):
        return False
    return _valid_numbers(data["main"]) and all(_valid_numbers(a) for a in data.get("agents") or []
                                                if isinstance(a, dict))


def export(sid: str | None, since: str | None, until: str | None) -> tuple[dict | None, str]:
    """(export, "ok") or (None, why not) for the session's numbers in [since, until]."""
    if not sid:
        return None, "no session id"
    info, why = status()
    if info is None:
        return None, why
    cmd = [str(info.cli), "export", "--session", sid]
    if since:
        cmd += ["--since", since]
    if until:
        cmd += ["--until", until]
    rc, out, _ = util.run(cmd + ["--json"], timeout=EXPORT_TIMEOUT_S)
    if rc != 0:
        return None, f"agent-cost export exited {rc}"
    try:
        data = json.loads(out)
    except ValueError:
        return None, "agent-cost export is not JSON"
    if not _valid_export(data, sid):
        return None, "agent-cost export does not match contract 1"
    data["agents"] = [a for a in data.get("agents") or [] if isinstance(a, dict)]
    return data, "ok"
