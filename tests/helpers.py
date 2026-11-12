"""Hermetic test base: every test runs with HOME, the agent homes and the XDG dirs inside its own temp dir."""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent
REPO = TESTS.parent
SKILL = REPO / "skills" / "verify-loop"
LIB = SKILL / "lib"
BIN = SKILL / "bin"
VLOOP = BIN / "vloop"
PW_MCP = BIN / "pw-mcp"
SKILLS = REPO / "skills"
AGENTS_SRC = SKILL / "agents"
VL = str(VLOOP)

if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

SCRUB_PREFIXES = ("HERDR_", "CLAUDE_CODE_", "CLAUDE_PLUGIN_", "CODEX_", "VERIFY_", "AGENT_VAULT_", "CLAUDE_VAULT_",
                  "VLOOP_", "XDG_", "GIT_")
SCRUB_NAMES = ("CLAUDE_CONFIG_DIR", "CLAUDE_PROJECT_DIR", "PW_MCP_CHROME_APP", "CLAUDECODE")


def hermetic_env(root: Path, base: dict | None = None) -> dict:
    """A copy of `base` (default os.environ) with every user location moved under `root`."""
    env = {k: v for k, v in (base if base is not None else os.environ).items()
           if not k.startswith(SCRUB_PREFIXES) and k not in SCRUB_NAMES}
    home = root / "home"
    env.update({
        "HOME": str(home),
        "CLAUDE_CONFIG_DIR": str(home / ".claude"),
        "CODEX_HOME": str(home / ".codex"),
        "XDG_STATE_HOME": str(home / ".local" / "state"),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "VERIFY_STATE_DIR": str(root / "state"),
        "VERIFY_CONFIG": str(root / "config" / "config.json"),
        "VERIFY_TRANSCRIPTS_DIR": str(home / ".claude" / "projects"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
    })
    return env


class HermeticCase(unittest.TestCase):
    def setUp(self):
        super().setUp()
        self._hermetic_env = dict(os.environ)
        self._hermetic_cwd = os.getcwd()
        self.tmp = Path(os.path.realpath(tempfile.mkdtemp(prefix="vloop-test-")))
        self.home = self.tmp / "home"
        self.home.mkdir()
        os.environ.clear()
        os.environ.update(hermetic_env(self.tmp, self._hermetic_env))
        from vlooplib import platforms

        platforms.set_override(None)
        home = Path(os.path.realpath(Path.home()))
        if home != self.tmp and self.tmp not in home.parents:
            raise AssertionError(f"Path.home() {home} is outside the test dir {self.tmp}")

    def tearDown(self):
        os.chdir(self._hermetic_cwd)
        os.environ.clear()
        os.environ.update(self._hermetic_env)
        shutil.rmtree(self.tmp, ignore_errors=True)
        super().tearDown()
