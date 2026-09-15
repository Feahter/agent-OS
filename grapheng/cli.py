import argparse
import json
import shlex
import sys
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from . import telemetry
from .adapters import discover_local_executors
from .agent_nodes import AgentNodeHandler
from .console import ApprovalInbox, OperationsConsole
from .console_server import OperationsServer
from .control import LocalControlPlane
from .coordinator import OrcaCoordinator
from .distribution import AgentOSDistribution
from .economics import (
    BenchmarkProtocol,
    BenchmarkRun,
    BenchmarkSuite,
    PriceCatalog,
    RunEconomics,
)
from .engineering import EngineeringWorkflow, ProjectPolicy, default_project_policy
from .errors import ContractViolation, GraphEngineeringError
from .evaluation import EvaluationCase, EvaluationLab
from .learning import RSILoop
from .model import GraphSpec
from .optimization import (
    CanaryObservation,
    FailurePattern,
    RegressionCase,
    RegressionMeasurement,
    RSIOptimizationLab,
)
from .orca import OrcaBackend, OrcaClient, OrcaGraphCompiler
from .os import AgentOS
from .policy import AllowListGatePolicy
from .publication import VerifiedResultPublisher
from .resident import ResidentCoordinator
from .runtime import GraphRuntime, NodeOutcome, NodeRegistry
from .tasks import UserTaskModule, default_agent_os_home
from .token_reservations import HistoricalTokenReservations
from .validation import validate_graph


def _demo_registry() -> NodeRegistry:
    registry = NodeRegistry()
    registry.register("collect", lambda context: NodeOutcome({"facts": ["grounded"]}, tokens_used=10))
    registry.register(
        "draft",
        lambda context: NodeOutcome(
            {"draft": "report:" + ",".join(context.read("facts"))}, tokens_used=20
        ),
    )
    registry.register(
        "verify",
        lambda context: NodeOutcome(
            {"verified_report": {"text": context.read("draft"), "verified": True}},
            tokens_used=5,
        ),
    )
    return registry


def _json_file(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ContractViolation(f"JSON input does not exist: {path}") from error
    except OSError as error:
        raise ContractViolation(f"cannot read JSON input {path}: {error}") from error
    except json.JSONDecodeError as error:
        raise ContractViolation(f"invalid JSON input {path}: {error}") from error


def _run_optimization_command(args, parser) -> int:
    lab = RSIOptimizationLab(args.optimization_root)
    value: Mapping[str, Any]
    if args.action == "status":
        value = {
            "active": {
                kind: candidate.to_dict()
                for kind, candidate in lab.active_candidates().items()
            },
            "candidates": [item.to_dict() for item in lab.candidates()],
        }
    elif args.action == "freeze-suite":
        if args.cases is None:
            parser.error("rsi-opt freeze-suite requires --cases")
        raw = _json_file(args.cases)
        if not isinstance(raw, list):
            parser.error("rsi-opt --cases must contain a JSON array")
        value = lab.freeze_suite(
            tuple(RegressionCase.from_dict(item) for item in raw)
        ).to_dict()
    elif args.action == "suggest":
        if args.failures is None:
            parser.error("rsi-opt suggest requires --failures")
        raw = _json_file(args.failures)
        if not isinstance(raw, list):
            parser.error("rsi-opt --failures must contain a JSON array")
        value = {
            "candidates": [
                item.to_dict()
                for item in lab.propose_from_failures(
                    tuple(FailurePattern.from_dict(item) for item in raw),
                    args.min_occurrences,
                    args.rollout_percent,
                )
            ]
        }
    elif args.action == "propose":
        if args.kind is None or args.change is None or not args.rationale:
            parser.error(
                "rsi-opt propose requires --kind, --change, and --rationale"
            )
        raw = _json_file(args.change)
        if not isinstance(raw, dict):
            parser.error("rsi-opt --change must contain a JSON object")
        value = lab.propose(
            args.kind, raw, args.rationale, args.rollout_percent
        ).to_dict()
    elif args.action == "evaluate":
        if (
            not args.candidate_id
            or not args.suite_id
            or args.measurements is None
        ):
            parser.error(
                "rsi-opt evaluate requires --candidate-id, --suite-id, and --measurements"
            )
        raw = _json_file(args.measurements)
        if not isinstance(raw, list):
            parser.error("rsi-opt --measurements must contain a JSON array")
        value = lab.evaluate(
            args.candidate_id,
            args.suite_id,
            tuple(RegressionMeasurement.from_dict(item) for item in raw),
            args.max_quality_regression,
            args.max_cost_increase_percent,
            args.max_latency_increase_percent,
        ).to_dict()
    elif args.action == "approve":
        if not args.candidate_id or not args.actor:
            parser.error("rsi-opt approve requires --candidate-id and --actor")
        value = lab.approve(args.candidate_id, args.actor).to_dict()
    elif args.action == "activate":
        if not args.candidate_id:
            parser.error("rsi-opt activate requires --candidate-id")
        value = lab.activate(args.candidate_id).to_dict()
    elif args.action == "rollback":
        if args.kind is None:
            parser.error("rsi-opt rollback requires --kind")
        candidate = lab.rollback(args.kind)
        value = {
            "active_candidate": (
                candidate.to_dict() if candidate is not None else None
            )
        }
    else:
        if args.canary is None:
            parser.error("rsi-opt canary requires --canary")
        raw = _json_file(args.canary)
        if not isinstance(raw, dict):
            parser.error("rsi-opt --canary must contain a JSON object")
        observation = CanaryObservation(**raw)
        value = {
            "candidate_id": observation.candidate_id,
            "healthy": lab.observe_canary(observation),
        }
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))
    return 0


def _write_engineering_policy(path: Path, policy: ProjectPolicy) -> None:
    if path.exists() or path.is_symlink():
        raise FileExistsError(f"engineering policy already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(policy.to_dict(), ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def _engineering_workflow(args, agent_os) -> EngineeringWorkflow:
    policy = ProjectPolicy.load(args.workspace, args.policy)
    router = agent_os.router() if agent_os is not None else None
    reuse_store = agent_os.reuse_store() if agent_os is not None else None
    executors = discover_local_executors(router, reuse_store)
    if not executors.capabilities():
        raise RuntimeError("no supported local agent executor was discovered")
    return EngineeringWorkflow(
        args.workspace,
        args.task_dir,
        executors,
        policy,
        rsi_loop=RSILoop(agent_os.learning_root) if agent_os is not None else None,
        agent_os_root=agent_os.root if agent_os is not None else None,
    )


def _run_engineering_command(args, parser, agent_os) -> int:
    value: Mapping[str, Any]
    if args.action == "init":
        target = args.policy or args.workspace / ".agent-os" / "engineering.json"
        try:
            _write_engineering_policy(target, default_project_policy())
        except FileExistsError as error:
            parser.error(str(error))
        value = {"policy": str(target.resolve()), "created": True}
    elif args.action == "status":
        report = args.task_dir / "report.json"
        status = args.task_dir / "status.json"
        target = report if report.exists() else status
        value = (
            _json_file(target)
            if target.exists()
            else {"phase": "uninitialized", "task_dir": str(args.task_dir.resolve())}
        )
    else:
        if args.action == "ship" and not sys.stdin.isatty():
            parser.error(
                "engineer ship requires an interactive terminal; "
                "use engineer plan then engineer run --plan-digest for automation"
            )
        workflow = _engineering_workflow(args, agent_os)
        if args.action == "plan":
            value = workflow.prepare(args.objective).to_dict()
        elif args.action == "run":
            value = workflow.execute(args.approved_by, args.plan_digest)
        else:
            plan = workflow.prepare(args.objective)
            print(json.dumps(plan.to_dict(), ensure_ascii=False, sort_keys=True))
            answer = input(f"Approve plan {plan.digest}? [y/N] ").strip().lower()
            if answer not in ("y", "yes"):
                return 2
            approved_by = args.approved_by
            if not approved_by:
                approved_by = input("Approver name: ").strip()
                if not approved_by:
                    parser.error("engineer ship requires a non-empty approver name")
            value = workflow.execute(approved_by, plan.digest)
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))
    return 0 if value.get("success", True) else 1


def _run_evaluation_command(args, parser) -> int:
    lab = EvaluationLab(args.root)
    value: Mapping[str, Any]
    if args.action == "record-engineering":
        if args.case is None or args.report is None or not args.run_id:
            parser.error(
                "evaluate record-engineering requires --case, --report, and --run-id"
            )
        if args.user_inputs is None or args.human_decisions is None:
            parser.error(
                "evaluate record-engineering requires --user-inputs and --human-decisions"
            )
        value = lab.record_engineering(
            EvaluationCase.load(args.case),
            args.run_id,
            args.report,
            args.user_inputs,
            args.human_decisions,
            args.recovery_attempted,
            args.recovery_succeeded,
        ).to_dict()
    elif args.action == "baseline":
        if not args.name:
            parser.error("evaluate baseline requires --name")
        value = lab.create_baseline(args.name)
    else:
        value = lab.status()
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))
    return 0


def _run_benchmark_command(args, parser, agent_os=None) -> int:
    if args.action == "freeze-suite":
        required = {
            "--output": args.output,
            "--suite-id": args.suite_id,
            "--suite-version": args.suite_version,
            "--micro-protocol": args.micro_protocol,
            "--engineering-protocol": args.engineering_protocol,
            "--recovery-protocol": args.recovery_protocol,
        }
        missing = [flag for flag, value in required.items() if value is None]
        if missing:
            parser.error("benchmark freeze-suite requires " + ", ".join(missing))
        suite = BenchmarkSuite.freeze(
            args.output,
            suite_id=args.suite_id,
            suite_version=args.suite_version,
            micro=BenchmarkProtocol.load(args.micro_protocol),
            engineering=BenchmarkProtocol.load(args.engineering_protocol),
            recovery=BenchmarkProtocol.load(args.recovery_protocol),
        )
        print(json.dumps(suite.to_dict(), ensure_ascii=False, sort_keys=True))
        return 0

    if args.action == "freeze":
        required = {
            "--spec": args.benchmark_spec,
            "--inputs": args.benchmark_inputs,
            "--output": args.output,
            "--benchmark-id": args.benchmark_id,
            "--scenario-version": args.scenario_version,
            "--provider": args.provider,
            "--model": args.model,
            "--reasoning": args.reasoning,
            "--executor": args.executor,
            "--executor-version": args.executor_version,
        }
        missing = [flag for flag, value in required.items() if value is None]
        if missing:
            parser.error("benchmark freeze requires " + ", ".join(missing))
        graph = GraphSpec.from_json(args.benchmark_spec)
        validate_graph(graph)
        protocol = BenchmarkProtocol.freeze(
            args.output,
            benchmark_id=args.benchmark_id,
            scenario_version=args.scenario_version,
            provider=args.provider,
            model=args.model,
            reasoning=args.reasoning,
            executor=args.executor,
            executor_version=args.executor_version,
            graph=graph,
            inputs=_json_file(args.benchmark_inputs),
            concurrency_matrix=tuple(args.concurrency or (1, 2)),
            repetitions_per_cell=args.repetitions,
        )
        print(json.dumps(protocol.to_dict(), ensure_ascii=False, sort_keys=True))
        return 0

    if args.action == "execute":
        required = {
            "--root": args.root,
            "--protocol": args.protocol,
            "--spec": args.benchmark_spec,
            "--inputs": args.benchmark_inputs,
            "--workspace": args.workspace,
        }
        missing = [flag for flag, value in required.items() if value is None]
        if missing:
            parser.error("benchmark execute requires " + ", ".join(missing))
        if args.timeout_seconds <= 0:
            parser.error("benchmark execute --timeout-seconds must be positive")
        protocol = BenchmarkProtocol.load(args.protocol)
        graph = GraphSpec.from_json(args.benchmark_spec)
        validate_graph(graph)
        inputs = _json_file(args.benchmark_inputs)
        if BenchmarkProtocol.graph_fingerprint(graph) != protocol.dag_fingerprint:
            raise ContractViolation(
                "benchmark graph fingerprint does not match the protocol"
            )
        if BenchmarkProtocol.inputs_fingerprint(inputs) != protocol.input_fingerprint:
            raise ContractViolation(
                "benchmark input fingerprint does not match the protocol"
            )
        if not args.workspace.is_dir():
            raise ContractViolation("benchmark workspace must be a directory")
        for node in graph.nodes:
            if node.agent is None or node.kind != "agent":
                raise ContractViolation(
                    "benchmark execute currently requires agent-only graphs"
                )
            if node.agent.executor != protocol.executor:
                raise ContractViolation(
                    f"benchmark node {node.id} executor does not match the protocol"
                )
            if node.agent.model != protocol.model:
                raise ContractViolation(
                    f"benchmark node {node.id} model does not match the protocol"
                )
            if node.agent.workspace.mode != "shared":
                raise ContractViolation(
                    "benchmark execute requires shared agent workspaces"
                )

        reuse_store = agent_os.reuse_store() if agent_os is not None else None
        router = agent_os.router() if agent_os is not None else None
        executors = discover_local_executors(router, reuse_store)
        available = {
            capability.executor_id for capability in executors.capabilities()
        }
        if protocol.executor not in available:
            raise ContractViolation(
                f"benchmark executor is unavailable: {protocol.executor}"
            )
        token_reservations = (
            agent_os.token_reservations() if agent_os is not None else None
        )
        economics = RunEconomics(args.root)
        prices = PriceCatalog.load(args.prices) if args.prices is not None else None
        artifacts_root = args.root / "artifacts"
        if artifacts_root.is_symlink():
            raise ContractViolation("benchmark artifacts directory cannot be a symlink")
        plane = LocalControlPlane(
            artifacts_root,
            max_workers=1,
            reuse_store=reuse_store,
            token_reservations=token_reservations,
        )
        snapshots = []
        try:
            for concurrency in protocol.concurrency_matrix:
                run_graph = replace(graph, max_concurrency=concurrency)
                for _ in range(protocol.repetitions_per_cell):
                    registry = NodeRegistry()
                    registry.register(
                        "agent",
                        AgentNodeHandler(
                            run_graph,
                            executors,
                            args.workspace,
                            reasoning_effort=protocol.reasoning,
                        ),
                    )
                    run_id = plane.submit(
                        run_graph,
                        registry,
                        AllowListGatePolicy(set(args.allow_gate)),
                    )
                    completed = plane.wait(run_id, timeout=args.timeout_seconds)
                    snapshots.append(
                        economics.record_control_run(
                            protocol,
                            artifacts_root / "runs" / run_id,
                            verified=args.verified and completed.phase == "succeeded",
                            prices=prices,
                        )
                    )
        finally:
            plane.close()
        runs_by_concurrency = {
            str(concurrency): sum(
                snapshot["concurrency"] == concurrency for snapshot in snapshots
            )
            for concurrency in protocol.concurrency_matrix
        }
        result = {
            "benchmark_protocol_fingerprint": protocol.fingerprint,
            "runs": len(snapshots),
            "runs_by_concurrency": runs_by_concurrency,
            "successful_runs": sum(
                snapshot["outcome"]["success"] for snapshot in snapshots
            ),
            "verified_results": sum(
                snapshot["outcome"]["verified"] for snapshot in snapshots
            ),
            "run_ids": [snapshot["run_id"] for snapshot in snapshots],
        }
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0 if result["successful_runs"] == result["runs"] else 1

    if args.root is None:
        parser.error(f"benchmark {args.action} requires --root")
    economics = RunEconomics(args.root)
    if args.action == "record":
        if args.protocol is None:
            parser.error("benchmark record requires --protocol")
        if (args.run is None) == (args.run_artifacts is None):
            parser.error(
                "benchmark record requires exactly one of --run or --run-artifacts"
            )
        protocol = BenchmarkProtocol.load(args.protocol)
        prices = PriceCatalog.load(args.prices) if args.prices is not None else None
        if args.run_artifacts is not None:
            value = economics.record_control_run(
                protocol,
                args.run_artifacts,
                verified=args.verified,
                prices=prices,
            )
        else:
            if args.verified:
                parser.error("benchmark --verified is only valid with --run-artifacts")
            value = economics.record(protocol, BenchmarkRun.load(args.run), prices)
        print(json.dumps(value, ensure_ascii=False, sort_keys=True))
        return 0

    if args.action == "reprice":
        if args.run_id is None or args.prices is None:
            parser.error("benchmark reprice requires --run-id and --prices")
        value = economics.reprice(args.run_id, PriceCatalog.load(args.prices))
        print(json.dumps(value, ensure_ascii=False, sort_keys=True))
        return 0

    if args.baseline_protocol is None or args.candidate_protocol is None:
        parser.error(
            "benchmark report requires --baseline-protocol and --candidate-protocol"
        )
    value = economics.report(
        BenchmarkProtocol.load(args.baseline_protocol),
        BenchmarkProtocol.load(args.candidate_protocol),
    )
    if args.json_output:
        print(json.dumps(value, ensure_ascii=False, sort_keys=True))
    else:
        _print_benchmark_report(value)
    return 0


def _print_benchmark_report(value: Mapping[str, Any]) -> None:
    baseline = value["baseline"]["overall"]
    candidate = value["candidate"]["overall"]
    roi = value["roi"]

    def metric(item: Optional[float], suffix: str = "") -> str:
        return "unknown" if item is None else f"{item:.4f}{suffix}"

    def percentage(item: Optional[float]) -> str:
        return "unknown" if item is None else f"{item:.2%}"

    print(f"Benchmark {value['benchmark_id']} ({value['scenario_version']})")
    print(
        "verified success: "
        f"{percentage(baseline['verified_success_rate'])} -> "
        f"{percentage(candidate['verified_success_rate'])}"
    )
    print(
        "tokens / verified result: "
        f"{metric(baseline['tokens']['per_verified_result'])} -> "
        f"{metric(candidate['tokens']['per_verified_result'])}"
    )
    print(
        "median wall clock: "
        f"{metric(baseline['wall_clock_seconds']['median'], 's')} -> "
        f"{metric(candidate['wall_clock_seconds']['median'], 's')}"
    )
    print(
        "token reduction: "
        + (
            "unknown"
            if roi["token_reduction_fraction"] is None
            else f"{roi['token_reduction_fraction']:.2%}"
        )
    )
    print(
        "cost ROI: "
        + (
            roi["cost_roi_unavailable_reason"]
            if roi["cost_reduction_fraction"] is None
            else f"{roi['cost_reduction_fraction']:.2%}"
        )
    )
    warning_names = sorted(
        set(baseline["warnings"]["counts"])
        | set(candidate["warnings"]["counts"])
    )
    for warning in warning_names:
        print(
            f"warning: {warning} "
            f"(baseline={baseline['warnings']['counts'].get(warning, 0)}, "
            f"candidate={candidate['warnings']['counts'].get(warning, 0)})"
        )
    print(
        f"failed samples: baseline={len(baseline['failed_run_ids'])}, "
        f"candidate={len(candidate['failed_run_ids'])}"
    )


def _cost_text(usage: Mapping[str, Any]) -> str:
    cost = float(usage.get("cost_usd", 0.0))
    if usage.get("cost_complete") is True:
        return f"${cost:.4f}"
    if cost > 0:
        return f"${cost:.4f} (partial)"
    return "cost unknown"


def _print_user_task(value, json_output: bool) -> None:
    if json_output:
        print(json.dumps(value, ensure_ascii=False, sort_keys=True))
        return
    task_id = value["task_id"]
    phase = value["phase"]
    summary = value.get("summary") or {
        "succeeded": "Verified result is ready",
        "failed": "Task stopped without a verified result",
        "cancelled": "Task was cancelled before execution",
    }.get(phase, f"Task is {phase}")
    print(f"Task {task_id}: {summary}")
    intent = value.get("intent")
    if isinstance(intent, dict):
        project = ", ".join(intent.get("project_kinds", ())) or "generic"
        mode = "change" if intent.get("mutation_allowed", True) else "read-only"
        print(f"Intent: {intent.get('template', 'general')} · {project} · {mode}")
        if intent.get("objective"):
            print(f"Goal: {intent['objective']}")
        for constraint in intent.get("constraints", ()):
            print(f"Constraint: {constraint}")
        commands = intent.get("verification_commands", ())
        for command in commands:
            if isinstance(command, list):
                print(f"Verify: {' '.join(command)}")
    proposed_plan = value.get("proposed_plan")
    if isinstance(proposed_plan, dict):
        for step in proposed_plan.get("steps", ()):
            print(f"Plan: {step}")
    verification = value.get("verification")
    if isinstance(verification, dict) and phase in ("succeeded", "failed"):
        print(f"Verification: {'passed' if verification.get('passed') else 'not passed'}")
    usage = value.get("usage")
    if isinstance(usage, dict):
        print(
            "Usage: "
            f"{usage.get('tokens_used', 0)} tokens, "
            f"{_cost_text(usage)}"
        )
    scheduling = value.get("scheduling")
    if isinstance(scheduling, dict):
        print(
            "Queue: "
            f"{scheduling.get('state', 'unknown')} · "
            f"priority {scheduling.get('priority', 0)} · "
            f"attempt {scheduling.get('attempts', 0)}"
        )
    outcome = value.get("outcome")
    if isinstance(outcome, dict) and outcome.get("summary"):
        print(f"Outcome: {outcome['summary']}")
    artifacts = value.get("artifacts")
    if isinstance(artifacts, list):
        for artifact in artifacts:
            if isinstance(artifact, dict) and artifact.get("path"):
                print(f"Artifact: {artifact.get('name', 'result')} -> {artifact['path']}")
    next_action = value.get("next_action")
    if next_action:
        if next_action == "approve":
            print(f"Next: agent-os approve {task_id} --actor YOUR_NAME")
        elif next_action == "control":
            print(f"Next: agent-os control {task_id} resume --actor YOUR_NAME")
        else:
            print(f"Next: agent-os {next_action} {task_id}")


def _run_user_task_command(args) -> int:
    tasks = UserTaskModule(args.home)
    if args.command == "do":
        value = tasks.do(
            args.objective,
            args.workspace,
            args.policy,
            args.template,
            args.constraint,
        )
    elif args.command == "status":
        value = tasks.status(args.task_id)
    elif args.command == "approve":
        value = tasks.approve(
            args.task_id,
            args.actor,
            background=args.background,
            priority=args.priority,
        )
    elif args.command == "control":
        value = tasks.control(args.task_id, args.action, args.actor, args.priority)
    else:
        value = tasks.result(args.task_id)
    _print_user_task(value, args.json_output)
    return 1 if value.get("phase") == "failed" else 0


def _print_task_center(value, json_output: bool) -> None:
    if json_output:
        print(json.dumps(value, ensure_ascii=False, sort_keys=True))
        return
    counts = value["counts"]
    resident = "running" if value["resident_running"] else "stopped"
    notifications = "on" if value["desktop_notifications"] else "off"
    print(
        "Agent OS Task Center: "
        f"{counts['active']} active · "
        f"{counts['needs_attention']} need attention · "
        f"resident {resident} · notifications {notifications}"
    )
    usage = value["usage"]
    completeness = "complete" if usage["complete"] else "partial"
    print(
        f"Usage: {usage['tokens_used']} tokens · "
        f"{_cost_text(usage)} · {completeness}"
    )
    jobs = value["jobs"]
    if not jobs:
        print("No tasks yet.")
        return
    for job in jobs:
        marker = (
            "!"
            if job["attention_required"]
            else "✓"
            if job["state"] == "succeeded"
            else "·"
        )
        priority = (
            f" · priority {job['priority']}"
            if job["priority"] is not None
            else ""
        )
        print(
            f"{marker} {job['kind']} {job['reference']} · "
            f"{job['state']}{priority}"
        )
        print(f"  {job['summary']}")
        if job.get("next_action"):
            print(f"  Next: {job['next_action']}")


def _print_setup(value, json_output: bool) -> None:
    if json_output:
        print(json.dumps(value, ensure_ascii=False, sort_keys=True))
        return
    labels = {
        "ready": "ready",
        "ready_with_warnings": "ready with warnings",
        "needs_agent": "needs a coding Agent",
        "blocked": "blocked",
    }
    readiness = value["readiness"]
    print(f"Agent OS setup: {labels.get(readiness, readiness)}")
    state = "initialized" if value["initialized"] else "already initialized"
    print(f"State: {state} · {value['root']}")
    executors = ", ".join(value["ready_executors"]) or "none"
    print(f"Ready coding Agents: {executors}")
    discovered = ", ".join(
        item["display_name"] for item in value.get("discovered_agents", [])
    )
    if discovered:
        print(f"Discovered but not integrated: {discovered}")
    print(f"Orca: {'ready' if value['ready_for_orca'] else 'not ready'}")
    print("Safety: environment inspection only · 0 model calls")
    notable = [item for item in value["checks"] if item["status"] != "pass"]
    if notable:
        print("Checks:")
        for check in notable:
            print(f"- {check['status'].upper()} {check['check_id']}: {check['summary']}")
    actions = value["next_actions"]
    if not actions:
        print("Next: agent-os do \"YOUR_GOAL\" --workspace /path/to/project")
        return
    print("Next steps:")
    for action in actions:
        print(f"- [{action['priority']}] {action['summary']}")
        command = action.get("command")
        if command:
            print(f"  Run: {shlex.join(command)}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Run one CLI invocation, reporting contract failures as errors not crashes.

    Every guard in Agent OS raises :class:`ContractViolation` with an
    actionable message. Letting those reach the terminal as a traceback made a
    normal, expected outcome - "this task needs clarification" - look like a
    crash, so they are reported here instead.
    """

    try:
        return _dispatch(argv)
    except ContractViolation as error:
        print(f"agent-os: {error}", file=sys.stderr)
        return 2
    except GraphEngineeringError as error:
        print(f"agent-os: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("agent-os: interrupted", file=sys.stderr)
        return 130


def _dispatch(argv: Optional[Sequence[str]] = None) -> int:
    parser = _command_parser()
    return _dispatch_parsed(parser.parse_args(argv), parser)


def _command_parser() -> argparse.ArgumentParser:
    """Build the CLI grammar independently from command execution."""

    parser = argparse.ArgumentParser(prog="agent-os")
    subparsers = parser.add_subparsers(dest="command", required=True)
    setup_parser = subparsers.add_parser("setup")
    setup_parser.add_argument("--home", type=Path)
    setup_parser.add_argument("--source-root", type=Path)
    setup_parser.add_argument("--json", action="store_true", dest="json_output")
    do_parser = subparsers.add_parser("do")
    do_parser.add_argument("objective")
    do_parser.add_argument("--workspace", type=Path, required=True)
    do_parser.add_argument("--policy", type=Path)
    do_parser.add_argument(
        "--template", choices=("fix", "test", "refactor", "research", "release")
    )
    do_parser.add_argument("--constraint", action="append", default=[])
    do_parser.add_argument("--home", type=Path)
    do_parser.add_argument("--json", action="store_true", dest="json_output")
    status_parser = subparsers.add_parser("status")
    status_parser.add_argument("task_id")
    status_parser.add_argument("--home", type=Path)
    status_parser.add_argument("--json", action="store_true", dest="json_output")
    approve_parser = subparsers.add_parser("approve")
    approve_parser.add_argument("task_id")
    approve_parser.add_argument("--actor", required=True)
    approve_parser.add_argument("--background", action="store_true")
    approve_parser.add_argument("--priority", type=int, default=0)
    approve_parser.add_argument("--home", type=Path)
    approve_parser.add_argument("--json", action="store_true", dest="json_output")
    control_parser = subparsers.add_parser("control")
    control_parser.add_argument("task_id")
    control_parser.add_argument(
        "action", choices=("pause", "resume", "cancel", "reprioritize")
    )
    control_parser.add_argument("--actor", required=True)
    control_parser.add_argument("--priority", type=int)
    control_parser.add_argument("--home", type=Path)
    control_parser.add_argument("--json", action="store_true", dest="json_output")
    result_parser = subparsers.add_parser("result")
    result_parser.add_argument("task_id")
    result_parser.add_argument("--home", type=Path)
    result_parser.add_argument("--json", action="store_true", dest="json_output")
    center_parser = subparsers.add_parser("center")
    center_parser.add_argument("--home", type=Path)
    center_parser.add_argument("--limit", type=int, default=20)
    center_parser.add_argument("--json", action="store_true", dest="json_output")
    validate_parser = subparsers.add_parser("validate")
    validate_parser.add_argument("spec", type=Path)
    demo_parser = subparsers.add_parser("demo")
    demo_parser.add_argument("spec", type=Path)
    demo_parser.add_argument("--work-dir", type=Path, required=True)
    agent_parser = subparsers.add_parser("agent-run")
    agent_parser.add_argument("spec", type=Path)
    agent_parser.add_argument("--work-dir", type=Path, required=True)
    agent_parser.add_argument("--workspace", type=Path, required=True)
    agent_parser.add_argument("--resume", action="store_true")
    agent_parser.add_argument("--allow-gate", action="append", default=[])
    agent_parser.add_argument("--learning-root", type=Path)
    agent_parser.add_argument("--optimization-root", type=Path)
    agent_parser.add_argument("--agent-os-root", type=Path)
    agent_parser.add_argument("--optimization-rollout-key")
    orca_plan_parser = subparsers.add_parser("orca-plan")
    orca_plan_parser.add_argument("spec", type=Path)
    orca_plan_parser.add_argument("--objective")
    orca_plan_parser.add_argument("--optimization-root", type=Path)
    orca_plan_parser.add_argument("--agent-os-root", type=Path)
    orca_plan_parser.add_argument("--optimization-rollout-key")
    orca_effect_parser = subparsers.add_parser("orca-effect")
    orca_effect_parser.add_argument(
        "action", choices=("inspect", "reconcile", "reset")
    )
    orca_effect_parser.add_argument("spec", type=Path)
    orca_effect_parser.add_argument("--root", type=Path, required=True)
    orca_effect_parser.add_argument("--workspace", type=Path, required=True)
    orca_effect_parser.add_argument("--effect-id")
    orca_effect_parser.add_argument("--actor")
    orca_effect_parser.add_argument("--reason")
    orca_effect_parser.add_argument("--receipt-digest")
    subparsers.add_parser("executors")
    console_parser = subparsers.add_parser("console")
    console_parser.add_argument("--control-root", type=Path, required=True)
    console_parser.add_argument("--run-id", required=True)
    console_parser.add_argument("--output", type=Path, required=True)
    console_parser.add_argument("--learning-root", type=Path)
    console_parser.add_argument("--optimization-root", type=Path)
    console_parser.add_argument("--agent-os-root", type=Path)
    live_console_parser = subparsers.add_parser("console-serve")
    live_console_parser.add_argument("--control-root", type=Path, required=True)
    live_console_parser.add_argument("--run-id", required=True)
    live_console_parser.add_argument("--port", type=int, default=0)
    live_console_parser.add_argument("--learning-root", type=Path)
    live_console_parser.add_argument("--optimization-root", type=Path)
    live_console_parser.add_argument("--agent-os-root", type=Path)
    approval_parser = subparsers.add_parser("approval")
    approval_parser.add_argument("--control-root", type=Path, required=True)
    approval_parser.add_argument("--run-id", required=True)
    approval_parser.add_argument("--gate", required=True)
    approval_parser.add_argument("--decision", choices=("allow", "deny"), required=True)
    approval_parser.add_argument("--actor", required=True)
    approval_parser.add_argument("--note")
    approval_parser.add_argument("--agent-os-root", type=Path)
    rsi_parser = subparsers.add_parser("rsi")
    rsi_parser.add_argument(
        "action",
        choices=(
            "status",
            "feedback",
            "propose",
            "evaluate",
            "approve",
            "activate",
            "rollback",
        ),
    )
    rsi_parser.add_argument("--learning-root", type=Path)
    rsi_parser.add_argument("--agent-os-root", type=Path)
    rsi_parser.add_argument("--candidate-id")
    rsi_parser.add_argument("--actor")
    rsi_parser.add_argument("--min-observations", type=int, default=6)
    rsi_parser.add_argument("--min-samples", type=int, default=3)
    rsi_parser.add_argument("--min-success-rate", type=float, default=0.8)
    rsi_parser.add_argument("--min-quality-score", type=float, default=0.8)
    rsi_parser.add_argument("--min-quality-samples", type=int)
    rsi_parser.add_argument("--rollout-percent", type=int, default=10)
    rsi_parser.add_argument("--task-id")
    rsi_parser.add_argument("--score", type=float)
    rsi_parser.add_argument("--source")
    optimization_parser = subparsers.add_parser("rsi-opt")
    optimization_parser.add_argument(
        "action",
        choices=(
            "status",
            "freeze-suite",
            "suggest",
            "propose",
            "evaluate",
            "approve",
            "activate",
            "rollback",
            "canary",
        ),
    )
    optimization_parser.add_argument("--optimization-root", type=Path)
    optimization_parser.add_argument("--agent-os-root", type=Path)
    optimization_parser.add_argument(
        "--kind", choices=("prompt_template", "graph_topology")
    )
    optimization_parser.add_argument("--cases", type=Path)
    optimization_parser.add_argument("--change", type=Path)
    optimization_parser.add_argument("--failures", type=Path)
    optimization_parser.add_argument("--measurements", type=Path)
    optimization_parser.add_argument("--canary", type=Path)
    optimization_parser.add_argument("--suite-id")
    optimization_parser.add_argument("--candidate-id")
    optimization_parser.add_argument("--rationale")
    optimization_parser.add_argument("--actor")
    optimization_parser.add_argument("--rollout-percent", type=int, default=10)
    optimization_parser.add_argument("--min-occurrences", type=int, default=3)
    optimization_parser.add_argument(
        "--max-quality-regression", type=float, default=0.0
    )
    optimization_parser.add_argument(
        "--max-cost-increase-percent", type=float, default=0.0
    )
    optimization_parser.add_argument(
        "--max-latency-increase-percent", type=float, default=0.0
    )
    os_parser = subparsers.add_parser("agent-os")
    os_parser.add_argument(
        "action",
        choices=(
            "status",
            "export",
            "import",
            "compatibility",
            "doctor",
            "rehearse",
            "release",
            "verify-release",
        ),
    )
    os_parser.add_argument("--root", type=Path)
    os_parser.add_argument("--bundle", type=Path)
    os_parser.add_argument("--release", type=Path)
    os_parser.add_argument("--source-root", type=Path)
    engineer_parser = subparsers.add_parser("engineer")
    engineer_parser.add_argument(
        "action", choices=("init", "plan", "run", "status", "ship")
    )
    engineer_parser.add_argument("--workspace", type=Path)
    engineer_parser.add_argument("--task-dir", type=Path)
    engineer_parser.add_argument("--policy", type=Path)
    engineer_parser.add_argument("--objective")
    engineer_parser.add_argument("--approved-by")
    engineer_parser.add_argument("--plan-digest")
    engineer_parser.add_argument("--agent-os-root", type=Path)
    evaluation_parser = subparsers.add_parser("evaluate")
    evaluation_parser.add_argument(
        "action", choices=("record-engineering", "baseline", "status")
    )
    evaluation_parser.add_argument("--root", type=Path, required=True)
    evaluation_parser.add_argument("--case", type=Path)
    evaluation_parser.add_argument("--report", type=Path)
    evaluation_parser.add_argument("--run-id")
    evaluation_parser.add_argument("--name")
    evaluation_parser.add_argument("--user-inputs", type=int)
    evaluation_parser.add_argument("--human-decisions", type=int)
    evaluation_parser.add_argument("--recovery-attempted", action="store_true")
    evaluation_parser.add_argument("--recovery-succeeded", action="store_true")
    benchmark_parser = subparsers.add_parser("benchmark")
    benchmark_parser.add_argument(
        "action",
        choices=(
            "freeze",
            "freeze-suite",
            "execute",
            "record",
            "reprice",
            "report",
        ),
    )
    benchmark_parser.add_argument("--root", type=Path)
    benchmark_parser.add_argument("--protocol", type=Path)
    benchmark_parser.add_argument("--run", type=Path)
    benchmark_parser.add_argument("--run-id")
    benchmark_parser.add_argument("--run-artifacts", type=Path)
    benchmark_parser.add_argument("--verified", action="store_true")
    benchmark_parser.add_argument("--prices", type=Path)
    benchmark_parser.add_argument("--baseline-protocol", type=Path)
    benchmark_parser.add_argument("--candidate-protocol", type=Path)
    benchmark_parser.add_argument("--json", action="store_true", dest="json_output")
    benchmark_parser.add_argument("--spec", type=Path, dest="benchmark_spec")
    benchmark_parser.add_argument("--inputs", type=Path, dest="benchmark_inputs")
    benchmark_parser.add_argument("--output", type=Path)
    benchmark_parser.add_argument("--benchmark-id")
    benchmark_parser.add_argument("--scenario-version")
    benchmark_parser.add_argument("--provider")
    benchmark_parser.add_argument("--model")
    benchmark_parser.add_argument("--reasoning")
    benchmark_parser.add_argument("--executor")
    benchmark_parser.add_argument("--executor-version")
    benchmark_parser.add_argument("--concurrency", type=int, action="append")
    benchmark_parser.add_argument("--repetitions", type=int, default=5)
    benchmark_parser.add_argument("--workspace", type=Path)
    benchmark_parser.add_argument("--allow-gate", action="append", default=[])
    benchmark_parser.add_argument("--timeout-seconds", type=float, default=3600.0)
    benchmark_parser.add_argument("--agent-os-root", type=Path)
    benchmark_parser.add_argument("--suite-id")
    benchmark_parser.add_argument("--suite-version")
    benchmark_parser.add_argument("--micro-protocol", type=Path)
    benchmark_parser.add_argument("--engineering-protocol", type=Path)
    benchmark_parser.add_argument("--recovery-protocol", type=Path)
    return parser


def _run_distribution_command(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
) -> int:
    distribution = AgentOSDistribution(source_root=args.source_root)
    if args.action == "compatibility":
        value = distribution.compatibility_matrix()
    elif args.action == "verify-release":
        if args.release is None:
            parser.error("agent-os verify-release requires --release")
        value = distribution.verify_release(args.release)
    else:
        if args.root is None:
            parser.error(f"agent-os {args.action} requires --root")
        if args.action == "doctor":
            value = distribution.doctor(args.root)
        elif args.action == "rehearse":
            value = distribution.rehearse(args.root, bundle=args.bundle)
        elif args.action == "release":
            if args.release is None:
                parser.error("agent-os release requires --release")
            value = distribution.create_release(args.root, args.release)
        else:
            os_state = AgentOS(args.root)
            if args.action == "status":
                value = os_state.status()
            elif args.action == "export":
                if args.bundle is None:
                    parser.error("agent-os export requires --bundle")
                value = {"bundle": str(os_state.export_bundle(args.bundle))}
            else:
                if args.bundle is None:
                    parser.error("agent-os import requires --bundle")
                value = os_state.import_bundle(args.bundle)
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))
    if args.action == "doctor" and not value["healthy"]:
        return 2
    return 0


def _run_executors_command() -> int:
    registry = discover_local_executors()
    profiles = registry.profiles()
    print(
        json.dumps(
            [
                {
                    "executor_id": item.executor_id,
                    "features": list(item.features),
                    "tools": list(item.tools),
                    "provider": profiles[item.executor_id].provider,
                    "estimated_cost_usd": profiles[item.executor_id].estimated_cost_usd,
                    "estimated_latency_seconds": profiles[
                        item.executor_id
                    ].estimated_latency_seconds,
                    "data_classifications": list(
                        profiles[item.executor_id].data_classifications
                    ),
                }
                for item in registry.capabilities()
            ],
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


def _run_rsi_command(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
    agent_os: Optional[AgentOS],
) -> int:
    learning_root = agent_os.learning_root if agent_os else args.learning_root
    if learning_root is None:
        parser.error("rsi requires --agent-os-root or --learning-root")
    loop = RSILoop(learning_root)
    if args.action == "status":
        active = loop.active_policy()
        value = {
            "active_policy": active.to_dict() if active is not None else None,
            "candidates": [item.to_dict() for item in loop.candidates()],
            "observations": len(loop.journal.read()),
            "quality_feedback": len(loop.feedback_journal.read()),
        }
    elif args.action == "feedback":
        if not args.task_id or args.score is None or not args.source:
            parser.error("rsi feedback requires --task-id, --score, and --source")
        value = loop.feedback(args.task_id, args.score, args.source).to_dict()
    elif args.action == "propose":
        value = loop.propose(
            min_observations=args.min_observations,
            min_samples=args.min_samples,
            min_success_rate=args.min_success_rate,
            min_quality_score=args.min_quality_score,
            min_quality_samples=args.min_quality_samples,
            rollout_percent=args.rollout_percent,
        ).to_dict()
    else:
        if not args.candidate_id and args.action != "rollback":
            parser.error(f"rsi {args.action} requires --candidate-id")
        if args.action == "evaluate":
            value = loop.evaluate(args.candidate_id).to_dict()
        elif args.action == "approve":
            if not args.actor:
                parser.error("rsi approve requires --actor")
            value = loop.approve(args.candidate_id, args.actor).to_dict()
        elif args.action == "activate":
            value = loop.activate(args.candidate_id).to_dict()
        else:
            policy = loop.rollback()
            value = {
                "active_policy": policy.to_dict() if policy is not None else None
            }
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))
    return 0


def _operations_console(
    args: argparse.Namespace,
    agent_os: Optional[AgentOS],
) -> OperationsConsole:
    learning_root = agent_os.learning_root if agent_os else args.learning_root
    optimization_root = (
        agent_os.optimization_root if agent_os else args.optimization_root
    )
    return OperationsConsole(
        args.control_root,
        approval_inbox=(
            agent_os.approval_inbox(args.control_root) if agent_os else None
        ),
        learning_root=learning_root,
        optimization_root=optimization_root,
        agent_os=agent_os,
    )


def _run_console_command(
    args: argparse.Namespace,
    agent_os: Optional[AgentOS],
) -> int:
    console = _operations_console(args, agent_os)
    if args.command == "console":
        output = console.render(args.run_id, args.output)
        print(json.dumps({"run_id": args.run_id, "output": str(output)}))
        return 0

    server = OperationsServer(console, args.run_id, port=args.port)
    print(
        json.dumps(
            {"run_id": args.run_id, "url": server.url},
            ensure_ascii=False,
            sort_keys=True,
        ),
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
    return 0


def _run_approval_command(
    args: argparse.Namespace,
    agent_os: Optional[AgentOS],
) -> int:
    inbox = (
        agent_os.approval_inbox(args.control_root)
        if agent_os
        else ApprovalInbox(args.control_root)
    )
    item = inbox.decide(
        args.run_id, args.gate, args.decision, args.actor, args.note
    )
    print(json.dumps(asdict(item), ensure_ascii=False, sort_keys=True))
    return 0


def _dispatch_parsed(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
) -> int:
    """Execute one already-parsed command without rebuilding CLI syntax."""

    if args.command == "setup":
        value = AgentOSDistribution(source_root=args.source_root).setup(
            args.home or default_agent_os_home()
        )
        _print_setup(value, args.json_output)
        return (
            0
            if value["ready_for_agent_execution"] and not value["blocking_checks"]
            else 2
        )

    if args.command in ("do", "status", "approve", "control", "result"):
        return _run_user_task_command(args)

    if args.command == "center":
        resident = ResidentCoordinator(args.home or default_agent_os_home())
        resident.ensure_running()
        value = resident.task_center(args.limit)
        _print_task_center(value, args.json_output)
        return 0

    if args.command == "engineer":
        if args.action != "status" and args.workspace is None:
            parser.error(f"engineer {args.action} requires --workspace")
        if args.action not in ("init", "status") and args.task_dir is None:
            parser.error(f"engineer {args.action} requires --task-dir")
        if args.action == "status" and args.task_dir is None:
            parser.error("engineer status requires --task-dir")
        if args.action in ("plan", "ship") and not args.objective:
            parser.error(f"engineer {args.action} requires --objective")
        if args.action == "run" and not args.approved_by:
            parser.error("engineer run requires --approved-by")
        if args.action == "run" and not args.plan_digest:
            parser.error("engineer run requires --plan-digest")

    agent_os_root = getattr(args, "agent_os_root", None)
    legacy_roots = [
        value
        for value in (
            getattr(args, "learning_root", None),
            getattr(args, "optimization_root", None),
        )
        if value is not None
    ]
    if agent_os_root is not None and legacy_roots:
        parser.error("--agent-os-root cannot be combined with legacy state roots")
    agent_os = AgentOS(agent_os_root) if agent_os_root is not None else None

    if args.command == "evaluate":
        return _run_evaluation_command(args, parser)

    if args.command == "benchmark":
        return _run_benchmark_command(args, parser, agent_os)

    if args.command == "engineer":
        return _run_engineering_command(args, parser, agent_os)

    if args.command == "agent-os":
        return _run_distribution_command(args, parser)

    if args.command == "executors":
        return _run_executors_command()

    if args.command in ("console", "console-serve"):
        return _run_console_command(args, agent_os)

    if args.command == "approval":
        return _run_approval_command(args, agent_os)

    if args.command == "rsi":
        return _run_rsi_command(args, parser, agent_os)

    if args.command == "rsi-opt":
        if agent_os is not None:
            args.optimization_root = agent_os.optimization_root
        if args.optimization_root is None:
            parser.error("rsi-opt requires --agent-os-root or --optimization-root")
        return _run_optimization_command(args, parser)

    return _run_graph_command(args, parser, agent_os)


def _run_graph_command(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
    agent_os: Optional[AgentOS],
) -> int:
    graph = GraphSpec.from_json(args.spec)
    optimization_root = (
        agent_os.optimization_root
        if agent_os is not None
        else getattr(args, "optimization_root", None)
    )
    if agent_os is not None:
        graph = agent_os.apply(
            graph, getattr(args, "optimization_rollout_key", None)
        )
    elif optimization_root is not None:
        graph = RSIOptimizationLab(optimization_root).apply(
            graph, getattr(args, "optimization_rollout_key", None)
        )
    validate_graph(graph)
    if args.command == "validate":
        print(json.dumps({"graph_id": graph.id, "valid": True, "nodes": len(graph.nodes)}))
        return 0

    if args.command == "orca-plan":
        plan = OrcaGraphCompiler().compile(graph, args.objective)
        print(json.dumps(plan.to_dict(), ensure_ascii=False, sort_keys=True))
        return 0

    if args.command == "orca-effect":
        coordinator = OrcaCoordinator(
            graph,
            OrcaBackend(OrcaClient(cwd=args.workspace)),
            args.root,
            args.workspace,
        )
        value: Mapping[str, Any]
        if args.action == "inspect":
            value = {
                "effects": [
                    effect.to_dict() for effect in coordinator.inspect_effects()
                ]
            }
        elif args.action == "reconcile":
            if args.effect_id is None or args.actor is None:
                parser.error(
                    "orca-effect reconcile requires --effect-id and --actor"
                )
            value = coordinator.reconcile_effect(
                args.effect_id, actor=args.actor
            ).to_dict()
        else:
            if any(
                item is None
                for item in (
                    args.effect_id,
                    args.actor,
                    args.reason,
                    args.receipt_digest,
                )
            ):
                parser.error(
                    "orca-effect reset requires --effect-id, --actor, --reason, and --receipt-digest"
                )
            value = coordinator.reset_effect(
                args.effect_id,
                actor=args.actor,
                reason=args.reason,
                receipt_digest=args.receipt_digest,
            ).to_dict()
        print(json.dumps(value, ensure_ascii=False, sort_keys=True))
        return 0

    if args.command == "agent-run":
        telemetry.configure(args.work_dir)
        learning_root = agent_os.learning_root if agent_os else args.learning_root
        learning_loop = RSILoop(learning_root) if learning_root else None
        router = agent_os.router() if agent_os else learning_loop.router() if learning_loop else None
        token_reservations = (
            agent_os.token_reservations()
            if agent_os
            else HistoricalTokenReservations(learning_loop.journal.read())
            if learning_loop
            else None
        )
        reuse_store = agent_os.reuse_store() if agent_os else None
        executors = discover_local_executors(router, reuse_store)
        nodes = NodeRegistry()
        nodes.register("agent", AgentNodeHandler(graph, executors, args.workspace))
        result = GraphRuntime(
            graph,
            nodes,
            work_dir=args.work_dir,
            gate_policy=AllowListGatePolicy(set(args.allow_gate)),
            verified_result_publisher=(
                VerifiedResultPublisher(
                    reuse_store, args.work_dir / "verified-publications.json"
                )
                if reuse_store is not None
                else None
            ),
            token_reservations=token_reservations,
        ).run(resume=args.resume)
        print(
            json.dumps(
                {
                    "run_id": result.run_id,
                    "success": result.success,
                    "statuses": {
                        node_id: status.value for node_id, status in result.statuses.items()
                    },
                    "tokens_used": result.tokens_used,
                    "cost_usd": result.cost_usd,
                    "usage": result.usage.to_dict(),
                    "artifacts": result.artifacts,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0 if result.success else 1

    # Demo runs have no Agent OS home argument; keep their telemetry beside
    # their disposable run evidence instead of bootstrapping ~/.agent-os.
    telemetry.configure(args.work_dir)
    runtime = GraphRuntime(
        graph,
        _demo_registry(),
        work_dir=args.work_dir,
        gate_policy=AllowListGatePolicy({"release"}),
    )
    result = runtime.run(resume=True)
    print(
        json.dumps(
            {
                "run_id": result.run_id,
                "success": result.success,
                "tokens_used": result.tokens_used,
                "cost_usd": result.cost_usd,
                "usage": result.usage.to_dict(),
                "artifacts": result.artifacts,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if result.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
