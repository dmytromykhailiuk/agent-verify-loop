"""Codex `apply_patch` envelopes: which files a patch adds, updates, deletes or moves."""
from __future__ import annotations

import re
from dataclasses import dataclass

BEGIN = "*** Begin Patch"
END = "*** End Patch"
_OP_RE = re.compile(r"^\*\*\* (Add|Update|Delete) File:\s*(.*?)\s*$")
_MOVE_RE = re.compile(r"^\*\*\* Move to:\s*(.*?)\s*$")
_HEREDOC_RE = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
_MARKERS = ("*** Begin Patch", "*** Add File:", "*** Update File:", "*** Delete File:")


@dataclass
class PatchOp:
    op: str
    path: str
    move_to: str | None = None


def looks_like_patch(text) -> bool:
    return isinstance(text, str) and any(m in text for m in _MARKERS)


def _unwrap_heredoc(lines: list[str]) -> list[str]:
    for i, ln in enumerate(lines):
        if BEGIN in ln or _OP_RE.match(ln.strip()):
            return lines
        m = _HEREDOC_RE.search(ln)
        if m:
            delim = m.group(2)
            body = []
            for rest in lines[i + 1:]:
                if rest.strip() == delim:
                    break
                body.append(rest)
            return body
    return lines


_SHELL_HEAD_RE = re.compile(r"^\s*apply_patch\s+<<-?\s*(['\"])([A-Za-z_][A-Za-z0-9_]*)\1\s*$")


def is_pure_shell_patch(cmd: str) -> bool:
    if "\r" in cmd:
        return False
    lines = cmd.strip().split("\n")
    m = _SHELL_HEAD_RE.match(lines[0])
    if not m:
        return False
    stripped = [ln.strip() for ln in lines[1:]]
    if m.group(2) not in stripped:
        return False
    return not any(stripped[stripped.index(m.group(2)) + 1:])


_CHDIR_RE = re.compile(r"\b(?:cd|pushd|popd)\b|(?:^|\s)(?:-C|--chdir)(?=[\s=]|$)")


def changes_dir(cmd: str) -> bool:
    return bool(_CHDIR_RE.search(cmd))


def all_paths(cmd: str) -> list[str]:
    """Every file a shell command's patch envelopes name, across all heredocs and `printf '…\\n…'` strings."""
    out: list[str] = []
    for raw in cmd.replace("\r", "\n").replace("\\n", "\n").split("\n"):
        ln = raw.strip().strip("'\"").strip()
        star = ln.find("*** ")
        ln = ln[star:] if star > 0 else ln
        m = _OP_RE.match(ln) or _MOVE_RE.match(ln)
        p = m and m.group(m.lastindex)
        if p and p not in out:
            out.append(p)
    return out


def parse(patch_text: str) -> list[PatchOp]:
    if not isinstance(patch_text, str) or not patch_text:
        return []
    lines = _unwrap_heredoc(patch_text.replace("\r\n", "\n").replace("\r", "\n").split("\n"))
    ops: list[PatchOp] = []
    for raw in lines:
        ln = raw.strip()
        if ln == END:
            continue
        m = _OP_RE.match(ln)
        if m:
            if m.group(2):
                ops.append(PatchOp(m.group(1).lower(), m.group(2)))
            continue
        m = _MOVE_RE.match(ln)
        if m and ops and ops[-1].op == "update" and m.group(1):
            ops[-1].move_to = m.group(1)
    return ops


def paths(patch_text: str) -> list[str]:
    out: list[str] = []
    for op in parse(patch_text):
        for p in (op.path, op.move_to):
            if p and p not in out:
                out.append(p)
    return out
