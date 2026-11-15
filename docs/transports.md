# Transports

A **transport** decides how a verifier (the reviewer, the QA agent, the retro writer) is started and waited for.
The loop itself (`begin`, `brief`, `collect`, `judge`, …) never changes; only the `next:` lines that tell the main
agent how to launch the verifiers do. The core knows three built-in transports and has no Herdr code: Herdr is an
extension that ships in the same package and is registered like any third-party transport.

## Built-in transports

| Name | Who spawns the verifier | Used when |
|---|---|---|
| `agent` | The main session. `begin` prints `Agent(subagent_type="agent-verify-loop:verify-reviewer", …)` lines (Claude Code) or "spawn the custom agent `verify-reviewer` …" (Codex); the session spawns them and runs `vloop collect`. | Default. |
| `headless` | `vloop spawn <role>` runs `claude -p --agent …` or `codex exec -` as a detached child process and blocks until it finishes or the role's timeout (`timeouts_s`). `vloop spawn-wait` picks a run up again after a timeout. | Browser QA always; every role with `"transport": "headless"`. It cannot run the retro (`supports_retro = False`): the retro then runs in-session. |
| `herdr` (extension) | `vloop spawn <role>` runs `herdr-agent run …`: the verifier is a real interactive session in its own Herdr tab that closes when it finishes. | `auto` picks it when `HERDR_ENV=1`, the session is at Herdr depth 0 and `herdr-agent` is found. |

## Selection rule

The config key `transport` (user config, `.agent-verify-loop.json`) is `auto` (default) or a transport name.

1. `auto` takes the available transport with the highest `priority` among those with `auto = True`:
   `herdr` (100) inside Herdr, otherwise `agent` (0). `headless` is never picked by `auto`.
2. An unknown name is logged and treated as `auto`.
3. **Hybrid rule.** The chosen transport runs every role, except browser QA on a transport with
   `supports_browser = False` (the in-session `agent` transport): an in-session sub-agent cannot get the Playwright MCP
   server, so that role runs `headless`. The plan block shows which transport each role uses.
4. A run remembers the session's transport in its `context.json`, so later attempts use the same one.

`vloop doctor` prints the transport the current config selects.

## The Transport contract

Subclass `vlooplib.transports.base.Transport` (`skills/verify-loop/lib/vlooplib/transports/base.py`).

| Member | Meaning |
|---|---|
| `name` | Unique id, the value of the `transport` config key. |
| `priority`, `auto` | Rank for `auto` selection; `auto = False` keeps the transport out of it. |
| `label` | Human name shown in plan and error lines. |
| `in_session` | `True`: `vloop` only prints `spawn_plan`; the main session spawns and runs `collect`. `False`: `spawn_plan` prints a `vloop spawn <role>` line and `vloop` calls `spawn`/`wait`. |
| `supports_browser`, `supports_retro` | May the transport run browser QA (it must attach the Playwright MCP server) / the retro agent. |
| `available(cfg)` | Can this transport be used now? |
| `spawn_plan(role, ctx, attempt, prompt_path, model, effort)` | Lines printed in the `next:` block of the plan. |
| `spawn(role, ctx, attempt, prompt_path, model, effort, cfg)` | Run the verifier and block until it ends or times out. Returns a `SpawnResult`. |
| `wait(info, timeout)` | Wait for a verifier that `spawn` left running (`timeout`, `blocked`). |
| `spawn_retro(ctx, run_dir, cwd, has_repo, cfg)` | Run the retro writer. Only called when `supports_retro`. |
| `hook_context(payload)` | Return a `HookContext(target_sid, is_child, suppress_gate, …)`: which session a hook event belongs to. A verifier session must report `suppress_gate = True` so the Stop gate does not fire inside it, and `is_child` with the parent's session id so its events are attributed to the parent. |
| `child_env_strip()` | Env var names that must not leak into child processes of vloop (triage, headless verifiers). |
| `locate_transcript(info)` | Find the verifier's transcript for the metrics; `None` if unknown. |
| `plan_footer`, `waitable`, `where`, `event_fields` | Optional presentation and bookkeeping helpers. |

`SpawnResult(status, rc, doc, stderr, info, result, screen)`:

- `status` is one of `done`, `manual`, `blocked`, `timeout`, `dead`, `no_result`, `error`.
- `info` is stored in the run and handed back to `wait`; put ids there (pid, tab id).
- `result` is the verifier's final message; `vloop` records it as the verdict when the verdict file is missing.
- `screen` is the snapshot of a blocked verifier, printed so the user can answer a dialog.

`vloop spawn` maps the statuses to exit codes: `done` 0, `blocked` 3 (show the snapshot to the user and ask; never
answer a permission or trust dialog yourself), `timeout` 4 (run `vloop spawn-wait` later), `dead` 5, `no_result` 6.

A verifier always writes its verdict file in the attempt directory. `vloop collect` also compares a fingerprint of the
worktree taken before the spawn with the worktree afterwards; a change outside the run directory makes the verdict
INCONCLUSIVE ("verifier modified files"). This integrity check is the backstop for transports where the PreToolUse
guard cannot see every tool call.

## Writing a third-party transport

A transport is a single Python file. Register it either by defining `TRANSPORT` or by calling `register()` on import:

```python
from vlooplib.transports import register
from vlooplib.transports.base import SpawnResult, Transport


class MyTransport(Transport):
    name = "mine"
    priority = 50
    label = "My runner"

    def available(self, cfg):
        return True

    def spawn_plan(self, role, ctx, attempt, prompt_path, model, effort):
        return [f"vloop spawn {role} --run {ctx['run_id']}"]

    def spawn(self, role, ctx, attempt, prompt_path, model, effort, cfg):
        return SpawnResult(status="done", result="…final message…", info={"transport": self.name})


TRANSPORT = MyTransport
```

Load it with the config key `transport_modules` (a list of file paths) or the env var `VLOOP_TRANSPORTS`
(paths separated by `os.pathsep`). A module that fails to import is logged and skipped; it never breaks vloop or the
hooks. Select it with `"transport": "mine"`; with `auto`, it competes by `priority` unless `auto = False`.

Rules for extensions:

- Never print or write secrets. Strip session-identifying env vars of the parent via `child_env_strip()`.
- Verifiers get their instructions from the task file named in the prompt (`Your task file is <path>. Read it
  completely and follow it.`); build the command line from `prompt_path`, the model and the effort you are given.
- Do not add hooks of your own that gate turns: report child sessions through `hook_context` instead.

## Herdr (extension)

The `herdr` transport (`transports/herdr.py`) is the only place that knows Herdr. It needs the `herdr-agent` CLI:
`VERIFY_HERDR_AGENT` (a path), then `HERDR_AGENT`, then `herdr-agent` on `PATH`, then the `herdr-agent` skill in the Claude
home. Its `herdr-agent version --json` must report `integration` ≥ 1; otherwise vloop warns and uses the `agent` transport.

- **Selection.** `auto` picks it inside Herdr (`HERDR_ENV=1`) at depth 0. A delegated sub-agent (Herdr depth ≥ 1)
  falls back to `agent`, and the loop is not run there (it lists changed files for the orchestrator instead). Force it
  with `"transport": "herdr"`.
- **Spawning.** `begin` prints one `vloop spawn <role> --run <id>` line per role that runs (`reviewer`, any specialist
  `security`/`perf`/`ux`, `qa`). Run each as its own background Bash call; the tab closes when the agent finishes.
- **Dialogs.** A folder-trust or permission dialog in a tab makes `spawn` exit 3 with the screen snapshot. Show it to
  the user, let them decide, run what they decided (`herdr-agent keys …`), then `vloop spawn-wait <role> --run <id>`.
  Exit 4 (timeout) → `vloop spawn-wait` later. When the prompt was never delivered (`last_prompt_at_ns` is 0 in
  `herdr-agent status`), `spawn-wait` re-sends it from the agent's `prompt.md`. Exit 7 (`api_error`, the turn ended on a
  Claude Code API error) prints the hint and the resume command; `spawn-wait` then sends `continue`.
- **Retro.** The retro agent also runs in a tab (`vloop spawn retro --run <id>`) when agent-vault is installed.
- **Hooks.** Events from a Herdr child are attributed to the root session (`HERDR_ROOT_SESSION_ID`, falling back to `HERDR_PARENT_SESSION_ID`), and the Stop
  gate is suppressed inside children.
