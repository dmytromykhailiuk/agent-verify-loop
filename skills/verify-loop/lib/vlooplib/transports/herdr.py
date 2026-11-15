"""Herdr transport (extension): each verifier runs through `herdr-agent run` in its own Herdr tab. Everything Herdr
lives here: availability, command lines, exit-code mapping, env stripping, parent-session attribution in hooks and the
agent-name transcript fallback."""
from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path

from ..roles import REVIEW_ROLES
from . import common
from .base import HookContext, SpawnResult, Transport

EXIT_STATUS = {0: "done", 3: "blocked", 4: "timeout", 5: "dead", 6: "no_result", 7: "api_error"}
MIN_INTEGRATION = 1
ENV_STRIP = ["HERDR_ENV", "HERDR_PANE_ID", "HERDR_SOCKET_PATH", "HERDR_TAB_ID", "HERDR_WORKSPACE_ID", "HERDR_AGENT_DIR",
             "HERDR_AGENT_ID", "HERDR_AGENT_DEPTH", "HERDR_PARENT_SESSION_ID", "HERDR_ROOT_SESSION_ID"]
HERDR_ROLES = {"reviewer": "verify-review", "security": "verify-security", "perf": "verify-perf", "ux": "verify-ux",
               "qa": "verify-qa", "retro": "vault-retro"}
RETRO_TOOLS = ["Bash(git log *)", "Bash(git show *)", "Bash(git diff *)", "Bash(git ls-files *)", "Bash(git grep *)",
               "Bash(git rev-parse *)", "Bash(git status *)", "Bash(wc *)", "Bash(sort *)", "Bash(uniq *)",
               "Bash(head *)", "Bash(grep *)", "Bash(awk *)"]


def depth() -> int:
    try:
        return int(os.environ.get("HERDR_AGENT_DEPTH") or 0)
    except ValueError:
        return 0


def _executable(path: str | None) -> str | None:
    if not path:
        return None
    p = os.path.expanduser(path)
    return p if os.path.isfile(p) and os.access(p, os.X_OK) else None


def herdr_agent() -> str | None:
    """`VERIFY_HERDR_AGENT`, then `HERDR_AGENT`, then `herdr-agent` on PATH, then the herdr-agent skill in the Claude
    home."""
    from .. import paths

    override = os.environ.get("VERIFY_HERDR_AGENT")
    if override:
        return _executable(override)
    return (_executable(os.environ.get("HERDR_AGENT")) or shutil.which("herdr-agent")
            or _executable(str(paths.claude_home() / "skills" / "herdr-agent" / "bin" / "herdr-agent")))


def _integration_cache() -> Path:
    from .. import util

    return util.state_dir() / "herdr-integration.json"


def _cache_key(binary: str) -> tuple[str, int] | None:
    try:
        real = os.path.realpath(binary)
        version_file = Path(real).parent.parent / "lib" / "herdragent" / "__init__.py"
        try:
            stamp = os.stat(version_file).st_mtime_ns
        except OSError:
            stamp = os.stat(real).st_mtime_ns
    except OSError:
        return None
    return real, stamp


def integration_number(binary: str) -> int | None:
    """`integration` of `herdr-agent version --json`; only successful answers are cached on disk, per resolved CLI and
    the mtime of the herdr-agents lib version file."""
    from .. import util

    key = _cache_key(binary)
    if key is None:
        return None
    real, stamp = key
    cache = util.read_json(_integration_cache(), {})
    hit = cache.get(real) if isinstance(cache, dict) else None
    if isinstance(hit, dict) and hit.get("mtime") == stamp and isinstance(hit.get("integration"), int):
        return hit["integration"]
    import subprocess

    try:
        p = subprocess.run([binary, "version", "--json"], capture_output=True, text=True, timeout=15,
                           stdin=subprocess.DEVNULL)
        doc = _parse_doc(p.stdout) if p.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None
    value = (doc or {}).get("integration")
    if not isinstance(value, int) or isinstance(value, bool):
        return None
    cache = cache if isinstance(cache, dict) else {}
    cache[real] = {"mtime": stamp, "integration": value}
    try:
        util.atomic_write_json(_integration_cache(), cache)
    except OSError:
        pass
    return value


_warned: set[str] = set()


def integration_ok(binary: str | None = None) -> bool:
    binary = binary or herdr_agent()
    if not binary:
        return False
    number = integration_number(binary)
    if number is not None and number >= MIN_INTEGRATION:
        return True
    if binary not in _warned:
        _warned.add(binary)
        found = f"integration {number}" if number is not None else "no integration number"
        sys.stderr.write(f"vloop: {binary} reports {found}; the Herdr transport needs integration >= "
                         f"{MIN_INTEGRATION}. Update herdr-agents; using the Agent-tool transport.\n")
    return False


def _bin() -> str:
    return herdr_agent() or os.path.expanduser(os.environ.get("VERIFY_HERDR_AGENT") or "herdr-agent")


def retro_tools() -> list[str]:
    from .. import paths

    return RETRO_TOOLS + [f"Bash({paths.tilde(paths.cli())} ledger *)"]


def role_cmd(role: str, *, run_dir: Path, att_dir: Path, repo_root: str, model: str, effort: str, cfg: dict) -> list[str]:
    """herdr-agent command for a review role or QA of one attempt."""
    t = (cfg.get("timeouts_s") or {}).get(role, 1200)
    prompt = common.task_prompt(Path(att_dir) / f"{role}.prompt.md")
    cmd = [_bin(), "run", HERDR_ROLES[role], "--model", model, "--effort", effort, "--cwd", str(repo_root),
           "--timeout", str(int(t)), "--json", "--prompt", prompt, "--",
           "--agent", common.agent_name(role), "--permission-mode", str(cfg.get("permission_mode") or "auto")]
    if role in REVIEW_ROLES:
        return cmd + ["--strict-mcp-config", "--allowedTools", *common.claude_tools(role, run_dir)]
    mcp = Path(att_dir) / "mcp.json"
    if cfg.get("qa_mcp_transport", "mcp-config") == "mcp-config":
        cmd += ["--strict-mcp-config"]
        if mcp.is_file():
            cmd += ["--mcp-config", str(mcp)]
    return cmd + ["--allowedTools", *common.claude_tools(role, run_dir)]


def vault_root(cwd: str, has_repo: bool) -> str:
    from ..integrations import vault

    root = vault.vault_root(cwd, None if has_repo else "_harness") or vault.vault_root_hint(cwd)
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


def send_cmd(herdr_id: str, prompt: str, timeout: float | None = None, *, prompt_file: bool = False) -> list[str]:
    cmd = [_bin(), "send", herdr_id, "--prompt-file" if prompt_file else "--prompt", prompt, "--wait", "--close",
           "--json"]
    if timeout:
        cmd += ["--timeout", str(int(timeout))]
    return cmd


def resume_cmd(herdr_id: str) -> str:
    return f"{_bin()} send {herdr_id} --prompt continue --wait --close"


def prompt_delivered(herdr_id: str) -> bool:
    """False only when `herdr-agent status` proves the agent never received its prompt."""
    import subprocess

    try:
        p = subprocess.run([_bin(), "status", herdr_id], capture_output=True, text=True, timeout=30,
                           stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return True
    meta = _parse_doc(p.stdout) if p.returncode == 0 else None
    if not meta or "last_prompt_at_ns" not in meta:
        return True
    return bool(meta["last_prompt_at_ns"])


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
        info["herdr_dir"] = doc.get("dir") or info.get("herdr_dir")
    if rc == 7 and info.get("herdr_id"):
        info["resume"] = resume_cmd(info["herdr_id"])
    return SpawnResult(status=EXIT_STATUS.get(rc, "error"), rc=rc, doc=doc, stderr=stderr, info=info,
                       result=(doc or {}).get("result") if rc == 0 else None, screen=(doc or {}).get("screen"))


class HerdrTransport(Transport):
    name = "herdr"
    priority = 100
    label = "Herdr"
    supports_retro = True

    def available(self, cfg: dict) -> bool:
        if os.environ.get("HERDR_ENV") != "1" or depth() != 0:
            return False
        binary = herdr_agent()
        return binary is not None and integration_ok(binary)

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
        info = dict(info or {})
        d = info.get("herdr_dir")
        prompt = os.path.join(d, "prompt.md") if d else None
        if prompt and os.path.isfile(prompt) and not prompt_delivered(hid):
            cmd = send_cmd(hid, prompt, timeout, prompt_file=True)
        elif info.get("status") == "api_error":
            cmd = send_cmd(hid, "continue", timeout)
        else:
            cmd = wait_cmd(hid, timeout)
        rc, doc, err = run_herdr(cmd, float(timeout or 0))
        return _result(rc, doc, err, info)

    def hook_context(self, payload: dict) -> HookContext:
        target = os.environ.get("HERDR_ROOT_SESSION_ID") or os.environ.get("HERDR_PARENT_SESSION_ID")
        d = depth()
        if d >= 1 and target:
            return HookContext(target_sid=target, is_child=True, suppress_gate=True, depth=d,
                               parent_session_id=os.environ.get("HERDR_PARENT_SESSION_ID") or target)
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
