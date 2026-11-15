# Verify-loop triage · run {{run_id}} · attempt {{attempt}} · {{repo_slug}}

Decide how much independent verification this change needs: the QA scope (none / checks / run / browser), the
reviewer tier (standard / deep), which specialist reviewers run (security / perf / ux), change size, risk and your
confidence. You do not review or test the change. Pick the cheapest plan that still covers the request.

## User request (verbatim, newest last)

{{user_prompts}}

## Implementer's brief (acceptance criteria, how to exercise, out of scope)

{{brief}}

## Change stats

{{diff_stat}}

## Changed files

{{changed_files}}

## Signals

{{signals}}

## Security-sensitive files (security_globs; a hint, trust the diff)

{{security_files}}

## UI spec hints (design references found in the request and brief)

{{ui_spec_hints}}

## Perf hints (patterns in added lines; a hint, trust the diff)

{{perf_hints}}

## Calibration (this project's past runs: how often each choice found a blocking issue)

{{calibration}}

## Project facts

```json
{{project_facts}}
```

## Login state for browser QA

{{auth_status}}

## Previous plan (re-triage only)

{{previous_plan}}

## Delta since the previous attempt (re-triage only)

{{delta_stat}}

On re-triage, judge what the delta needs; the loop keeps the higher of the previous and your new scope/tier.

## Diff excerpt (capped; source files first, then tests)

```diff
{{diff_excerpt}}
```

## Output (exact shape; every field required, no other fields)

Return it through the StructuredOutput tool when it is offered; otherwise as the whole answer.

```json
{
  "change_size": "small | medium | large",
  "risk": "low | medium | high",
  "qa": {
    "scope": "none | checks | run | browser",
    "reason": "one concrete line, <= 160 chars",
    "focus": ["<= 5 short items"],
    "needs_login": false
  },
  "review": {
    "tier": "standard | deep",
    "reason": "one concrete line, <= 160 chars",
    "focus": ["<= 5 short items"]
  },
  "specialists": {
    "security": {"run": false, "reason": "one concrete line, <= 160 chars", "focus": ["<= 5 short items"]},
    "perf": {"run": false, "reason": "one concrete line, <= 160 chars", "focus": ["<= 5 short items"]},
    "ux": {"run": false, "ui_spec": "clear | partial | none | n/a", "reason": "one concrete line, <= 160 chars",
           "focus": ["<= 5 short items"]}
  },
  "confidence": "high | medium | low"
}
```

Answer with the JSON object only.
