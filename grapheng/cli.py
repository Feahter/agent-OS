import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

from .adapters import discover_local_executors
from .agent_nodes import AgentNodeHandler
from .console import ApprovalInbox, OperationsConsole
from .console_server import OperationsServer
from .distribution import AgentOSDistribution
from .engineering import EngineeringWorkflow, ProjectPolicy, default_project_policy
from .learning import RSILoop
from .model import GraphSpec
from .os import AgentOS
from .optimization import (
    CanaryObservation,
    FailurePattern,
    RegressionCase,
    RegressionMeasurement,
    RSIOptimizationLab,
)
from .orca import OrcaGraphCompiler
from .policy import AllowListGatePolicy
from .publication import VerifiedResultPublisher
from .runtime import GraphRuntime, NodeOutcome, NodeRegistry
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
    return json.loads(path.read_text(encoding="utf-8"))


def _run_optimization_command(args, parser) -> int:
    lab = RSIOptimizationLab(args.optimization_root)
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


def main() -> int:
    parser = argparse.ArgumentParser(prog="grapheng")
    subparsers = parser.add_subparsers(dest="command", required=True)
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
    args = parser.parse_args()

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

    if args.command == "engineer":
        return _run_engineering_command(args, parser, agent_os)

    if args.command == "agent-os":
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

    if args.command == "executors":
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

    if args.command == "console":
        learning_root = agent_os.learning_root if agent_os else args.learning_root
        optimization_root = (
            agent_os.optimization_root if agent_os else args.optimization_root
        )
        output = OperationsConsole(
            args.control_root,
            approval_inbox=(
                agent_os.approval_inbox(args.control_root) if agent_os else None
            ),
            learning_root=learning_root,
            optimization_root=optimization_root,
            agent_os=agent_os,
        ).render(args.run_id, args.output)
        print(json.dumps({"run_id": args.run_id, "output": str(output)}))
        return 0

    if args.command == "console-serve":
        learning_root = agent_os.learning_root if agent_os else args.learning_root
        optimization_root = (
            agent_os.optimization_root if agent_os else args.optimization_root
        )
        console = OperationsConsole(
            args.control_root,
            approval_inbox=(
                agent_os.approval_inbox(args.control_root) if agent_os else None
            ),
            learning_root=learning_root,
            optimization_root=optimization_root,
            agent_os=agent_os,
        )
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

    if args.command == "approval":
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

    if args.command == "rsi":
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
                value = {"active_policy": policy.to_dict() if policy is not None else None}
        print(json.dumps(value, ensure_ascii=False, sort_keys=True))
        return 0

    if args.command == "rsi-opt":
        if agent_os is not None:
            args.optimization_root = agent_os.optimization_root
        if args.optimization_root is None:
            parser.error("rsi-opt requires --agent-os-root or --optimization-root")
        return _run_optimization_command(args, parser)

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

    if args.command == "agent-run":
        learning_root = agent_os.learning_root if agent_os else args.learning_root
        router = (
            agent_os.router()
            if agent_os
            else RSILoop(learning_root).router() if learning_root else None
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
                    "artifacts": result.artifacts,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0 if result.success else 1

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
                "artifacts": result.artifacts,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0 if result.success else 1


if __name__ == "__main__":
    raise SystemExit(main())
