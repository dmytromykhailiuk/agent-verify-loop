# QA · run {{run_id}} · attempt {{attempt}} · mode {{mode}} · scope {{qa_scope}}

- Repo: `{{repo_root}}` ({{repo_slug}}, branch {{branch}}). Work from this directory; never edit project files.
- {{code_ref_note}}
- Time budget: about {{time_budget_min}} minutes. Stop every process you started and write a verdict before it runs
  out, even a partial one (unfinished checks → `skip` with a note).
- Scope: **{{qa_scope}}** — {{qa_reason}}
- Run directory: `{{run_dir}}` · attempt directory: `{{attempt_dir}}`
- Artifacts (screenshots, traces): `{{artifacts_dir}}`
- Logs (app log, command output, `status-before.txt` / `status-after.txt`): `{{logs_dir}}`

Follow your system prompt (procedure, modes, verdict rules, output contract). This file holds the run data only.

## The user's request (verbatim — every distinct ask needs a check)

{{user_prompts}}

## Implementer's brief (acceptance criteria are a minimum, how to exercise, out of scope)

{{brief}}

## Focus (from triage)

{{focus}}

## The change

{{diff_summary}}

- Full patch (baseline → this attempt): `{{patch_path}}`

## Re-review inputs (attempt 2 and later)

- Delta since the previous attempt: `{{delta_path}}`
- Previous findings (re-check the failing ones first; reuse ids):

{{previous_findings}}

### Fix notes (implementer claims — verify them)

{{fix_notes}}

### Disputes

{{disputes}}

### User guidance

{{user_guidance}}

## Project context

- Project instruction files (CLAUDE.md / AGENTS.md) to read first (repo-relative):

{{repo_claude_md}}

- Project facts (detected; repo config values win):

```json
{{project_facts}}
```

- Repo verify config (`.agent-verify-loop.json`, legacy `.claude/verify.json`: commands, app, auth, notes):

```
{{verify_json}}
```

- Run/verify recipes in the repo (read them before starting anything):

{{recipes}}

- App URL: {{app_url}}

<!-- if vault_notes -->
- Vault notes covering the changed code (summaries only, do not open the vault):

{{vault_notes}}

<!-- endif -->
<!-- if lessons -->
- Lessons from past retros (follow them):

{{lessons}}
<!-- endif -->

## Login state

{{auth_status}}

Never type credentials and never try to bypass a login.

## Browser

{{browser_note}}

## Output

1. Write the verdict JSON (QA schema 2, every required field) with your file-writing tool (Write in Claude Code, apply_patch in Codex) to:
   `{{verdict_path}}`
   Use `"schema": 2, "role": "qa", "run_id": "{{run_id}}", "attempt": {{attempt}}, "mode": "{{mode}}", "scope": "{{qa_scope}}"`.
   Types: `evidence`/`repro` arrays of strings; `logged_in` boolean; text fields plain strings.
2. Write the file, then end with a ≤10-line summary and the same JSON in a ```json fence.
   Your final message is also a delivery path (headless runs keep only it, e.g. `codex exec -o`): the fence must
   hold the complete verdict JSON, identical to the file. If writing the file fails, still end with the JSON.
