"""Private git snapshots, fingerprints and diffs (§2.14.4). The user's .git is never written."""
from __future__ import annotations

import hashlib
import os
import time
from contextlib import contextmanager
from pathlib import Path

from . import util

EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


def _git(repo_root, *args, env=None, timeout: float = 30, input_text: str | None = None) -> tuple[int, str, str]:
    return util.run(["git", "-C", str(repo_root), *args], env=env, timeout=timeout, input_text=input_text)


def _out(repo_root, *args, env=None, timeout: float = 30) -> str | None:
    rc, so, _ = _git(repo_root, *args, env=env, timeout=timeout)
    return so.strip() if rc == 0 else None


# --------------------------------------------------------------------------- repo


def repo_for(path: str | Path) -> dict | None:
    p = os.path.abspath(os.path.expanduser(str(path)))
    while p and not os.path.isdir(p):
        parent = os.path.dirname(p)
        if parent == p:
            return None
        p = parent
    rc, so, _ = _git(p, "rev-parse", "--path-format=absolute", "--show-toplevel", "--git-common-dir", timeout=10)
    if rc != 0:
        return None
    lines = so.strip().splitlines()
    if len(lines) < 2 or not lines[0]:
        return None
    root = os.path.realpath(lines[0])
    return {"root": root, "key": hashlib.sha1(root.encode("utf-8")).hexdigest()[:12],
            "slug": util.slugify(os.path.basename(root)), "common_dir": os.path.realpath(lines[1])}


def _snap_dir(repo: dict) -> Path:
    d = util.state_dir() / "snap" / repo["key"]
    (d / "objects").mkdir(parents=True, exist_ok=True, mode=0o700)
    return d


def git_env(repo: dict) -> dict:
    env = dict(os.environ)
    for k in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES"):
        env.pop(k, None)
    env["GIT_OBJECT_DIRECTORY"] = str(_snap_dir(repo) / "objects")
    env["GIT_ALTERNATE_OBJECT_DIRECTORIES"] = os.path.join(repo["common_dir"], "objects")
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


@contextmanager
def snapshot_lock(repo: dict, timeout: float = 8.0):
    from . import state

    with state._flock(_snap_dir(repo) / "lock", timeout):
        yield


# --------------------------------------------------------------------------- snapshot


def head_tree(repo: dict) -> str | None:
    return _out(repo["root"], "rev-parse", "--verify", "-q", "HEAD^{tree}", timeout=10)


def _fallback(repo: dict, t0: float) -> dict:
    tree = head_tree(repo)
    ms = int((time.monotonic() - t0) * 1000)
    if tree:
        return {"tree": tree, "kind": "head-fallback", "ms": ms}
    return {"tree": EMPTY_TREE, "kind": "empty", "ms": ms}


def snapshot(repo: dict, session_id: str, timeout: float = 20.0) -> dict:
    t0 = time.monotonic()
    try:
        s = _snap_dir(repo)
        index = s / f"index-{session_id}"
        rc, so, _ = _git(repo["root"], "rev-parse", "--path-format=absolute", "--git-path", "index", timeout=timeout)
        src = so.strip() if rc == 0 else ""
        if src and os.path.isfile(src):
            import shutil

            shutil.copyfile(src, index)
        elif index.exists():
            index.unlink()
        env = git_env(repo)
        env["GIT_INDEX_FILE"] = str(index)
        left = timeout - (time.monotonic() - t0)
        if left <= 0:
            return _fallback(repo, t0)
        rc, _, _ = _git(repo["root"], "add", "-A", "--", ".", env=env, timeout=left)
        if rc != 0:
            return _fallback(repo, t0)
        left = timeout - (time.monotonic() - t0)
        if left <= 0:
            return _fallback(repo, t0)
        rc, so, _ = _git(repo["root"], "write-tree", env=env, timeout=left)
        tree = so.strip()
        if rc != 0 or len(tree) not in (40, 64) or any(c not in "0123456789abcdef" for c in tree):
            return _fallback(repo, t0)
        return {"tree": tree, "kind": "snapshot", "ms": int((time.monotonic() - t0) * 1000)}
    except Exception as e:
        util.log_error("gitsnap.snapshot", e)
        return _fallback(repo, t0)


# --------------------------------------------------------------------------- refs and objects


def tree_exists(repo: dict, sha: str) -> bool:
    if not sha:
        return False
    if sha == EMPTY_TREE:
        return True
    rc, _, _ = _git(repo["root"], "cat-file", "-e", f"{sha}^{{tree}}", env=git_env(repo), timeout=10)
    return rc == 0


def resolve_commit(repo: dict, ref: str) -> str | None:
    return _out(repo["root"], "rev-parse", "--verify", "-q", f"{ref}^{{commit}}", timeout=10)


def commit_tree(repo: dict, commit: str) -> str | None:
    return _out(repo["root"], "rev-parse", "--verify", "-q", f"{commit}^{{tree}}", timeout=10)


def merge_base(repo: dict, a: str, b: str) -> str | None:
    return _out(repo["root"], "merge-base", a, b, timeout=30)


def current_branch(repo: dict) -> str | None:
    return _out(repo["root"], "symbolic-ref", "--short", "-q", "HEAD", timeout=10)


def default_branch(repo: dict) -> str:
    root = repo["root"]
    head = _out(root, "symbolic-ref", "--short", "-q", "refs/remotes/origin/HEAD")
    cands = []
    for name in ((head.split("/", 1)[1] if head and "/" in head else None), "master", "main"):
        if name and name not in cands and resolve_commit(repo, f"origin/{name}"):
            cands.append(name)
    if len(cands) > 1:
        def tip_time(name):
            v = _out(root, "log", "-1", "--format=%ct", f"origin/{name}")
            return int(v) if v and v.isdigit() else 0
        return max(cands, key=tip_time)
    if cands:
        return cands[0]
    for name in ("master", "main", "develop"):
        if resolve_commit(repo, name):
            return name
    return current_branch(repo) or "HEAD"


def is_ignored(repo: dict, rel: str) -> bool:
    rc, _, _ = _git(repo["root"], "check-ignore", "-q", "--", rel, timeout=10)
    return rc == 0


# --------------------------------------------------------------------------- fingerprints


def _fp(pairs: dict[str, str], rels: list[str]) -> str:
    body = "".join(f"{p}\0{pairs.get(p, '-')}\n" for p in sorted(set(rels)))
    return hashlib.sha1(body.encode("utf-8")).hexdigest()


def fingerprint_worktree(repo: dict, rels: list[str]) -> str:
    rels = sorted(set(rels or []))
    files = [r for r in rels if os.path.isfile(os.path.join(repo["root"], r))]
    blobs: dict[str, str] = {}
    if files:
        rc, so, _ = _git(repo["root"], "hash-object", "--stdin-paths", input_text="\n".join(files) + "\n", timeout=30)
        shas = so.split()
        if rc == 0 and len(shas) == len(files):
            blobs = dict(zip(files, shas))
        else:  # rare (e.g. a path with a newline): hash one by one
            for r in files:
                rc, so, _ = _git(repo["root"], "hash-object", "--", r, timeout=10)
                if rc == 0 and so.strip():
                    blobs[r] = so.strip()
    return _fp(blobs, rels)


def _ls_tree(repo: dict, tree: str, rels: list[str]) -> dict[str, str]:
    if not rels or tree == EMPTY_TREE:
        return {}
    rc, so, _ = util.run(["git", "--literal-pathspecs", "-C", repo["root"], "ls-tree", "-r", "-z", tree, "--", *rels],
                         env=git_env(repo), timeout=30)
    out: dict[str, str] = {}
    if rc != 0:
        return out
    for rec in so.split("\0"):
        if not rec or "\t" not in rec:
            continue
        meta, path = rec.split("\t", 1)
        parts = meta.split()
        if len(parts) >= 3:
            out[path] = parts[2]
    return out


def fingerprint_tree(repo: dict, tree: str, rels: list[str]) -> str:
    rels = sorted(set(rels or []))
    return _fp(_ls_tree(repo, tree, rels), rels)


# --------------------------------------------------------------------------- diffs


def _pathspec(paths):
    return ["--", *paths] if paths else []


def diff_numstat(repo: dict, a: str, b: str, paths: list[str] | None = None) -> list[dict]:
    env = git_env(repo)
    base = ["git", "--literal-pathspecs", "-C", repo["root"], "diff-tree", "-r", "-M", "-z", "--no-ext-diff"]
    rc, so, _ = util.run(base + ["--name-status", a, b] + _pathspec(paths), env=env, timeout=60)
    if rc != 0:
        return []
    status: dict[str, tuple[str, str | None]] = {}
    toks = so.split("\0")
    i = 0
    while i < len(toks):
        st = toks[i]
        if not st:
            i += 1
            continue
        letter = st[0]
        if letter in "RC" and i + 2 < len(toks):
            status[toks[i + 2]] = (letter, toks[i + 1])
            i += 3
        else:
            if i + 1 < len(toks):
                status[toks[i + 1]] = (letter, None)
            i += 2
    rc, so, _ = util.run(base + ["--numstat", a, b] + _pathspec(paths), env=env, timeout=60)
    if rc != 0:
        return []
    rows = []
    toks = so.split("\0")
    i = 0
    while i < len(toks):
        rec = toks[i]
        if not rec:
            i += 1
            continue
        parts = rec.split("\t")
        if len(parts) < 3:
            i += 1
            continue
        added, removed, path = parts[0], parts[1], parts[2]
        old = None
        if path == "" and i + 2 < len(toks):
            old, path = toks[i + 1], toks[i + 2]
            i += 3
        else:
            i += 1
        binary = added == "-" or removed == "-"
        letter, old_s = status.get(path, ("R" if old else "M", old))
        rows.append({"path": path, "old_path": old if old is not None else old_s, "status": letter,
                     "added": 0 if binary else int(added), "removed": 0 if binary else int(removed),
                     "binary": binary})
    return rows


def diff_patch(repo: dict, a: str, b: str, paths: list[str] | None = None) -> str:
    rc, so, _ = util.run(["git", "--literal-pathspecs", "-c", "core.quotePath=false", "-C", repo["root"], "diff",
                          "--no-color", "--no-ext-diff", "-M", a, b] + _pathspec(paths),
                         env=git_env(repo), timeout=120)
    return so if rc == 0 else ""


_HUNK = r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@"


def diff_hunks(repo: dict, a: str, b: str) -> dict[str, list[list[int]]]:
    import re

    rc, so, _ = util.run(["git", "-c", "core.quotePath=false", "-C", repo["root"], "diff", "--no-color",
                          "--no-ext-diff", "-M", "-U0", a, b], env=git_env(repo), timeout=120)
    out: dict[str, list[list[int]]] = {}
    if rc != 0:
        return out
    cur = None
    for ln in so.splitlines():
        if ln.startswith("+++ "):
            t = ln[4:]
            if t.startswith('"') and t.endswith('"'):
                t = t[1:-1]
            cur = None if t == "/dev/null" else (t[2:] if t.startswith("b/") else t)
            if cur is not None:
                out.setdefault(cur, [])
        elif ln.startswith("@@") and cur is not None:
            m = re.match(_HUNK, ln)
            if m:
                out[cur].append([int(m.group(1)), int(m.group(2)) if m.group(2) is not None else 1])
    return out


def whitespace_only(repo: dict, a: str, b: str) -> bool:
    if a == b:
        return False
    rc, _, _ = _git(repo["root"], "diff", "--no-ext-diff", "--ignore-all-space", "--quiet", a, b,
                    env=git_env(repo), timeout=60)
    return rc == 0


def file_line_count(repo_root: str, rel: str) -> int | None:
    try:
        with open(os.path.join(repo_root, rel), "rb") as f:
            data = f.read()
    except OSError:
        return None
    if not data:
        return 0
    return data.count(b"\n") + (0 if data.endswith(b"\n") else 1)
