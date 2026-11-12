"""MCP stdio handshake against pw-mcp (WP7 step 3).

Usage: python3 mcp_handshake.py [--pointer FILE] [--timeout S]
Runs initialize → notifications/initialized → tools/list over newline-delimited JSON-RPC and prints the tool count
and whether browser_navigate is present. Exit 0 when browser_navigate is present.
"""
from __future__ import annotations

import argparse
import json
import os
import select
import subprocess
import sys
import time
from pathlib import Path

PW_MCP = Path(__file__).resolve().parents[2] / "skills" / "verify-loop" / "bin" / "pw-mcp"


def _read_response(proc: subprocess.Popen, want_id: int, deadline: float) -> dict:
    buf = b""
    while time.time() < deadline:
        r, _, _ = select.select([proc.stdout], [], [], 0.5)
        if not r:
            if proc.poll() is not None:
                raise RuntimeError(f"pw-mcp exited with {proc.returncode}")
            continue
        chunk = os.read(proc.stdout.fileno(), 65536)
        if not chunk:
            raise RuntimeError("pw-mcp closed stdout")
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            if not line.strip():
                continue
            msg = json.loads(line)
            if msg.get("id") == want_id:
                return msg
    raise TimeoutError(f"no response for id {want_id}")


def _send(proc: subprocess.Popen, obj: dict) -> None:
    proc.stdin.write((json.dumps(obj) + "\n").encode())
    proc.stdin.flush()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pointer")
    ap.add_argument("--timeout", type=float, default=60)
    args = ap.parse_args(argv)
    cmd = [str(PW_MCP)] + (["--pointer", args.pointer] if args.pointer else [])
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    deadline = time.time() + args.timeout
    try:
        _send(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                     "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                "clientInfo": {"name": "verify-loop-e2e", "version": "1.0"}}})
        init = _read_response(proc, 1, deadline)
        server = (init.get("result") or {}).get("serverInfo") or {}
        _send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        _send(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        tools = [t.get("name") for t in ((_read_response(proc, 2, deadline).get("result") or {}).get("tools") or [])]
    finally:
        proc.kill()
        proc.wait(timeout=10)
    print(f"server: {server.get('name')} {server.get('version')}")
    print(f"tools: {len(tools)}")
    present = "browser_navigate" in tools
    print(f"browser_navigate: {'present' if present else 'MISSING'}")
    return 0 if present else 1


if __name__ == "__main__":
    sys.exit(main())
