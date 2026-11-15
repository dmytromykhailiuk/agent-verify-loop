---
name: verify-review
description: Standalone independent code review (the verify-loop reviewer) of this session's changes, the working tree, a commit range, a branch or a pull request — one pass, structured findings, no fix loop unless asked. Use on "/verify-review", "$verify-review", "review my changes independently".
argument-hint: "[<A..B>|<branch>|pr:<id>] [--focus \"…\"] [--criteria \"…\"] [--review standard|deep] [--model sonnet|opus] [--security|--perf|--ux on|off]"
disable-model-invocation: true
allowed-tools: Bash(${CLAUDE_SKILL_DIR}/../verify-loop/bin/vloop *)
---

# verify-review

One independent review pass; the loop's bookkeeping (`vloop`) lives in the sibling `verify-loop` skill.

**Paths.** `<skill_dir>` is the folder that holds this `SKILL.md` (Claude Code: `${CLAUDE_SKILL_DIR}`; Codex: the path
the skill was loaded from). `$VL` is `<skill_dir>/../verify-loop/bin/vloop` as an absolute path (resolve the `..`).
Always type `$VL` the same way so permission rules match.

## 1. Map the arguments (`$ARGUMENTS`)
Build one command: `$VL begin --standalone --roles reviewer [--target …] [--focus "…"] [--criteria "…"] [--review …] [--model …] [--security|--perf|--ux on|off]`.
- No target → this session's changes (`--target session`, the default); `worktree` → uncommitted changes.
- `A..B` → `--target A..B`; a branch name → `--target branch:<name>`; `pr:<id>` → `--target pr:<id>`.
- `--focus`, `--criteria`, `--review standard|deep`, `--model sonnet|opus` → passed through unchanged.
- The reviewer brings the specialist reviewers along (`security`, `perf`, `ux`; triage turns each on only when the
  diff warrants it). `--security on|off`, `--perf on|off`, `--ux on|off` force one on or off. A security-only (or
  perf/UX-only) review: `--roles security` instead of `--roles reviewer`.
- Free text that isn't a target or flag → `--focus "<text>"`.
- If the user gave acceptance criteria or context in the conversation, pass them with `--criteria`, or as a brief:
  `--brief-file - <<'EOF'` with `## Acceptance criteria`, `## How to exercise`, `## Out of scope`. Facts only: never
  your reasoning or claims that the code works. For session changes, run `git status` first and name files that are
  not part of the change under Out of scope.

## 2. Run
1. Run the begin command. Relay its first lines and the plan block verbatim (the user sees the triage decision).
2. Launch every role the `next:` lines print (reviewer and any specialist), each with its printed
   `subagent_type`, all in one message (details in the `verify-loop` skill, section Launch):
   - In-session line (`Agent(subagent_type="verify-reviewer" …)` in Claude Code, "spawn the custom agent
     `verify-reviewer` …" in Codex; installed from the Claude plugin the name is `agent-verify-loop:verify-reviewer`):
     spawn it in the background, then `$VL collect --run <id>`; if collect says a verdict is missing,
     `$VL record <role> --run <id> <<'EOF'` + that agent's final message + `EOF` (`<role>`: `reviewer`, `security`,
     `perf`, `ux`).
   - `$VL spawn <role> --run <id>` lines: one Bash call per line in the background; wait for the notifications, don't poll.
     Exit 3 → show the user the dialog snapshot and ask; never answer a permission or trust dialog yourself; after
     their answer run what they decided, then `$VL spawn-wait <role> --run <id>`. Exit 4 → later
     `$VL spawn-wait <role> --run <id>`.
   - Agent not installed: a general-purpose sub-agent whose task starts with `Read <skill_dir>/../verify-loop/agents/<agent>.md
     and follow it as your instructions.` (`<agent>` is the printed `subagent_type` without a plugin prefix) followed by the printed task message.
3. `$VL judge --run <id>`.

## 3. Report
- Present the report as printed: status line, blocking findings (with file:line, scenario, fix), minor findings,
  suggestions. `perf` and `ux` findings are advisory (never blocking). Don't soften or drop findings, don't add your own.
- This is one pass. Offer fixes only if the user's request implies it ("review and fix"). If you fix, say which
  findings, and offer a re-review with `verify-review` (or `verify-loop` for session changes).
- If you are sure a finding is wrong, say so with evidence; `$VL dispute <id> --run <id> --reason "<evidence>"`
  records it for a later run.
