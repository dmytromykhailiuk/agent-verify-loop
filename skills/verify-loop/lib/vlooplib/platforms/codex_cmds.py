"""Codex command lines: `codex exec` triage and headless runs, in-session spawn instructions.

`codex exec` has no flag to pick a custom agent, so the agent's instructions go into the stdin prompt (`-`).
`-c key=value` values are TOML; every value is rendered by `toml_value` so it always parses."""
from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path

from .base import task_prompt

BIN_ENV = "VERIFY_CODEX_BIN"
TRIAGE_SCHEMA = "triage.codex.schema.json"
SANDBOX = {"reviewer": "read-only", "security": "read-only", "perf": "read-only", "ux": "read-only",
           "qa": "workspace-write", "triage": "read-only"}
_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")


def codex_bin() -> str:
    return os.path.expanduser(os.environ.get(BIN_ENV) or "codex")


def toml_key(k: str) -> str:
    return k if _BARE_KEY.match(k) else json.dumps(k, ensure_ascii=False)


def toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return json.dumps(v)
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(toml_value(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{" + ", ".join(f"{toml_key(str(k))} = {toml_value(x)}" for k, x in v.items()) + "}"
    raise ValueError(f"no TOML form for {type(v).__name__}")


def cfg_arg(key: str, value) -> list[str]:
    return ["-c", f"{key}={toml_value(value)}"]


def mcp_args(mcp_config: str | Path) -> list[str]:
    """`-c mcp_servers.<name>.<field>=…` for each server of a Claude-style `{"mcpServers": {…}}` file."""
    doc = json.loads(Path(mcp_config).read_text(encoding="utf-8"))
    servers = (doc or {}).get("mcpServers") or {}
    out: list[str] = []
    for name, spec in servers.items():
        if not isinstance(spec, dict) or not spec.get("command"):
            continue
        base = f"mcp_servers.{toml_key(str(name))}"
        out += cfg_arg(f"{base}.command", str(spec["command"]))
        out += cfg_arg(f"{base}.args", [str(a) for a in spec.get("args") or []])
        if isinstance(spec.get("env"), dict) and spec["env"]:
            out += cfg_arg(f"{base}.env", {str(k): str(v) for k, v in spec["env"].items()})
    return out


def agent_prompt(agent: str, prompt: str) -> str:
    """The stdin prompt for `codex exec -`: the bundled agent's instructions, then the task."""
    from ..transports import common

    try:
        body = common.agent_body(agent)
    except OSError:
        body = ""
    return f"{body}\n\n---\n\n{prompt}\n" if body else f"{prompt}\n"


def _codex_cfg(cfg: dict) -> dict:
    return ((cfg.get("platforms") or {}).get("codex") or {})


def triage_out_file() -> str:
    from .. import util

    d = util.state_dir() / "tmp"
    d.mkdir(parents=True, exist_ok=True)
    fd, path = tempfile.mkstemp(prefix="triage-", suffix=".json", dir=str(d))
    os.close(fd)
    return path


def triage_cmd(prefix: list[str] | None, cfg: dict, schema_json: str, prompt: str) -> list[str]:
    """`codex exec -` with the strict triage schema; the answer lands in the `-o` file, usage in the `--json` events.
    `schema_json` (the Claude schema) is unused: Codex needs the strict variant as a file."""
    from .. import paths

    t = _codex_cfg(cfg).get("triage") or {}
    cmd = [*(prefix or [codex_bin()]), "exec", "-", "--output-schema", str(paths.schema_dir() / TRIAGE_SCHEMA),
           "-o", triage_out_file(), "--json", "-s", "read-only", "--skip-git-repo-check", "--ephemeral"]
    if t.get("model"):
        cmd += ["-m", str(t["model"])]
    return cmd + cfg_arg("model_reasoning_effort", str(t.get("effort") or "medium"))


def out_file(cmd: list[str]) -> str | None:
    for flag in ("-o", "--output-last-message"):
        if flag in cmd[:-1]:
            return cmd[cmd.index(flag) + 1]
    return None


def headless_cmd(role: str, *, agent: str, model: str, effort: str, prompt: str,
                 mcp_config: str | Path | None = None, allowed_tools: list[str] | None = None,
                 permission_mode: str | None = None, out_file: str | Path | None = None,
                 writable_roots: list[str] | None = None) -> list[str]:
    """`codex exec -` for a verifier: the prompt (agent instructions + task) comes on stdin (see `agent_prompt`).
    `allowed_tools`/`permission_mode` are Claude notions; the sandbox (`-s`) is the Codex equivalent."""
    cmd = [codex_bin(), "exec", "-", "--json", "-s", SANDBOX.get(role, "read-only"), "--skip-git-repo-check"]
    if out_file:
        cmd += ["-o", str(out_file)]
    if model:
        cmd += ["-m", model]
    if effort:
        cmd += cfg_arg("model_reasoning_effort", effort)
    if writable_roots and SANDBOX.get(role) == "workspace-write":
        cmd += cfg_arg("sandbox_workspace_write.writable_roots", [str(r) for r in writable_roots])
    if mcp_config:
        cmd += mcp_args(mcp_config)
    return cmd


def spawn_instructions(role: str, *, agent: str, model: str, attempt: int, prompt_path: str | Path) -> str:
    who = f"the custom agent `{agent}`" + (f" (model {model})" if model else "")
    return (f"spawn {who} as a subagent for verify {role} #{attempt} with exactly this message: "
            f"\"{task_prompt(prompt_path)}\" — then wait for it to finish")
