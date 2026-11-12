"""QA toolbox: browser discovery, `--install-browser`, Playwright pointer, `auth capture` / `auth status` (§2.10.2, §2.17)."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import config, paths, util

PINNED_MCP = "0.0.83"
MIN_NODE = 18
TOOLBOX_FILES = ("package.json", "package-lock.json", "capture-auth.mjs")
NPM_TIMEOUT_S = 900


def _err(msg: str, code: int = 2) -> int:
    sys.stderr.write(f"vloop: error: {msg}\n")
    return code


def installed_toolbox_dir() -> Path:
    return paths.data_dir() / "qa-toolbox"


def mcp_cli(toolbox: Path | None = None) -> Path:
    return (toolbox or toolbox_dir()) / "node_modules" / "@playwright" / "mcp" / "cli.js"


def toolbox_dir() -> Path:
    """The installed toolbox (data dir, survives plugin updates); a toolbox installed in the skill dir by an older
    version still works; with none installed, the location `--install-browser` writes to."""
    for cand in (installed_toolbox_dir(), paths.qa_toolbox_src_dir()):
        if mcp_cli(cand).is_file():
            return cand
    return installed_toolbox_dir()


# --------------------------------------------------------------------------- browser


@dataclass(frozen=True)
class Browser:
    kind: str
    executable: str | None = None
    label: str = ""
    system: bool = True

    def describe(self) -> str:
        if not self.system:
            return "none found; Playwright's bundled chromium will be used"
        return f"{self.kind}: {self.executable or self.label}"


def _os_name() -> str:
    plat = sys.platform
    if plat.startswith("darwin"):
        return "macos"
    if plat.startswith("win"):
        return "windows"
    return "linux"


def _mac_roots() -> list[Path]:
    return [Path("/Applications"), Path.home() / "Applications"]


def _mac_executable(app: Path) -> str | None:
    exe = app / "Contents" / "MacOS" / app.stem
    return str(exe) if exe.is_file() else None


def _app_browser(path: Path, kind: str, with_executable: bool) -> Browser:
    exe = None
    if path.is_file():
        exe = str(path)
    elif with_executable:
        exe = _mac_executable(path)
    return Browser(kind, exe, str(path))


def find_chrome() -> Browser:
    """First usable browser: PW_MCP_CHROME_APP (when set, only that: a missing path means "use the bundled
    chromium"), then Chrome / Chromium / Edge for this OS, else Playwright's bundled chromium (`system=False`)."""
    env = os.environ.get("PW_MCP_CHROME_APP")
    if env:
        if os.path.exists(os.path.expanduser(env)):
            return _app_browser(Path(env).expanduser(), "chrome", True)
        return Browser("chromium", None, "", system=False)
    system = _os_name()
    if system == "macos":
        for root in _mac_roots():
            for app, kind in (("Google Chrome.app", "chrome"), ("Chromium.app", "chromium"),
                              ("Microsoft Edge.app", "msedge")):
                if (root / app).exists():
                    return _app_browser(root / app, kind, kind != "chrome")
    elif system == "linux":
        for cmd, kind in (("google-chrome", "chrome"), ("google-chrome-stable", "chrome"), ("chromium", "chromium"),
                          ("chromium-browser", "chromium")):
            found = shutil.which(cmd)
            if found:
                return Browser(kind, found, found)
    else:
        roots = [os.environ.get(v) for v in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")]
        for kind, rel in (("chrome", "Google/Chrome/Application/chrome.exe"),
                          ("msedge", "Microsoft/Edge/Application/msedge.exe")):
            for root in filter(None, roots):
                cand = Path(root) / rel
                if cand.is_file():
                    return Browser(kind, None, str(cand))
    return Browser("chromium", None, "", system=False)


def check_node() -> tuple[bool, str]:
    node = shutil.which("node")
    if not node:
        return False, "node: missing (install Node.js ≥ 18)"
    rc, out, _ = util.run([node, "--version"], timeout=10)
    ver = out.strip()
    try:
        major = int(ver.lstrip("v").split(".")[0])
    except ValueError:
        return False, f"node: unknown version {ver!r} at {node} (need ≥ {MIN_NODE})"
    if rc != 0 or major < MIN_NODE:
        return False, f"node: too old ({ver} at {node}; need ≥ {MIN_NODE})"
    return True, f"node: ok ({ver})"


def check_chrome() -> tuple[bool, str]:
    b = find_chrome()
    if b.system:
        return True, f"chrome: ok ({b.describe()})"
    return False, "chrome: missing (install Google Chrome, Chromium or Edge; else Playwright's bundled chromium is used)"


def check_toolbox(toolbox: Path | None = None) -> tuple[bool, str]:
    toolbox = toolbox or toolbox_dir()
    cli = mcp_cli(toolbox)
    if not cli.is_file():
        return False, f"playwright-mcp: missing ({toolbox}; run {util.vloop_cli()} setup --install-browser)"
    pkg = util.read_json(cli.parent / "package.json", {}) or {}
    ver = pkg.get("version") or "?"
    if ver != PINNED_MCP:
        return False, (f"playwright-mcp: version {ver} installed, pinned {PINNED_MCP} "
                       f"(run {util.vloop_cli()} setup --install-browser)")
    return True, f"playwright-mcp: ok ({ver})"


def _npm(npm: str, args: list[str], cwd: Path) -> int:
    sys.stderr.write(f"$ npm {' '.join(args)}  (in {cwd})\n")
    try:
        sys.stderr.fileno()
        sink = sys.stderr
    except (OSError, ValueError, AttributeError):
        sink = subprocess.PIPE
    try:
        r = subprocess.run([npm, *args], cwd=str(cwd), stdin=subprocess.DEVNULL, stdout=sink,
                           stderr=subprocess.STDOUT if sink is subprocess.PIPE else sink, text=True,
                           timeout=NPM_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return 124
    except OSError as e:
        sys.stderr.write(f"vloop: cannot run npm: {e}\n")
        return 127
    if sink is subprocess.PIPE and r.stdout:
        sys.stderr.write(r.stdout)
    return r.returncode


def install_browser(dest: Path | None = None, dry_run: bool = False) -> int:
    """Copy the toolbox manifest and capture script to the data dir and `npm ci` (falling back to `npm install`) there.
    The plugin root is replaced on plugin updates, so nothing is installed into the bundle."""
    dest = Path(dest) if dest else installed_toolbox_dir()
    ok_node, node_line = check_node()
    npm = shutil.which("npm")
    if not ok_node or not npm:
        sys.stderr.write("vloop: the browser toolbox needs Node.js ≥ 18 and npm on PATH; "
                         f"{node_line if not ok_node else 'npm: missing'}\n")
        return 1
    if dry_run:
        print(f"would install the browser toolbox into {paths.tilde(dest)} (npm ci)")
        return 0
    src = paths.qa_toolbox_src_dir()
    dest.mkdir(parents=True, exist_ok=True)
    for name in TOOLBOX_FILES:
        if (src / name).is_file():
            shutil.copy2(src / name, dest / name)
    rc = _npm(npm, ["ci", "--no-audit", "--no-fund"], dest) if (dest / "package-lock.json").is_file() else 1
    if rc != 0:
        rc = _npm(npm, ["install", "--no-audit", "--no-fund"], dest)
    if rc != 0:
        sys.stderr.write(f"vloop: npm exited {rc}; the browser toolbox is not installed\n")
        return 1
    ok, line = check_toolbox(dest)
    print(f"browser toolbox: {paths.tilde(dest)}\n{line}")
    return 0 if ok else 1


def cmd_check_browser(args=None) -> int:
    results = [check_node(), check_chrome(), check_toolbox()]
    for _, line in results:
        print(line)
    return 0 if all(ok for ok, _ in results) else 1


# --------------------------------------------------------------------------- Playwright pointer (§2.17)


def write_pointer(ctx: dict, attempt: int, att_dir: Path, cfg: dict, project: dict, auth: dict | None) -> dict:
    pw = cfg.get("playwright") or {}
    storage = None
    if auth and auth.get("status") in ("ok", "stale") and auth.get("exists") and auth.get("path"):
        storage = auth["path"]
    ptr = {"run_id": ctx["run_id"], "attempt": attempt, "output_dir": str(Path(att_dir) / "qa-artifacts"),
           "storage_state": storage, "ignore_https_errors": bool(project.get("ignore_https_errors", True)),
           "viewport": pw.get("viewport") or "1366x900", "written_at": util.now_iso()}
    util.atomic_write_json(Path(att_dir) / "pw-pointer.json", ptr)
    util.atomic_write_json(util.state_dir() / "qa-current" / f"{ctx['repo']['key']}.json", ptr)
    mcp = {"mcpServers": {"playwright": {"type": "stdio", "command": str(paths.pw_mcp()),
                                         "args": ["--pointer", str(Path(att_dir) / "pw-pointer.json")]}}}
    util.atomic_write_json(Path(att_dir) / "mcp.json", mcp)
    return ptr


# --------------------------------------------------------------------------- auth


def _repo_root(path: str | None) -> str:
    from . import gitsnap

    p = os.path.abspath(os.path.expanduser(path or os.getcwd()))
    repo = gitsnap.repo_for(p)
    return repo["root"] if repo else p


def state_summary(path: Path) -> dict:
    """Counts only — never values: cookies, origins with localStorage, earliest cookie expiry (date or None)."""
    data = util.read_json(path, {}) or {}
    cookies = [c for c in data.get("cookies") or [] if isinstance(c, dict)]
    origins = [o for o in data.get("origins") or [] if isinstance(o, dict) and o.get("localStorage")]
    exps = []
    for c in cookies:
        try:
            e = float(c.get("expires") or 0)
        except (TypeError, ValueError):
            e = 0
        if e > 0:
            exps.append(e)
    earliest = datetime.fromtimestamp(min(exps), timezone.utc).strftime("%Y-%m-%d") if exps else None
    return {"cookies": len(cookies), "origins": len(origins), "earliest_expiry": earliest}


def status_text(st: dict) -> str:
    line = f"auth: {st['status']} · {st['path']}"
    if st.get("exists") and st.get("age_days") is not None:
        line += f" · saved {st['age_days']} days ago"
    return line


def cmd_auth_status(args) -> int:
    from . import classify

    root = _repo_root(args.repo)
    st = classify.auth_status(root, config.load(root))
    if args.json:
        print(json.dumps(st, indent=2, ensure_ascii=False))
    else:
        print(status_text(st))
    return 0


def cmd_auth_capture(args) -> int:
    from . import classify

    root = _repo_root(args.repo)
    path = Path(args.out).expanduser() if args.out else Path(classify.auth_status(root, config.load(root))["path"])
    toolbox = toolbox_dir()
    script = toolbox / "capture-auth.mjs"
    if not mcp_cli(toolbox).is_file() or not script.is_file():
        return _err(f"Playwright MCP is not installed; run {util.vloop_cli()} setup --install-browser", 1)
    node = shutil.which("node")
    if not node:
        return _err("node not found", 1)
    try:
        timeout_min = float(args.timeout_min)
    except (TypeError, ValueError):
        return _err("--timeout-min must be a number")
    if timeout_min <= 0:
        return _err("--timeout-min must be positive")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    tmp = path.with_name(path.name + ".tmp")
    cmd = [node, str(script), "--url", args.url, "--out", str(tmp), "--timeout-ms", str(int(timeout_min * 60000))]
    if args.until_url:
        cmd += ["--until-url", args.until_url]
    browser = find_chrome()
    cmd += ["--browser", browser.kind]
    if browser.executable:
        cmd += ["--executable-path", browser.executable]
    try:
        r = subprocess.run(cmd, stdin=subprocess.DEVNULL, timeout=timeout_min * 60 + 120)
        rc = r.returncode
    except subprocess.TimeoutExpired:
        rc = 124
    if not tmp.is_file() or not isinstance(util.read_json(tmp), dict):
        tmp.unlink(missing_ok=True)
        return _err(f"no login state captured (capture-auth exit {rc})", 1)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    os.chmod(path, 0o600)
    s = state_summary(path)
    print(f"login state saved: {path} ({s['cookies']} cookies, {s['origins']} origins with localStorage, "
          f"earliest cookie expiry {s['earliest_expiry'] or 'none'})")
    return 0


def register(sub) -> None:
    p = sub.add_parser("auth", help="login state for browser QA (capture / status)")
    asub = p.add_subparsers(dest="auth_cmd", metavar="<capture|status>")
    asub.required = True
    c = asub.add_parser("capture", help="open Chrome, log in by hand, save the storage state (no passwords)")
    c.add_argument("--session", default=None, help="session id (unused)")
    c.add_argument("--url", required=True, help="login page URL")
    c.add_argument("--repo", default=None, help="repo path (default: cwd)")
    c.add_argument("--until-url", default=None, help="finish when the page URL matches this regex")
    c.add_argument("--timeout-min", default=10, help="give up after N minutes (default 10)")
    c.add_argument("--out", default=None, help="storage-state path (default: the repo's auth path)")
    c.set_defaults(fn=cmd_auth_capture)
    s = asub.add_parser("status", help="age and expiry of the saved login state")
    s.add_argument("--session", default=None, help="session id (unused)")
    s.add_argument("--repo", default=None, help="repo path (default: cwd)")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_auth_status)
