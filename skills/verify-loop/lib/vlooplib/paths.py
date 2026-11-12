"""Every location vloop reads or writes. Environment variables are read at call time."""
from __future__ import annotations

import os
from pathlib import Path

APP = "agent-verify-loop"
REPO_CONFIG_LOCAL = ".agent-verify-loop.local.json"
REPO_CONFIG = ".agent-verify-loop.json"
LEGACY_REPO_CONFIG = ".claude/verify.json"

SKILL_DIR: Path = Path(os.path.abspath(__file__)).parent.parent.parent
# Bundled data is found next to the code that reads it, not via the invoked bin path, which may be a lone copy or link.
BUNDLE_DIR: Path = Path(os.path.realpath(__file__)).parent.parent.parent


def _env_path(name: str) -> Path | None:
    v = os.environ.get(name)
    return Path(v).expanduser() if v else None


def _xdg(name: str, fallback: str) -> Path:
    return _env_path(name) or Path.home() / fallback


def skill_dir() -> Path:
    return Path(SKILL_DIR)


def cli() -> Path:
    return skill_dir() / "bin" / "vloop"


def pw_mcp() -> Path:
    return skill_dir() / "bin" / "pw-mcp"


def prompts_dir() -> Path:
    return BUNDLE_DIR / "prompts"


def schema_dir() -> Path:
    return BUNDLE_DIR / "schema"


def config_dir() -> Path:
    return BUNDLE_DIR / "config"


def agents_src_dir() -> Path:
    return BUNDLE_DIR / "agents"


def qa_toolbox_src_dir() -> Path:
    return skill_dir() / "qa-toolbox"


def claude_home() -> Path:
    return _env_path("CLAUDE_CONFIG_DIR") or Path.home() / ".claude"


def codex_home() -> Path:
    return _env_path("CODEX_HOME") or Path.home() / ".codex"


def state_dir() -> Path:
    return _env_path("VERIFY_STATE_DIR") or _xdg("XDG_STATE_HOME", ".local/state") / APP


def user_config_path() -> Path:
    return _env_path("VERIFY_CONFIG") or _xdg("XDG_CONFIG_HOME", ".config") / APP / "config.json"


def plugin_root() -> Path | None:
    root = os.environ.get("CLAUDE_PLUGIN_ROOT")
    if not root:
        return None
    real_root = Path(os.path.realpath(os.path.expanduser(root)))
    real_skill = Path(os.path.realpath(skill_dir()))
    if real_skill == real_root or real_root in real_skill.parents:
        return real_root
    return None


def data_dir() -> Path:
    plugin_data = _env_path("CLAUDE_PLUGIN_DATA")
    if plugin_data and plugin_root() is not None:
        return plugin_data
    return _xdg("XDG_DATA_HOME", ".local/share") / APP


def repo_config_paths(repo: str | Path) -> list[Path]:
    root = Path(repo)
    return [root / REPO_CONFIG_LOCAL, root / REPO_CONFIG, root / LEGACY_REPO_CONFIG]


def tilde(path: str | Path) -> str:
    p = str(path)
    home = str(Path.home()).rstrip("/")
    if home and (p == home or p.startswith(home + "/")):
        return "~" + p[len(home):]
    return p
