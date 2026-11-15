---
name: verify-loop
description: Independent verification loop for code changes made in this session — a cheap triage agent sizes the change, a fresh code-review agent (always) and a QA agent (tests, the running app, Playwright for web UIs — only when triage says it adds value) check it; fix blocking findings and re-run until it passes; after 3 failed attempts escalate to the user. Use after implementing or changing code in a git repo, when the verify-loop Stop gate asks for it, or on "/verify-loop", "$verify-loop", "verify my changes", "run QA and review", "перевір зміни", "прожени QA і рев'ю".
argument-hint: "[status|reset] [--qa none|checks|run|browser] [--review standard|deep] [--security|--perf|--ux on|off] [--no-triage] [--repo <path>]"
allowed-tools: Bash(${CLAUDE_SKILL_DIR}/bin/vloop *)
---

# verify-loop

Independent agents verify what this session changed: triage sizes it, a reviewer always reads it, QA runs it when
that adds value. You fix blocking findings until the loop passes. The `vloop` CLI does the bookkeeping; you follow its
`next:` lines and relay its output.

## Paths

`<skill_dir>` is the folder that holds this `SKILL.md` (Claude Code: `${CLAUDE_SKILL_DIR}`; Codex: the path the skill
was loaded from). `$VL` is `<skill_dir>/bin/vloop` as an absolute path; it needs `python3` ≥ 3.10. Always type `$VL`
the same way so permission rules match. The session id comes from the environment (`CLAUDE_CODE_SESSION_ID`,
`CODEX_THREAD_ID`); the CLI detects Claude Code or Codex by itself.

## The loop

0. **Arguments** (`$ARGUMENTS`). `status` → `$VL status`, show the output, stop. `reset` → `$VL reset`, show the
   output, stop. Anything else (`--qa`, `--review`, `--security`, `--perf`, `--ux`, `--no-triage`, `--repo`) is
   passed to `begin`.

1. **Self-check first.** Finish the task. Run the fast checks you already know (typecheck, lint, related tests) and
   fix what they show. Don't start the loop on half-done work.

2. **Begin.** Run `git status` and `git diff --stat` first, so the brief can account for every changed path. Then
   `$VL begin [flags]`. Relay the first line. `SKIP` or `NOTHING` → relay it and stop (step 8 is not needed). An
   `also pending:` line → after this loop, run the command it gives.

3. **Brief.** Write the brief for the agents:
   ```
   $VL brief --run <id> <<'EOF'
   ## Acceptance criteria
   - <testable behavior from the user's request — what, not how>
   ## How to exercise
   - <facts: routes/URLs, commands, test data, feature flags, user role>
   ## Out of scope
   - <…>
   EOF
   ```
   - Account for every changed path. List the files that belong to the task; name files that are not part of it (the
     user's own edits, earlier iterations of the same session) under Out of scope as "not part of this change, do
     not review or flag", or ask the user first.
   - Add `--app-url <url>` if the app is already running.
   - Never include your plan, your reasoning, "I fixed…", or claims that it works. The agents judge the code, not your
     account of it.
   - Relay the plan block verbatim (the user sees the triage decision).

   If a `CONSENT NEEDED` block follows: ask the user exactly as its `next:` lines say (Claude Code: AskUserQuestion,
   header `Browser QA`, the three options; Codex: ask in the conversation with the same three options; put the
   why/url/checks lines in the question), run the matching `$VL consent allow|no-browser|skip-qa --run <id>` and
   continue with the `next:` lines it prints. Never call `$VL consent allow` without the user's answer. The same
   applies after `begin --rerun`.

4. **Spawn the agents** exactly as the `next:` lines say (see Launch below). Don't edit files while they run.
   - In-session lines (`Agent(...)` in Claude Code, "spawn the custom agent …" in Codex): spawn every printed role in
     one message, in the background, each with the `subagent_type` printed on its line; when they are done,
     `$VL collect --run <id>`. If collect says a verdict is missing: `$VL record <role> --run <id> <<'EOF'` + the
     agent's final message verbatim + `EOF` (`<role>` is `reviewer`, `security`, `perf`, `ux` or `qa`).
   - The plan may add specialist reviewers next to the reviewer: `security` (blocks only with evidence), `perf` and
     `ux` (advisory: their findings are reported but never fail the loop). Spawn every role the `next:` lines print,
     never only the reviewer and QA.
   - `$VL spawn <role> --run <id>` lines (headless or an extension transport): one Bash call per
     role, all in one message, in the background. Wait for the notifications; don't poll. Exit 3 → the child is
     blocked on a dialog: show the user the snapshot it printed and ask; never answer a permission or trust dialog
     yourself. After the user's answer run what they decided, then `$VL spawn-wait <role> --run <id>`. Exit 4
     (timeout) → later `$VL spawn-wait <role> --run <id>`.
   - When every role has a verdict, the output says `next: … judge`. If everything was carried, go straight to step 5.
   - `collect` also checks that no verifier modified the worktree; if it says "verifier modified files" the verdict
     is INCONCLUSIVE: look at the paths it lists before re-running.

5. **Judge.** `$VL judge --run <id>`. The first line is `RESULT: PASS|FAIL|INCONCLUSIVE|ESCALATE`. Relay the status
   line, the blocking findings and the unverified items.

6. **FAIL → fix and re-run.** Fix every blocking finding. If you are sure a finding is wrong, run
   `$VL dispute <finding-id> --run <id> --reason "<evidence>"` instead of arguing in text; the next reviewer rules on
   it. Minor findings and suggestions are optional and never a reason to re-run. Then:
   ```
   $VL begin --rerun --run <id> --fix-notes - <<'EOF'
   R1: <what changed, where>
   R2: disputed
   EOF
   ```
   one line per blocking finding, and continue at step 4 (relay the new plan block; handle `CONSENT NEEDED` as in
   step 3).
   **INCONCLUSIVE** → fix the environment when it's simple (start the app, free a port), then
   `$VL begin --rerun --run <id> --fix-notes -` as above. Login problems (`auth_missing`/`auth_expired`): give the
   user the printed `! … vloop auth capture --url …` command (it opens Chrome and needs them to log in), then re-run.

7. **ESCALATE** (3 failed attempts, or 2 inconclusive in a row) → show the report and ask the user (Claude Code:
   AskUserQuestion, header `Verify loop`) with the four options it prints: retry, waive, accept, stop. "Other" → map
   the user's words to one action plus `--note "<their words>"`. Run `$VL decide retry|waive|accept|stop --run <id>
   [--ids R1,Q2] [--note "…"]`, relay its output and continue as it says.

8. **Retro** (after PASS, `decide accept` or `decide stop`): `$VL retro-plan --run <id>`.
   - `retro: full` → launch it exactly as the `next:` line says, in the background, and finish your answer. When the
     retro reports back, relay 2–5 lines. If the output lists pending proposals (agent-vault only), follow
     `verify-retro` (`--proposals`): ask per proposal (Apply / Reject / Later), apply only on an explicit Apply.
   - `retro: light` → relay the line. `light only (no agent-vault)` means no vault is installed: that is expected,
     not an error.

## Launch

Spawn each verifier by the form the `next:` lines print:

- **Claude Code**: the Agent tool with the printed `subagent_type` (`verify-reviewer`, `verify-security`,
  `verify-perf`, `verify-ux`, `verify-qa`). Installed from the plugin they are listed as
  `agent-verify-loop:verify-reviewer` etc.; use whichever is listed (the plan prints the other form too). The prompt is exactly the printed one (`Your task file is …`), the model is the
  printed one, and `run_in_background: true`.
- **Codex**: spawn the custom agent named on the line (`verify-reviewer`, `verify-security`, `verify-perf`,
  `verify-ux`, `verify-qa`) with the printed message, and wait for it.
  Codex plugins can't ship agents: if they are not in your agent list, `vloop setup --agent codex --only agents`
  installs them (`$VL doctor` shows what is missing).
- **Headless / extension transports**: `$VL spawn <role> --run <id>` runs the verifier as a child process
  (`claude -p` / `codex exec`), or through an extension transport. Browser QA always runs this way: an
  in-session agent cannot get the Playwright MCP server. Which transport applies is the plan's business; do not pick
  one yourself (`transports.md` in the repo's `docs/` explains the selection).
- **The named agent isn't installed** (not in the agent list): spawn a general-purpose sub-agent whose task starts
  with `Read <skill_dir>/agents/<agent>.md and follow it as your instructions.` followed by the printed task
  message. Afterwards tell the user once that `vloop setup` installs the agents.
- **No sub-agent support at all**: tell the user the loop needs a sub-agent-capable session, or the `headless`
  transport (`"transport": "headless"` in the user config), which makes `begin` print `$VL spawn` lines instead.

Never approve a sub-agent's permission or trust dialog on the user's behalf; show it and ask.

## Rules

- Your final answer states every unverified criterion and any `skip` or `stop`; never hide a skipped check.
- Relay the loop's lines (begin, plan, consent, role lines, RESULT and status line) verbatim.
- Never approve browser QA for the user.
- If the user said not to verify at all: `$VL skip --reason "<their words>"` and tell them the change is unverified.
- If the user said not to run QA (e.g. "без QA", "no QA", "only review", "no tests/browser"): pass `--qa none` to
  `begin` (or to `begin --rerun` if the loop is already open). It overrides triage and repo floors for every attempt
  of the run; the review still runs. Never start QA, triage-chosen or not, against that request, and say in the final
  answer that QA was skipped by the user.
- If the user asked for or against a security, performance or UX review: pass `--security on|off`, `--perf on|off`
  or `--ux on|off` to `begin` (or `begin --rerun`). A flag beats the repo's `specialists` setting
  (`"specialists": {"security": "auto|on|off", …}` in `.agent-verify-loop.json`), which beats triage. Re-runs only
  turn specialists on, never off, unless a flag says `off`.
- If you stopped to ask the user something and the gate blocks, end with the question or run
  `$VL defer --reason "<why>"`.
- Verify agents never spawn agents; delegated sub-agents don't run this loop: they list the changed files in their
  final report and the orchestrator verifies.
- The gate mode (`warn`, `enforce`, `off`) is the user's choice: show it with `$VL mode`, change it only when asked
  (`$VL mode enforce`).
