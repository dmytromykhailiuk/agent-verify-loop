"""Headless transport: vloop runs the verifier as a child process (`claude -p` / `codex exec`). Always used for
browser QA (an in-session agent cannot get the Playwright MCP server); opt-in for every role with `transport: headless`.

The child runs detached (own process group, output to files in the attempt dir, exit code written by a /bin/sh
wrapper), so `spawn` can stop waiting at the role's timeout and `spawn-wait` can pick the run up from another process.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

from . import common
from .base import HookContext, SpawnResult, Transport

CMD_ENV = "VERIFY_HEADLESS_CMD"
GUARD_ENV = "VLOOP_GUARD_ROLE"
RUN_DIR_ENV = "VLOOP_RUN_DIR"
PARENT_ENV = "VLOOP_PARENT_SESSION_ID"
WRAPPER = 'rc_file=$1; shift; "$@"; echo $? > "$rc_file"'
POLL_S = 0.5
_procs: dict[int, subprocess.Popen] = {}


def _prefix() -> list[str] | None:
    raw = os.environ.get(CMD_ENV)
    if not raw:
        return None
    try:
        v = json.loads(raw)
    except ValueError:
        return None
    return v if isinstance(v, list) and v and all(isinstance(x, str) for x in v) else None


def files(att_dir: Path, role: str) -> dict:
    base = Path(att_dir) / f"{role}.headless"
    return {"stdout": f"{base}.stdout", "stderr": f"{base}.stderr", "rc": f"{base}.rc", "stdin": f"{base}.stdin",
            "final": str(Path(att_dir) / f"{role}.final.md")}


def build(role: str, *, platform: str, att_dir: Path, run_dir: Path, model: str | None, effort: str,
          cfg: dict) -> tuple[list[str], str | None]:
    """(argv, stdin text or None) for one verifier run on this platform."""
    from .. import platforms
    from ..platforms import codex_cmds

    prompt = common.task_prompt(Path(att_dir) / f"{role}.prompt.md")
    mcp = Path(att_dir) / "mcp.json"
    mcp_config = str(mcp) if role == "qa" and mcp.is_file() else None
    f = files(att_dir, role)
    if platform == "codex":
        cmd = codex_cmds.headless_cmd(role, agent=common.agent_name(role), model=model or "", effort=effort,
                                      prompt=prompt, mcp_config=mcp_config, out_file=f["final"],
                                      writable_roots=[str(run_dir), common.tmpdir()] if role == "qa" else None)
        stdin = codex_cmds.agent_prompt(common.AGENT_TYPES[role], prompt)
    else:
        cmd = platforms.get(platform).headless_cmd(
            role, agent=common.agent_name(role), model=model or "", effort=effort, prompt=prompt,
            mcp_config=mcp_config, allowed_tools=common.claude_tools(role, run_dir),
            permission_mode=str(cfg.get("permission_mode") or "auto"))
        stdin = None
    pre = _prefix()
    if pre:
        cmd = [*pre, *cmd[1:]]
    return cmd, stdin


def child_env(role: str, run_dir: Path, parent_sid: str | None) -> dict:
    from . import strip_env

    drop = set(strip_env())
    env = {k: v for k, v in os.environ.items() if k not in drop}
    env.update({GUARD_ENV: role, RUN_DIR_ENV: str(run_dir)})
    if parent_sid:
        env[PARENT_ENV] = parent_sid
    return env


def launch(cmd: list[str], *, cwd: str, env: dict, stdin: str | None, f: dict) -> int:
    for k in ("rc", "stdout", "stderr"):
        Path(f[k]).unlink(missing_ok=True)
    if stdin is not None:
        Path(f["stdin"]).write_text(stdin, encoding="utf-8")
    fin = open(f["stdin"], "rb") if stdin is not None else subprocess.DEVNULL
    try:
        with open(f["stdout"], "wb") as out, open(f["stderr"], "wb") as err:
            p = subprocess.Popen(["/bin/sh", "-c", WRAPPER, "vloop-headless", f["rc"], *cmd], cwd=cwd, env=env,
                                 stdin=fin, stdout=out, stderr=err, start_new_session=True)
    finally:
        if fin is not subprocess.DEVNULL:
            fin.close()
    _procs[p.pid] = p
    return p.pid


def _alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _read(path: str | None) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace") if path else ""
    except OSError:
        return ""


def _rc(path: str | None) -> int | None:
    try:
        return int(_read(path).strip())
    except ValueError:
        return None


def _block(info: dict, timeout: float | None) -> str:
    """'exited', 'timeout' or 'dead'."""
    pid = info.get("pid")
    rcf = (info.get("files") or {}).get("rc")
    deadline = None if timeout is None else time.monotonic() + max(0.0, float(timeout))
    proc = _procs.get(int(pid)) if pid else None
    if proc is not None:
        try:
            proc.wait(timeout=None if deadline is None else max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            return "timeout"
        _procs.pop(int(pid), None)
        return "exited" if _rc(rcf) is not None else "dead"
    while True:
        if _rc(rcf) is not None:
            return "exited"
        if not _alive(pid):
            return "exited" if _rc(rcf) is not None else "dead"
        if deadline is not None and time.monotonic() >= deadline:
            return "timeout"
        time.sleep(POLL_S)


def finish(info: dict, state: str) -> SpawnResult:
    from .. import util

    info = dict(info)
    f = info.get("files") or {}
    if state == "timeout":
        return SpawnResult(status="timeout", rc=4, info=info)
    out, err = _read(f.get("stdout")), _read(f.get("stderr"))
    started = util.parse_iso(info.get("started_at"))
    dur = max(0.0, time.time() - started) if started is not None else 0.0
    if state == "dead":
        return SpawnResult(status="dead", rc=5, stderr=err or "the headless agent exited without an exit code",
                           info=info)
    rc = _rc(f.get("rc"))
    if info.get("platform") == "codex":
        usage = common.codex_usage(out, info.get("model"), dur)
        info["session_id"] = usage.pop("session_id", None) or info.get("session_id")
        result, doc = _read(f.get("final")) or None, None
    else:
        doc = None
        try:
            d = json.loads(out.strip() or "null")
            doc = d if isinstance(d, dict) else None
        except ValueError:
            pass
        usage = common.claude_usage(doc, info.get("model"), dur)
        info["session_id"] = (doc or {}).get("session_id") or info.get("session_id")
        result = (doc or {}).get("result") if isinstance((doc or {}).get("result"), str) else None
    info["usage"] = usage
    if rc != 0:
        return SpawnResult(status="error", rc=rc if rc is not None else 1, doc=doc,
                           stderr=(err or out).strip()[-2000:] or f"exit {rc}", info=info, result=result)
    if doc is not None and doc.get("is_error"):
        return SpawnResult(status="error", rc=1, doc=doc, stderr=str(doc.get("result") or "is_error")[:2000],
                           info=info, result=None)
    if not result and not (Path(f.get("final") or "").parent / f"{info.get('verifier')}.json").exists():
        return SpawnResult(status="no_result", rc=6, doc=doc, stderr="the agent finished without a final message",
                           info=info)
    return SpawnResult(status="done", rc=0, doc=doc, stderr=err, info=info, result=result)


class HeadlessTransport(Transport):
    name = "headless"
    priority = -10
    label = "headless"
    auto = False

    def available(self, cfg: dict) -> bool:
        return True

    def spawn_plan(self, role: str, ctx: dict, attempt: int, prompt_path: str | Path, model: str,
                   effort: str) -> list[str]:
        from .. import util

        return [f"{util.vloop_cli()} spawn {role} --run {ctx['run_id']}"]

    def spawn(self, role: str, ctx: dict, attempt: int, prompt_path: str | Path, model: str, effort: str,
              cfg: dict) -> SpawnResult:
        from .. import platforms, util

        att = Path(prompt_path).parent
        run_dir = att.parent
        plat = platforms.detect().name
        m = common.platform_model(cfg, role, model, plat)
        info = {"transport": self.name, "platform": plat, "verifier": role, "session_id": None,
                "started_at": util.now_iso(), "model": m or ("inherit" if plat == "codex" else model),
                "effort": effort, "files": files(att, role), "pid": None}
        try:
            cmd, stdin = build(role, platform=plat, att_dir=att, run_dir=run_dir, model=m, effort=effort, cfg=cfg)
            info["pid"] = launch(cmd, cwd=ctx["repo"]["root"], env=child_env(role, run_dir, ctx.get("session_id")),
                                 stdin=stdin, f=info["files"])
        except (OSError, ValueError) as e:
            return SpawnResult(status="error", rc=1, stderr=str(e), info=info)
        timeout = float((cfg.get("timeouts_s") or {}).get(role, 1800))
        return finish(info, _block(info, timeout))

    def wait(self, info: dict, timeout: float | None) -> SpawnResult:
        info = dict(info or {})
        if not info.get("pid"):
            return SpawnResult(status="error", rc=1, stderr="no headless agent recorded", info=info)
        return finish(info, _block(info, timeout))

    def hook_context(self, payload: dict) -> HookContext:
        sid = str((payload or {}).get("session_id") or "")
        if os.environ.get(GUARD_ENV):
            return HookContext(target_sid=sid, is_child=False, suppress_gate=True)
        return HookContext(target_sid=sid)

    def child_env_strip(self) -> list[str]:
        return list(common.CLAUDE_SESSION_ENV) + [GUARD_ENV, RUN_DIR_ENV, PARENT_ENV]

    def waitable(self, info: dict) -> bool:
        return bool((info or {}).get("pid"))

    def where(self, info: dict) -> str:
        return f"pid {(info or {}).get('pid')}"

    def event_fields(self, info: dict) -> dict:
        return {"pid": (info or {}).get("pid")}

    def locate_transcript(self, info: dict) -> Path | None:
        from .. import platforms

        info = info or {}
        return platforms.get(info.get("platform") or platforms.detect().name).transcript_for(info.get("session_id"))
