# Reliability canary report

Use `scripts/run_reliability_soak.py` for the deterministic baseline, then add
the separately authorized real Adapter/Orca canary evidence. A report is a
release gate, not a narrative success claim: missing tools, version drift,
unknown cost or an unrun crash point remain explicit blockers.

Before any real call, run the no-model preflight:

```bash
uv run python scripts/prepare_reliability_canary.py \
  --output reliability-canary-preflight.json \
  --operator OPERATOR \
  --authorize-model-calls \
  --maximum-cost-usd LIMIT \
  --authorization-reference APPROVAL_ID
```

When an operator has an explicit user instruction to exclude OpenCode, append:

```bash
  --waive-opencode \
  --waiver-approved-by user \
  --waiver-reference APPROVAL_ID \
  --waiver-reason "User directed this canary to skip OpenCode"
```

Only OpenCode is waivable. The report must retain `approved_by`, `approved_at`,
`reason`, and `approval_reference`; Codex, Claude Code, Pi, Orca, and every
crash point remain mandatory.

The preflight performs only version/help probes and Git inspection. It records
`model_calls: 0` and `orca_objects_created: 0`, and fails unless the worktree is
clean, every exact tool version/platform is certified, and bounded authorization
is present. A green preflight authorizes nothing by itself; the supplied approval
must still be valid for the subsequent operator-controlled canary.

## Required identity

- source commit and dirty-worktree status;
- operating system and architecture;
- exact Codex, Claude Code, Pi, OpenCode and Orca versions;
- start/end timestamps and operator;
- model-call and maximum-cost authorization.

## Required crash matrix

Record the outcome and retained evidence path for Agent process death before
and after workspace write, before and after output parsing, plus Orca message,
controlled merge and cleanup boundaries. For each row include stable effect
identity, receipt before/after recovery, external terminal observation, retry
count and whether human action was required.

The JSON contract is deliberately stricter than a checklist. Set
`real_adapter_canary.performed` to `true` only after every referenced evidence
file has been retained next to the report:

```json
{
  "performed": true,
  "source_commit": "40-or-64-character-lowercase-git-object-id",
  "dirty_worktree": false,
  "platform": {"operating_system": "Darwin", "architecture": "arm64"},
  "started_at": "2026-09-15T00:00:00Z",
  "finished_at": "2026-09-15T01:00:00Z",
  "operator": "release-operator",
  "authorization": {
    "model_calls": true,
    "maximum_cost_usd": 5.0,
    "reference": "approval-or-ticket-id"
  },
  "adapter_results": {
    "codex": {"version": "...", "protocol": "exec-jsonl-v1", "passed": true, "evidence_path": "evidence/adapter-codex.json"},
    "claude-code": {"version": "...", "protocol": "json-envelope-v1", "passed": true, "evidence_path": "evidence/adapter-claude-code.json"},
    "pi-agent": {"version": "...", "protocol": "message-end-jsonl-v1", "passed": true, "evidence_path": "evidence/adapter-pi-agent.json"},
    "opencode": {"version": "...", "protocol": "run-jsonl-v1", "passed": true, "evidence_path": "evidence/adapter-opencode.json"},
    "orca": {"version": "...", "protocol": "orca-json-command-v1", "passed": true, "evidence_path": "evidence/adapter-orca.json"}
  },
  "waivers": {
    "opencode": {
      "approved_by": "user",
      "approved_at": "2026-09-15T03:00:00Z",
      "reason": "User directed this canary to skip OpenCode",
      "approval_reference": "approval-or-thread-reference"
    }
  },
  "crash_results": {
    "before_write": {
      "passed": true,
      "effect_identity": "stable-effect-id",
      "receipt_before": "prepared",
      "receipt_after": "completed",
      "external_terminal": "observed",
      "retry_count": 0,
      "human_action_required": false,
      "evidence_path": "evidence/crash-before-write.json"
    }
  },
  "duplicate_effects": 0,
  "terminal_divergences": 0
}
```

`crash_results` must contain all seven keys: `before_write`, `after_write`,
`before_output_parse`, `after_output_parse`, `orca_message`, `orca_merge`, and
`orca_cleanup`, each with the same fields shown above. Evidence paths must be
relative, must stay inside the report directory, and must exist when the gate
runs. The Adapter map must contain exactly the five entries shown above, or the
four non-OpenCode entries plus a valid OpenCode waiver.

## Required soak metrics

- iterations and elapsed time;
- lease expirations/takeovers and checkpoints observed;
- duplicate effects;
- recovery latency (minimum/average/maximum);
- manual interventions;
- conflicting terminal states;
- failure sample paths.

The release gate passes only when `scripts/check_reliability_report.py` accepts
the JSON report. Deterministic/fake evidence can validate the harness but can
never set `real_adapter_canary.performed` to true.
