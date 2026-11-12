"""`vloop setup` (install agents, hooks and permissions for Claude Code / Codex), `vloop uninstall` and `vloop doctor`."""
from __future__ import annotations

import hashlib
import json
import os
import platform as pyplatform
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

from . import paths, util

AGENTS = ("claude", "codex", "all")
PLATFORMS = ("claude", "codex")
SCOPES = ("user", "project")
PARTS = ("agents", "hooks", "perms")
ONLY_PARTS = PARTS + ("browser",)

SESSION_MATCHER = "^(startup|resume|clear|compact)$"
TOOL_MATCHER = {"claude": "Edit|Write|MultiEdit|NotebookEdit|Bash", "codex": "Bash|apply_patch|Edit|Write"}
HOOK_EVENTS = (
    ("session-start", "SessionStart"),
    ("prompt", "UserPromptSubmit"),
    ("pre-tool", "PreToolUse"),
    ("post-tool", "PostToolUse"),
    ("stop", "Stop"),
    ("subagent-start", "SubagentStart"),
    ("subagent-stop", "SubagentStop"),
)
EVENT_KEY = dict(HOOK_EVENTS)
HOOK_RE = re.compile(r"""(?:^|[/'"\s])vloop['"]?\s+(?:--platform[ =]\w+\s+)?hook\s+([a-z-]+)""")
SKILL_BIN_RE = re.compile(r"(?P<root><skill>|\{\{\s*SKILL_DIR\s*\}\}|(?:~|/)[^\s\"'`]*?/verify-loop)/bin/(?P<tool>vloop|pw-mcp)\b")


class SetupError(Exception):
    pass


@dataclass
class Plan:
    agent: str
    scope: str
    writes: dict = field(default_factory=dict)
    backups: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    hooks: set = field(default_factory=set)


# --------------------------------------------------------------------------- locations


def skill_dir() -> Path:
    return paths.skill_dir()


def cli_path() -> Path:
    return paths.cli()


def agent_sources() -> list:
    return sorted(paths.agents_src_dir().glob("*.md"))


def _inside(path, root) -> bool:
    if not root:
        return False
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except (ValueError, OSError):
        return False


def plugin_bundle() -> Path | None:
    """Root of the plugin bundle this skill runs from (manifest two levels above the skill dir, or an entry of a
    plugin cache), or None."""
    sd = skill_dir()
    root = sd.parent.parent
    if (root / ".claude-plugin" / "plugin.json").is_file() or (root / ".codex-plugin" / "plugin.json").is_file():
        return root
    for home in (paths.claude_home(), paths.codex_home()):
        cache = (home / "plugins" / "cache")
        if _inside(sd, cache):
            try:
                rel = sd.resolve().relative_to(cache.resolve())
            except ValueError:
                continue
            return cache / Path(*rel.parts[:3]) if len(rel.parts) >= 3 else cache
    return paths.plugin_root()


def bundle_agent() -> str | None:
    """Which agent owns the plugin bundle (its plugin cache lives under that agent's home)."""
    bundle = plugin_bundle()
    if not bundle:
        return None
    for name, home in (("claude", paths.claude_home()), ("codex", paths.codex_home())):
        if _inside(bundle, home):
            return name
    if paths.plugin_root() is not None:
        return "claude"
    return None


def base_dir(agent: str, scope: str, repo_root) -> Path:
    if scope == "project":
        if not repo_root:
            raise SetupError("--scope project needs a git repo")
        return Path(repo_root) / (".claude" if agent == "claude" else ".codex")
    return paths.claude_home() if agent == "claude" else paths.codex_home()


def settings_files(agent: str, scope: str, repo_root, *, everything: bool = False) -> list[Path]:
    base = base_dir(agent, scope, repo_root)
    if agent == "codex":
        return [base / "hooks.json"]
    if everything:
        return [base / "settings.local.json", base / "settings.json"]
    return [base / ("settings.json" if scope == "user" else "settings.local.json")]


def repo_root_for(path: str | None) -> str | None:
    from . import gitsnap

    repo = gitsnap.repo_for(os.path.abspath(os.path.expanduser(path or os.getcwd())))
    return repo["root"] if repo else None


def _tilde(p) -> str:
    return paths.tilde(p)


def _perm_path(p) -> str:
    t = _tilde(p)
    return t if t.startswith("~") else "/" + t


def state_file() -> Path:
    return paths.data_dir() / "installed.json"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_state() -> dict:
    data = util.read_json(state_file(), {})
    data = data if isinstance(data, dict) else {}
    files = data.get("files") if isinstance(data.get("files"), dict) else {}
    hooks = data.get("hooks") if isinstance(data.get("hooks"), list) else []
    return {"files": files, "hooks": [h for h in hooks if isinstance(h, str)]}


# --------------------------------------------------------------------------- hooks


def hook_command(agent: str, event: str) -> str:
    flag = " --platform codex" if agent == "codex" else ""
    return f"'{cli_path()}'{flag} hook {event}"


def own_commands(state: dict | None = None) -> set:
    out = {hook_command(a, ev) for a in PLATFORMS for ev, _ in HOOK_EVENTS}
    out.update((state or read_state())["hooks"])
    return out


def hook_defs(agent: str) -> list:
    """(settings key, matcher or None, hook entry) for every event this install registers."""
    defs = []
    for event, key in HOOK_EVENTS:
        entry = {"type": "command", "command": hook_command(agent, event)}
        matcher = None
        if event == "session-start":
            matcher, entry["timeout"] = SESSION_MATCHER, 5
        elif event == "prompt":
            entry["async"] = True
            if agent == "codex":
                entry["timeout"] = 5
        elif event in ("pre-tool", "post-tool"):
            matcher, entry["timeout"] = TOOL_MATCHER[agent], 15 if event == "pre-tool" else 10
        elif event == "stop":
            entry["timeout"] = 20
        else:
            entry["timeout"] = 5
        defs.append((key, matcher, entry))
    return defs


def _is_ours(hook, own: set) -> bool:
    return isinstance(hook, dict) and hook.get("command") in own


def _groups(data: dict, key: str) -> list:
    hooks = data.get("hooks")
    groups = hooks.get(key) if isinstance(hooks, dict) else None
    return groups if isinstance(groups, list) else []


def _ours_in(data: dict, key: str, own: set) -> list:
    return [h for g in _groups(data, key) if isinstance(g, dict) and isinstance(g.get("hooks"), list)
            for h in g["hooks"] if _is_ours(h, own)]


def strip_hooks(data: dict, own: set, keys=None) -> int:
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return 0
    removed = 0
    for key in list(keys or hooks):
        groups = hooks.get(key)
        if not isinstance(groups, list):
            continue
        kept = []
        for grp in groups:
            inner = grp.get("hooks") if isinstance(grp, dict) else None
            if not isinstance(inner, list):
                kept.append(grp)
                continue
            left = [h for h in inner if not _is_ours(h, own)]
            removed += len(inner) - len(left)
            if left:
                kept.append({**grp, "hooks": left})
        if kept:
            hooks[key] = kept
        else:
            hooks.pop(key)
    if not hooks:
        data.pop("hooks")
    return removed


def add_hooks(data: dict, agent: str, own: set) -> None:
    for key, matcher, entry in hook_defs(agent):
        ours = _ours_in(data, key, own)
        if len(ours) == 1 and ours[0] == entry:
            grp = next(g for g in _groups(data, key) if isinstance(g, dict) and ours[0] in (g.get("hooks") or []))
            if grp.get("matcher") == matcher:
                continue
        strip_hooks(data, own, [key])
        group = {"hooks": [entry]}
        if matcher:
            group = {"matcher": matcher, **group}
        data.setdefault("hooks", {}).setdefault(key, []).append(group)


def hook_entries(path: Path) -> list[tuple[str, str]]:
    """(settings key, command) of every vloop `hook <event>` command in a settings/hooks file, ours or not."""
    data = _read_object(path, strict=False)
    hooks = data.get("hooks") if isinstance(data.get("hooks"), dict) else {}
    out = []
    for key, groups in hooks.items():
        for grp in groups if isinstance(groups, list) else []:
            for h in (grp.get("hooks") if isinstance(grp, dict) and isinstance(grp.get("hooks"), list) else []):
                cmd = h.get("command") if isinstance(h, dict) else None
                if isinstance(cmd, str) and HOOK_RE.search(cmd):
                    out.append((key, cmd))
    return out


# --------------------------------------------------------------------------- json files


def _read_object(path: Path, strict: bool = True) -> dict:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = None
    if not isinstance(data, dict):
        if strict:
            raise SetupError(f"{path} is not a valid JSON object; not changed")
        return {}
    return data


def _load(path: Path) -> dict:
    data = _read_object(path)
    hooks = data.get("hooks") if isinstance(data.get("hooks"), dict) else {}
    perms = data.get("permissions") if isinstance(data.get("permissions"), dict) else {}
    shapes = [("hooks", data.get("hooks"), dict), ("permissions", data.get("permissions"), dict),
              ("permissions.allow", perms.get("allow"), list)]
    shapes += [(f"hooks.{k}", hooks.get(k), list) for _, k in HOOK_EVENTS]
    for key, value, kind in shapes:
        if value is not None and not isinstance(value, kind):
            raise SetupError(f"{path}: `{key}` must be a JSON {'object' if kind is dict else 'array'}; not changed")
    return data


def _dump(data: dict) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def claude_rules() -> list:
    allow = []
    skills = list(dict.fromkeys([skill_dir(), skill_dir().resolve()]))
    for sd in skills:
        cli = sd / "bin" / "vloop"
        allow += [f"Bash({cli} *)", f"Bash({_tilde(cli)} *)", f"Read({_perm_path(sd)}/**)"]
    state = paths.state_dir()
    allow += [f"Read({_perm_path(state)}/**)", f"Edit({_perm_path(state)}/**)"]
    return list(dict.fromkeys(allow))


def _add_perms(data: dict, allow: list) -> None:
    perms = data.setdefault("permissions", {})
    cur = perms.setdefault("allow", [])
    perms["allow"] = cur + [r for r in allow if r not in cur]


def _strip_perms(data: dict, allow: list) -> None:
    perms = data.get("permissions")
    if not isinstance(perms, dict) or not isinstance(perms.get("allow"), list):
        return
    perms["allow"] = [r for r in perms["allow"] if r not in allow]
    if not perms["allow"]:
        perms.pop("allow")
    if not perms:
        data.pop("permissions")


def count_perms(path: Path) -> int:
    perms = _read_object(path, strict=False).get("permissions")
    allow = perms.get("allow") if isinstance(perms, dict) and isinstance(perms.get("allow"), list) else []
    ours = set(claude_rules())
    return sum(1 for r in allow if r in ours)


# --------------------------------------------------------------------------- agents


def rewrite_claude_agent(text: str) -> str:
    """Point the agent's guard hook and Playwright MCP command at the installed vloop / pw-mcp."""
    def sub(m):
        if m.group("tool") == "pw-mcp":
            return str(paths.pw_mcp())
        cli = str(paths.cli())
        return f"'{cli}'" if re.search(r"\s", cli) else cli

    return SKILL_BIN_RE.sub(sub, text)


def render_agent_files(agent: str, base: Path) -> dict:
    from . import platforms
    from .platforms import codex_agents

    out = {}
    for src in agent_sources():
        md = src.read_text(encoding="utf-8")
        if agent == "claude":
            name, text = platforms.get("claude").render_agent(md)
            text = rewrite_claude_agent(text)
        else:
            name, text = codex_agents.render_agent(md)
        out[base / "agents" / name] = text
    return out


def agent_file_names(agent: str) -> list[str]:
    return [f"{src.stem}.md" if agent == "claude" else f"{src.stem}.toml" for src in agent_sources()]


def _owned(path: Path, current: str, bundled: str, files: dict) -> bool:
    return current == bundled or files.get(str(path)) == _sha(current)


def _plan_agent(plan: Plan, path: Path, bundled: str, uninstall: bool, force: bool, files: dict) -> None:
    if not path.is_file():
        if not uninstall:
            plan.writes[path] = bundled
        return
    current = path.read_text(encoding="utf-8", errors="replace")
    owned = _owned(path, current, bundled, files)
    if uninstall:
        if owned or force:
            plan.writes[path] = None
        else:
            plan.notes.append(f"kept {_tilde(path)}: not written by setup or edited since (--force removes it)")
        return
    if current == bundled:
        return
    if owned:
        plan.writes[path] = bundled
    elif force:
        plan.backups.append(path)
        plan.writes[path] = bundled
    else:
        plan.notes.append(f"kept {_tilde(path)}: not written by setup or edited since "
                          f"(--force replaces it and keeps a .bak copy)")


# --------------------------------------------------------------------------- plans


def build_plan(agent: str, scope: str, parts: tuple, repo_root, uninstall: bool, force: bool,
               state: dict | None = None) -> Plan:
    state = state or read_state()
    own = own_commands(state)
    plan = Plan(agent, scope)
    base = base_dir(agent, scope, repo_root)
    bundle = plugin_bundle()
    if bundle and bundle_agent() == agent and not force and not uninstall:
        provided = ("agents", "hooks") if agent == "claude" else ("hooks",)
        skipped = [p for p in parts if p in provided]
        if skipped:
            plan.notes.append(f"skipped {', '.join(skipped)}: the plugin at {_tilde(bundle)} provides them "
                              f"(--force installs them anyway)")
        parts = tuple(p for p in parts if p not in provided)

    if "agents" in parts:
        for path, text in render_agent_files(agent, base).items():
            _plan_agent(plan, path, text, uninstall, force, state["files"])

    targets = settings_files(agent, scope, repo_root, everything=uninstall)
    rules = claude_rules() if agent == "claude" else []
    for i, target in enumerate(targets):
        data = _load(target)
        before = _dump(data) if target.exists() else None
        if "hooks" in parts:
            if uninstall:
                strip_hooks(data, own)
            elif i == 0:
                add_hooks(data, agent, own)
                plan.hooks.update(e["command"] for _, _, e in hook_defs(agent))
        if "perms" in parts and agent == "claude":
            if uninstall:
                _strip_perms(data, rules)
            elif i == 0:
                _add_perms(data, rules)
        after = _dump(data)
        if after != (before or _dump({})):
            plan.writes[target] = after

    if agent == "codex" and not uninstall and "perms" in parts:
        plan.notes.append(f"Codex sandbox: let verify-loop write its state and reach the app under test by adding to "
                          f"{_tilde(paths.codex_home() / 'config.toml')}:\n"
                          f"    [sandbox_workspace_write]\n"
                          f"    writable_roots = [{json.dumps(str(paths.state_dir()))}]\n"
                          f"    network_access = true")
    return plan


def backup_path(path: Path) -> Path:
    candidate, n = path.with_name(path.name + ".bak"), 0
    while candidate.exists():
        n += 1
        candidate = path.with_name(f"{path.name}.bak.{n}")
    return candidate


def _write(path: Path, text: str) -> None:
    try:
        mode = path.stat().st_mode & 0o777
    except OSError:
        mode = 0o644
    util.atomic_write_text(path, text, mode)


def apply(plan: Plan, uninstall: bool = False, full: bool = False) -> None:
    agent_paths = {p for p in plan.writes if p.parent.name == "agents"}
    for path in plan.backups:
        backup_path(path).write_bytes(path.read_bytes())
    for path, text in plan.writes.items():
        if text is None:
            path.unlink(missing_ok=True)
        else:
            _write(path, text)
    if not (agent_paths or plan.hooks):
        return
    state = read_state()
    files = dict(state["files"])
    for path in agent_paths:
        text = plan.writes[path]
        if text is None:
            files.pop(str(path), None)
        else:
            files[str(path)] = _sha(text)
    hooks = sorted(set(state["hooks"]) | plan.hooks)
    if uninstall and not files and full:
        state_file().unlink(missing_ok=True)
        return
    util.atomic_write_json(state_file(), {"files": dict(sorted(files.items())), "hooks": hooks})


# --------------------------------------------------------------------------- commands


def _parts(args) -> tuple:
    parts = tuple(p.strip() for p in (args.only or ",".join(PARTS)).split(",") if p.strip())
    bad = [p for p in parts if p not in ONLY_PARTS]
    if bad:
        raise SetupError(f"unknown part(s) {', '.join(bad)}; use {', '.join(ONLY_PARTS)}")
    return parts


def _agents(args) -> tuple:
    from . import platforms

    if args.agent == "all":
        return PLATFORMS
    if args.agent:
        return (args.agent,)
    forced = platforms.get_override()
    if forced:
        return (forced,)
    found = tuple(a for a, home in (("claude", paths.claude_home()), ("codex", paths.codex_home())) if home.is_dir())
    return found or ("claude",)


def _set_mode(mode: str, dry_run: bool) -> None:
    from . import config

    path = _tilde(config.user_config_path())
    if dry_run:
        print(f"would set gate mode to {mode} in {path}")
        return
    old, new = config.set_user_mode(mode)
    print(f"gate mode: {old} -> {new} ({path})")


def cmd_setup(args) -> int:
    try:
        return _setup(args)
    except SetupError as e:
        sys.stderr.write(f"vloop: setup: {e}\n")
        return 1


def _setup(args) -> int:
    from . import qatools

    parts = _parts(args)
    agents = _agents(args)
    scope = args.scope or "user"
    repo = repo_root_for(getattr(args, "repo", None)) if scope == "project" else None
    if scope == "project" and not repo:
        raise SetupError("--scope project needs a git repo")
    only_browser = (args.only is not None and set(parts) == {"browser"}) or (
        bool(args.install_browser) and args.only is None and args.agent is None and not args.uninstall)
    do_browser = bool(args.install_browser) or "browser" in parts
    parts = tuple(p for p in parts if p in PARTS)
    plans = []
    if not only_browser:
        state = read_state()
        plans = [build_plan(a, scope, parts, repo, args.uninstall, args.force, state) for a in agents]
    verb = "remove" if args.uninstall else "write"
    full = set(parts) == set(PARTS) and args.agent == "all"
    for plan in plans:
        print(f"{plan.agent} ({plan.scope}):")
        if not plan.writes:
            print("  nothing to change")
        for path, text in plan.writes.items():
            action = "delete" if text is None else verb if not path.exists() else "update"
            bak = f" (backup {backup_path(path).name})" if path in plan.backups else ""
            print(f"  {'would ' if args.dry_run else ''}{action} {_tilde(path)}{bak}")
        for n in plan.notes:
            print(f"  note: {n}")
        if not args.dry_run:
            apply(plan, args.uninstall, full)
    if args.mode and not args.uninstall:
        _set_mode(args.mode, args.dry_run)
    rc = 0
    if do_browser and not args.uninstall:
        rc = qatools.install_browser(dry_run=args.dry_run)
    if not args.dry_run and not args.uninstall and plans:
        print("Start a new session so the agents and hooks load.")
    return rc


def cmd_uninstall(args) -> int:
    args.uninstall = True
    return cmd_setup(args)


def cmd_doctor(args) -> int:
    report = diagnose(args)
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(render_report(report))
    return 1 if report["problems"] else 0


def _executable(p: Path) -> bool:
    return p.is_file() and os.access(p, os.X_OK)


def _scope_homes(agent: str, repo_root) -> list[tuple[str, Path]]:
    homes = [("user", base_dir(agent, "user", None))]
    if repo_root:
        homes.append(("project", base_dir(agent, "project", repo_root)))
    return homes


def diagnose(args) -> dict:
    from . import config, qatools, transports
    from .integrations import vault

    repo = repo_root_for(getattr(args, "repo", None))
    state = read_state()
    own = own_commands(state)
    bundle = plugin_bundle()
    owner = bundle_agent()
    problems: list[str] = []
    notes: list[str] = []
    cli = cli_path()
    cli_state = "ok" if _executable(cli) else ("not executable" if cli.is_file() else "missing")
    if cli_state != "ok":
        problems.append(f"the CLI {cli} is {cli_state}")
    mode, mode_source = config.gate_mode()
    try:
        transport = transports.select(config.load(repo)).name
    except Exception as e:  # noqa: BLE001 - doctor must report, not crash
        transport = f"error ({e})"
    try:
        info = vault.discover()
    except Exception:  # noqa: BLE001
        info = None
    report = {
        "python": pyplatform.python_version(),
        "skill_dir": str(skill_dir()),
        "plugin_bundle": str(bundle) if bundle else None,
        "cli": {"path": str(cli), "status": cli_state},
        "repo": repo,
        "state_dir": {"path": str(paths.state_dir()), "exists": paths.state_dir().is_dir()},
        "config": {"path": str(config.user_config_path()), "exists": config.user_config_path().is_file(),
                   "gate_mode": mode, "gate_mode_source": mode_source},
        "transport": transport,
        "agent_vault": str(info.cli) if info else None,
        "platforms": {"claude": paths.claude_home().is_dir() or bool(shutil.which("claude")),
                      "codex": paths.codex_home().is_dir() or bool(shutil.which("codex"))},
        "installed": {},
    }
    for agent in PLATFORMS:
        entry = {"agents": {}, "hooks": {}, "perms": {}, "plugin_hooks": owner == agent and bool(bundle),
                 "plugin_agents": owner == agent == "claude" and bool(bundle)}
        names = agent_file_names(agent)
        found: dict[str, list] = {}
        for scope, base in _scope_homes(agent, repo):
            entry["agents"][scope] = {"present": sum(1 for n in names if (base / "agents" / n).is_file()),
                                      "total": len(names)}
            files = settings_files(agent, scope, repo, everything=True)
            events = set()
            for f in files:
                if not _valid_object(f):
                    problems.append(f"{f} is not a valid JSON object")
                    continue
                for key, cmd in hook_entries(f):
                    found.setdefault(key, []).append((scope, f, cmd, cmd in own))
                    if cmd in own:
                        events.add(key)
            entry["hooks"][scope] = {"present": len(events), "total": len(HOOK_EVENTS),
                                     "files": [str(f) for f in files if hook_entries(f)]}
            if agent == "claude":
                entry["perms"][scope] = {"present": sum(count_perms(f) for f in files), "total": len(claude_rules())}
        dups, legacy = [], []
        for key, items in found.items():
            sources = [(f, cmd) for _, f, cmd, _ in items]
            if entry["plugin_hooks"]:
                sources.append(("plugin hooks/hooks.json", ""))
            if len(sources) > 1:
                dups.append({"event": key, "sources": [str(f) for f, _ in sources]})
            legacy += [(f, cmd) for _, f, cmd, is_own in items if not is_own]
        entry["duplicates"] = dups
        entry["foreign_vloop_hooks"] = sorted({str(f) for f, _ in legacy})
        if dups:
            where = sorted({s for d in dups for s in d["sources"]})
            problems.append(f"{agent}: vloop hooks are registered more than once "
                            f"({', '.join(d['event'] for d in dups)}) in {', '.join(_tilde(w) for w in where)}; "
                            f"keep one install (vloop setup --agent {agent} --uninstall removes this one's copies)")
        if legacy and not dups:
            notes.append(f"{agent}: vloop hooks from another install in {', '.join(_tilde(f) for f in sorted({str(f) for f, _ in legacy}))} "
                         f"(not managed by setup)")
        report["installed"][agent] = entry
    ok_node, node_line = qatools.check_node()
    browser = qatools.find_chrome()
    ok_tb, tb_line = qatools.check_toolbox()
    report["browser"] = {"node": node_line, "browser": browser.describe(), "system_browser": browser.system,
                         "toolbox": tb_line, "toolbox_ok": ok_tb, "node_ok": ok_node}
    report["problems"] = problems
    report["notes"] = notes
    return report


def _valid_object(path: Path) -> bool:
    try:
        _read_object(path)
    except SetupError:
        return False
    return True


def render_report(r: dict) -> str:
    out = [f"Python:     {r['python']}",
           f"Skill:      {_tilde(r['skill_dir'])}" + (f"  (plugin bundle {_tilde(r['plugin_bundle'])})"
                                                      if r["plugin_bundle"] else ""),
           f"CLI:        {r['cli']['path']}  ({r['cli']['status']})",
           f"Repo:       {_tilde(r['repo']) if r['repo'] else '-'}",
           f"State dir:  {_tilde(r['state_dir']['path'])}" + ("" if r["state_dir"]["exists"] else "  (not created yet)"),
           f"Config:     {_tilde(r['config']['path'])}" + ("" if r["config"]["exists"] else "  (none; defaults)")
           + f" · gate mode: {r['config']['gate_mode']} (from {r['config']['gate_mode_source']})",
           f"Transport:  {r['transport']}",
           f"agent-vault: {_tilde(r['agent_vault']) if r['agent_vault'] else 'not found (optional)'}",
           "Platforms:  " + ", ".join(f"{p} ({'detected' if ok else 'not detected'})"
                                       for p, ok in r["platforms"].items())]
    for agent, e in r["installed"].items():
        out.append(f"{agent}:")
        agents = "; ".join(f"{s} {v['present']}/{v['total']}" for s, v in e["agents"].items())
        out.append(f"  agents:  {agents}" + ("; plugin provides its own" if e["plugin_agents"] else ""))
        hooks = "; ".join(f"{s} {v['present']}/{v['total']}" for s, v in e["hooks"].items())
        out.append(f"  hooks:   {hooks}" + ("; plugin hooks/hooks.json" if e["plugin_hooks"] else ""))
        if e["perms"]:
            out.append("  perms:   " + "; ".join(f"{s} {v['present']}/{v['total']}" for s, v in e["perms"].items()))
    b = r["browser"]
    out += ["browser toolbox:", f"  {b['node']}", f"  browser: {b['browser']}", f"  {b['toolbox']}"]
    for n in r["notes"]:
        out.append(f"note: {n}")
    for p in r["problems"]:
        out.append(f"warning: {p}")
    out.append("doctor: " + ("ok" if not r["problems"] else f"{len(r['problems'])} problem(s)"))
    return "\n".join(out)


# --------------------------------------------------------------------------- argparse


def add_setup_args(p) -> None:
    p.add_argument("--agent", choices=AGENTS, default=None,
                   help="platform(s) to install for (default: the ones whose home exists, else claude)")
    p.add_argument("--scope", choices=SCOPES, default=None, help="user (default) or project install")
    p.add_argument("--only", default=None, help=f"comma-separated subset of {','.join(ONLY_PARTS)}")
    p.add_argument("--mode", choices=("warn", "enforce"), default=None, help="gate mode to set in the user config")
    p.add_argument("--install-browser", "--install", dest="install_browser", action="store_true",
                   help="install the browser QA toolbox (npm ci) into the data dir")
    p.add_argument("--dry-run", action="store_true", help="print what would change")
    p.add_argument("--uninstall", action="store_true", help="remove what setup installed")
    p.add_argument("--force", action="store_true", help="overwrite files changed by hand (keeps a .bak copy)")
    p.add_argument("--repo", default=None, help="repo path for --scope project (default: cwd)")


def cmd_setup_dispatch(args) -> int:
    if getattr(args, "check_browser", False):
        from . import qatools

        return qatools.cmd_check_browser(args)
    return cmd_setup(args)


def register(sub) -> None:
    p = sub.add_parser("setup", help="install agents, hooks and permissions for Claude Code / Codex")
    p.add_argument("--session", default=None, help="session id (unused)")
    p.add_argument("--check-browser", action="store_true", help="only check node, Chrome and the browser toolbox")
    add_setup_args(p)
    p.set_defaults(fn=cmd_setup_dispatch)

    u = sub.add_parser("uninstall", help="remove what `vloop setup` installed (same as setup --uninstall)")
    add_setup_args(u)
    u.set_defaults(fn=cmd_uninstall)

    d = sub.add_parser("doctor", help="check the installation (agents, hooks, permissions, browser toolbox)")
    d.add_argument("--json", action="store_true")
    d.add_argument("--repo", default=None, help="repo path for project-scope checks (default: cwd)")
    d.set_defaults(fn=cmd_doctor)
