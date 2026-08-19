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

## Current delivery snapshot

As of 2026-08-19, the repository is published as the `0.0.1` preview, while implementation has already reached into several 0.1 and 0.2 capabilities. The milestone numbers below describe maturity and user evidence, not whether the first implementation exists.

| Work package | Status | Evidence and remaining gate |
| --- | --- | --- |
| Trusted foundation | Preview shipped | Graph execution, artifacts, approvals, budgets, recovery, controlled merge, verified reuse, governed learning and portable state are implemented. |
| P1: real-project evaluation | P1.1–P1.3 complete; P1.4 pending | Privacy-minimized records and immutable baselines exist. Three authorized real-project pilots still need to establish quality, intervention, latency and cost baselines. |
| P2: unified task interface | Complete in preview | `do / status / approve / control / result` share one task state and preserve GraphSpec as the advanced interface. |
| P3: intent compilation | Complete in preview | Fix, test, refactor, research and release-preparation goals compile into reviewable plans with safe defaults. |
| P4: resident coordination | Complete in preview | Background queues, priority, pause/resume/cancel, crash recovery, task summaries, notifications and the Orca lifecycle share one control plane. |
| P5: compatibility and faults | P5.1–P5.2 complete; P5.3 pending | Versioned, platform-scoped protocol evidence, offline fault injection and a zero-model first-use setup with actionable remediation exist. Current certification is Darwin arm64; clean-machine and real model-output/Orca lifecycle pilots remain pending. |
| P6: personal RSI and cost | Pending | Starts after the real-project baseline. User-controlled memory and policy gains must remain inspectable, reversible and bounded by frozen quality gates. |

The combined P1.4/P5.3 pilot is the only near-term package that intentionally spends model tokens or modifies external projects. It must remain opt-in with named projects, budgets, stop conditions and recorded intervention.

## Milestones

### 0.0.x: trusted foundation — current release line

Prove that heterogeneous agents can share governed execution semantics. The current base includes the graph runtime, artifact contracts, Codex/Claude Code/Pi/OpenCode adapters, Orca coordination, approvals, budgets, recovery, controlled merge, verified reuse, Reality Anchors, governed RSI and portable state. It also contains preview implementations from later milestones; those capabilities are not considered mature until their user-outcome gates pass.

Exit gates:

- **Met:** core contracts have stable automated and fault-injection coverage.
- **Pending authorization:** at least three monitored real-project pilots establish success, failure, intervention and cost baselines.
- **In progress:** preview interfaces are clearly separated from interfaces intended for compatibility.

### 0.1: first-use success

Let a new user complete a real task in minutes without writing a graph or complex policy.

Key outcomes:

- One-command installation, environment diagnosis and agent discovery.
- Discovery-only inventory for additional local Agents, kept separate from certified execution.
- A natural-language `agent-os do` entry point and an intent-to-graph compiler.
- Templates for fixes, tests, refactors, research and release preparation.
- Safe defaults and questions only when critical context is missing.
- Human-readable progress, errors and results rather than internal JSON noise.
- Real end-to-end compatibility tests for Codex, Claude Code, Pi, OpenCode and Orca.

Preview status: the unified task actions, intent compiler, five initial templates, resident execution and a human-readable `agent-os setup` journey are implemented. Setup safely initializes local state, classifies readiness and emits structured remediation without model calls. OpenCode is now a certified Adapter on Darwin arm64; OpenClaw, Hermes Agent, Aider, Gemini CLI and GitHub Copilot CLI remain discovery-only. The remaining gates are clean-machine rehearsal, certification of the next prioritized Adapter and authorized real-project pilots.

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

Preview status: the resident queue, unified lifecycle, task center, notifications and recovery paths are implemented. The public `CliAgentAdapter` kit now powers four certified executors without adding a second runtime seam. The remaining work is long-running soak evidence, discoverable configuration and external conformance evidence before freezing the kit for compatibility.

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

Preview status: prompt-free observations, governed candidates, regression evaluation, approval, canary and rollback primitives exist. Personal and project memory controls, real-task baselines and measurable cost improvements are still pending.

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

1. **Run the combined P1.4/P5.3 pilot after explicit authorization.** Use three low-risk projects and fixed budgets to measure verified success, false completion, intervention, latency, token use and cost across Codex, Claude Code, Pi and Orca.
2. **Complete clean-install evidence.** The zero-model `setup` flow now initializes state, reports supported/unverified/broken states and provides actionable remediation. Next, rehearse it on clean environments and add evidence for each supported version/platform combination.
3. **Define the 0.1 compatibility line.** Mark which task, artifact, event, policy and state interfaces are public; add migration fixtures and upgrade rehearsals before promising stability.
4. **Start P6 personal RSI from the frozen baseline.** Add inspect/correct/forget/export controls for memory, then optimize agent, model, context and verification choices behind quality and budget gates.
5. **Harden the Agent Adapter kit and select the next Adapter.** Exercise the public kit outside built-in executors, freeze its minimal compatibility surface, then assess OpenClaw and Hermes by user value, non-interactive protocol stability, permission control and maintenance cost before implementing one.

## Explicit non-goals

- Replacing Codex, Claude Code, Pi or other specialist agents.
- Requiring ordinary users to author task graphs.
- Treating more agents, more concurrency or larger models as intelligence by itself.
- Allowing RSI to bypass permissions, budgets, verification or human approval.
- Requiring a cloud service for core capabilities.
- Locking users into non-exportable memory, private formats or hidden policy.

## Roadmap governance

Before implementation, every milestone item should state the user problem, target measure, non-goals, risks, migration impact and exit gate. Before release, it needs evidence from real tasks, not unit tests alone. Roadmap updates must distinguish implemented code, released contracts, pending evidence and work that requires user authorization. Feedback may change the sequence, but the north star, user experience contract, safety floor and portability principle require an explicit design decision to change.
