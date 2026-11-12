# Changelog

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
