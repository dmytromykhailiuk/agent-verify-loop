---
name: verify-retro
description: Self-reflection on an agent session or a verify-loop run — what went well and badly (outcome, efficiency, tokens, process) with evidence, written to the vault as a retro note and deduplicated lessons when agent-vault is installed (metrics only otherwise); harness changes are only proposed. Use on "/verify-retro", "$verify-retro", "retro of this session".
argument-hint: "[--run <id>] [--session <id>] [--proposals]"
disable-model-invocation: true
allowed-tools: Bash(${CLAUDE_SKILL_DIR}/../verify-loop/bin/vloop *)
---

# verify-retro

**Paths.** `<skill_dir>` is the folder that holds this `SKILL.md` (Claude Code: `${CLAUDE_SKILL_DIR}`; Codex: the path
the skill was loaded from). `$VL` is `<skill_dir>/../verify-loop/bin/vloop` as an absolute path (resolve the `..`).
Always type `$VL` the same way so permission rules match.

The retro is written by the agent-vault `vault-keeper`. Without agent-vault there is nobody to write it: `$VL doctor`
shows `agent-vault: not found (optional)`, `retro-plan` prints `retro: light only (no agent-vault)` and
`retro-prep` says so. Then relay that line and stop; the metrics ledger is still updated.

## `--proposals` — review pending harness/repo change proposals
Only when agent-vault is installed (`$VL doctor` shows its path). `$V` is the CLI path from that line (or from
`retro-plan`'s `proposals:` line).
1. `$V lessons --proposals`. Nothing printed → say there are no open proposals and stop.
2. For each proposal: read the lesson note's `## Proposed change` (through the vault skill / `vault-keeper`, not by
   opening the vault yourself), then ask the user (Claude Code: AskUserQuestion, header `Proposal`): **Apply** /
   **Reject** / **Later**, with the target path and a one-line summary of the diff in the question.
3. Apply → make the edit yourself with the normal tools (the user sees the permission prompts), then
   `$V proposal accept <note>`. Reject → `$V proposal reject <note>`. Later → leave it.
4. Never apply a proposal to harness files (CLAUDE.md, AGENTS.md, skills, agents, hooks, settings, Codex config) or a
   repo's `.agent-verify-loop.json` without the user's explicit Apply.

## Otherwise — run a retro
1. Pick the scope: a run the user names → `$VL retro-prep --run <id>`; else `$VL retro-prep` for this session (the
   id comes from `CLAUDE_CODE_SESSION_ID` / `CODEX_THREAD_ID`; pass `--session <id>` if the CLI can't find it).
2. It prints `retro task: <path>` and a `next:` line. Launch it exactly as the `next:` line says, in the background:
   - In-session line (`Agent(subagent_type="vault-keeper" …)` in Claude Code, `agent-vault:vault-keeper` when the
     vault is a plugin; "spawn the custom agent `vault-keeper` …" in Codex): spawn it in the background.
   - `$VL spawn retro --run <id>` line (a transport with retro support, i.e. an extension such as tabs): one Bash call in the background; wait
     for the notification. Exit 3 → show the user the dialog snapshot and ask; never answer it yourself.
3. When it reports back, relay 2–5 lines: outcome, what went well/badly, lessons created or updated, and any
   proposals (then handle them as in `--proposals`, steps 2–4).
