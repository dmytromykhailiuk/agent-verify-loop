"""Regenerates the Codex rollout fixtures under sessions/. Shapes follow real Codex CLI rollouts
(session_meta, turn_context, event_msg task_started/user_message/token_count/task_complete, response_item
message/function_call/function_call_output/custom_tool_call); all text is placeholder text.
Run: python3 build_fixtures.py (writes next to this file)."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "sessions"
MAIN = "cccccccc-0000-4000-8000-000000000001"
SUB = "cccccccc-0000-4000-8000-0000000000a1"
D = "2026-10-20T"


def line(ts, typ, payload):
    return {"timestamp": D + ts, "type": typ, "payload": payload}


def usage(inp, cached, out, reason):
    return {"input_tokens": inp, "cached_input_tokens": cached, "output_tokens": out,
            "reasoning_output_tokens": reason, "total_tokens": inp + out}


def token_count(ts, total, last):
    return line(ts, "event_msg", {"type": "token_count", "info": {
        "total_token_usage": total, "last_token_usage": last, "model_context_window": 258400}, "rate_limits": {}})


def ctx(ts, model, effort="medium", turn="t1"):
    return line(ts, "turn_context", {"turn_id": turn, "cwd": "/repo/toy", "model": model, "effort": effort,
                                     "approval_policy": "on-request"})


def say(ts, text):
    return line(ts, "response_item", {"type": "message", "role": "assistant",
                                      "content": [{"type": "output_text", "text": text}]})


def shell(ts, call, cmd):
    return line(ts, "response_item", {"type": "function_call", "name": "shell", "call_id": call,
                                      "arguments": json.dumps({"command": ["bash", "-lc", cmd], "workdir": "/repo/toy"})})


def out(ts, call, text, code=0):
    return line(ts, "response_item", {"type": "function_call_output", "call_id": call,
                                      "output": json.dumps({"output": text, "metadata": {"exit_code": code}})})


def main():
    t1a, t1b = usage(1000, 200, 100, 30), usage(3000, 1200, 350, 100)
    t1b_last = usage(2000, 1000, 250, 70)
    t2_total, t2_last = usage(6000, 3000, 600, 150), usage(3000, 1800, 250, 50)
    L = [
        line("10:00:00.000Z", "session_meta", {"id": MAIN, "cwd": "/repo/toy", "originator": "codex_cli_rs",
                                                "cli_version": "0.99.0", "source": "cli"}),
        ctx("10:00:00.500Z", "gpt-5-codex"),
        line("10:00:01.000Z", "event_msg", {"type": "task_started", "model_context_window": 258400}),
        line("10:00:01.100Z", "response_item", {"type": "message", "role": "user", "content": [
            {"type": "input_text", "text": "<environment_context>…</environment_context>"}]}),
        line("10:00:01.200Z", "event_msg", {"type": "user_message", "message": "<prompt 1: implement the change>"}),
        say("10:00:05.000Z", "<assistant text 1>"),
        shell("10:00:06.000Z", "call_1", "rg -n pattern src"),
        out("10:00:07.000Z", "call_1", "<rg out>"),
        token_count("10:00:08.000Z", t1a, t1a),
        token_count("10:00:08.500Z", t1a, t1a),
        shell("10:00:10.000Z", "call_2", "npm test"),
        out("10:00:20.000Z", "call_2", "<test failure>", code=1),
        line("10:00:21.000Z", "event_msg", {"type": "token_count", "info": None, "rate_limits": {}}),
        token_count("10:00:22.000Z", t1b, t1b_last),
        line("10:00:30.000Z", "event_msg", {"type": "task_complete", "last_agent_message": "<done>"}),
        line("10:01:00.000Z", "compacted", {"message": "<summary>"}),
        ctx("10:05:00.000Z", "gpt-5.1-codex-max", effort="high", turn="t2"),
        line("10:05:00.100Z", "event_msg", {"type": "task_started", "model_context_window": 258400}),
        line("10:05:00.200Z", "event_msg", {"type": "user_message", "message": "<prompt 2: fix the failing test>"}),
        line("10:05:10.000Z", "response_item", {"type": "custom_tool_call", "name": "apply_patch", "call_id": "call_3",
                                                 "input": "*** Begin Patch\n*** Update File: src/a.ts\n*** End Patch"}),
        line("10:05:11.000Z", "response_item", {"type": "custom_tool_call_output", "call_id": "call_3",
                                                 "output": "Done!"}),
        token_count("10:05:12.000Z", t2_total, t2_last),
        token_count("10:05:12.200Z", t2_total, t2_last),
        line("10:05:20.000Z", "response_item", {"type": "function_call", "name": "spawn_agent", "call_id": "call_4",
                                                 "arguments": json.dumps({"agent_type": "vault-keeper",
                                                                          "message": "Operation: search\nfind X"})}),
        line("10:05:25.000Z", "event_msg", {"type": "turn_aborted", "reason": "interrupted"}),
        say("10:05:40.000Z", "<assistant text 2>"),
        line("10:05:50.000Z", "event_msg", {"type": "task_complete", "last_agent_message": "<done 2>"}),
    ]
    sub = [
        line("10:02:00.000Z", "session_meta", {"id": SUB, "cwd": "/repo/toy", "forked_from_id": MAIN,
                                                "source": {"subagent": "review"}}),
        ctx("10:02:00.500Z", "gpt-5-codex", effort="low", turn="s1"),
        line("10:02:01.000Z", "event_msg", {"type": "user_message", "message": "<task file prompt>"}),
        token_count("10:03:00.000Z", usage(500, 100, 100, 20), usage(500, 100, 100, 20)),
        token_count("10:04:00.000Z", usage(1100, 400, 300, 60), usage(600, 300, 200, 40)),
    ]
    day = OUT / "2026" / "10" / "20"
    if OUT.exists():
        shutil.rmtree(OUT)
    day.mkdir(parents=True)
    for sid, name, rows in ((MAIN, "rollout-2026-10-20T10-00-00-", L), (SUB, "rollout-2026-10-20T10-02-00-", sub)):
        (day / f"{name}{sid}.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))


if __name__ == "__main__":
    main()
