# Agent OS Roadmap

[English](ROADMAP.md) | [简体中文](ROADMAP.zh-CN.md)

This roadmap describes the long-term path from the current preview to a general operating layer for agents. Version numbers indicate maturity, not fixed dates. A milestone ships only after it meets its user-outcome and quality gates.

## North star

**A user states an outcome and its constraints. Agent OS selects, coordinates and governs the right agents, then delivers a verified, recoverable and cost-controlled result.**

Graphs, model routing, context assembly, concurrency, retries and state migration should be implementation details for most users. The primary experience should revolve around four things: intent, consequential choices, progress and results.

The everyday entry point should eventually be as simple as:

```bash
agent-os do "Fix the login timeout, add tests, verify the change, and commit it"
```

Agent OS should inspect the environment, plan the work, choose executors, isolate mutations, verify the result, recover from failures and retain useful experience. It should interrupt the user only for material permissions, costs or irreversible actions.

## Product principles

1. **Start from user intent.** Users describe the result, not a GraphSpec or provider-specific flags.
2. **Simple by default, inspectable on demand.** Reliable defaults cover common work; graphs, routing, budgets and policies remain progressively available.
3. **One task model across agents.** Codex, Claude Code, Pi, Orca and future tools share a stable task interface.
4. **Trust evidence, not self-reported completion.** Tests, review, Reality Anchors and traceable evidence determine success.
5. **Make failure recoverable.** Work can pause, resume, roll back and migrate without silently replaying effects.
6. **Become easier with use.** Learn preferences, project conventions and successful paths to reduce repeated instructions and approvals.
7. **Become more efficient with use.** Reduce latency, model calls and cost without lowering the quality gate.
8. **Govern all learning.** RSI improvements are evaluated, approved, canaried, explainable and reversible.
9. **Stay local-first and portable.** Users own data, credentials and state. Open, versioned formats prevent agent, model and cloud lock-in.
10. **Maintain one control plane.** Status, approvals, cost, evidence and policy must have a single source of truth.

## User experience contract

The public surface should converge on five stable actions:

| User action | User outcome |
| --- | --- |
| `do` | Submit a natural-language goal with optional time, quality and cost constraints. |
| `status` | Understand current work, waits, spend and the likely remaining effort. |
| `approve` | Decide only consequential choices with clear risk and impact. |
| `control` | Pause, resume, cancel, reprioritize or change policy. |
| `result` | Receive artifacts, evidence, a change summary, cost and reusable learning. |

These actions should sit at deep module seams: the public interface stays small while graph compilation, agent differences, context management, recovery and learning remain in the implementation. GraphSpec and policy interfaces remain available to advanced users without becoming onboarding requirements.

## Milestones

### 0.0.x: trusted foundation — current

Prove that heterogeneous agents can share governed execution semantics. The current base includes the graph runtime, artifact contracts, Codex/Claude Code/Pi adapters, Orca coordination, approvals, budgets, recovery, controlled merge, verified reuse, Reality Anchors, governed RSI and portable state.

Exit gates:

- Core contracts have stable automated and fault-injection coverage.
- At least three monitored real-project pilots establish success, failure, intervention and cost baselines.
- Preview interfaces are clearly separated from interfaces intended for compatibility.

### 0.1: first-use success

Let a new user complete a real task in minutes without writing a graph or complex policy.

Key outcomes:

- One-command installation, environment diagnosis and agent discovery.
- A natural-language `agent-os do` entry point and an intent-to-graph compiler.
- Templates for fixes, tests, refactors, research and release preparation.
- Safe defaults and questions only when critical context is missing.
- Human-readable progress, errors and results rather than internal JSON noise.
- Real end-to-end compatibility tests for Codex, Claude Code, Pi and Orca.

Exit gates: a new user starts a first task within five minutes; common single-repository tasks reach verified results; interrupted tasks resume with one command.

### 0.2: daily work surface

Make Agent OS the place users trust with work that lasts minutes or hours.

Key outcomes:

- A resident local coordinator with background execution, queues, priorities and schedules.
- One task center for progress, dependencies, approvals, summarized logs, cost, evidence and lineage.
- Pause, resume, cancel, retry and checkpoint recovery.
- Notifications and an approval inbox that remove terminal babysitting.
- Discoverable project configuration with inheritable personal, team and project policy.
- A stable Agent Adapter kit and protocol conformance tests.

Exit gates: long work survives terminal exit; common failures are self-recoverable; users no longer open each agent tool to discover task truth.

### 0.3: personal RSI and cost intelligence

Make repeated work require less attention, time and money without reducing quality.

Key outcomes:

- Layered memory for user preferences, project conventions and successful workflows, with inspect, correct, forget and export controls.
- Dynamic selection of agent, model, context, concurrency and verification strength.
- Semantic result reuse, incremental context and duplicate-work coalescing.
- Joint optimization of quality, latency, cost and human intervention.
- Explanations for what was learned, why behavior changed and which tasks are affected.
- Frozen regression suites, offline evaluation, human approval, canaries and automatic rollback.

Exit gates: quality holds on a fixed regression set; repeated-task cost and duration measurably improve; any learned behavior can be understood and reverted.

### 0.5: team Agent OS

Let teams share reliable ways of working while keeping permissions, data and accountability clear.

Key outcomes:

- Multi-project, cross-host scheduling and durable distributed state.
- Workspace, team and tenant isolation with fine-grained roles and budgets.
- Team policy packs, verification templates, shared artifacts and verified experience.
- Standard connectors for GitHub/GitLab, CI, issues, messaging and knowledge systems.
- Audit, retention, redaction, credential lifecycle and compliance export.
- Team-level capacity, quality, cost and failure-pattern views.

Exit gates: multiple teams are safely isolated in one deployment; work recovers across hosts; every external effect traces to intent, approval and evidence.

### 0.8: open and composable ecosystem

Allow new agents, tools and workflows to integrate safely without core-runtime changes.

Key outcomes:

- Stable extension interfaces for adapters, tools, artifacts, policies and workflows.
- Extension manifests with permissions, compatibility, signatures and provenance.
- Discoverable, testable and versioned workflow and policy packages.
- Agent capability negotiation and protocol conformance suites.
- Sandboxing, least privilege and supply-chain verification.

Exit gates: third parties build against public interfaces only; failed upgrades cannot corrupt core state; users understand permissions, cost and data impact before installation.

### 1.0: stable agent operating layer

Deliver durable, upgradeable and portable Agent OS contracts.

Key outcomes:

- Stable task, artifact, adapter, event, policy and state formats.
- A compatibility policy, automatic migration, backup/restore and downgrade paths.
- Formal macOS and Linux support, with Windows driven by validated demand.
- A published security model, threat analysis, signed releases, SBOMs and reproducible builds.
- Reliability and performance objectives, capacity models and longevity testing.
- A consistent experience from personal local use to team deployment.

Exit gates: upgrades and migrations pass against real historical state; core journeys meet published reliability targets; ordinary users complete work without understanding internal graphs.

### Beyond 1.0

Only after the 1.0 contracts stabilize should the project explore cross-device coordination, privacy-preserving collective learning, organization-wide resource markets and broader knowledge work. Every expansion remains subordinate to simplicity, trust, efficiency and user control.

## Continuous workstreams

| Workstream | Long-term outcome |
| --- | --- |
| Interaction | Intent-driven, progressively disclosed and consistent operation. |
| Orchestration | Explainable intent compilation and adaptive plans replace hand-authored graphs. |
| Trust | Verification grows into evidence chains, supply-chain trust and durable audit. |
| Intelligence | Fixed routing evolves into governed, correctable and reversible RSI. |
| Efficiency | Time, calls, reuse and human intervention are optimized together. |
| Ecosystem | Stable seams connect agents, tools, workflows and external systems. |
| Portability | State, policy, memory and artifacts remain exportable and upgradeable. |

## User-centered success measures

Progress is measured by outcomes, not module count:

| Dimension | Primary measures |
| --- | --- |
| Convenience | Time to first success, user inputs per task, installation success rate. |
| Simplicity | Human decisions per task, concepts users must learn, self-service error resolution. |
| Efficiency | End-to-end duration, queue time, useful parallelism, duplicate work eliminated. |
| Trust | First-pass verification, false completion, recovery success, untraceable effects. |
| Economics | Cost per verified result, not price per model call. |
| Intelligence | Repeated instructions eliminated, accepted policy gains, regression and rollback rates. |
| Control | Approval wait, correction effectiveness, memory deletion and migration success. |

Every RSI optimization has hard constraints: no safety regression, quality at or above baseline, visible cost and latency changes, and user opt-out and rollback. Telemetry remains local by default; external analysis requires explicit opt-in and inspectable payloads.

## Near-term priority order

1. Build a real-project evaluation set and baseline the user journey, quality, intervention, latency and cost.
2. Converge on the `do / status / approve / control / result` task interface while keeping GraphSpec as an advanced interface.
3. Add intent compilation and high-frequency task templates that produce reviewable execution plans.
4. Turn the coordinator into a reliable resident local process with background work, notification and recovery.
5. Establish a monitored compatibility matrix and fault-injection suite for real agents and Orca.
6. Use those baselines to introduce personal RSI, context reuse and cost optimization without opaque self-learning.

## Explicit non-goals

- Replacing Codex, Claude Code, Pi or other specialist agents.
- Requiring ordinary users to author task graphs.
- Treating more agents, more concurrency or larger models as intelligence by itself.
- Allowing RSI to bypass permissions, budgets, verification or human approval.
- Requiring a cloud service for core capabilities.
- Locking users into non-exportable memory, private formats or hidden policy.

## Roadmap governance

Before implementation, every milestone item should state the user problem, target measure, non-goals, risks, migration impact and exit gate. Before release, it needs evidence from real tasks, not unit tests alone. Feedback may change the sequence, but the north star, user experience contract, safety floor and portability principle require an explicit design decision to change.
