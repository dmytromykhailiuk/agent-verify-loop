# Retro task — run {{run_id}}

Operation: retro

You are the vault-keeper running a retro on one {{platform}} agent session/task. Follow {{vault_ops_retro}}
(after reading reference/schema.md). Read only the files named here; never open raw transcripts.

## Tools

vloop: {{vloop}}
vault_cli: {{vault_cli}}
ops: {{vault_ops_retro}}

## Run

- Run: {{run_id}}
- Session: {{session_id}}
- Platform: {{platform}}
- Repo root: {{repo_root}}
- Project slug: {{project_slug}}
- Harness project: {{harness_project}} (cross-project lessons, retros of non-repo sessions)
- Retro policy: {{policy}}

## Inputs (read only these)

- Metrics (tokens, cost, phases, signals, findings history, triage evaluation, ledger baselines): {{metrics_path}}
- Digest (timeline, verify history, anomalies): {{digest_path}}
- Judge files, one per attempt (skim):
{{judge_paths}}
- Ledger baselines for this project: `{{vloop}} ledger --project {{project_slug}} --summary`

Cost may be `null` ("n/a") when the model is not in the pricing table: reason from tokens and minutes then, and never
estimate a dollar figure yourself.

## Rules

- Evidence only: every "went well/badly" item cites a number or a line from the metrics, the digest or a judge file.
- Lessons are deduplicated against existing ones (`{{vault_cli}} lessons`); bump `occurrences` instead of creating
  twins.
- Harness or repo changes (CLAUDE.md / AGENTS.md, skills, agents, hooks, settings or Codex config, the repo's
  `.agent-verify-loop.json`) are only proposed in a lesson's `## Proposed change`; never edit those files.
- Write only inside the vault; commit with `{{vault_cli}} commit -m "retro: <topic>"`.
- End with the final message format of ops/retro.md (≤ 200 words, first line `Retro: <one-line verdict>`).
