# Changelog

## Unreleased

- agent-cost integration (export contract 1): with `cost_source` `auto` (default) or `agent-cost`, USD, tokens and
  wall/active/waiting/idle time of the main session and the loop's agents come from `agent-cost export` (until the
  run's close; `waiting_ms` missing → 0; the run window takes all four times from agent-cost); triage keeps its
  `claude -p` numbers; any failure falls back to vloop's own accounting. Agents after the close (the retro) take
  their numbers from a second export without `--until`, per agent with the same fallback; after `vloop spawn retro`
  finishes, the run's ledger row and `metrics.json` are recomputed so they carry the retro's numbers. Each agent and the main session carry
  `cost_source`, metrics carry `cost_provider`, and the digest, ledger rows and `ledger --summary` show active,
  waiting and idle minutes; the retro policy keeps using active time. An export with non-numeric numbers falls back.
  `cost_source` is allowed in repo config. A transport-launched retro keeps role `retro` whatever agent-cost's
  purpose. `vloop doctor` reports agent-cost and its contract.
- Fix: an in-session `vault-keeper` counts as the retro only when it is this run's retro, not any vault-keeper in
  the window. Ledger `cost_usd` now includes `retro`. Agent `cost_source` is now the data origin; the former detail
  (`estimate`, `cost-state`, …) moved to `cost_basis`.
- Herdr integration: CLI lookup `VERIFY_HERDR_AGENT` → `HERDR_AGENT` → `PATH` → skill; `integration` number check with
  fallback to the Agent-tool transport; exit 7 (`api_error`) with hint and resume; `spawn-wait` re-sends a prompt that
  was never delivered; child edits are attributed to `HERDR_ROOT_SESSION_ID` (fallback `HERDR_PARENT_SESSION_ID`);
  relative vault dirs are excluded from edit tracking; the retro hint uses `herdr-agent run` inside Herdr at depth 0.

## 0.1.0

First standalone release of the verify loop, extracted from a hand-installed Claude Code setup.

- Packaging like agent-vault: Claude Code plugin (`.claude-plugin/`), Codex plugin (`.codex-plugin/`,
  `.agents/plugins/`), skills.sh-compatible `skills/`, plugin `hooks/hooks.json`, and `scripts/sync_agents.py` that
  writes the plugin agent copies without the keys Claude plugin agents ignore (`hooks`, `mcpServers`, `permissionMode`).
- Skills `verify-loop`, `verify-review`, `verify-qa`, `verify-retro` (the old `/retro`), agent-neutral for Claude Code
  and Codex, with a Launch section (Agent tool, Codex custom agent, headless lines, generic sub-agent fallback).
- Agents `verify-triage`, `verify-reviewer`, `verify-qa` with `{{SKILL_DIR}}` placeholders that `vloop setup` fills in;
  Codex gets TOML agents.
- Platforms: Claude Code and Codex (hook payload normalization, `apply_patch` edit paths, `codex exec` triage and
  headless verifiers, Claude and Codex transcripts for metrics).
- Hooks: SessionStart, UserPromptSubmit, PreToolUse, PostToolUse, Stop, SubagentStart, SubagentStop; the reviewer/QA
  guard routes by agent type; a worktree integrity check backs it up where the guard cannot see every call.
- Transports: `agent`, `headless`, and a Herdr extension, selected by `transport` (default `auto`); third-party
  transports via `transport_modules` / `VLOOP_TRANSPORTS` (docs/transports.md).
- `vloop setup | uninstall | doctor`, `--install-browser` for the optional Playwright toolbox.
- Optional agent-vault integration through contract version 1 (`version --json`); everything works without it.
- Gate mode defaults to `warn`. Codex metrics count tokens and time; USD only for models in `config/pricing.json`.
- Docs: README, docs/transports.md, docs/templates.md. CI: Python 3.10–3.13 on Ubuntu and macOS.
