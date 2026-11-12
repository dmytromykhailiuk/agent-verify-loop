# Prompt templates

`vloop brief` renders one task file per verifier from the templates in
`skills/verify-loop/prompts/{triage,reviewer,qa,retro}.md`. The verifier's role instructions live in the agent files
(`skills/verify-loop/agents/`); the task file carries the data of the run.

## Placeholders

`{{name}}` is replaced by the value for the run. A placeholder with no value renders as `(none)`. A template that uses
an unknown placeholder fails to render, so a typo never reaches an agent. The allowed names per template are listed
in `tests/test_templates.py`.

## Optional blocks

A block wrapped in `<!-- if name -->` … `<!-- endif -->` is dropped, markers included, when the value of `name` is
empty or only whitespace. It is kept, without the markers, when `name` has content:

```markdown
<!-- if lessons -->
- Lessons from past retros (follow them):

{{lessons}}
<!-- endif -->
```

The reviewer and QA templates use this for `vault_notes` (summaries of agent-vault notes covering the changed code)
and `lessons` (lessons from past retros). Without agent-vault both values are empty, so the task file simply has no
vault sections. Blocks do not nest and have no `else`; the condition is a single placeholder name (letters, digits,
`.`, `_`, `-`).

The retro template fills `{{vloop}}`, `{{vault_cli}}` and `{{vault_ops_retro}}` the same way: the vault ones are empty
when agent-vault is not installed (and the retro is then "light only": metrics and the ledger, no vault note).
