---
name: verify-qa
description: Standalone independent QA (the verify-loop QA agent) of this session's changes, the working tree, a commit range, a branch or a pull request — typecheck/lint/related tests, running the app/CLI/HTTP endpoints, and Playwright in a real browser for web UIs; the URL is optional. One pass, structured checks and findings with evidence, no fix loop unless asked. Use on "/verify-qa", "$verify-qa", "QA my changes independently".
argument-hint: "[<url>] [<A..B>|<branch>|pr:<id>] [--focus \"…\"] [--criteria \"…\"] [--qa checks|run|browser] [--model haiku|sonnet|opus]"
disable-model-invocation: true
allowed-tools: Bash(${CLAUDE_SKILL_DIR}/../verify-loop/bin/vloop *)
---

# verify-qa

One independent QA pass; the loop's bookkeeping (`vloop`) lives in the sibling `verify-loop` skill.

**Paths.** `<skill_dir>` is the folder that holds this `SKILL.md` (Claude Code: `${CLAUDE_SKILL_DIR}`; Codex: the path
the skill was loaded from). `$VL` is `<skill_dir>/../verify-loop/bin/vloop` as an absolute path (resolve the `..`).
Always type `$VL` the same way so permission rules match.

## 1. Map the arguments (`$ARGUMENTS`)
Build one command: `$VL begin --standalone --roles qa [--url …] [--target …] [--focus "…"] [--criteria "…"] [--qa …] [--model …]`.
- An `http(s)://` argument → `--url <url>`.
- No target → this session's changes (default); `worktree`, `A..B`, a branch (`branch:<name>`), `pr:<id>` → `--target`.
- `--focus`, `--criteria`, `--qa checks|run|browser`, `--model haiku|sonnet|opus` → passed through unchanged.
- Free text that isn't a target or flag → `--focus "<text>"`.
- If the user gave acceptance criteria or how to exercise the change (routes, test data, user role), pass them with
  `--criteria`, or as a brief: `--brief-file - <<'EOF'` with `## Acceptance criteria`, `## How to exercise`,
  `## Out of scope`. Facts only: never your reasoning or claims that it works. For session changes, run `git status`
  first and name files that are not part of the change under Out of scope.

## 2. Run
1. Run the begin command. Relay its first lines and the plan block verbatim. On a clean tree with a URL, `--focus` or
   `--criteria` it is an exploratory check of the running app (the begin line says `no code diff`); that is expected.
2. If a `CONSENT NEEDED` block follows (triage chose the browser without `--url`/`--qa browser`): ask the user exactly
   as its `next:` lines say (Claude Code: AskUserQuestion, header `Browser QA`, the three options; Codex: ask in the
   conversation with the same options; put the why/url/checks lines in the question), run the matching
   `$VL consent allow|no-browser|skip-qa --run <id>` and continue with the `next:` lines it prints. Never call
   `consent allow` without the user's answer.
3. Launch QA exactly as the `next:` lines say (details in the `verify-loop` skill, section Launch). Browser QA is
   always a `$VL spawn qa --run <id>` line, because an in-session agent cannot get the Playwright MCP server:
   - `$VL spawn qa --run <id>` line: one Bash call in the background; wait for the notification, don't poll. Exit 3 →
     show the user the dialog snapshot and ask; never answer a permission or trust dialog yourself; after their
     answer run what they decided, then `$VL spawn-wait qa --run <id>`. Exit 4 → later `$VL spawn-wait qa --run <id>`.
   - In-session line (`checks`/`run` scope: `Agent(subagent_type="verify-qa" …)` in Claude Code, "spawn the custom
     agent `verify-qa` …" in Codex; from the Claude plugin the name is `agent-verify-loop:verify-qa`): spawn it in the
     background, then `$VL collect --run <id>`; if collect says the verdict is missing,
     `$VL record qa --run <id> <<'EOF'` + the agent's final message + `EOF`.
   - Agent not installed: a general-purpose sub-agent whose task starts with `Read <skill_dir>/../verify-loop/agents/verify-qa.md
     and follow it as your instructions.` followed by the printed task message.
4. `$VL judge --run <id>`.

## 3. Report
- Present the report as printed: status line, failing checks/findings with evidence (screenshot paths, console
  errors, commands), unverified criteria with their `how_to_fix`, skipped items. Never hide an unverified criterion.
- Login problems (`auth_missing`/`auth_expired`): give the user the printed `! … vloop auth capture --url …` command;
  it opens a Chrome window where they log in themselves.
- One pass. Offer fixes only if the user's request implies it.
