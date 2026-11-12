---
name: verify-review
description: Standalone independent code review (the verify-loop reviewer) of this session's changes, the working tree, a commit range, a branch or a pull request — one pass, structured findings, no fix loop unless asked. Use on "/verify-review", "$verify-review", "review my changes independently".
argument-hint: "[<A..B>|<branch>|pr:<id>] [--focus \"…\"] [--criteria \"…\"] [--review standard|deep] [--model sonnet|opus]"
disable-model-invocation: true
allowed-tools: Bash(${CLAUDE_SKILL_DIR}/../verify-loop/bin/vloop *)
---

# verify-review

One independent review pass; the loop's bookkeeping (`vloop`) lives in the sibling `verify-loop` skill.

**Paths.** `<skill_dir>` is the folder that holds this `SKILL.md` (Claude Code: `${CLAUDE_SKILL_DIR}`; Codex: the path
the skill was loaded from). `$VL` is `<skill_dir>/../verify-loop/bin/vloop` as an absolute path (resolve the `..`).
Always type `$VL` the same way so permission rules match.

## 1. Map the arguments (`$ARGUMENTS`)
Build one command: `$VL begin --standalone --roles reviewer [--target …] [--focus "…"] [--criteria "…"] [--review …] [--model …]`.
- No target → this session's changes (`--target session`, the default); `worktree` → uncommitted changes.
- `A..B` → `--target A..B`; a branch name → `--target branch:<name>`; `pr:<id>` → `--target pr:<id>`.
- `--focus`, `--criteria`, `--review standard|deep`, `--model sonnet|opus` → passed through unchanged.
- Free text that isn't a target or flag → `--focus "<text>"`.
- If the user gave acceptance criteria or context in the conversation, pass them with `--criteria`, or as a brief:
  `--brief-file - <<'EOF'` with `## Acceptance criteria`, `## How to exercise`, `## Out of scope`. Facts only: never
  your reasoning or claims that the code works. For session changes, run `git status` first and name files that are
  not part of the change under Out of scope.

## 2. Run
1. Run the begin command. Relay its first lines and the plan block verbatim (the user sees the triage decision).
2. Launch the reviewer exactly as the `next:` lines say (details in the `verify-loop` skill, section Launch):
   - In-session line (`Agent(subagent_type="verify-reviewer" …)` in Claude Code, "spawn the custom agent
     `verify-reviewer` …" in Codex; installed from the Claude plugin the name is `agent-verify-loop:verify-reviewer`):
     spawn it in the background, then `$VL collect --run <id>`; if collect says the verdict is missing,
     `$VL record reviewer --run <id> <<'EOF'` + the agent's final message + `EOF`.
   - `$VL spawn reviewer --run <id>` line: one Bash call in the background; wait for the notification, don't poll.
     Exit 3 → show the user the dialog snapshot and ask; never answer a permission or trust dialog yourself; after
     their answer run what they decided, then `$VL spawn-wait reviewer --run <id>`. Exit 4 → later
     `$VL spawn-wait reviewer --run <id>`.
   - Agent not installed: a general-purpose sub-agent whose task starts with `Read <skill_dir>/../verify-loop/agents/verify-reviewer.md
     and follow it as your instructions.` followed by the printed task message.
3. `$VL judge --run <id>`.

## 3. Report
- Present the report as printed: status line, blocking findings (with file:line, scenario, fix), minor findings,
  suggestions. Don't soften or drop findings, don't add your own.
- This is one pass. Offer fixes only if the user's request implies it ("review and fix"). If you fix, say which
  findings, and offer a re-review with `verify-review` (or `verify-loop` for session changes).
- If you are sure a finding is wrong, say so with evidence; `$VL dispute <id> --run <id> --reason "<evidence>"`
  records it for a later run.
