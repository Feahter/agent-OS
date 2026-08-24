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
| Agent adapters | Normalizes Codex, Claude Code, Pi and OpenCode capabilities, tools, usage, cost and structured outputs. |
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

agent-os setup

agent-os validate examples/minimal_graph.json
agent-os demo examples/minimal_graph.json --work-dir /tmp/agent-os-demo
```

`setup` initializes the local portable state, inspects Python, filesystem semantics,
Codex, Claude Code, Pi, OpenCode and optional Orca, then discovers other installed
Agent tools including OpenClaw and Hermes Agent. It prints prioritized repair steps,
uses only help/version probes and makes zero model calls. Add `--json` for the
versioned diagnostic contract or `--home /path/to/state` to choose another local
state directory.

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

For work that should survive the terminal, approve it for background execution. The local resident starts automatically, keeps a durable priority queue, and uses the same task status and reports as foreground execution:

```bash
agent-os approve task-0123456789abcdef \
  --actor operator \
  --background \
  --priority 10

agent-os status task-0123456789abcdef
agent-os control task-0123456789abcdef pause --actor operator
agent-os control task-0123456789abcdef resume --actor operator
agent-os control task-0123456789abcdef reprioritize --priority 20 --actor operator
agent-os control task-0123456789abcdef cancel --actor operator

agent-os center
agent-os center --json
```

Pause and cancellation take effect at the next safe checkpoint between Agent calls or verification steps. An in-flight Agent process is allowed to reach that checkpoint; Agent OS does not claim arbitrary mid-call suspension. After restart or resume, completed mutating calls are recovered from Effect Receipts instead of being replayed.

`center` is the one-glance task view for engineering tasks, advanced Graph runs and Orca jobs. Items that need approval, an answer or recovery are shown first, followed by active and completed work; totals are not truncated by the display limit. The resident uses the host's native notification command on macOS or Linux for waiting, paused and terminal events. Delivery is best-effort and durably deduplicated: a notification failure never changes the task result. Notification history stays in `runtime/resident` and is excluded from portable RSI state.

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

Advanced integrations can submit Graph or Orca work to the same `ResidentCoordinator`, so the shared priority queue restores it after the caller exits. The resident stores only the job kind, state locator, ordering and control intent. Graph truth stays in `LocalControlPlane`, gate decisions still have one writer in `ApprovalInbox`, and Orca Run/Task/Dispatch state plus Effect Receipts stay in `OrcaCoordinator`:

```python
from pathlib import Path
from grapheng import GraphSpec, ResidentCoordinator

resident = ResidentCoordinator(Path.home() / ".agent-os")
graph = GraphSpec.from_json(Path("graph.json"))
resident.schedule_graph(graph, Path("/path/to/project"), priority=10)
# Or: resident.schedule_orca(graph, Path("/path/to/project"), priority=10)
resident.start_background()
```

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

`estimated_tokens` is an admission estimate. For Agent nodes, `max_tokens` is a hard execution contract: Agent OS forwards it to the request, requires an executor with the `token_budget` capability, and refuses to start an unbounded executor. A graph-level `max_tokens` therefore requires every Agent node to declare its own `max_tokens`; concurrent admission conservatively reserves those hard limits. The bundled CLI adapters currently report token usage but do not claim a native hard token limit, so token-capped Agent graphs fail before a model call. Use an executor that explicitly implements `token_budget`, or use Claude Code's `agent.max_cost_usd` hard dollar limit and treat `estimated_tokens` plus observed usage as measurements. Orca also rejects token- or dollar-capped nodes until its worker protocol can enforce them during execution.

## Supported tools

| Tool | Verified version (Darwin arm64) | Protocol | Integration |
| --- | --- | --- | --- |
| Codex | `0.148.0-alpha.9`, `0.149.0-alpha.4.1` | `exec-jsonl-v1` | Local CLI adapter |
| Claude Code | `2.1.234`, `2.1.241` | `json-envelope-v1` | Local CLI adapter |
| Pi | `0.84.1` | `message-end-jsonl-v1` | Local CLI adapter |
| [OpenCode](https://opencode.ai/) | `1.18.18` | `run-jsonl-v1` | Local CLI adapter |
| Orca | `1.4.180` | `orca-json-command-v1` | Graph compiler, backend and coordinator |

Adapters translate the common `read / shell / edit / write` tool contract into each product's protocol. Claude Code discovery also adapts to versions that temporarily omit the `--safe-mode` flag: isolation remains enabled through `CLAUDE_CODE_SAFE_MODE=1`, while versions that expose the flag receive both forms. This is separate from `--permission-mode dontAsk`, which governs tool authorization rather than configuration isolation. The original tool set was verified on Darwin arm64 on 2026-08-18; OpenCode was verified from its official Darwin arm64 release binary on 2026-08-19, and the installed Codex and Claude Code updates were verified on 2026-08-24. Evidence is version-controlled and travels with release bundles. Certification is platform-scoped and enforced during runtime discovery: a protocol-compatible but unverified version or platform remains visible in diagnostics but cannot execute a task.

`CliAgentAdapter` is the public kit for new local CLI integrations. It centralizes bounded process execution, environment isolation, timeout and process-fault classification, while the existing `AgentExecutor` protocol remains the single runtime seam. OpenCode uses non-interactive JSONL, `--pure`, project-config isolation and deny-by-default permissions; it reports observed token and cost data but does not claim an unsupported hard cost limit. OpenCode owns its session persistence, so its product session data is not copied into Agent OS portable bundles.

Setup also recognizes the following tools without treating them as executable Adapters:

| Discovered tool | Safe command probe | Agent OS status |
| --- | --- | --- |
| [OpenClaw](https://openclaw.ai/) | `openclaw` | Discovery only; Adapter pending |
| [Hermes Agent](https://github.com/NousResearch/hermes-agent) | `hermes` | Discovery only; Adapter pending |
| [Aider](https://aider.chat/) | `aider` | Discovery only; Adapter pending |
| [Gemini CLI](https://github.com/google-gemini/gemini-cli) | `gemini` | Discovery only; Adapter pending |
| [GitHub Copilot CLI](https://github.com/github/copilot-cli) | `copilot` | Discovery only; Adapter pending |

Discovery reports installation, path, version-probe state and integration state in
`discovered_agents`. A tool remains outside `ready_executors` until its real Adapter,
protocol conformance tests and version/platform compatibility evidence are complete.

Runtime discovery and setup use the same help/version compatibility authority and neither calls a model; Orca's version can also be read from its application bundle. Codex executes `codex exec --help` because an executable launcher does not prove that its architecture-specific native binary is present; OpenCode executes `opencode run --help`. Codex is not certified for Darwin x86_64. Only an exact protocol, version and platform evidence match can enter the executor registry; unknown versions, platforms, missing executables and protocol drift fail closed before a model call.

The offline fault baseline covers rate limits, timeouts, process crashes, damaged output and protocol drift. Rate limits and timeouts are classified as retryable while remaining governed by graph retry, budget and provider policies. A crashed process or unknown protocol can never be reported as a successful result.

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
agent-os agent-os compatibility
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

- `agent-run` and `engineer` remain synchronous CLI entry points; advanced Graph/Orca background submission currently uses the Python interface.
- Orca now shares the Agent OS resident lifecycle instead of requiring a separate daemon; question and escalation handling still uses the coordinator Python interface.
- Data classification is enforced as policy metadata; credential lifecycle remains the responsibility of each tool.
- Compatibility certification is currently exact-versioned to the table above; run `doctor` and add protocol evidence after upgrading a tool.
- Real-project and real-Orca rollout still needs a small, monitored pilot.
- Cross-host state, multi-tenant isolation and signed releases are not implemented yet.

## Contributing

The project will evolve around intent-driven use, one control plane, governed RSI and portable state. See the [long-term roadmap](ROADMAP.md). Read [CONTRIBUTING.md](CONTRIBUTING.md) before opening a pull request. Security issues should follow [SECURITY.md](SECURITY.md).

## License

[MIT](LICENSE)
