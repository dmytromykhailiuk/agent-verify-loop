# agent-verify-loop

An independent verification loop for code changes made by a coding agent (Claude Code and Codex). When the agent has
changed code, a cheap **triage** step sizes the change, a fresh **reviewer** agent always reads the diff, and a **QA**
agent runs the tests, the app or a real browser when that adds value. The agent fixes the blocking findings and the
loop runs again until it passes; after three failed attempts it asks you. A **Stop gate** reminds the agent (`warn`) or
blocks the end of the turn (`enforce`) while changes are unverified.

The verifiers never see the implementer's reasoning, only the request, the acceptance criteria and the code. The
reviewer is read-only and QA never edits project files: a PreToolUse guard denies it, and an integrity check compares
the worktree before and after each verifier ran.

| Piece | What it does |
|---|---|
| `verify-loop` skill | The loop: begin → brief → consent → spawn → collect → judge → fix/re-run (≤ 3) → decide → retro. |
| `verify-review`, `verify-qa` skills | One standalone review or QA pass of the session, the working tree, a range, a branch or a PR. |
| `verify-retro` skill | Retro of a run or session: metrics, ledger, and (with agent-vault) lessons and change proposals. |
| `verify-triage`, `verify-reviewer`, `verify-qa` agents | The independent verifiers. Triage runs headless (`claude -p` / `codex exec`). |
| `verify-security`, `verify-perf`, `verify-ux` agents | Read-only specialist reviewers, turned on per change by triage, a flag or the repo config. |
| Hooks | SessionStart, UserPromptSubmit, PreToolUse, PostToolUse, Stop, SubagentStart, SubagentStop: change tracking, the gate, the guard. |
| `vloop` CLI | Python 3.10+, no dependencies: the bookkeeping behind all of the above, plus `setup` and `doctor`. |

## The loop

```
 you change code ──► Stop gate: unverified changes? ──► /verify-loop
                                                            │
   self-check ─► begin ─► brief (criteria, how to exercise, out of scope) ─► triage sizes it
                                                            │
      consent (only for browser QA) ─► spawn reviewer (+ specialists, + QA if triage says so)
                                                            │
                                      collect ─► integrity check ─► judge
                                                            │
              PASS ────────────────────────────────────────┤
              FAIL ─► fix blocking findings ─► begin --rerun ─► (≤ 3 attempts) ─► ESCALATE ─► you decide
                                                            │
                                                    retro-plan ─► light | full retro
```

Triage picks the QA scope (`none`, `checks`, `run`, `browser`) and the reviewer tier (`standard`, `deep`); a repo can
set floors. Browser QA needs your consent each time and always runs headless with a pinned Playwright MCP.

Triage can also turn on specialist reviewers that run next to the reviewer:

| Specialist | Turned on when | Effect on the result |
|---|---|---|
| `security` | auth/session/token/crypto/permission/payment code, untrusted input, workflows, SQL | Blocks only with evidence: a security/data finding with medium+ confidence, a valid anchor, an input → sink → impact scenario and an `evidence_ref` to the entry point, inside the diff. |
| `perf` | hot paths, IO or queries in loops, large client imports | Advisory: findings are reported, never fail the loop. |
| `ux` | UI files changed and no clear UI spec in the request (reads code, no browser) | Advisory: findings are reported, never fail the loop. |

Precedence: `--security|--perf|--ux on|off` on `begin` > repo `specialists` (`auto`, `on`, `off`) > triage. A re-run
never turns a specialist off: a failed one rechecks, a passed one reviews the delta or is carried forward when the
delta touches nothing it covers. When a specialist runs, the reviewer skips that part of its checklist.

## Requirements

- `python3` ≥ 3.10 and `git` on `PATH`.
- macOS or Linux. Windows works through WSL only (the CLI uses `fcntl` and process groups).
- `node` ≥ 18 and `npm`, only for browser QA (`vloop setup --install-browser`), plus a Chrome/Chromium.
- Claude Code (`claude`) and/or Codex (`codex`). Headless runs and triage call these CLIs.

## Install

### Claude Code plugin (skills + agents + hooks)

```
/plugin marketplace add dmytromykhailiuk/agent-verify-loop
/plugin install agent-verify-loop@agent-verify-loop
```

Everything is wired up; the agents appear as `agent-verify-loop:verify-reviewer` and `agent-verify-loop:verify-qa`.
Plugin agents cannot carry hooks, so the reviewer/QA guard is the global PreToolUse hook, which routes by the agent's
type. Optionally pre-approve the CLI with `vloop setup --agent claude --only perms`.

### Codex plugin (skills + hooks)

```
codex plugin marketplace add dmytromykhailiuk/agent-verify-loop
```

Install `agent-verify-loop` from `/plugins`, then add the custom agents (Codex plugins can't ship them):

```
<skill_dir>/bin/vloop setup --agent codex --only agents
```

Codex notes:

- **Hook trust.** Codex asks you to review new or changed hooks in `/hooks`, and the trust is tied to each hook's hash.
  Review and trust them after every plugin update; until then the hooks (and the Stop gate) do not run.
- **Sandbox.** vloop writes its state and nested `codex exec` verifiers need to reach the app under test. `vloop setup`
  prints the lines to add to `~/.codex/config.toml`:
  ```
  [sandbox_workspace_write]
  writable_roots = ["<state dir>"]
  network_access = true
  ```
  Without them triage falls back to a mechanical decision and QA may report `blocked` checks.
- **Models.** Codex verifiers use the session's model unless you set `platforms.codex.models` (see Configuration).

### skills.sh (skills only, any agent)

```
npx skills add dmytromykhailiuk/agent-verify-loop -g -a claude-code -a codex
```

skills.sh copies only the skill folders, so install the agents, hooks and permissions once:

```
<claude home>/skills/verify-loop/bin/vloop setup --agent claude    # Claude home: ~/.claude or $CLAUDE_CONFIG_DIR
~/.agents/skills/verify-loop/bin/vloop setup --agent codex         # skill dir for Codex
```

Without the agents the skills still work: they spawn a general-purpose sub-agent that reads
`<skill_dir>/agents/<name>.md` as its instructions.

### `vloop setup`, `vloop doctor`

```
vloop setup [--agent claude|codex|all] [--scope user|project] [--only agents,hooks,perms,browser]
            [--mode warn|enforce] [--install-browser] [--dry-run] [--uninstall] [--force]
vloop uninstall          # same as setup --uninstall
vloop doctor [--json]
```

| Agent | agents | hooks | perms |
|---|---|---|---|
| Claude Code | `~/.claude/agents/verify-*.md` with the guard and Playwright paths filled in | the 7 events in `~/.claude/settings.json` | allow rules for the CLI, the skill dir and the state dir |
| Codex | `~/.codex/agents/verify-*.toml` | the 7 events in `~/.codex/hooks.json` | prints the `writable_roots` / network block above |

`--scope project` writes to `<repo>/.claude/…` and `<repo>/.codex/…`. Setup is idempotent and keeps every other key.
It replaces or removes only the agent files it wrote itself (hashes in
`${XDG_DATA_HOME:-~/.local/share}/agent-verify-loop/installed.json`); a file you edited is kept unless you pass
`--force`, which saves a `.bak` copy. When the skill runs from an installed plugin, setup skips what the plugin already
provides. `vloop doctor` shows what is installed where and warns about duplicate or leftover hooks, the transport in
use, agent-vault, and the browser toolbox.

`--install-browser` copies the pinned toolbox manifest to the data dir and runs `npm ci` there (nothing is installed
into the plugin folder, which is replaced on updates). On its own it installs only the toolbox; combine it with
`--agent` or `--only` to install agents, hooks or permissions in the same run.

## Use

`/verify-loop` in Claude Code, `$verify-loop` in Codex, or plain words ("verify my changes"). The Stop gate also asks
for it when changes are unverified. Flags: `--qa none|checks|run|browser`, `--review standard|deep`,
`--security|--perf|--ux on|off`, `--no-triage`, `--repo <path>`; `status` and `reset` as the first argument.

| Command | What it does |
|---|---|
| `/verify-loop [status\|reset] [flags]` | The loop described above. |
| `/verify-review [A..B\|branch\|pr:<id>] [--focus …] [--criteria …]` | One independent review pass. |
| `/verify-qa [url] [A..B\|branch\|pr:<id>] [--qa checks\|run\|browser]` | One independent QA pass; a URL with no diff checks the running app. |
| `/verify-retro [--run <id>] [--proposals]` | Retro of a run or the session; reviews proposals when agent-vault is installed. |

The `vloop` CLI (use `vloop <command> --help`):

| Command | Purpose |
|---|---|
| `begin`, `brief`, `spawn`, `spawn-wait`, `collect`, `record`, `judge`, `consent`, `dispute`, `decide`, `findings` | The loop steps; the skill drives them. |
| `status`, `pending`, `mode`, `defer`, `skip`, `reset` | Gate state: what is unverified, switch the gate mode, pass the gate once, close loops without verifying. |
| `repo-config init\|show` | Create or show the repo config. |
| `retro-plan`, `retro-prep`, `ledger`, `gate-report` | Metrics, retro policy, per-project medians, gate/triage statistics. |
| `auth capture\|status` | Save or check the login state for browser QA (you log in by hand; no passwords stored). |
| `setup`, `uninstall`, `doctor`, `version --json`, `gc` | Installation, health check, machine-readable version, cleanup of old runs. |
| `hook <event>`, `guard <role>` | Entry points for the agent hooks. |

## Configuration

Effective config = defaults ⊕ user config ⊕ repo config (allowed keys only) ⊕ environment.

- **User config**: `${XDG_CONFIG_HOME:-~/.config}/agent-verify-loop/config.json` (`VERIFY_CONFIG` overrides the path).
- **Repo config**: `.agent-verify-loop.local.json` (personal, keep it out of git), then `.agent-verify-loop.json`
  (shared); the legacy `.claude/verify.json` is still read. `vloop repo-config init` creates one. Repo keys:
  `enabled`, `commands` (typecheck/lint/test commands with `{project}`/`{files}`), `app` (URL, start command),
  `auth`, `floors` (minimum QA scope / review tier), `specialists` (`{"security": "auto|on|off", "perf": …, "ux": …}`),
  `*_globs` (including `security_globs`), `notes`.
- **State**: `${XDG_STATE_HOME:-~/.local/state}/agent-verify-loop` (`VERIFY_STATE_DIR`). **Data** (browser toolbox,
  `installed.json`): `${XDG_DATA_HOME:-~/.local/share}/agent-verify-loop`.

| Key | Meaning |
|---|---|
| `mode` | Gate mode. `warn` (default): the Stop hook reminds, never blocks. `enforce`: blocks the end of a turn with unverified changes (at most `gate.max_blocks_per_fp` times per fingerprint). `off`: tracking only. `VERIFY_LOOP_MODE` overrides; `vloop mode [warn\|enforce\|off]`. Non-CLI entry points (IDE) are capped to `gate.non_cli_mode`. Review `vloop gate-report` before enforcing. |
| `transport` | `auto` (default), `agent`, `headless`, `herdr` or a third-party name; `transport_modules` lists extension files. See [docs/transports.md](docs/transports.md). |
| `integrity.enabled` | Compare the worktree before and after each verifier; a change outside the run dir makes the verdict INCONCLUSIVE. `integrity.qa_allowed_paths` whitelists paths QA may touch (e.g. a test database). |
| `platforms.codex.triage` | `{model, effort}` for `codex exec` triage (`null` model = Codex default). |
| `platforms.codex.models` | `{reviewer, security, perf, ux, qa}` model names for Codex verifiers; `null` = the session's model. |
| `triage.*`, `models.*`, `timeouts_s`, `loop.*` | Triage model and budget (sonnet/medium), reviewer/specialist/QA models per tier (`security`: `standard`, `high_risk` when triage rates the risk high, `delta`), per-role timeouts, attempt limits. |
| `specialists` | `{"security", "perf", "ux"}` each `auto` (default: triage decides), `on` or `off`. Allowed in repo config. |
| `qa_browser_consent` | `ask` (default). Browser QA never starts without your answer. |
| `cost_source` | `auto` (default): take USD, tokens and wall/active/waiting/idle time of the main session and the loop's agents from [agent-cost](#integration-with-agent-cost) when it is installed, else vloop's own numbers. `own`: always vloop's own. `agent-cost`: like `auto`, but `retro-plan` prints a warning when agent-cost is unavailable. Allowed in repo config. |
| `gate.scan_on_stop` | Per platform: scan the worktree at Stop to catch edits made through Bash (default on for Codex, which reports no edit paths for shell commands). |

Prompt templates and their optional blocks are described in [docs/templates.md](docs/templates.md).

Metrics count tokens and time for both platforms. USD is shown only for models listed in `config/pricing.json`.
Every agent and the main session carry `cost_source` (`agent-cost`, `own`, or `claude-p` for triage) and a `time`
block with wall, active, waiting and idle milliseconds (wall = active + waiting + idle). Active is time spent working;
waiting is non-active time while one of its sub-agents ran (agent-cost only; vloop's own accounting reports 0); idle is
the rest (waiting for the user, long pauses). `metrics.json` (`window.waiting_min`), the digest, ledger rows
(`minutes.waiting`) and `vloop ledger --summary` show all four; the retro policy uses active time.

## Integration with agent-cost

agent-cost is optional. vloop finds it through `AGENT_COST_CLI`, then `agent-cost` on `PATH`, then the Claude plugin
cache, and uses it only if `agent-cost version --json` names `agent-cost` with `contract` 1. At `retro-plan` /
`retro-prep` it runs `agent-cost export --session <main sid> --since <run start> --until <run close, or now while open> --json` (10 s
timeout)
and overlays USD, tokens and time onto the main session and onto each agent matched by session id, then Herdr id.
When agent-cost is used, the run window's wall/active/waiting/idle minutes all come from its main-session `time`
(a missing `waiting_ms` counts as 0); phase minutes and per-phase main tokens stay vloop's own. Triage keeps the
numbers `claude -p` reported. Agents that start after the run closed (the retro) are outside that window: vloop runs a
second `export` without `--until` and takes only their numbers from it (`cost_provider.after_close`); after
`vloop spawn retro` finishes it recomputes the run's ledger row and `metrics.json` so they include the retro. An agent
agent-cost does not list, or any failure (no CLI, timeout, bad JSON, non-numeric numbers, another contract), silently falls back to vloop's own numbers; `metrics.json` records the provider and the
reason under `cost_provider`. The retro policy compares the main session's active minutes from agent-cost when
available. An in-session `vault-keeper` counts as the `retro` role only when it is this run's retro (agent-cost
purpose `verify:retro` with this run id, or its prompt names this run's directory); a retro vloop launched through a
transport (`retro_done`) stays `retro` whatever purpose agent-cost reports. `vloop doctor` shows whether
agent-cost is detected and its contract.

## Integration with agent-vault

[agent-vault](https://github.com/dmytromykhailiuk/agent-vault) is optional. Both CLIs answer `version --json` with a `contract`
number (currently 1) so each can check the other.

- **With it:** reviewer and QA task files carry vault notes covering the changed code and lessons from past retros;
  `retro-plan` can run a full retro through the `vault-keeper` agent that writes a retro note, deduplicated lessons
  and harness/repo change *proposals* (never applied without your explicit Apply).
- **Discovery:** `AGENT_VAULT_CLI` / `VERIFY_VAULT_CLI`, then `vault` on `PATH`, then the Claude/Codex plugin caches and
  the skill dirs. A candidate counts only if `vault version --json` names `agent-vault`, so HashiCorp's `vault` is
  never used.
- **Without it:** nothing breaks. Lessons and notes are empty, the task files simply lack those sections, and the
  retro is "light only (no agent-vault)": metrics and the ledger are still updated.

## Migrating from a hand-installed `~/.claude/skills/verify-loop`

1. Install the plugin (or run the new `vloop setup`).
2. Remove the old hand-wired hooks: every `vloop hook …` entry in `~/.claude/settings.json` whose command points at
   `~/.claude/skills/verify-loop/bin/vloop` (SessionStart, UserPromptSubmit, PreToolUse, PostToolUse, Stop), and the
   old skill folders and agents you no longer need (`~/.claude/skills/verify-loop`, `verify-qa`, `verify-review`,
   `retro`, `~/.claude/agents/verify-*.md`).
3. Run `vloop doctor`. It warns when vloop hooks are registered twice and notes hooks "from another install" that
   `setup` does not manage; it exits non-zero while a problem remains.
4. Rename `.claude/verify.json` to `.agent-verify-loop.json` at your leisure; both are read.
5. `/retro` is now `/verify-retro`; the `herdr-agent` allowance moved to the Herdr transport (docs/transports.md).

## Development

```
python3 -m unittest discover -s tests
python3 scripts/sync_agents.py          # write the plugin copies agents/*.md
python3 scripts/sync_agents.py --check  # CI: fail when they drift
```

Layout: `skills/verify-loop/` is the skill (what skills.sh installs): CLI in `bin/`, code in `lib/vlooplib/`, prompts,
schemas, config defaults, the pinned browser toolbox and the **canonical** agent files in `agents/`. They use
`{{SKILL_DIR}}` placeholders that `vloop setup` replaces. `agents/` at the repo root holds the plugin copies:
`sync_agents.py` strips `hooks`, `mcpServers` and `permissionMode` (Claude plugin agents ignore them). `hooks/` and
`.claude-plugin/` are the Claude Code plugin parts, `.codex-plugin/` and `.agents/plugins/` the Codex ones. Tests are
hermetic: every test runs with `HOME`, the agent homes and the XDG dirs inside its own temp dir.

## License

MIT
