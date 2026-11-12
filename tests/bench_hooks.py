"""WP1 hook latency bench (§2.11 budgets): p95 of 20 runs per hook in a small temp repo. Prints OK when within."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import VLOOP, hermetic_env  # noqa: E402

RUNS = 20
BUDGETS_MS = {"stop (no state)": 80, "pre-tool Bash (fast path)": 80, "post-tool Edit (warm)": 150}


def p95(xs: list[float]) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(0.95 * (len(xs) - 1))))]


def timed(env: dict, event: str, payload: dict) -> float:
    raw = json.dumps(payload)
    t0 = time.perf_counter()
    p = subprocess.run([sys.executable, str(VLOOP), "hook", event], input=raw, capture_output=True, text=True, env=env)
    ms = (time.perf_counter() - t0) * 1000
    if p.returncode != 0:
        raise SystemExit(f"hook {event} failed: {p.stderr}")
    return ms


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="vloop-bench-"))
    try:
        env = hermetic_env(Path(os.path.realpath(tmp)))
        (tmp / "home").mkdir()
        env.update(VERIFY_STATE_DIR=str(tmp / "state"), VERIFY_CONFIG=str(tmp / "config.json"),
                   VERIFY_LOOP_ALLOW_TMP="1", CLAUDE_CODE_ENTRYPOINT="cli")
        (tmp / "state").mkdir()
        (tmp / "state" / "gc.stamp").write_text(time.strftime("%Y-%m-%d") + "\n")
        repo = tmp / "repo"
        repo.mkdir()
        g = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)  # noqa: E731
        g("init", "-q", "-b", "master")
        for i in range(20):
            (repo / f"f{i}.js").write_text(f"export const v{i} = {i};\n")
        g("add", "-A")
        g("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
        repo = Path(os.path.realpath(repo))
        target = repo / "f1.js"

        results = {}
        results["stop (no state)"] = [timed(env, "stop", {"session_id": "nostate", "cwd": str(repo),
                                                          "hook_event_name": "Stop", "last_assistant_message": "x"})
                                      for _ in range(RUNS)]
        results["pre-tool Bash (fast path)"] = [
            timed(env, "pre-tool", {"session_id": "b1", "cwd": str(repo), "hook_event_name": "PreToolUse",
                                    "tool_name": "Bash", "tool_input": {"command": "ls"}}) for _ in range(RUNS)]
        edit = {"session_id": "e1", "cwd": str(repo), "hook_event_name": "PostToolUse", "tool_name": "Edit",
                "tool_input": {"file_path": str(target)}, "tool_response": {}}
        timed(env, "prompt", {"session_id": "e1", "cwd": str(repo), "prompt": "x", "transcript_path": "/dev/null"})
        timed(env, "post-tool", edit)  # warm-up
        target.write_text("changed\n")
        results["post-tool Edit (warm)"] = [timed(env, "post-tool", edit) for _ in range(RUNS)]

        ok = True
        for name, xs in results.items():
            v = p95(xs)
            within = v < BUDGETS_MS[name]
            ok &= within
            print(f"{name:28s} p95 {v:6.1f} ms  (budget {BUDGETS_MS[name]} ms) {'ok' if within else 'OVER'}")
        print("OK" if ok else "FAIL")
        return 0 if ok else 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
