"""Reviewer / QA PreToolUse guards (§2.12): read-only reviewer, QA that never modifies the project."""
from __future__ import annotations

import json
import os
import re
import sys

from . import util

EDIT_TOOLS = ("Write", "Edit", "MultiEdit", "NotebookEdit")
SEPARATORS = {"&&", "||", ";", ";;", "|", "|&", "&", "\n", "(", ")"}
OPERATORS = ("&>>", "<<<", "<<-", "&&", "||", "|&", ";;", ">>", ">&", "&>", ">|", "<<", "<&", "<>",
             ";", "|", "&", ">", "<", "(", ")")
OUT_REDIRS = {">", ">>", ">|", "&>", "&>>", ">&", "<>"}
WRAPPERS = {"timeout", "time", "nice", "nohup", "command", "builtin"}

REVIEWER_PROGRAMS = {"git", "rg", "grep", "ls", "wc", "head", "tail", "cat", "sed", "find", "jq", "sort", "uniq",
                     "awk", "diff", "stat", "file", "pwd", "echo", "printf", "true", "test", "[", "basename",
                     "dirname", "realpath", "tr", "cut", "nl", "cd", "column", "tree"}
REVIEWER_GIT = {"diff", "log", "show", "status", "blame", "grep", "ls-files", "ls-tree", "rev-parse", "merge-base",
                "cat-file", "rev-list", "describe", "shortlog", "name-rev", "for-each-ref"}
FIND_FORBIDDEN = {"-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fprintf", "-fls", "-fprint0"}

QA_GIT = {"checkout", "switch", "reset", "restore", "stash", "commit", "push", "pull", "fetch", "merge", "rebase",
          "cherry-pick", "revert", "apply", "am", "add", "rm", "mv", "clean", "worktree", "submodule", "tag",
          "update-ref", "gc", "prune", "notes"}
QA_NPM = {"install", "i", "add", "uninstall", "remove", "rm", "un", "update", "up", "upgrade", "link", "dedupe",
          "prune", "publish", "version"}
QA_FS = {"rm", "mv", "cp", "chmod", "chown", "touch", "mkdir", "ln", "truncate", "dd"}
PKG_MANAGERS = {"npm", "yarn", "pnpm", "pip", "pip3", "poetry"}


# --------------------------------------------------------------------------- tokenizer


class ParseError(ValueError):
    pass


def _match_paren(s: str, i: int) -> int:
    """i points just after '$(' ; returns index of the matching ')'."""
    depth, q = 1, None
    while i < len(s):
        c = s[i]
        if q:
            if c == "\\" and q == '"':
                i += 2
                continue
            if c == q:
                q = None
        elif c in "'\"":
            q = c
        elif c == "\\":
            i += 2
            continue
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    raise ParseError("unbalanced $(")


def tokenize(cmd: str) -> tuple[list[tuple[str, str]], list[str]]:
    """Shell-ish tokenizer. Returns ([(kind, value)], substitutions) with kind 'word' or 'op'.
    Heredoc bodies are skipped; $(…), `…`, <(…), >(…) bodies are returned as substitutions."""
    toks: list[tuple[str, str]] = []
    subs: list[str] = []
    heredocs: list[tuple[str, bool]] = []
    i, n = 0, len(cmd)
    word, in_word = "", False

    def flush():
        nonlocal word, in_word
        if in_word:
            toks.append(("word", word))
        word, in_word = "", False

    while i < n:
        c = cmd[i]
        if c == "\n":
            flush()
            toks.append(("op", "\n"))
            i += 1
            for delim, strip in heredocs:
                while i < n:
                    j = cmd.find("\n", i)
                    line = cmd[i:] if j == -1 else cmd[i:j]
                    i = n if j == -1 else j + 1
                    if (line.lstrip("\t") if strip else line) == delim:
                        break
            heredocs = []
            continue
        if c in " \t\r":
            flush()
            i += 1
            continue
        if c == "#" and not in_word:
            j = cmd.find("\n", i)
            i = n if j == -1 else j
            continue
        if c == "\\":
            if i + 1 < n and cmd[i + 1] == "\n":
                i += 2
                continue
            word += cmd[i + 1] if i + 1 < n else ""
            in_word = True
            i += 2
            continue
        if c == "'":
            j = cmd.find("'", i + 1)
            if j == -1:
                raise ParseError("unbalanced quote")
            word += cmd[i + 1:j]
            in_word = True
            i = j + 1
            continue
        if c == '"':
            i += 1
            in_word = True
            while True:
                if i >= n:
                    raise ParseError("unbalanced quote")
                d = cmd[i]
                if d == '"':
                    i += 1
                    break
                if d == "\\" and i + 1 < n and cmd[i + 1] in '"\\$`\n':
                    word += cmd[i + 1]
                    i += 2
                    continue
                if d == "$" and cmd[i + 1:i + 2] == "(":
                    j = _match_paren(cmd, i + 2)
                    subs.append(cmd[i + 2:j])
                    word += "$(…)"
                    i = j + 1
                    continue
                if d == "`":
                    j = cmd.find("`", i + 1)
                    if j == -1:
                        raise ParseError("unbalanced backtick")
                    subs.append(cmd[i + 1:j])
                    word += "$(…)"
                    i = j + 1
                    continue
                word += d
                i += 1
            continue
        if c == "$" and cmd[i + 1:i + 2] == "(":
            j = _match_paren(cmd, i + 2)
            subs.append(cmd[i + 2:j])
            word += "$(…)"
            in_word = True
            i = j + 1
            continue
        if c == "`":
            j = cmd.find("`", i + 1)
            if j == -1:
                raise ParseError("unbalanced backtick")
            subs.append(cmd[i + 1:j])
            word += "$(…)"
            in_word = True
            i = j + 1
            continue
        if c in "<>" and cmd[i + 1:i + 2] == "(":
            j = _match_paren(cmd, i + 2)
            subs.append(cmd[i + 2:j])
            word += "<(…)"
            in_word = True
            i = j + 1
            continue
        op = next((o for o in OPERATORS if cmd.startswith(o, i)), None)
        if op:
            if op in (">", ">>", ">|", ">&", "<", "<<", "<<-", "<<<", "<&", "<>") and in_word and word.isdigit():
                word, in_word = "", False  # fd number prefix (2>, 1>>)
            flush()
            toks.append(("op", op))
            i += len(op)
            if op in ("<<", "<<-"):
                while i < n and cmd[i] in " \t":
                    i += 1
                m = re.match(r"""(['"]?)([^\s'";|&<>()]+)\1""", cmd[i:])
                if not m:
                    raise ParseError("bad heredoc")
                heredocs.append((m.group(2), op == "<<-"))
                toks.append(("word", m.group(2)))
                i += m.end()
            continue
        word += c
        in_word = True
        i += 1
    flush()
    return toks, subs


def segments(cmd: str) -> tuple[list[dict], list[str]]:
    """Split into simple commands: [{"words": [...], "redirs": [(op, target)]}], substitutions."""
    toks, subs = tokenize(cmd)
    segs, cur = [], {"words": [], "redirs": []}
    i = 0
    while i < len(toks):
        kind, val = toks[i]
        if kind == "op" and val in SEPARATORS:
            if cur["words"] or cur["redirs"]:
                segs.append(cur)
            cur = {"words": [], "redirs": []}
            i += 1
            continue
        if kind == "op":
            target = toks[i + 1][1] if i + 1 < len(toks) and toks[i + 1][0] == "word" else ""
            cur["redirs"].append((val, target))
            i += 2 if target else 1
            continue
        cur["words"].append(val)
        i += 1
    if cur["words"] or cur["redirs"]:
        segs.append(cur)
    for s in segs:
        s["words"] = _strip_wrappers(s["words"])
    return segs, subs


def _strip_wrappers(words: list[str]) -> list[str]:
    w = list(words)
    while w and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", w[0]):
        w.pop(0)
    changed = True
    while w and changed:
        changed = False
        prog = os.path.basename(w[0])
        if prog not in WRAPPERS:
            break
        if prog == "command" and len(w) > 1 and w[1] in ("-v", "-V"):
            return ["command", *w[1:]]
        w.pop(0)
        changed = True
        if prog == "timeout":
            while w and w[0].startswith("-"):
                flag = w.pop(0)
                if flag in ("-s", "-k", "--signal", "--kill-after") and w:
                    w.pop(0)
            if w:
                w.pop(0)  # duration
        elif prog == "nice":
            while w and w[0].startswith("-"):
                flag = w.pop(0)
                if flag == "-n" and w:
                    w.pop(0)
        else:
            while w and w[0] in ("-p", "--"):
                w.pop(0)
        while w and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", w[0]):
            w.pop(0)
    return w


# --------------------------------------------------------------------------- paths


def _real(p: str) -> str:
    return os.path.realpath(os.path.expanduser(p))


def _under(path: str, root: str) -> bool:
    path, root = _real(path), _real(root)
    return path == root or path.startswith(root.rstrip("/") + "/")


def runs_dir() -> str:
    return str(util.state_dir() / "runs")


def _tmp_roots() -> list[str]:
    roots = ["/tmp", "/private/tmp"]
    if os.environ.get("TMPDIR"):
        roots.append(os.environ["TMPDIR"])
    return roots


def _repo_root(cwd: str | None) -> str | None:
    if not cwd or not os.path.isdir(cwd):
        return None
    rc, so, _ = util.run(["git", "-C", cwd, "rev-parse", "--show-toplevel"], timeout=10)
    return os.path.realpath(so.strip()) if rc == 0 and so.strip() else None


def _resolve(arg: str, cwd: str | None) -> str | None:
    a = os.path.expanduser(os.path.expandvars(arg))
    if "$" in a or "…" in a:
        return None
    if not os.path.isabs(a):
        if not cwd:
            return None
        a = os.path.join(cwd, a)
    return _real(a)


# --------------------------------------------------------------------------- output


def _deny(role: str, why: str, hint: str) -> dict:
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "deny",
                                   "permissionDecisionReason": f"{role} guard: {why}. {hint}"}}


def _allow_reviewer() -> dict:
    return {"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "allow",
                                   "permissionDecisionReason": "reviewer guard: read-only"}}


def _reviewer_hint() -> str:
    return (f"Use read-only commands (git diff/show/log, rg, cat, sed -n) and write only your verdict file "
            f"under {runs_dir()}.")


def _qa_hint() -> str:
    return (f"QA must not modify the project; write scratch files under {runs_dir()}/<run_id>/attempt-<N> or "
            f"$TMPDIR and report blocked checks instead.")


def patch_paths(payload: dict) -> list[str] | None:
    """Absolute paths a Codex `apply_patch` call writes (relative ones resolve against the payload cwd); None when the
    call is not a patch."""
    from .platforms import applypatch, codex

    tool = payload.get("tool_name") or ""
    ti = payload.get("tool_input")
    text = None
    if tool in codex.PATCH_TOOLS:
        text = codex.patch_text(ti)
    elif tool in codex.SHELL_TOOLS:
        cmd = codex.shell_command(ti.get("command") if isinstance(ti, dict) else ti)
        if cmd and applypatch.looks_like_patch(cmd) and applypatch.is_pure_shell_patch(cmd):
            text = cmd
    if text is None:
        return None
    cwd = payload.get("cwd") or os.getcwd()
    return [_real(p if os.path.isabs(os.path.expanduser(p)) else os.path.join(cwd, p)) for p in applypatch.paths(text)]


_SEARCH_PROGS = {"rg", "grep", "egrep", "fgrep", "ag", "ack"}
_GIT_READ_SUBS = {"grep", "log", "show", "diff", "blame"}
_APPLY_PATCH_RE = re.compile(r"apply_patch\b")


def _search_segment(words: list[str]) -> bool:
    prog = os.path.basename(words[0]) if words else ""
    if prog in _SEARCH_PROGS:
        return True
    return prog == "git" and _git_sub(words)[0] in _GIT_READ_SUBS


def invokes_apply_patch(cmd: str) -> bool:
    """True unless every mention of apply_patch is an argument of a read-only search; listing the invocation forms
    instead (separators, do/then, env -i, \\apply_patch, ...) keeps missing some. Mentions are matched on tokenized
    words, so quoting tricks like apply_pat''ch cannot hide a call."""
    raw = len(_APPLY_PATCH_RE.findall(cmd))
    try:
        segs, subs = segments(cmd)
    except Exception:
        return bool(raw)
    if any(invokes_apply_patch(s) for s in subs):
        return True
    words = [w for seg in segs for w in seg["words"]]
    if raw > sum(len(_APPLY_PATCH_RE.findall(w)) for w in words):
        return True
    return any(_APPLY_PATCH_RE.search(w) for seg in segs if not _search_segment(seg["words"]) for w in seg["words"])


def embedded_patch_paths(payload: dict) -> list[str] | None:
    """Paths of an apply_patch call embedded in a shell command that is not a pure patch (it still goes through the
    Bash checks); None when the shell command carries no patch."""
    from .platforms import applypatch, codex

    if (payload.get("tool_name") or "") not in codex.SHELL_TOOLS:
        return None
    ti = payload.get("tool_input")
    cmd = codex.shell_command(ti.get("command") if isinstance(ti, dict) else ti)
    if not cmd or not applypatch.looks_like_patch(cmd) or not invokes_apply_patch(cmd):
        return None
    found = applypatch.all_paths(cmd)
    relative = [p for p in found if not os.path.isabs(os.path.expanduser(p))]
    if relative and applypatch.changes_dir(cmd):
        return []
    cwd = payload.get("cwd") or os.getcwd()
    return [_real(p if os.path.isabs(os.path.expanduser(p)) else os.path.join(cwd, p)) for p in found]


# --------------------------------------------------------------------------- reviewer


def _git_sub(words: list[str]) -> tuple[str | None, list[str]]:
    i = 1
    while i < len(words):
        w = words[i]
        if w in ("-C", "-c", "--git-dir", "--work-tree", "--namespace"):
            i += 2
            continue
        if w.startswith("-"):
            i += 1
            continue
        return w, words[i + 1:]
    return None, []


def _reviewer_segment(seg: dict) -> str | None:
    words = seg["words"]
    if not words:
        return None
    prog = os.path.basename(words[0])
    args = words[1:]
    if prog == "tee":
        return "read-only reviewer: tee not allowed"
    if prog not in REVIEWER_PROGRAMS:
        return f"read-only reviewer: {prog} not allowed"
    if prog == "git":
        sub, rest = _git_sub(words)
        if sub is None:
            return None
        if any(a.startswith("--output") for a in rest):
            return "read-only reviewer: git --output not allowed"
        if sub in REVIEWER_GIT:
            return None
        if sub == "branch":
            ok = {"--show-current", "--list", "-a", "-r", "-v", "-vv", "--all", "--remotes"}
            if all(a in ok for a in rest) or ("--list" in rest and all(a in ok or not a.startswith("-") for a in rest)):
                return None
        elif sub == "remote":
            if rest in ([], ["-v"]):
                return None
        elif sub == "config":
            if rest and rest[0] in ("--get", "--get-all", "--list", "-l", "--get-regexp"):
                return None
        return f"read-only reviewer: git {sub} not allowed"
    if prog == "sed":
        flags = [a for a in args if a.startswith("-")]
        if any(a == "--in-place" or a.startswith("--in-place=") or (not a.startswith("--") and "i" in a)
               for a in flags):
            return "read-only reviewer: sed -i not allowed"
        if not any(a in ("--quiet", "--silent") or (not a.startswith("--") and "n" in a[1:]) for a in flags):
            return "read-only reviewer: sed requires -n"
    if prog == "rg" and any(a == "--pre" or a.startswith("--pre=") for a in args):
        return "read-only reviewer: rg --pre runs a command"
    if prog == "find":
        bad = [a for a in args if a in FIND_FORBIDDEN]
        if bad:
            return f"read-only reviewer: find {bad[0]} not allowed"
    if prog == "awk":
        i = 0
        while i < len(args):
            a = args[i]
            if a in ("-F", "-v", "-f"):
                i += 2
                continue
            if a.startswith("-"):
                i += 1
                continue
            if ">" in a or "system" in a:
                return "read-only reviewer: awk program with > or system() not allowed"
            break
    return None


def _reviewer_bash(cmd: str) -> str | None:
    try:
        segs, subs = segments(cmd)
    except ParseError:
        return "unparseable command"
    for seg in segs:
        for op, target in seg["redirs"]:
            if op in OUT_REDIRS:
                if op == ">&" and (target.isdigit() or target == "-"):
                    continue
                if target == "/dev/null":
                    continue
                return "read-only reviewer: output redirection not allowed"
        why = _reviewer_segment(seg)
        if why:
            return why
    for sub in subs:
        why = _reviewer_bash(sub)
        if why:
            return why
    return None


def check_reviewer(payload: dict) -> dict | None:
    tool = payload.get("tool_name") or ""
    ti = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    pp = patch_paths(payload)
    if pp is not None:
        if pp and all(_under(p, runs_dir()) for p in pp):
            return _allow_reviewer()
        return _deny("reviewer", f"write only your verdict file under {runs_dir()}", _reviewer_hint())
    if tool in EDIT_TOOLS:
        path = ti.get("file_path") or ti.get("notebook_path") or ""
        if path and _under(path, runs_dir()):
            return _allow_reviewer()
        return _deny("reviewer", f"write only your verdict file under {runs_dir()}", _reviewer_hint())
    if tool in ("Bash", "shell", "exec_command", "local_shell"):
        from .platforms import codex

        ep = embedded_patch_paths(payload)
        if ep is not None and not (ep and all(_under(p, runs_dir()) for p in ep)):
            return _deny("reviewer", f"write only your verdict file under {runs_dir()}", _reviewer_hint())
        why = _reviewer_bash(codex.shell_command(ti.get("command")) or "")
        if why:
            return _deny("reviewer", why, _reviewer_hint())
        return _allow_reviewer()
    return None


# --------------------------------------------------------------------------- qa


def _qa_allowed_target(path: str) -> bool:
    return any(_under(path, r) for r in [runs_dir(), *_tmp_roots()])


def _in_repo(arg: str, cwd: str | None, repo: str | None) -> bool:
    if not repo:
        return False
    p = _resolve(arg, cwd)
    if p is None or _qa_allowed_target(p):
        return False
    return _under(p, repo)


def _pkg_manager(prog: str, args: list[str]) -> str | None:
    pos = [a for a in args if not a.startswith("-")]
    first = pos[0] if pos else None
    if prog == "npm":
        if first in QA_NPM:
            return f"npm {first}"
        if pos[:2] == ["pkg", "set"]:
            return "npm pkg set"
        if pos[:2] == ["audit", "fix"]:
            return "npm audit fix"
    elif prog == "yarn":
        if first in ("add", "remove", "upgrade", "up"):
            return f"yarn {first}"
        if first == "install" and not any(a in ("--frozen-lockfile", "--immutable") for a in args):
            return "yarn install without --frozen-lockfile"
    elif prog == "pnpm":
        if first in ("add", "remove", "rm", "update", "up"):
            return f"pnpm {first}"
        if first in ("install", "i") and "--frozen-lockfile" not in args:
            return "pnpm install without --frozen-lockfile"
    elif prog in ("pip", "pip3"):
        if first == "install":
            return f"{prog} install"
    elif prog == "poetry":
        if first in ("add", "remove", "update", "lock"):
            return f"poetry {first}"
    return None


def _qa_segment(seg: dict, cwd: str | None, repo: str | None) -> str | None:
    words = seg["words"]
    for op, target in seg["redirs"]:
        if op in OUT_REDIRS and not (op == ">&" and (target.isdigit() or target == "-")):
            if target and target != "/dev/null" and _in_repo(target, cwd, repo):
                return f"output redirection into the project ({target})"
    if not words:
        return None
    prog = os.path.basename(words[0])
    args = words[1:]
    joined = " ".join(words)
    if prog == "git":
        sub, rest = _git_sub(words)
        if sub in QA_GIT:
            return f"git {sub} modifies the repository"
        if sub == "branch" and any(a in ("-d", "-D", "-m", "-M", "-c", "-C", "--delete", "--move", "--copy")
                                   for a in rest):
            return "git branch with -d/-m/-c modifies the repository"
        if sub == "config" and not any(a.startswith("--get") or a in ("--list", "-l") for a in rest):
            return "git config writes"
    if prog in ("bash", "sh", "zsh") and "-c" in args:
        i = args.index("-c")
        if i + 1 < len(args):
            return _qa_bash(args[i + 1], cwd, repo)
    if prog == "python3" or prog == "python":
        if args[:3] == ["-m", "pip", "install"]:
            return "pip install"
    pm = _pkg_manager(prog, args)
    if pm:
        return f"{pm} changes dependencies"
    if prog == "npx":
        rest = list(args)
        while rest and rest[0].startswith("-"):
            flag = rest.pop(0)
            if flag in ("-p", "--package") and rest:
                rest.pop(0)
        if rest and os.path.basename(rest[0]) in PKG_MANAGERS:
            pm = _pkg_manager(os.path.basename(rest[0]), rest[1:])
            if pm:
                return f"{pm} changes dependencies"
    if prog == "sed" and any(a == "--in-place" or a.startswith("--in-place=")
                             or (a.startswith("-") and not a.startswith("--") and "i" in a) for a in args):
        return "sed -i edits files"
    if prog in ("perl", "ruby") and any(a.startswith("-") and not a.startswith("--") and "i" in a[1:] for a in args
                                        if not a.startswith("-e")):
        return f"{prog} -i edits files"
    if prog == "tee":
        for a in args:
            if not a.startswith("-") and _in_repo(a, cwd, repo):
                return f"tee into the project ({a})"
    if prog in QA_FS:
        if prog == "dd":
            targets = [a[3:] for a in args if a.startswith("of=")]
        elif prog in ("cp", "ln"):
            pos = [a for a in args if not a.startswith("-")]
            targets = pos[-1:] if len(pos) >= 2 else pos
            if "-t" in args and args.index("-t") + 1 < len(args):
                targets = [args[args.index("-t") + 1]]
        else:
            targets = [a for a in args if not a.startswith("-")]
        for t in targets:
            if _in_repo(t, cwd, repo):
                return f"{prog} on a project path ({t})"
    if re.search(r"(^|[\s/])(jest|vitest)(\s|$)|playwright test", joined) and any(
            a in ("-u", "--updateSnapshot", "--update-snapshots") or a.startswith("--update-snapshots=")
            for a in args):
        return "snapshot update writes test files"
    if (re.search(r"(^|[\s/])eslint(\s|$)", joined) or re.search(r"\b(nx|ng) lint\b", joined)) and any(
            a == "--fix" or a.startswith("--fix=") for a in args):
        return "lint --fix edits files"
    if re.search(r"(^|[\s/])prettier(\s|$)", joined) and any(a in ("--write", "-w") for a in args):
        return "prettier --write edits files"
    if re.search(r"\b(nx|ng) (g|generate)\b", joined):
        return "code generators modify the project"
    if re.search(r"\bprisma migrate (dev|reset)\b", joined) or re.search(r"\bprisma db push\b", joined):
        return "migrations change the database schema"
    return None


def _qa_bash(cmd: str, cwd: str | None, repo: str | None) -> str | None:
    try:
        segs, subs = segments(cmd)
    except ParseError:
        return None
    for seg in segs:
        why = _qa_segment(seg, cwd, repo)
        if why:
            return why
    for sub in subs:
        why = _qa_bash(sub, cwd, repo)
        if why:
            return why
    return None


def check_qa(payload: dict) -> dict | None:
    tool = payload.get("tool_name") or ""
    ti = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    pp = patch_paths(payload)
    if pp is not None:
        if pp and all(_qa_allowed_target(p) for p in pp):
            return None
        return _deny("qa", "QA never edits project files", _qa_hint())
    if tool in EDIT_TOOLS:
        path = ti.get("file_path") or ti.get("notebook_path") or ""
        if path and _qa_allowed_target(path):
            return None
        return _deny("qa", "QA never edits project files", _qa_hint())
    if tool in ("Bash", "shell", "exec_command", "local_shell"):
        from .platforms import codex

        cwd = payload.get("cwd") or os.getcwd()
        ep = embedded_patch_paths(payload)
        if ep is not None and not (ep and all(_qa_allowed_target(p) for p in ep)):
            return _deny("qa", "QA never edits project files", _qa_hint())
        why = _qa_bash(codex.shell_command(ti.get("command")) or "", cwd, _repo_root(cwd))
        if why:
            return _deny("qa", why, _qa_hint())
    return None


# --------------------------------------------------------------------------- entry points


def check(role: str, payload: dict) -> dict | None:
    if role == "reviewer":
        return check_reviewer(payload)
    if role == "qa":
        return check_qa(payload)
    return None


def run_guard(role: str, payload: dict, platform: str | None = None) -> int:
    out = check(role, payload)
    if platform is None:
        from . import platforms

        platform = platforms.detect(payload).name
    if out is not None and platform == "codex" and \
            (out.get("hookSpecificOutput") or {}).get("permissionDecision") != "deny":
        out = None
    if out is not None:
        sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    return 0


def cmd_guard(args) -> int:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        return run_guard(args.role, payload if isinstance(payload, dict) else {})
    except BaseException as e:
        util.log_error(f"guard.{getattr(args, 'role', '?')}", e)
        return 0
