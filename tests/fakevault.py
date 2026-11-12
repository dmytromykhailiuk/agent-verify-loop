"""Fake `vault` CLIs for tests: agent-vault v1, a pre-contract legacy install and look-alikes (HashiCorp vault)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

SCRIPT = """#!{python}
import json, os, sys
here = os.path.dirname(os.path.realpath(__file__))
cfg = json.load(open(os.path.join(here, "fake.json")))
a = sys.argv[1:]
with open(os.path.join(here, "calls.log"), "a") as f:
    f.write(json.dumps(a) + "\\n")
cmd = a[0] if a else ""
def opt(name, default=None):
    return a[a.index(name) + 1] if name in a else default
if cmd == "version":
    mode = cfg["version"]
    if mode == "v1":
        print(json.dumps({{"name": "agent-vault", "version": cfg.get("v", "1.4.0"), "contract": 1,
                           "skill_dir": os.path.dirname(here), "cli": os.path.join(here, "vault")}}))
    elif mode == "foreign-json":
        print(json.dumps({{"name": "vault", "version": "1.15.0"}}))
    elif mode == "text":
        print("Vault v1.15.0 (cafe), built 2024-01-01")
    elif mode == "legacy":
        sys.stderr.write("vault: unknown command: version\\n")
        sys.exit(2)
elif cmd == "lessons" and "--proposals" in a:
    rows = cfg.get("proposals", [])
    if rows:
        print(json.dumps(rows))
elif cmd == "lessons":
    rows = cfg.get("lessons", [])
    if rows:
        if opt("--format") == "json":
            print(json.dumps(rows))
        else:
            for r in rows:
                print("- " + r["rule"] + " (x" + str(r["occurrences"]) + ")")
elif cmd == "covering":
    rows = cfg.get("covering", [])
    if rows:
        if opt("--format") == "json":
            print(json.dumps(rows))
        else:
            for r in rows:
                print("- [[" + r["link"] + "]] " + r["summary"])
elif cmd == "resolve":
    print(json.dumps({{"vault_root": cfg.get("vroot", "/v") + "/" + (opt("--project") or "p")}}))
"""


class FakeVault:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.skill_dir = self.root / "skills" / "vault"
        self.cli = self.skill_dir / "bin" / "vault"
        self.ops = self.skill_dir / "ops" / "retro.md"
        self.cfg_path = self.cli.parent / "fake.json"
        self.log = self.cli.parent / "calls.log"

    def configure(self, **cfg) -> "FakeVault":
        current = json.loads(self.cfg_path.read_text()) if self.cfg_path.is_file() else {}
        current.update(cfg)
        self.cfg_path.write_text(json.dumps(current))
        return self

    def calls(self) -> list[list[str]]:
        return [json.loads(x) for x in self.log.read_text().splitlines()] if self.log.is_file() else []


def install(root: Path, version: str = "v1", plugin: str | None = None, **cfg) -> FakeVault:
    """A fake vault under `root` (skills/vault/bin/vault + ops/retro.md). `plugin` also writes a plugin manifest."""
    fv = FakeVault(root)
    fv.cli.parent.mkdir(parents=True, exist_ok=True)
    fv.ops.parent.mkdir(parents=True, exist_ok=True)
    fv.cli.write_text(SCRIPT.format(python=sys.executable))
    fv.cli.chmod(0o755)
    fv.ops.write_text("# retro ops\n")
    if plugin:
        manifest = fv.root / ".claude-plugin" / "plugin.json"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(json.dumps({"name": plugin}))
    fv.configure(version=version, **cfg)
    return fv
