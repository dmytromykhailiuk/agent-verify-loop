#!/usr/bin/env python3
"""Write the plugin copies agents/*.md from the canonical skills/verify-loop/agents/*.md. Claude plugin agents ignore
`hooks`, `mcpServers` and `permissionMode`, so those keys are stripped (the guard runs from the global PreToolUse hook,
routed by agent_type). --check only compares."""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "skills" / "verify-loop" / "agents"
DST = ROOT / "agents"
STRIP_KEYS = ("hooks", "mcpServers", "permissionMode")
KEY = re.compile(r"^([A-Za-z][\w-]*):")


def plugin_copy(text: str) -> str:
    if not text.startswith("---\n"):
        return text
    end = text.find("\n---\n", 3)
    if end < 0:
        return text
    head, rest = text[4:end].split("\n"), text[end + 1:]
    out, skipping = [], False
    for line in head:
        m = KEY.match(line)
        if m:
            skipping = m.group(1) in STRIP_KEYS
        elif skipping and not (line == "" or line[0] in " \t" or line.startswith("- ")):
            skipping = False
        if skipping or "{{" in line:
            continue
        out.append(line)
    return "---\n" + "\n".join(out) + "\n" + rest


def main(argv) -> int:
    check = "--check" in argv
    src = {p.name: plugin_copy(p.read_text(encoding="utf-8")) for p in sorted(SRC.glob("*.md"))}
    dst = {p.name: p.read_text(encoding="utf-8") for p in sorted(DST.glob("*.md"))}
    drift = sorted(n for n in set(src) | set(dst) if src.get(n) != dst.get(n))
    if check:
        for n in drift:
            print(f"out of sync: agents/{n}")
        return 1 if drift else 0
    DST.mkdir(exist_ok=True)
    for n in drift:
        if n in src:
            (DST / n).write_text(src[n], encoding="utf-8")
        else:
            (DST / n).unlink()
        print(f"synced agents/{n}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
