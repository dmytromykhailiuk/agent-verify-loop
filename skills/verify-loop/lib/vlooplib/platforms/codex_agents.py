"""Codex custom agents: render a canonical agent .md into ~/.codex/agents/<name>.toml."""
from __future__ import annotations

import json
import re

EFFORTS = ("minimal", "low", "medium", "high", "xhigh")
CLAUDE_MODEL_ALIASES = ("sonnet", "opus", "haiku", "fable", "inherit")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_KEY = re.compile(r"^([A-Za-z][\w-]*):[ \t]*(.*)$")


def _unquote(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] == '"':
        try:
            return json.loads(v)
        except ValueError:
            return v[1:-1]
    if len(v) >= 2 and v[0] == v[-1] == "'":
        return v[1:-1].replace("''", "'")
    return v


def parse(md_text: str) -> tuple[dict, str]:
    """(top-level scalar frontmatter fields, body). Nested blocks are skipped; `>`/`|` scalars are joined."""
    text = md_text.replace("\r\n", "\n")
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---", 3)
    if end < 0:
        return {}, text
    head = text[4:end].split("\n")
    rest = text[end + 4:]
    body = rest[1:] if rest.startswith("\n") else rest
    fields: dict = {}
    i = 0
    while i < len(head):
        m = _KEY.match(head[i])
        i += 1
        if not m:
            continue
        key, val = m.group(1), m.group(2)
        block = []
        while i < len(head) and (head[i].startswith((" ", "\t")) or head[i] == ""):
            block.append(head[i].strip())
            i += 1
        if val[:1] in (">", "|"):
            fields[key] = (" " if val[0] == ">" else "\n").join(ln for ln in block if ln or val[0] == "|").strip()
        elif val:
            fields[key] = _unquote(val)
    return fields, body


def _basic_string(s: str) -> str:
    return json.dumps(s, ensure_ascii=False).replace("\x7f", "\\u007f")


def _multiline(s: str) -> str:
    if "'''" not in s and not _CONTROL.search(s):
        return "'''\n" + s + "'''"
    return _basic_string(s)


def render_agent(md_text: str) -> tuple[str, str]:
    """(file name, TOML text). Claude-only keys (tools, permissionMode, mcpServers, hooks, model aliases) are dropped:
    Codex subagents inherit the parent's sandbox, tools and MCP servers."""
    fm, body = parse(md_text)
    name = str(fm.get("name") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.:-]+", name):
        raise ValueError("agent file has no valid name: in its frontmatter")
    lines = [f"name = {_basic_string(name)}", f"description = {_basic_string(str(fm.get('description') or ''))}"]
    model = str(fm.get("model") or "").strip()
    if model and model.lower() not in CLAUDE_MODEL_ALIASES and not model.lower().startswith("claude-"):
        lines.append(f"model = {_basic_string(model)}")
    effort = str(fm.get("effort") or "").strip().lower()
    if effort in EFFORTS:
        lines.append(f'model_reasoning_effort = "{effort}"')
    lines.append(f"developer_instructions = {_multiline(body.strip() + chr(10))}")
    return f"{name}.toml", "\n".join(lines) + "\n"
