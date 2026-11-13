"""agent-vault integration (contract 1): discovering the vault CLI and the calls vloop makes to it.

Every call degrades to "no vault": empty lessons, no notes, no root. Nothing here raises for a missing vault.
"""
from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from .. import paths, util

NAME = "agent-vault"
CONTRACT = 1
CLI_ENV = ("VERIFY_VAULT_CLI", "AGENT_VAULT_CLI")
VAULT_DIR_ENV = ("AGENT_VAULT_DIR", "CLAUDE_VAULT_DIR")
DEFAULT_VAULT_DIR = "~/claude-vault"
CALL_TIMEOUT_S = 8
PROBE_TIMEOUT_S = 3
REL = Path("skills") / "vault" / "bin" / "vault"


@dataclass
class VaultInfo:
    cli: Path
    skill_dir: Path
    ops_retro: Path
    version: str | None = None
    contract: int | None = None

    def to_dict(self) -> dict:
        return {"cli": str(self.cli), "skill_dir": str(self.skill_dir), "ops_retro": str(self.ops_retro),
                "version": self.version, "contract": self.contract}


_cache: dict[tuple, VaultInfo | None] = {}


def reset_cache() -> None:
    _cache.clear()


def _cache_key() -> tuple:
    return (tuple(os.environ.get(n) for n in CLI_ENV), os.environ.get("PATH"), str(paths.claude_home()),
            str(paths.codex_home()), str(Path.home()))


def _legacy_cli() -> Path:
    return paths.claude_home() / REL


def _executable(p: Path) -> bool:
    return p.is_file() and os.access(p, os.X_OK)


def _candidates() -> list[Path]:
    out: list[Path] = []
    for name in CLI_ENV:
        v = os.environ.get(name)
        if v:
            out.append(Path(v).expanduser())
    on_path = shutil.which("vault")
    if on_path:
        out.append(Path(on_path))
    claude, codex = paths.claude_home(), paths.codex_home()
    cache = (claude / "plugins" / "cache")
    if cache.is_dir():
        out += sorted((p for p in cache.glob("*/agent-vault/*/" + REL.as_posix()) if p.is_file()),
                      key=lambda p: p.stat().st_mtime, reverse=True)
    if (codex / "plugins").is_dir():
        out += sorted(p for p in (codex / "plugins").glob("**/" + REL.as_posix()) if p.is_file())
    out += [_legacy_cli(), Path.home() / ".agents" / REL]
    return out


def _probe(cli: Path) -> tuple[str, dict]:
    """("vault", data) for agent-vault, ("foreign", data) for another JSON-speaking `vault`, ("none", {}) when the
    candidate does not answer `version --json` with a JSON object (a pre-contract install)."""
    rc, out, _ = util.run([str(cli), "version", "--json"], timeout=PROBE_TIMEOUT_S)
    try:
        data = json.loads(out)
    except ValueError:
        return "none", {}
    if not isinstance(data, dict):
        return "none", {}
    return ("vault" if data.get("name") == NAME else "foreign"), data


def _contract_of(data: dict) -> int | None:
    try:
        return int(data.get("contract"))
    except (TypeError, ValueError):
        return None


def _info_for(cli: Path, version: str | None = None, contract: int | None = None,
              skill_dir: str | None = None) -> VaultInfo:
    skill = Path(skill_dir) if skill_dir and Path(skill_dir).is_dir() else Path(os.path.realpath(cli)).parent.parent
    return VaultInfo(cli=cli, skill_dir=skill, ops_retro=skill / "ops" / "retro.md", version=version,
                     contract=contract)


def _find() -> VaultInfo | None:
    legacy = _legacy_cli()
    seen: set[str] = set()
    for cand in _candidates():
        key = str(cand)
        if key in seen or not _executable(cand):
            continue
        seen.add(key)
        kind, data = _probe(cand)
        if kind == "vault":
            return _info_for(cand, str(data.get("version") or "") or None, _contract_of(data),
                             data.get("skill_dir") if isinstance(data.get("skill_dir"), str) else None)
        if kind == "none" and cand == legacy:
            return _info_for(cand, None, 0)
    return None


def discover(cwd: str | Path | None = None) -> VaultInfo | None:
    key = _cache_key()
    if key not in _cache:
        _cache[key] = _find()
    return _cache[key]


def info() -> dict | None:
    found = discover()
    return found.to_dict() if found else None


def available() -> bool:
    return discover() is not None


def configured_cli() -> Path:
    found = discover()
    return found.cli if found else _legacy_cli()


def ops_retro_hint() -> str:
    found = discover()
    return paths.tilde(found.ops_retro if found else _info_for(_legacy_cli()).ops_retro)


def keeper_agent(platform: str | None = None) -> str:
    """The retro agent's name: namespaced (`<plugin>:vault-keeper`) on Claude Code when agent-vault is a plugin."""
    base = "vault-keeper"
    found = discover()
    if platform == "codex" or found is None:
        return base
    manifest = _read_json_object(found.skill_dir.parent.parent / ".claude-plugin" / "plugin.json")
    name = manifest.get("name")
    return f"{name}:{base}" if isinstance(name, str) and name else base


def run(args: list[str], timeout: float = CALL_TIMEOUT_S) -> tuple[int, str]:
    found = discover()
    if found is None:
        return 127, ""
    rc, out, _ = util.run([str(found.cli), *args], timeout=timeout)
    return rc, out


def _text(args: list[str]) -> str:
    rc, out = run(args)
    return out.strip() if rc == 0 else ""


def _json_list(args: list[str]) -> list[dict]:
    rc, out = run(args)
    if rc != 0 or not out.strip():
        return []
    try:
        data = json.loads(out)
    except ValueError:
        return []
    if isinstance(data, dict):
        data = data.get("lessons") or data.get("notes")
    return [d for d in data if isinstance(d, dict)] if isinstance(data, list) else []


def lessons(cwd: str | Path, audience: str, limit: int) -> list[dict]:
    return _json_list(["lessons", "--cwd", str(cwd), "--audience", audience, "--limit", str(limit),
                       "--format", "json"])


def lessons_bullets(cwd: str | Path, audience: str, limit: int) -> str:
    return _text(["lessons", "--cwd", str(cwd), "--audience", audience, "--limit", str(limit), "--format", "bullets"])


def proposals(cwd: str | Path) -> list[dict]:
    return _json_list(["lessons", "--cwd", str(cwd), "--proposals", "--format", "json"])


def covering(cwd: str | Path, paths_: list[str], limit: int = 8) -> str:
    if not paths_:
        return ""
    return _text(["covering", "--cwd", str(cwd), "--format", "bullets", "--limit", str(limit), *paths_[:12]])


def covering_notes(cwd: str | Path, paths_: list[str], limit: int = 8) -> list[dict]:
    if not paths_:
        return []
    return _json_list(["covering", "--cwd", str(cwd), "--format", "json", "--limit", str(limit), *paths_[:12]])


def vault_root(cwd: str | Path, project: str | None = None) -> Path | None:
    cmd = ["resolve", "--cwd", str(cwd)]
    if project:
        cmd += ["--project", project]
    rc, out = run(cmd, timeout=15)
    if rc != 0:
        return None
    try:
        v = json.loads(out).get("vault_root")
    except (ValueError, AttributeError):
        return None
    return Path(v) if v else None


def _read_json_object(path: Path) -> dict:
    data = util.read_json(path, {})
    return data if isinstance(data, dict) else {}


def _dir_template() -> str:
    """The configured vault dir template without a subprocess: env, agent-vault user config, Claude settings."""
    for n in VAULT_DIR_ENV:
        v = os.environ.get(n)
        if v and v.strip():
            return v.strip()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    cfg = (Path(xdg).expanduser() if xdg else Path.home() / ".config") / "agent-vault" / "config.json"
    v = _read_json_object(cfg).get("vault_dir")
    if isinstance(v, str) and v.strip():
        return v.strip()
    env = _read_json_object(paths.claude_home() / "settings.json").get("env")
    if isinstance(env, dict):
        for n in VAULT_DIR_ENV:
            v = env.get(n)
            if isinstance(v, str) and v.strip():
                return v.strip()
    return DEFAULT_VAULT_DIR


def vault_root_hint(cwd: str | Path | None = None) -> Path | None:
    """Vault root `vault resolve` would use for the configured template; relative ones resolve against the repo root of `cwd`."""
    tpl = os.path.expanduser(_dir_template())
    head, _, last = tpl.rstrip("/").rpartition("/")
    if last == "{project}" and "{project}" not in head:
        root_t = head or ("/" if tpl.startswith("/") else ".")
    elif "{project}" in tpl:
        parts = tpl.split("/")
        i = next(i for i, c in enumerate(parts) if "{project}" in c)
        root_t = "/".join(parts[:i]) or ("/" if tpl.startswith("/") else ".")
    else:
        root_t = tpl
    repo_root = None
    if cwd:
        from .. import gitsnap

        repo = gitsnap.repo_for(cwd)
        repo_root = Path(os.path.realpath(repo["root"])) if repo else None
    if os.path.isabs(root_t):
        return Path(os.path.realpath(os.path.normpath(root_t)))
    if not repo_root:
        return None
    root = Path(os.path.realpath(os.path.normpath(os.path.join(repo_root, root_t))))
    if repo_root and (root == repo_root or root in repo_root.parents):
        return None
    return root
