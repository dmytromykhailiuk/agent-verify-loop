"""Effective configuration: defaults ⊕ user config ⊕ allowed keys of the repo config files (§2.4)."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path

from . import paths, util

MODES = ("off", "warn", "enforce")
REPO_KEYS = ("enabled", "commands", "app", "auth", "floors", "ui_globs", "risky_globs", "ignore_globs",
             "docs_globs", "config_globs", "test_globs", "notes", "cost_source", "specialists", "security_globs")
USER_COMMENT = ("Gate mode: off | warn | enforce. Switch deliberately: review `vloop gate-report`, "
                "then `vloop mode enforce`.")

_defaults_cache: dict | None = None


def defaults() -> dict:
    global _defaults_cache
    if _defaults_cache is None:
        _defaults_cache = util.read_json(paths.config_dir() / "defaults.json", {}) or {}
    return copy.deepcopy(_defaults_cache)


def user_config_path() -> Path:
    return paths.user_config_path()


def user_config() -> dict:
    d = util.read_json(user_config_path(), {})
    return d if isinstance(d, dict) else {}


def repo_config_files(repo_root: str | Path | None) -> list[Path]:
    if not repo_root:
        return []
    return [p for p in paths.repo_config_paths(repo_root) if p.is_file()]


def repo_config_raw(repo_root: str | Path | None) -> dict:
    merged: dict = {}
    for p in reversed(repo_config_files(repo_root)):
        d = util.read_json(p, {})
        if isinstance(d, dict):
            _merge(merged, d)
    return merged


def repo_config(repo_root: str | Path | None) -> dict:
    return {k: v for k, v in repo_config_raw(repo_root).items() if k in REPO_KEYS}


def repo_config_label(repo_root: str | Path | None) -> str:
    files = repo_config_files(repo_root)
    if not files:
        return paths.REPO_CONFIG
    return ", ".join(str(p.relative_to(Path(repo_root))) for p in files)


def _merge(base: dict, over: dict) -> dict:
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = copy.deepcopy(v)
    return base


def load(repo_root: str | Path | None = None) -> dict:
    try:
        cfg = defaults()
        _merge(cfg, user_config())
        rc = repo_config(repo_root)
        for k, v in rc.items():
            if k.endswith("_globs"):
                if isinstance(v, list):
                    cfg[k] = list(cfg.get(k) or []) + [x for x in v if isinstance(x, str)]
            elif isinstance(v, dict) and isinstance(cfg.get(k), dict):
                _merge(cfg[k], v)
            else:
                cfg[k] = copy.deepcopy(v)
        env = os.environ.get("VERIFY_LOOP_MODE")
        if env in MODES:
            cfg["mode"] = env
        return cfg
    except BaseException as e:  # never raises
        util.log_error("config.load", e)
        return defaults()


def gate_mode(entrypoint: str | None = None) -> tuple[str, str]:
    env = os.environ.get("VERIFY_LOOP_MODE")
    if env in MODES:
        return env, "env"
    uc = user_config()
    if uc.get("mode") in MODES:
        mode, source = uc["mode"], "user config"
    else:
        mode, source = defaults().get("mode", "warn"), "defaults"
        if mode not in MODES:
            mode = "warn"
    if entrypoint and entrypoint != "cli":
        gate = _merge(defaults().get("gate", {}), uc.get("gate") or {})
        cap = gate.get("non_cli_mode", "warn")
        if cap in MODES and MODES.index(cap) < MODES.index(mode):
            mode = cap
    return mode, source


def repo_enabled(repo_root) -> bool:
    return repo_config(repo_root).get("enabled", True) is not False


def set_user_mode(mode: str) -> tuple[str, str]:
    from datetime import date, timedelta

    if mode not in MODES:
        raise ValueError(f"mode must be one of {', '.join(MODES)}")
    p = user_config_path()
    uc = user_config()
    old = uc.get("mode") if uc.get("mode") in MODES else defaults().get("mode", "warn")
    if not uc:
        uc = {"schema": 1}
    uc["mode"] = mode
    if not uc.get("installed_at"):
        uc["installed_at"] = util.today()
    if not uc.get("enforce_review_after"):
        uc["enforce_review_after"] = (date.today() + timedelta(days=7)).isoformat()
    uc.setdefault("comment", USER_COMMENT)
    util.atomic_write_json(p, uc)
    return old, mode


def dumps(cfg: dict) -> str:
    return json.dumps(cfg, indent=2, ensure_ascii=False)
