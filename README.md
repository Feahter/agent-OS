# Agent OS: Graph Engineering for Coding Agents

**Graph-engineered orchestration for coding agents.**

[English](README.md) | [简体中文](README.zh-CN.md) | [Roadmap](ROADMAP.md)

[![Version](https://img.shields.io/badge/version-0.0.1-blue.svg)](https://github.com/Feahter/agent-OS/releases/tag/v0.0.1)
[![CI](https://github.com/Feahter/agent-OS/actions/workflows/ci.yml/badge.svg)](https://github.com/Feahter/agent-OS/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.9%2B-3776AB.svg)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-yellow.svg)](LICENSE)

Agent OS is a local-first runtime for coordinating Codex, Claude Code, Pi and Orca as one governed execution system. It models work as a validated graph, moves data through explicit artifacts, and keeps approvals, budgets, verification, recovery and learning under one control plane.

The runtime has no third-party dependencies. Python 3.9+ and a POSIX-compatible system are required.

> **Status:** `0.0.1` is a pre-alpha release. The core contracts are tested, but public APIs may still change.

## Why Agent OS

Coding agents are useful on their own, but they do not naturally share execution semantics. Each tool has its own sessions, permissions, outputs and retry behavior. A shell script can start several agents; it cannot reliably answer which result is trusted, whether a side effect already happened, or what may be replayed after a crash.

Agent OS puts those decisions in a small, auditable runtime:

- Dependencies, concurrency and terminal conditions live in an explicit graph.
- Every node declares exactly which artifacts it reads and writes.
- Only Reality Anchor-approved results enter the persistent cache.
- Budgets, approvals, retries, time limits and protected paths fail closed.
- RSI can improve routing and conservative workflow candidates, but cannot relax safety constraints.
- Prompts, credentials and runtime state stay under the operator's control.

## Execution model

The standard engineering loop is deliberately finite:

```text
objective
  → read-only exploration
  → plan artifact
  → digest-bound approval
  → implementation
  → automated checks
  → independent read-only review
  → bounded repair cycles
  → Reality Anchor report
  → prompt-free learning signal
```

Every transition produces structured evidence. A changed workspace, policy or plan invalidates the prior approval. Mutating calls use effect receipts so an uncertain crash cannot silently replay the same side effect.

## What is included

| Area | What it does |
| --- | --- |
| Graph runtime | Validates DAGs, artifact contracts, concurrency, retries, gates, budgets and terminal Reality Anchors. |
| Agent adapters | Normalizes Codex, Claude Code and Pi capabilities, tools, usage, cost and structured outputs. |
| Policy routing | Selects an executor by capability, data class, quality, cost, latency, rate limit and circuit state. |
| Engineering workflow | Runs exploration, planning, approval, implementation, checks, independent review and bounded repair. |
| Orca coordination | Compiles graphs into Run/Task/Dispatch contracts and coordinates isolated workers and controlled merges. |
| Recovery | Persists checkpoints, effect receipts, leases and event cursors for crash-safe continuation. |
| RSI | Learns from prompt-free telemetry and quality feedback with evaluation, approval, canary rollout and rollback. |
| Verified reuse | Deduplicates concurrent work and persists only explicitly verified, policy-compatible results. |
| Operations | Provides local status, approvals, lineage, cost views, diagnostics, migration and release verification. |

## Quick start

```bash
git clone https://github.com/Feahter/agent-OS.git
cd agent-OS
python3 -m pip install -e .

agent-os validate examples/minimal_graph.json
agent-os demo examples/minimal_graph.json --work-dir /tmp/agent-os-demo
```

### Daily task interface

Use the same five actions regardless of which local agent is selected. Agent OS can infer bounded checks for common Python, Node, Rust, Go and Make projects; initialize a policy when you want explicit project rules:

```bash
agent-os do "Fix the login timeout and add a regression test" \
  --workspace /path/to/project \
  --constraint "Keep the public API stable"

agent-os status task-0123456789abcdef
agent-os approve task-0123456789abcdef --actor operator
agent-os result task-0123456789abcdef
```

`do` compiles the goal into a reviewable intent, performs read-only exploration and planning, returns a stable task ID, and stops at a digest-bound approval. The approval binds the goal, constraints, template, verification commands, project rules and proposed steps; any change invalidates it. `approve` executes only that plan and returns after bounded checks and an independent review. Use `control TASK_ID cancel --actor NAME` to cancel before approval. Add `--json` to any action for machine-readable output.

The first templates are `fix`, `test`, `refactor`, `research` and `release`. Selection is automatic, or can be overridden with `--template`. Research tasks are enforced as read-only. If the goal is too vague or no trusted verification command can be found, `do` stops before calling an agent and tells you what context is missing. For custom checks and budgets, run `agent-os engineer init --workspace /path/to/project` once and edit `.agent-os/engineering.json`.

The default home is `~/.agent-os`; override it with `AGENT_OS_HOME` or `--home`. Operational task state stays under `tasks/` and remains the single source for status and results. Portable, prompt-free learning and policy state stays under `state/`; raw objectives, project paths and runtime evidence are deliberately excluded from state exports.

These commands can call real local agents and incur model cost. GraphSpec and the existing lower-level commands remain available as advanced interfaces.

Run the test suite:

```bash
PYTHONPATH=. python3 -m unittest discover -s tests -v
```

### Baseline user outcomes

Evaluation records retain task classification, verification, duration, cost, intervention and recovery metrics. They do not copy objectives, project paths, prompts or raw responses:

```bash
agent-os evaluate record-engineering \
  --root /path/to/evaluation-state \
  --case examples/evaluation_case.json \
  --report /path/to/task/report.json \
  --run-id pilot-001 \
  --user-inputs 2 \
  --human-decisions 1

agent-os evaluate baseline \
  --root /path/to/evaluation-state \
  --name v0.0.1
```

Baselines measure time, cost and intervention per verified result. Existing baseline names cannot be overwritten, so later versions can produce comparable snapshots from the same cases.

### Use the engineering workflow

Initialize a project policy, then keep per-task state outside the target workspace:

```bash
agent-os engineer init --workspace /path/to/project

agent-os engineer plan \
  --workspace /path/to/project \
  --task-dir /tmp/agent-os-tasks/task-001 \
  --agent-os-root /path/to/agent-os-state \
  --objective "Fix the login timeout and add a regression test"

agent-os engineer run \
  --workspace /path/to/project \
  --task-dir /tmp/agent-os-tasks/task-001 \
  --agent-os-root /path/to/agent-os-state \
  --approved-by operator \
  --plan-digest PLAN_DIGEST
```

`engineer ship` combines planning and execution for interactive terminals, but still requires an explicit `y/N` confirmation. Automation must use the separate `plan` and `run --plan-digest` flow.

### Run a heterogeneous graph

`examples/heterogeneous_agents.json` asks Claude Code to produce an artifact and Pi to review it:

```bash
agent-os agent-run examples/heterogeneous_agents.json \
  --work-dir /tmp/agent-os-run \
  --workspace /path/to/project \
  --agent-os-root /path/to/agent-os-state
```

This command can call real models and incur cost. Shared writable workspaces are rejected for unordered agents; isolated workspaces must use the Orca backend.

## Graph contract

```json
{
  "id": "research-report",
  "max_concurrency": 2,
  "max_tokens": 1000,
  "require_reality_anchor": true,
  "nodes": [
    {
      "id": "verify",
      "kind": "verify",
      "deps": ["draft"],
      "reads": ["report"],
      "writes": ["verification"],
      "verifier_for": "draft",
      "reality_anchor": true,
      "gate": "release"
    }
  ]
}
```

A node can read only declared artifacts and must produce exactly its declared outputs. Graph validation rejects cycles, missing producers, unordered writes, unsafe shared workspaces and ungrounded terminal paths before execution starts.

## Supported tools

| Tool | Role | Integration |
| --- | --- | --- |
| Codex | Coding and review agent | Local CLI adapter |
| Claude Code | Coding and review agent | Local CLI adapter |
| Pi | Lightweight coding and review agent | Local CLI adapter |
| Orca | Isolated worker lifecycle and delivery | Graph compiler, backend and coordinator |

Adapters translate the common `read / shell / edit / write` tool contract into each product's protocol. Executor discovery runs only protocol checks such as `--help` and `--version`; it does not call a model.

## Governed RSI

RSI here means controlled policy improvement, not self-modifying runtime code.

1. Agent calls record prompt-free success, latency and cost observations.
2. Reality Anchors, verifiers or operators attach quality scores.
3. The system creates a versioned candidate and evaluates it against hard thresholds or a frozen regression suite.
4. An operator approves the candidate before activation.
5. Activation starts with a deterministic canary; regressions trigger rollback.

Learned policy never overrides data permissions, budgets, provider rate limits, circuit breakers, gates or Reality Anchor requirements. Sparse task-specific data falls back to the global policy instead of making unstable local decisions.

## Safety and recovery

- Graph, policy and workspace fingerprints bind approvals to the reviewed state.
- Effect receipts prevent blind replay of uncertain external writes.
- Checkpoints verify graph identity before resuming completed nodes.
- Provider rate limits and circuit breakers are shared safely across processes.
- Persistent reuse requires exact request identity, compatible scope and explicit verification.
- `confidential` and `restricted` requests bypass persistent reuse by default.
- Agent OS bundles exclude credentials, prompts, raw responses, worktrees and active leases.
- Controlled Git merge requires an isolated source, exact verification, a named gate and an unchanged target branch.

The project fails closed when state is damaged, a future schema is encountered or an external protocol drifts.

## State portability

The stable Agent OS root contains learning, optimization, reuse, approvals and routing state. Exported bundles use a strict file allowlist, per-file SHA-256 checksums and staged schema migration.

```bash
agent-os agent-os status --root /path/to/agent-os-state
agent-os agent-os export --root /path/to/agent-os-state --bundle /tmp/agent-os.bundle
agent-os agent-os import --root /path/to/restored-state --bundle /tmp/agent-os.bundle
agent-os agent-os doctor --root /path/to/agent-os-state
```

## Project layout

```text
grapheng/     runtime, adapters, coordination, governance and RSI
examples/     GraphSpec and policy examples
tests/        contract, recovery and cross-process integration tests
.workflow/    implementation plans and acceptance evidence
```

## Current limits

- `agent-run` and `engineer` are synchronous CLI entry points.
- The Orca coordinator is available as a Python API, not a resident daemon.
- Data classification is enforced as policy metadata; credential lifecycle remains the responsibility of each tool.
- Real-project and real-Orca rollout still needs a small, monitored pilot.
- Cross-host state, multi-tenant isolation and signed releases are not implemented yet.

## Contributing

The project will evolve around intent-driven use, one control plane, governed RSI and portable state. See the [long-term roadmap](ROADMAP.md). Read [CONTRIBUTING.md](CONTRIBUTING.md) before opening a pull request. Security issues should follow [SECURITY.md](SECURITY.md).

## License

[MIT](LICENSE)
