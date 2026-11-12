"""Herdr transport (extension): each verifier runs through `herdr-agent run` in its own Herdr tab. Everything Herdr
lives here: availability, command lines, exit-code mapping, env stripping, parent-session attribution in hooks and the
agent-name transcript fallback."""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

from . import common
from .base import HookContext, SpawnResult, Transport

EXIT_STATUS = {0: "done", 3: "blocked", 4: "timeout", 5: "dead", 6: "no_result"}
ENV_STRIP = ["HERDR_ENV", "HERDR_PANE_ID", "HERDR_SOCKET_PATH", "HERDR_TAB_ID", "HERDR_WORKSPACE_ID", "HERDR_AGENT_DIR",
             "HERDR_AGENT_ID", "HERDR_AGENT_DEPTH", "HERDR_PARENT_SESSION_ID"]
HERDR_ROLES = {"reviewer": "verify-review", "qa": "verify-qa", "retro": "vault-retro"}
RETRO_TOOLS = ["Bash(git log *)", "Bash(git show *)", "Bash(git diff *)", "Bash(git ls-files *)", "Bash(git grep *)",
               "Bash(git rev-parse *)", "Bash(git status *)", "Bash(wc *)", "Bash(sort *)", "Bash(uniq *)",
               "Bash(head *)", "Bash(grep *)", "Bash(awk *)"]


def depth() -> int:
    try:
        return int(os.environ.get("HERDR_AGENT_DEPTH") or 0)
    except ValueError:
        return 0


def herdr_agent() -> str | None:
    """`VERIFY_HERDR_AGENT`, then `herdr-agent` on PATH, then the herdr-agent skill in the Claude home."""
    from .. import paths

    env = os.environ.get("VERIFY_HERDR_AGENT")
    if env:
        p = os.path.expanduser(env)
        return p if os.path.isfile(p) and os.access(p, os.X_OK) else None
    found = shutil.which("herdr-agent")
    if found:
        return found
    p = paths.claude_home() / "skills" / "herdr-agent" / "bin" / "herdr-agent"
    return str(p) if p.is_file() and os.access(p, os.X_OK) else None


def _bin() -> str:
    return herdr_agent() or os.path.expanduser(os.environ.get("VERIFY_HERDR_AGENT") or "herdr-agent")


def retro_tools() -> list[str]:
    from .. import paths

    return RETRO_TOOLS + [f"Bash({paths.tilde(paths.cli())} ledger *)"]


def role_cmd(role: str, *, run_dir: Path, att_dir: Path, repo_root: str, model: str, effort: str, cfg: dict) -> list[str]:
    """herdr-agent command for the reviewer or QA of one attempt."""
    t = (cfg.get("timeouts_s") or {}).get(role, 1200)
    prompt = common.task_prompt(Path(att_dir) / f"{role}.prompt.md")
    cmd = [_bin(), "run", HERDR_ROLES[role], "--model", model, "--effort", effort, "--cwd", str(repo_root),
           "--timeout", str(int(t)), "--json", "--prompt", prompt, "--",
           "--agent", common.agent_name(role), "--permission-mode", str(cfg.get("permission_mode") or "auto")]
    if role == "reviewer":
        return cmd + ["--strict-mcp-config", "--allowedTools", *common.claude_tools(role, run_dir)]
    mcp = Path(att_dir) / "mcp.json"
    if cfg.get("qa_mcp_transport", "mcp-config") == "mcp-config":
        cmd += ["--strict-mcp-config"]
        if mcp.is_file():
            cmd += ["--mcp-config", str(mcp)]
    return cmd + ["--allowedTools", *common.claude_tools(role, run_dir)]


def vault_root(cwd: str, has_repo: bool) -> str:
    from ..integrations import vault

    root = vault.vault_root(cwd, None if has_repo else "_harness") or vault.vault_root_hint()
    return str(root) if root else os.path.expanduser(vault.DEFAULT_VAULT_DIR)


def retro_cmd(*, run_dir: Path, cwd: str, has_repo: bool, cfg: dict) -> list[str]:
    m, e = (cfg.get("models") or {}).get("retro") or ["sonnet", "medium"]
    t = (cfg.get("timeouts_s") or {}).get("retro", 1200)
    vr = vault_root(cwd, has_repo)
    return [_bin(), "run", HERDR_ROLES["retro"], "--model", m, "--effort", e, "--cwd", str(cwd),
            "--timeout", str(int(t)), "--json", "--prompt", common.task_prompt(Path(run_dir) / "retro" / "task.md"),
            "--", "--agent", common.agent_name("retro"), "--permission-mode", str(cfg.get("permission_mode") or "auto"),
            "--strict-mcp-config", "--allowedTools", *retro_tools(), common.rule("Read", vr), common.rule("Edit", vr),
            common.rule("Read", run_dir)]


def wait_cmd(herdr_id: str, timeout: float | None = None) -> list[str]:
    cmd = [_bin(), "wait", herdr_id, "--close", "--json"]
    if timeout:
        cmd += ["--timeout", str(int(timeout))]
    return cmd


def _parse_doc(out: str) -> dict | None:
    out = (out or "").strip()
    if not out:
        return None
    for cand in (out, out[out.find("{"):] if "{" in out else ""):
        try:
            d = json.loads(cand)
            return d if isinstance(d, dict) else None
        except ValueError:
            continue
    return None


def run_herdr(cmd: list[str], timeout_s: float) -> tuple[int, dict | None, str]:
    """(exit code, --json doc or None, stderr). Our own timeout (herdr-agent's + margin) maps to exit 4."""
    import subprocess

    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s + 300 if timeout_s else None,
                           stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired as e:
        out = e.stdout.decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
        return 4, _parse_doc(out), "vloop: herdr-agent did not return in time"
    except OSError as e:
        return 1, None, str(e)
    return p.returncode, _parse_doc(p.stdout), p.stderr or ""


def _result(rc: int, doc: dict | None, stderr: str, info: dict) -> SpawnResult:
    info = dict(info or {})
    if doc:
        info["herdr_id"] = doc.get("id") or info.get("herdr_id")
        info["session_id"] = doc.get("session_id") or info.get("session_id")
    return SpawnResult(status=EXIT_STATUS.get(rc, "error"), rc=rc, doc=doc, stderr=stderr, info=info,
                       result=(doc or {}).get("result") if rc == 0 else None, screen=(doc or {}).get("screen"))


class HerdrTransport(Transport):
    name = "herdr"
    priority = 100
    label = "Herdr"
    supports_retro = True

    def available(self, cfg: dict) -> bool:
        return os.environ.get("HERDR_ENV") == "1" and depth() == 0 and herdr_agent() is not None

    def spawn_plan(self, role: str, ctx: dict, attempt: int, prompt_path: str | Path, model: str,
                   effort: str) -> list[str]:
        from .. import util

        return [f"{util.vloop_cli()} spawn {role} --run {ctx['run_id']}"]

    def spawn(self, role: str, ctx: dict, attempt: int, prompt_path: str | Path, model: str, effort: str,
              cfg: dict) -> SpawnResult:
        from .. import util

        att = Path(prompt_path).parent
        cmd = role_cmd(role, run_dir=att.parent, att_dir=att, repo_root=ctx["repo"]["root"], model=model,
                       effort=effort, cfg=cfg)
        timeout = float((cfg.get("timeouts_s") or {}).get(role, 1800))
        info = {"transport": self.name, "herdr_id": None, "session_id": None, "started_at": util.now_iso(),
                "model": model, "effort": effort}
        rc, doc, err = run_herdr(cmd, timeout)
        return _result(rc, doc, err, info)

    def spawn_retro(self, ctx: dict, run_dir: Path, cwd: str, has_repo: bool, cfg: dict) -> SpawnResult:
        from .. import util

        info = {"transport": self.name, "herdr_id": None, "session_id": None, "started_at": util.now_iso()}
        cmd = retro_cmd(run_dir=run_dir, cwd=cwd, has_repo=has_repo, cfg=cfg)
        rc, doc, err = run_herdr(cmd, float((cfg.get("timeouts_s") or {}).get("retro", 1200)))
        return _result(rc, doc, err, info)

    def wait(self, info: dict, timeout: float | None) -> SpawnResult:
        hid = (info or {}).get("herdr_id")
        if not hid:
            return SpawnResult(status="error", rc=1, stderr="no herdr agent id recorded", info=dict(info or {}))
        rc, doc, err = run_herdr(wait_cmd(hid, timeout), float(timeout or 0))
        return _result(rc, doc, err, info)

    def hook_context(self, payload: dict) -> HookContext:
        parent = os.environ.get("HERDR_PARENT_SESSION_ID")
        d = depth()
        if d >= 1 and parent:
            return HookContext(target_sid=parent, is_child=True, suppress_gate=True, depth=d, parent_session_id=parent)
        return HookContext(target_sid=str((payload or {}).get("session_id") or ""), suppress_gate=d >= 1,
                           depth=d if d >= 1 else None)

    def child_env_strip(self) -> list[str]:
        return list(ENV_STRIP)

    def waitable(self, info: dict) -> bool:
        return bool((info or {}).get("herdr_id"))

    def where(self, info: dict) -> str:
        return f"tab {(info or {}).get('herdr_id')}"

    def event_fields(self, info: dict) -> dict:
        return {"herdr_id": (info or {}).get("herdr_id")}

    def locate_transcript(self, info: dict) -> Path | None:
        from .. import metrics

        info = info or {}
        return metrics.find_session_transcript(info.get("session_id")) or \
            metrics.find_by_agent_name(info.get("herdr_id"), info.get("started_at"))
