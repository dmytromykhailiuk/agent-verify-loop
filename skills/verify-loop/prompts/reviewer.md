# Code review · run {{run_id}} · attempt {{attempt}} · mode {{mode}}

- Repo: `{{repo_root}}` ({{repo_slug}}, branch {{branch}}). Work from this directory.
- {{code_ref_note}}
- Time budget: about {{time_budget_min}} minutes. Write a verdict before it runs out, even a partial one
  (list what you did not get to under `coverage.files_skipped`).
- Review tier: {{review_tier}}
- Run directory: `{{run_dir}}` · attempt directory: `{{attempt_dir}}`

Follow your system prompt (procedure, calibration rubric, severity, re-review protocol, output contract). This file
holds the run data only.

## The user's request (verbatim — the source of truth for what the change must do)

{{user_prompts}}

## Implementer's brief (acceptance criteria, how to exercise, out of scope)

{{brief}}

The brief is the implementer's summary; when it conflicts with the user's request, the request wins.

## Focus (from triage — start here, but review the whole change)

{{focus}}

## The change

{{diff_summary}}

- Full patch (baseline → this attempt): `{{patch_path}}` — read it completely, then read whole files and callers.
- Baseline note: {{baseline_note}}
- Files in the diff that this session did not edit (possibly the user's or a tool's edits; review them only as far
  as the user's request covers them):

{{outside_files}}

## Re-review inputs (attempt 2 and later)

- Delta since the previous attempt: `{{delta_path}}` — review it fully.
- Previous findings (give every id a `rechecked[]` status; reuse ids; new ids continue the numbering):

{{previous_findings}}

### Fix notes (implementer claims — verify them)

{{fix_notes}}

### Disputes (rule on each in `rechecked[].dispute_ruling`)

{{disputes}}

### User guidance

{{user_guidance}}

## Project context

- Project instruction files (CLAUDE.md / AGENTS.md) to read first (repo-relative):

{{repo_claude_md}}

<!-- if vault_notes -->
- Vault notes covering the changed code (summaries only, do not open the vault):

{{vault_notes}}

<!-- endif -->
<!-- if lessons -->
- Lessons from past retros (follow them):

{{lessons}}
<!-- endif -->

## Output

1. Write the verdict JSON (reviewer schema 2, every required field) with your file-writing tool (Write in Claude Code, apply_patch in Codex) to:
   `{{verdict_path}}`
   Use `"schema": 2, "role": "reviewer", "run_id": "{{run_id}}", "attempt": {{attempt}}, "mode": "{{mode}}"`.
   Types: `coverage.files_reviewed` is an integer count (not a list); `line` integer or null.
2. Write the file, then end with a ≤10-line summary and the same JSON in a ```json fence.
   Your final message is also a delivery path (headless runs keep only it, e.g. `codex exec -o`): the fence must
   hold the complete verdict JSON, identical to the file. If writing the file fails, still end with the JSON.
