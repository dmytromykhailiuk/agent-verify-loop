"""Small shared helpers: time, paths, atomic writes, subprocess, globs, formatting."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from . import paths


# --------------------------------------------------------------------------- time


def now_iso() -> str:
    t = time.time()
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + f".{int(t * 1000) % 1000:03d}Z"


def today() -> str:
    return time.strftime("%Y-%m-%d")


def parse_iso(s: str | None) -> float | None:
    """Epoch seconds of an `now_iso()` string (or any ISO-8601 with Z / offset); None when unparsable."""
    if not s:
        return None
    from datetime import datetime

    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def local_hhmm(s: str | None) -> str:
    t = parse_iso(s)
    return time.strftime("%H:%M", time.localtime(t)) if t is not None else "?"


# --------------------------------------------------------------------------- paths


def state_dir() -> Path:
    p = paths.state_dir()
    if not p.is_dir():
        p.mkdir(parents=True, exist_ok=True, mode=0o700)
    return p


def skill_dir() -> Path:
    return paths.skill_dir()


def vloop_cli() -> str:
    return paths.tilde(paths.cli())


def session_id_from_env() -> str | None:
    from . import platforms

    return platforms.detect().session_id_from_env()


# --------------------------------------------------------------------------- files


def atomic_write_text(path: Path, text: str, mode: int = 0o644) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(path.parent / f".{path.name}.{os.getpid()}.{time.monotonic_ns()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_json(path: Path, obj, mode: int = 0o644) -> None:
    atomic_write_text(path, json.dumps(obj, indent=2, ensure_ascii=False) + "\n", mode)


def read_json(path: Path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def log_error(component: str, exc: BaseException) -> None:
    try:
        p = state_dir() / "hook-errors.log"
        with open(p, "a", encoding="utf-8") as f:
            f.write(f"{now_iso()}\t{component}\t{exc!r}\n")
    except BaseException:
        pass


# --------------------------------------------------------------------------- subprocess


def run(cmd: list[str], cwd=None, env=None, timeout: float = 30, input_text: str | None = None) -> tuple[int, str, str]:
    import subprocess

    try:
        p = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            env=env,
            input=input_text,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired as e:
        return 124, "", f"timeout after {timeout}s: {e}"
    except FileNotFoundError as e:
        return 127, "", str(e)
    except (OSError, subprocess.SubprocessError) as e:
        return 1, "", str(e)


# --------------------------------------------------------------------------- names and globs


def slugify(s: str) -> str:
    import re

    s = re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")
    return s or "project"


def glob_to_regex(pat: str):
    import re

    i, out = 0, ""
    while i < len(pat):
        c = pat[i]
        if c == "*":
            if pat[i : i + 2] == "**":
                i += 2
                if pat[i : i + 1] == "/":
                    i += 1
                    out += "(?:.*/)?"
                else:
                    out += ".*"
                continue
            out += "[^/]*"
        elif c == "?":
            out += "[^/]"
        elif c == "[":
            j = pat.find("]", i + 1)
            if j == -1:
                out += re.escape(c)
            else:
                out += pat[i : j + 1]
                i = j
        else:
            out += re.escape(c)
        i += 1
    return re.compile("^" + out + "$")


def path_matcher(patterns: list[str]):
    compiled = []
    for pat in patterns or []:
        compiled.append(("/" in pat, glob_to_regex(pat)))

    def matches(path: str) -> bool:
        base = path.rsplit("/", 1)[-1]
        for has_slash, rx in compiled:
            if rx.match(path if has_slash else base):
                return True
        return False

    return matches


# --------------------------------------------------------------------------- formatting


def render_template(text: str, values: dict[str, str]) -> str:
    import re

    def sub(m):
        name = m.group(1)
        if name not in values:
            raise KeyError(name)
        v = values[name]
        if v is None or v == "":
            return "(none)"
        return str(v)

    return re.sub(r"\{\{\s*([A-Za-z0-9_.-]+)\s*\}\}", sub, text)


def short_dur(seconds: float) -> str:
    s = int(round(max(0.0, float(seconds or 0))))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"


def fmt_tokens(n: int) -> str:
    n = int(n or 0)
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        return f"{round(n / 1000)}k"
    v = f"{n / 1_000_000:.1f}".rstrip("0").rstrip(".")
    return f"{v}M"


def fmt_paths(paths: list[str], n: int = 3) -> str:
    paths = list(paths or [])
    head = ", ".join(paths[:n])
    if len(paths) > n:
        head += f", +{len(paths) - n} more"
    return head
