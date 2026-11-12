"""Regenerates the anonymized WP4 fixtures (transcripts, events, run dir). Structure mirrors real Claude Code 2.1.296
transcripts; all message text is placeholder text. Run: python3 build_fixtures.py (writes next to this file)."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent
S = "aaaaaaaa-0000-4000-8000-000000000001"     # main session
S2 = "aaaaaaaa-0000-4000-8000-000000000002"    # second session (gate report only)
H1 = "bbbbbbbb-0000-4000-8000-000000000001"    # herdr reviewer session (found by id, has cost-state)
H2 = "bbbbbbbb-0000-4000-8000-000000000002"    # herdr qa session (found by agent-name only)
R = "20261013-102000-toy-ab12"
D = "2026-10-13T"
U1 = {"input_tokens": 10, "cache_creation_input_tokens": 1000, "cache_read_input_tokens": 5000, "output_tokens": 100,
      "cache_creation": {"ephemeral_5m_input_tokens": 0, "ephemeral_1h_input_tokens": 1000},
      "output_tokens_details": {"thinking_tokens": 40}}


def asst(ts, mid, blocks, usage=U1, model="claude-opus-5-5", sidechain=False, effort="medium"):
    return {"parentUuid": None, "isSidechain": sidechain, "type": "assistant", "uuid": f"u-{mid}-{ts}",
            "timestamp": D + ts, "requestId": f"req-{mid}", "effort": effort, "sessionId": S,
            "message": {"id": mid, "model": model, "role": "assistant", "content": blocks, "usage": usage}}


def user(ts, content, **extra):
    d = {"parentUuid": None, "isSidechain": False, "type": "user", "uuid": f"uu-{ts}", "timestamp": D + ts,
         "sessionId": S, "message": {"role": "user", "content": content}}
    d.update(extra)
    return d


def tool(ts, mid, tid, name, inp, result="<result>", is_error=False, rts=None):
    return [asst(ts, mid, [{"type": "tool_use", "id": tid, "name": name, "input": inp}]),
            user(rts or ts, [{"type": "tool_result", "tool_use_id": tid, "content": result, "is_error": is_error}])]


def main_transcript():
    L = [asst("09:00:00.000Z", "m0", [{"type": "text", "text": "<previous task text>"}]),
         user("10:00:00.000Z", "<prompt 1: implement the change>"),
         asst("10:00:10.000Z", "m1", [{"type": "text", "text": "<assistant text 1>"}]),
         asst("10:00:10.500Z", "m1", [{"type": "tool_use", "id": "t1", "name": "Grep", "input": {"pattern": "<pat>"}}]),
         user("10:00:12.000Z", [{"type": "tool_result", "tool_use_id": "t1", "content": "<grep out>"}])]
    L += tool("10:00:20.000Z", "m2", "t2", "Glob", {"pattern": "src/**/*.ts"})
    L += tool("10:00:30.000Z", "m3", "t3", "Read", {"file_path": "/repo/src/a.ts"})
    L += tool("10:00:40.000Z", "m4", "t4", "Read", {"file_path": "/repo/src/a.ts"})
    L += tool("10:00:50.000Z", "m5", "t5", "Read", {"file_path": "/repo/src/a.ts"})
    L += tool("10:01:00.000Z", "m6", "t6", "Bash",
              {"command": "~/.claude/skills/herdr-agent/bin/herdr-agent run vault-search --prompt '<q>'"})
    L += tool("10:02:00.000Z", "m7", "t7", "Bash", {"command": "npm test"}, "<fail output>", True)
    L += tool("10:03:00.000Z", "m8", "t8", "Bash", {"command": "npm test"}, "<fail output>", True)
    L += tool("10:04:00.000Z", "m9", "t9", "Bash", {"command": "cat big.log"}, [{"type": "text", "text": "x" * 25000}])
    L += tool("10:05:00.000Z", "m10", "t10", "Bash", {"command": "git push"},
              "Permission to use Bash has been denied.", True)
    L += [asst("10:06:00.000Z", "m-syn", [{"type": "text", "text": "<synthetic>"}],
               usage={"input_tokens": 999999, "output_tokens": 999999}, model="<synthetic>"),
          asst("10:07:00.000Z", "m-side", [{"type": "text", "text": "<sidechain>"}],
               usage={"input_tokens": 888888, "output_tokens": 888888}, sidechain=True),
          user("10:08:00.000Z", [{"type": "text", "text": "[Request interrupted by user]"}]),
          {"type": "system", "subtype": "turn_duration", "durationMs": 120000, "timestamp": D + "10:09:00.000Z",
           "isSidechain": False, "sessionId": S},
          {"type": "system", "subtype": "compact_boundary", "content": "Conversation compacted",
           "timestamp": D + "10:10:00.000Z", "isSidechain": False, "sessionId": S},
          user("10:10:30.000Z", "<compact summary>", isCompactSummary=True),
          asst("10:25:00.000Z", "m11", [{"type": "text", "text": "<verify text>"}]),
          user("10:35:00.000Z", "<prompt 2: correction>"),
          asst("10:36:00.000Z", "m12", [{"type": "text", "text": "<fix text>"}], effort="high"),
          {"type": "system", "subtype": "turn_duration", "durationMs": 60000, "timestamp": D + "10:37:00.000Z",
           "isSidechain": False, "sessionId": S},
          asst("10:42:00.000Z", "m13", [{"type": "text", "text": "<verify 2 text>"}]),
          asst("10:46:00.000Z", "m14", [{"type": "text", "text": "<wrap-up text>"}])]
    return L


def herdr_reviewer():
    u = {"input_tokens": 5, "cache_creation_input_tokens": 2000, "cache_read_input_tokens": 10000, "output_tokens": 500}
    return [{"type": "agent-name", "agentName": "verify-review-111", "sessionId": H1},
            dict(asst("10:22:00.000Z", "h1a", [{"type": "text", "text": "<r>"}], usage=u, model="claude-sonnet-5-5"), sessionId=H1),
            dict(asst("10:28:00.000Z", "h1b", [{"type": "text", "text": "<r>"}], usage=u, model="claude-sonnet-5-5"), sessionId=H1),
            {"type": "cost-state", "sessionId": H1, "totalCostUSD": 0.42, "totalDuration": 400000}]


def herdr_qa():
    u = {"input_tokens": 3, "cache_creation_input_tokens": 3000, "cache_read_input_tokens": 0, "output_tokens": 700}
    return [{"type": "agent-name", "agentName": "verify-qa-222", "sessionId": H2},
            dict(asst("10:23:00.000Z", "h2a", [{"type": "text", "text": "<q>"}], usage=u, model="claude-sonnet-5-5"), sessionId=H2)]


def sub(ts, mid, usage, model):
    d = asst(ts, mid, [{"type": "text", "text": "<sub>"}], usage=usage, model=model, sidechain=True)
    return [user(ts, "<task prompt>", isSidechain=True), d]


def ev(ts, event, sid=S, run=None, attempt=None, repo="abc123abc123", **data):
    return {"ts": D + ts if "T" not in ts else ts, "sid": sid, "event": event, "repo": repo, "run": run,
            "attempt": attempt, "data": data}


def events():
    return [ev("09:00:00.000Z", "prompt", chars=10, task=1),
            ev("10:00:00.000Z", "prompt", chars=30, task=2),
            ev("10:05:00.000Z", "touch", paths=["src/a.ts"], source="edit", child_session=None),
            ev("10:10:00.000Z", "gate_warn", repos=["abc123abc123"], files=3, fp="f1", n=0),
            ev("10:20:00.000Z", "begin", run=R, attempt=1, kind="loop", files=3, added=40, removed=12,
               prefilter="triage", tree="t1", base_kind="snapshot", target="session"),
            ev("10:21:00.000Z", "triage", run=R, attempt=1, source="triage", model="claude-haiku-5-5", duration_s=6.1,
               cost_usd=0.004, qa_scope="run", review_tier="standard", change_size="medium", risk="medium",
               confidence="high", error=None),
            ev("10:21:05.000Z", "consent", run=R, attempt=1, decision="allow", note="staging only"),
            ev("10:30:00.000Z", "judge", run=R, attempt=1, result="fail"),
            ev("10:35:00.000Z", "prompt", chars=12, task=2),
            ev("10:40:00.000Z", "begin", run=R, attempt=2, kind="loop", files=3, added=40, removed=12,
               prefilter="triage", tree="t2", base_kind="snapshot", target="session"),
            ev("10:40:05.000Z", "triage", run=R, attempt=2, source="reused", model=None, duration_s=0, cost_usd=0,
               qa_scope="run", review_tier="standard", change_size="medium", risk="medium", confidence="high",
               error=None),
            ev("10:45:00.000Z", "judge", run=R, attempt=2, result="pass"),
            ev("10:45:00.500Z", "close", run=R, status="passed", reason="verified", with_tree=True)]


def events2():
    return [ev("11:00:00.000Z", "prompt", sid=S2, chars=5, task=1),
            ev("11:10:00.000Z", "gate_warn", sid=S2, repos=["k2"], files=2, fp="f2", n=0),
            ev("11:11:00.000Z", "gate_block", sid=S2, repos=["k2"], files=2, fp="f2", n=1),
            ev("11:12:00.000Z", "gate_ignored", sid=S2, repos=["k2"], files=2, fp="f2", n=2),
            ev("11:13:00.000Z", "gate_defer", sid=S2, reason="question"),
            ev("11:14:00.000Z", "gate_defer", sid=S2, reason="defer-cmd"),
            ev("11:15:00.000Z", "gate_skip_docs", sid=S2, files=["README.md"]),
            ev("11:16:00.000Z", "skip", sid=S2, reason="user said no", by="user"),
            ev("15:00:00.000Z", "begin", sid=S2, run="r2", attempt=1, kind="loop", files=1, added=3, removed=1,
               prefilter="trivial", tree="x", base_kind="snapshot", target="session"),
            ev("2026-10-01T09:00:00.000Z", "gate_warn", sid=S2, repos=["k2"], files=1, fp="old", n=0)]


def judge(n, result, roles, findings, unverified=()):
    return {"schema": 1, "run_id": R, "attempt": n, "kind": "loop", "result": result, "escalate": None,
            "roles": roles, "findings": findings, "suggestions": [], "unverified": list(unverified), "warnings": [],
            "browser_consent": None, "failed_attempts": 1, "inconclusive_streak": 0, "max_failed": 3,
            "elapsed_s": 300.0, "status_line": f"verify-loop · attempt {n}"}


def finding(fid, severity, blocking, **kw):
    f = {"role": "reviewer" if fid.startswith("R") else "qa", "id": fid, "severity": severity, "blocking": blocking,
         "late": False, "stuck": False, "waived": False, "downgraded_from": None, "downgrade_reason": None,
         "category": "correctness", "confidence": "high", "file": "src/a.ts", "line": 10, "title": "<title>",
         "problem": "<problem>", "scenario": "<scenario>", "suggestion": "", "check_id": None, "expected": None,
         "actual": None, "repro": [], "evidence": [], "dispute": None}
    f.update(kw)
    return f


def run_dir(root: Path):
    rd = root / "runs" / R
    ctx = {"schema": 1, "run_id": R, "kind": "loop", "session_id": S, "created_at": D + "10:20:00.000Z",
           "repo": {"key": "abc123abc123", "root": "/nonexistent/toy", "slug": "toy", "branch": "feature/x",
                    "head": "a1"},
           "target": {"spec": "session", "base_tree": "b0", "base_kind": "snapshot", "code_ref": "worktree",
                      "paths": []},
           "roles": ["reviewer", "qa"], "app_url": None, "transport": "herdr",
           "attempts": [
               {"n": 1, "begin_at": D + "10:20:00.000Z", "tree": "t1", "prefilter": "triage",
                "plan": "attempt-1/plan.json", "judge": "attempt-1/judge.json", "result": "fail", "elapsed_s": 540,
                "agents": {"reviewer": {"transport": "herdr", "herdr_id": "verify-review-111", "session_id": H1,
                                        "started_at": D + "10:21:30.000Z", "ended_at": D + "10:29:00.000Z",
                                        "status": "done", "model": "sonnet", "effort": "medium"},
                           "qa": {"transport": "herdr", "herdr_id": "verify-qa-222", "session_id": None,
                                  "started_at": D + "10:21:30.000Z", "ended_at": D + "10:29:30.000Z",
                                  "status": "done", "model": "sonnet", "effort": "medium"}}},
               {"n": 2, "begin_at": D + "10:40:00.000Z", "tree": "t2", "prefilter": "triage",
                "plan": "attempt-2/plan.json", "judge": "attempt-2/judge.json", "result": "pass", "elapsed_s": 280,
                "agents": {"reviewer": {"transport": "agent", "herdr_id": None, "session_id": None,
                                        "started_at": D + "10:41:00.000Z", "ended_at": D + "10:44:00.000Z",
                                        "status": "recorded", "model": "sonnet", "effort": "medium"}}}],
           "failed_attempts": 1, "inconclusive_streak": 0, "status": "passed", "waived": [],
           "disputes": {"R4": {"reason": "<reason>", "attempt": 1}}, "user_notes": [],
           "flags": {"qa": None, "review": None, "no_triage": False, "model": None},
           "browser_consent": {"status": "granted", "source": "user", "decision": "allow", "attempt": 1,
                               "at": D + "10:21:05.000Z", "note": "staging only"},
           "standalone": None}
    numstat = {"files": 3, "added": 40, "removed": 12, "changed_lines": 52, "rows": [], "ui_files": [],
               "risky_files": [], "test_files": [], "config_files": [], "docs_files": [], "ignored_files": [],
               "new_files": [], "deleted_files": [], "renamed_files": [], "new_source_files": [],
               "source_files": ["src/a.ts"], "docs_only": False}
    tri1 = {"schema": 1, "role": "triage", "run_id": R, "attempt": 1, "source": "triage", "change_size": "medium",
            "risk": "medium", "qa": {"scope": "run", "reason": "<r>", "focus": [], "needs_login": False},
            "review": {"tier": "standard", "reason": "<r>", "focus": []}, "confidence": "high",
            "agent": {"model": "claude-haiku-5-5", "duration_s": 6.1, "cost_usd": 0.004,
                      "tokens": {"input": 2, "cache_write": 9120, "cache_read": 0, "output": 310}, "error": None}}
    tri2 = dict(tri1, attempt=2, source="reused", reused_from=1, agent=None)
    plan1 = {"schema": 1, "run_id": R, "attempt": 1, "source": "triage", "triage_path": "attempt-1/triage.json",
             "roles": {"reviewer": {"run": True, "mode": "full", "tier": "standard", "model": "sonnet",
                                    "effort": "medium", "reason": "<r>", "timeout_s": 1200, "carried_from": None},
                       "qa": {"run": True, "mode": "full", "scope": "run", "model": "sonnet", "effort": "medium",
                              "reason": "<r>", "timeout_s": 1800, "carried_from": None, "focus": [],
                              "needs_login": False, "auth_status": "not_needed"}},
             "notes": [], "transport": "herdr"}
    plan2 = json.loads(json.dumps(plan1))
    plan2.update(attempt=2, source="reused")
    plan2["roles"]["reviewer"].update(mode="recheck")
    plan2["roles"]["qa"].update(run=False, carried_from=1)
    j1 = judge(1, "fail",
               {"reviewer": {"state": "fail", "carried": False, "carried_from": None, "blocking": 1, "minor": 1,
                             "suggestions": 0, "downgraded": 1, "reasons": []},
                "qa": {"state": "pass", "carried": False, "carried_from": None, "blocking": 0, "minor": 0,
                       "suggestions": 0, "downgraded": 0, "reasons": [], "scope": "run",
                       "checks": {"pass": 3, "fail": 0, "skip": 0, "blocked": 1}, "tainted": False}},
               [finding("R1", "major", True),
                finding("R4", "minor", False, downgraded_from="major", downgrade_reason="no concrete failing scenario")],
               [{"check_id": "C4", "criterion": "<c>", "need": "auth_expired", "how_to_fix": "<fix>"}])
    j2 = judge(2, "pass",
               {"reviewer": {"state": "pass", "carried": False, "carried_from": None, "blocking": 0, "minor": 0,
                             "suggestions": 0, "downgraded": 0, "reasons": []},
                "qa": {"state": "pass", "carried": True, "carried_from": 1, "blocking": 0, "minor": 0,
                       "suggestions": 0, "downgraded": 0, "reasons": [], "scope": "run",
                       "checks": {"pass": 3, "fail": 0, "skip": 0, "blocked": 1}, "tainted": False}},
               [finding("R7", "minor", False, late=True)])
    files = {"context.json": ctx, "attempt-1/diff.numstat.json": numstat, "attempt-2/diff.numstat.json": numstat,
             "attempt-1/triage.json": tri1, "attempt-2/triage.json": tri2, "attempt-1/plan.json": plan1,
             "attempt-2/plan.json": plan2, "attempt-1/judge.json": j1, "attempt-2/judge.json": j2}
    for name, obj in files.items():
        p = rd / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def write_jsonl(path: Path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, separators=(",", ":"), ensure_ascii=False) + "\n" for r in rows) + "{broken\n",
                    encoding="utf-8")


def main():
    for d in ("projects", "state"):
        shutil.rmtree(HERE / d, ignore_errors=True)
    proj = HERE / "projects" / "-repo-toy"
    write_jsonl(proj / f"{S}.jsonl", main_transcript())
    subdir = proj / S / "subagents"
    write_jsonl(subdir / "agent-r2.jsonl", sub("10:41:00.000Z", "s-r2", {"input_tokens": 1, "cache_creation_input_tokens": 1000,
                                                                        "cache_read_input_tokens": 0, "output_tokens": 100},
                                               "claude-sonnet-5-5"))
    (subdir / "agent-r2.meta.json").write_text(json.dumps({"agentType": "verify-reviewer", "description": "verify reviewer #2"}))
    write_jsonl(subdir / "agent-x.jsonl", sub("10:02:30.000Z", "s-x", {"input_tokens": 50, "cache_creation_input_tokens": 0,
                                                                      "cache_read_input_tokens": 0, "output_tokens": 50},
                                              "claude-haiku-5-5"))
    (subdir / "agent-x.meta.json").write_text(json.dumps({"agentType": "Explore"}))
    write_jsonl(subdir / "agent-old.jsonl", sub("08:00:00.000Z", "s-old", U1, "claude-opus-5-5"))
    (subdir / "agent-old.meta.json").write_text(json.dumps({"agentType": "verify-reviewer"}))
    other = HERE / "projects" / "-repo-toy-agents"
    write_jsonl(other / f"{H1}.jsonl", herdr_reviewer())
    write_jsonl(other / f"{H2}.jsonl", herdr_qa())
    st = HERE / "state"
    sess = {"schema": 1, "session_id": S, "created_at": D + "09:00:00.000Z", "updated_at": D + "10:46:00.000Z",
            "transcript_path": f"__TRANSCRIPTS__/-repo-toy/{S}.jsonl", "cwd": "/nonexistent/toy",
            "entrypoint": "cli", "depth": 0, "parent_session_id": None,
            "task": {"n": 2, "started_at": D + "10:00:00.000Z", "prompts": []}, "repos": {}}
    (st / "sessions").mkdir(parents=True)
    (st / "sessions" / f"{S}.json").write_text(json.dumps(sess, indent=2))
    write_jsonl(st / "sessions" / f"{S}.events.jsonl", events())
    write_jsonl(st / "sessions" / f"{S2}.events.jsonl", events2())
    run_dir(st)


if __name__ == "__main__":
    main()
