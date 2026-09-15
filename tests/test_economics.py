import contextlib
import hashlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from grapheng import (
    AgentResult,
    BenchmarkProtocol,
    BenchmarkRun,
    BenchmarkSuite,
    ContractViolation,
    ExecutorCapabilities,
    ExecutorRegistry,
    GraphSpec,
    LocalControlPlane,
    ModelUsage,
    NodeOutcome,
    NodeRegistry,
    PriceCatalog,
    ReuseMetrics,
    RunEconomics,
)
from grapheng.cli import main


def protocol(**overrides):
    values = {
        "benchmark_id": "luna-six-node",
        "scenario_version": "v1",
        "provider": "openai",
        "model": "gpt-5.6-luna",
        "reasoning": "low",
        "executor": "codex",
        "executor_version": "0.1.0",
        "dag_fingerprint": "a" * 64,
        "input_fingerprint": "b" * 64,
        "concurrency_matrix": (1, 2),
        "repetitions_per_cell": 5,
    }
    values.update(overrides)
    return BenchmarkProtocol(**values)


def run(run_id, usage, **overrides):
    values = {
        "run_id": run_id,
        "concurrency": 1,
        "success": True,
        "verified": True,
        "wall_clock_seconds": 61.503,
        "verification_seconds": 3.0,
        "human_actions": 0,
        "repair_loops": 0,
        "recoveries": 0,
        "model_calls": 6,
        "usage": usage,
    }
    values.update(overrides)
    return BenchmarkRun(**values)


def prices(**overrides):
    values = {
        "catalog_id": "openai-public",
        "version": "2026-09-01",
        "effective_at": "2026-09-01T00:00:00Z",
        "provider": "openai",
        "model": "gpt-5.6-luna",
        "input_usd_per_million": 1.0,
        "cached_input_usd_per_million": 0.5,
        "output_usd_per_million": 2.0,
        "cached_input_mode": "included_in_input",
    }
    values.update(overrides)
    return PriceCatalog(**values)


def complete_usage(cost_usd=None, cost_complete=False):
    return ModelUsage(
        input_tokens=100,
        cached_input_tokens=40,
        output_tokens=10,
        total_tokens=110,
        cost_usd=cost_usd,
        input_tokens_complete=True,
        cached_input_tokens_complete=True,
        output_tokens_complete=True,
        total_tokens_complete=True,
        cost_complete=cost_complete,
    )


def exact_usage(total_tokens, cost_usd=None, cost_complete=False):
    output_tokens = min(10, total_tokens)
    return ModelUsage(
        input_tokens=total_tokens - output_tokens,
        cached_input_tokens=0,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
        cost_usd=cost_usd,
        input_tokens_complete=True,
        cached_input_tokens_complete=True,
        output_tokens_complete=True,
        total_tokens_complete=True,
        cost_complete=cost_complete,
    )


def completed_control_run(root, concurrency=2):
    graph = GraphSpec.from_dict(
        {
            "id": "benchmark-artifacts",
            "max_concurrency": concurrency,
            "require_reality_anchor": False,
            "nodes": [
                {
                    "id": "work",
                    "kind": "agent",
                    "writes": ["answer"],
                    "agent": {"prompt": "private benchmark prompt"},
                }
            ],
        }
    )
    usage = complete_usage()
    registry = NodeRegistry()
    registry.register(
        "agent",
        lambda context: NodeOutcome(
            {"answer": 42},
            tokens_used=110,
            usage=usage,
            metadata={"reuse_status": "miss"},
        ),
    )
    control_root = root / "control"
    plane = LocalControlPlane(control_root, owner_id="benchmark-test")
    try:
        run_id = plane.submit(graph, registry)
        plane.wait(run_id, timeout=2)
    finally:
        plane.close()
    return graph, control_root / "runs" / run_id, usage.with_accounted_totals(110, 0.0)


class RunEconomicsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_measured_estimated_and_unknown_costs_are_distinct(self):
        economics = RunEconomics(self.root / "economics", clock=lambda: 100.0)
        measured = economics.record(
            protocol(),
            run("measured-zero", complete_usage(0.0, True)),
            prices(),
        )
        estimated = economics.record(
            protocol(),
            run(
                "estimated-zero",
                ModelUsage(0, 0, 0, 0, None, True, True, True, True, False),
            ),
            prices(
                input_usd_per_million=0,
                cached_input_usd_per_million=0,
                output_usd_per_million=0,
            ),
        )
        unknown = economics.record(
            protocol(), run("unknown", ModelUsage.unknown())
        )

        self.assertEqual("measured", measured["cost"]["source"])
        self.assertEqual(0.0, measured["cost"]["amount_usd"])
        self.assertEqual("estimated", estimated["cost"]["source"])
        self.assertEqual(0.0, estimated["cost"]["amount_usd"])
        self.assertEqual("unknown", unknown["cost"]["source"])
        self.assertIsNone(unknown["cost"]["amount_usd"])

    def test_price_catalog_expiry_is_explicit_and_v1_remains_readable(self):
        catalog = prices(
            effective_at="1970-01-01T00:00:00Z",
            valid_until="1970-01-01T00:00:01Z",
        )
        snapshot = RunEconomics(
            self.root / "economics", clock=lambda: 100.0
        ).record(protocol(), run("expired-prices", complete_usage()), catalog)

        self.assertEqual(
            ["price catalog expired before benchmark time"],
            snapshot["warnings"],
        )
        legacy = catalog.to_dict()
        legacy["schema_version"] = 1
        legacy.pop("valid_until")
        self.assertIsNone(PriceCatalog.from_dict(legacy).valid_until)

        with self.assertRaisesRegex(ContractViolation, "must be after"):
            prices(
                effective_at="2026-09-01T00:00:00Z",
                valid_until="2026-09-01T00:00:00Z",
            )

    def test_roi_report_aggregates_price_catalog_warnings(self):
        economics = RunEconomics(
            self.root / "economics", clock=lambda: 100.0
        )
        baseline = protocol(executor_version="baseline")
        candidate = protocol(executor_version="candidate")
        expired = prices(
            effective_at="1970-01-01T00:00:00Z",
            valid_until="1970-01-01T00:00:01Z",
        )
        future = prices(effective_at="1970-01-01T00:03:20Z")
        economics.record(
            baseline, run("baseline-expired", complete_usage()), expired
        )
        economics.record(
            baseline, run("baseline-current", complete_usage())
        )
        economics.record(
            candidate, run("candidate-future", complete_usage()), future
        )

        report = economics.report(baseline, candidate)

        self.assertEqual(
            {
                "counts": {
                    "price catalog expired before benchmark time": 1,
                },
                "affected_run_ids": ["baseline-expired"],
            },
            report["baseline"]["overall"]["warnings"],
        )
        self.assertEqual(
            {
                "counts": {
                    "price catalog is not yet effective at benchmark time": 1,
                },
                "affected_run_ids": ["candidate-future"],
            },
            report["candidate"]["overall"]["warnings"],
        )

    def test_estimate_separates_cached_tokens_without_double_counting(self):
        snapshot = RunEconomics(self.root / "economics").record(
            protocol(), run("cached", complete_usage()), prices()
        )

        estimate = snapshot["cost"]["estimated"]
        self.assertAlmostEqual(0.000060, estimate["components_usd"]["input"])
        self.assertAlmostEqual(
            0.000020, estimate["components_usd"]["cached_input"]
        )
        self.assertAlmostEqual(0.000020, estimate["components_usd"]["output"])
        self.assertAlmostEqual(0.000100, snapshot["cost"]["amount_usd"])

    def test_incomplete_token_components_produce_partial_estimate(self):
        usage = ModelUsage(
            input_tokens=100,
            output_tokens=10,
            total_tokens=110,
            input_tokens_complete=True,
            output_tokens_complete=True,
            total_tokens_complete=True,
        )
        snapshot = RunEconomics(self.root / "economics").record(
            protocol(), run("partial", usage), prices()
        )

        self.assertEqual("estimated", snapshot["cost"]["source"])
        self.assertFalse(snapshot["cost"]["complete"])
        self.assertIsNone(snapshot["cost"]["amount_usd"])
        self.assertEqual(
            ["cached_input_tokens", "input_tokens"],
            snapshot["cost"]["estimated"]["missing_components"],
        )
        self.assertAlmostEqual(
            0.000020, snapshot["cost"]["estimated"]["partial_amount_usd"]
        )

    def test_reprice_uses_an_explicit_catalog_without_changing_the_run(self):
        economics = RunEconomics(self.root / "economics")
        original = economics.record(
            protocol(), run("scenario", complete_usage()), prices()
        )
        path = self.root / "economics" / "runs" / "scenario.json"
        before = path.read_bytes()

        scenario = economics.reprice(
            "scenario",
            prices(
                version="2026-10-01",
                effective_at="2026-10-01T00:00:00Z",
                input_usd_per_million=2,
                cached_input_usd_per_million=1,
                output_usd_per_million=4,
            ),
        )

        self.assertEqual(original["cost"], scenario["original_cost"])
        self.assertAlmostEqual(
            0.000200, scenario["scenario_estimate"]["amount_usd"]
        )
        self.assertEqual(
            "2026-10-01",
            scenario["scenario_estimate"]["catalog"]["version"],
        )
        self.assertEqual(before, path.read_bytes())
        with self.assertRaisesRegex(ContractViolation, "does not match"):
            economics.reprice("scenario", prices(provider="other"))

    def test_unknown_timing_and_intervention_metrics_remain_null(self):
        snapshot = RunEconomics(self.root / "economics").record(
            protocol(),
            run(
                "unknown-metrics",
                complete_usage(),
                verification_seconds=None,
                human_actions=None,
                repair_loops=None,
                recoveries=None,
            ),
        )

        self.assertIsNone(snapshot["latency"]["verification_seconds"])
        self.assertEqual(
            {"human_actions": None, "repair_loops": None, "recoveries": None},
            snapshot["intervention"],
        )

    def test_legacy_benchmark_run_defaults_new_metrics_to_unknown(self):
        value = run("legacy-run", complete_usage()).to_dict()
        value["schema_version"] = 1
        for field in (
            "queue_wait_seconds",
            "model_seconds",
            "critical_path_seconds",
            "reuse",
            "nodes",
        ):
            value.pop(field)

        loaded = BenchmarkRun.from_dict(value)

        self.assertIsNone(loaded.queue_wait_seconds)
        self.assertIsNone(loaded.model_seconds)
        self.assertIsNone(loaded.critical_path_seconds)
        self.assertIsNone(loaded.reuse.hit)
        self.assertEqual((), loaded.nodes)

    def test_roi_report_reads_legacy_economics_snapshots(self):
        economics = RunEconomics(self.root / "economics")
        baseline = protocol(executor_version="baseline")
        candidate = protocol(executor_version="candidate")
        economics.record(baseline, run("legacy-baseline", exact_usage(100)))
        economics.record(candidate, run("legacy-candidate", exact_usage(80)))
        for run_id in ("legacy-baseline", "legacy-candidate"):
            path = self.root / "economics" / "runs" / f"{run_id}.json"
            value = json.loads(path.read_text(encoding="utf-8"))
            if run_id == "legacy-baseline":
                value["schema_version"] = 1
                value.pop("reuse")
                value.pop("nodes")
                for field in (
                    "queue_wait_seconds",
                    "model_seconds",
                    "critical_path_seconds",
                ):
                    value["latency"].pop(field)
            else:
                value["schema_version"] = 2
            value.pop("warnings")
            path.write_text(json.dumps(value), encoding="utf-8")

        report = economics.report(baseline, candidate)

        self.assertEqual(0.2, report["roi"]["token_reduction_fraction"])
        self.assertEqual(
            {
                "hit": None,
                "miss": None,
                "coalesced": None,
                "bypassed": None,
                "none": None,
                "saved_tokens": None,
            },
            report["baseline"]["overall"]["reuse"],
        )
        for cohort in (report["baseline"], report["candidate"]):
            for field in (
                "queue_wait_seconds",
                "model_seconds",
                "critical_path_seconds",
            ):
                self.assertEqual(
                    {"median": None, "p95": None}, cohort["overall"][field]
                )
        for field in (
            "queue_wait_median_reduction_fraction",
            "model_median_reduction_fraction",
            "critical_path_median_reduction_fraction",
        ):
            self.assertIsNone(report["roi"][field])

    def test_protocol_and_run_contracts_reject_private_extra_fields(self):
        protocol_value = protocol().to_dict()
        protocol_value["prompt"] = "must not enter the benchmark index"
        run_value = run("private", complete_usage()).to_dict()
        run_value["workspace"] = "/private/project/path"

        with self.assertRaisesRegex(ContractViolation, "invalid contract"):
            BenchmarkProtocol.from_dict(protocol_value)
        with self.assertRaisesRegex(ContractViolation, "invalid contract"):
            BenchmarkRun.from_dict(run_value)
        with self.assertRaisesRegex(ContractViolation, "version identifier"):
            protocol(executor_version="/private/project/path")
        with self.assertRaisesRegex(ContractViolation, "ISO-8601"):
            prices(effective_at="/private/project/path")

    def test_run_ids_are_immutable_and_concurrency_must_be_in_matrix(self):
        economics = RunEconomics(self.root / "economics")
        benchmark_run = run("immutable", complete_usage())
        economics.record(protocol(), benchmark_run)

        with self.assertRaisesRegex(ContractViolation, "already exists"):
            economics.record(protocol(), benchmark_run)
        with self.assertRaisesRegex(ContractViolation, "protocol matrix"):
            economics.record(
                protocol(), run("bad-concurrency", complete_usage(), concurrency=4)
            )

    def test_report_uses_all_runs_and_verified_result_denominator(self):
        economics = RunEconomics(self.root / "economics")
        baseline = protocol(executor_version="baseline")
        candidate = protocol(executor_version="candidate")
        economics.record(
            baseline,
            run(
                "baseline-success",
                exact_usage(100),
                wall_clock_seconds=10,
                human_actions=1,
                model_calls=2,
                queue_wait_seconds=2,
                model_seconds=8,
                critical_path_seconds=9,
                reuse=ReuseMetrics(1, 0, 0, 0, 0, 100),
            ),
        )
        economics.record(
            baseline,
            run(
                "baseline-failure",
                exact_usage(50),
                success=False,
                verified=False,
                wall_clock_seconds=20,
                model_calls=1,
                queue_wait_seconds=4,
                model_seconds=10,
                critical_path_seconds=12,
                reuse=ReuseMetrics(0, 0, 1, 0, 0, 20),
            ),
        )
        economics.record(
            candidate,
            run(
                "candidate-success",
                exact_usage(70),
                wall_clock_seconds=8,
                human_actions=0,
                model_calls=2,
                queue_wait_seconds=1,
                model_seconds=6,
                critical_path_seconds=7,
                reuse=ReuseMetrics(2, 0, 0, 0, 0, 200),
            ),
        )
        economics.record(
            candidate,
            run(
                "candidate-failure",
                exact_usage(10),
                success=False,
                verified=False,
                wall_clock_seconds=12,
                model_calls=1,
                queue_wait_seconds=2,
                model_seconds=8,
                critical_path_seconds=9,
                reuse=ReuseMetrics(0, 0, 1, 0, 0, 50),
            ),
        )

        report = economics.report(baseline, candidate)
        baseline_summary = report["baseline"]["overall"]
        candidate_summary = report["candidate"]["overall"]

        self.assertEqual(0.5, baseline_summary["verified_success_rate"])
        self.assertEqual(150, baseline_summary["tokens"]["per_verified_result"])
        self.assertEqual(50, baseline_summary["tokens"]["per_model_call"])
        self.assertEqual(
            ["baseline-failure"], baseline_summary["failed_run_ids"]
        )
        self.assertEqual(80, candidate_summary["tokens"]["per_verified_result"])
        self.assertEqual(
            {
                "hit": 1,
                "miss": 0,
                "coalesced": 1,
                "bypassed": 0,
                "none": 0,
                "saved_tokens": 120,
            },
            baseline_summary["reuse"],
        )
        self.assertEqual(250, candidate_summary["reuse"]["saved_tokens"])
        self.assertAlmostEqual(
            (150 - 80) / 150, report["roi"]["token_reduction_fraction"]
        )
        self.assertIsNone(report["roi"]["cost_reduction_fraction"])
        self.assertEqual(
            3.0, baseline_summary["queue_wait_seconds"]["median"]
        )
        self.assertEqual(3.9, baseline_summary["queue_wait_seconds"]["p95"])
        self.assertEqual(9.0, baseline_summary["model_seconds"]["median"])
        self.assertEqual(9.9, baseline_summary["model_seconds"]["p95"])
        self.assertEqual(
            10.5, baseline_summary["critical_path_seconds"]["median"]
        )
        self.assertEqual(
            11.85, baseline_summary["critical_path_seconds"]["p95"]
        )
        self.assertEqual(
            0.5, report["roi"]["queue_wait_median_reduction_fraction"]
        )
        self.assertAlmostEqual(
            2 / 9, report["roi"]["model_median_reduction_fraction"]
        )
        self.assertAlmostEqual(
            2.5 / 10.5,
            report["roi"]["critical_path_median_reduction_fraction"],
        )
        self.assertFalse(report["baseline"]["matrix_complete"])

    def test_report_rejects_non_comparable_protocols(self):
        economics = RunEconomics(self.root / "economics")

        with self.assertRaisesRegex(ContractViolation, "not comparable"):
            economics.report(
                protocol(executor_version="baseline"),
                protocol(executor_version="candidate", input_fingerprint="c" * 64),
            )

    def test_cli_freezes_prompt_free_immutable_protocol(self):
        graph_value = {
            "id": "frozen-benchmark",
            "max_concurrency": 2,
            "require_reality_anchor": False,
            "nodes": [
                {
                    "id": "work",
                    "kind": "agent",
                    "writes": ["answer"],
                    "agent": {"prompt": "private benchmark prompt"},
                }
            ],
        }
        graph = GraphSpec.from_dict(graph_value)
        spec_path = self.root / "graph.json"
        inputs_path = self.root / "inputs.json"
        output_path = self.root / "protocol.json"
        spec_path.write_text(json.dumps(graph_value), encoding="utf-8")
        inputs = {
            "objective": "private benchmark input",
            "workspace": "/private/project/path",
        }
        inputs_path.write_text(json.dumps(inputs), encoding="utf-8")
        arguments = [
            "benchmark",
            "freeze",
            "--spec",
            str(spec_path),
            "--inputs",
            str(inputs_path),
            "--output",
            str(output_path),
            "--benchmark-id",
            "micro-six-node",
            "--scenario-version",
            "v1",
            "--provider",
            "openai",
            "--model",
            "gpt-5.6-luna",
            "--reasoning",
            "low",
            "--executor",
            "codex",
            "--executor-version",
            "0.1.0",
            "--concurrency",
            "1",
            "--concurrency",
            "2",
        ]
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            status = main(arguments)

        self.assertEqual(0, status)
        value = json.loads(output.getvalue())
        self.assertEqual(
            BenchmarkProtocol.graph_fingerprint(graph), value["dag_fingerprint"]
        )
        encoded_inputs = json.dumps(
            inputs,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        self.assertEqual(
            hashlib.sha256(encoded_inputs).hexdigest(), value["input_fingerprint"]
        )
        self.assertEqual([1, 2], value["concurrency_matrix"])
        self.assertEqual(value, json.loads(output_path.read_text(encoding="utf-8")))
        serialized = json.dumps(value, ensure_ascii=False)
        self.assertNotIn("private benchmark prompt", serialized)
        self.assertNotIn("private benchmark input", serialized)
        self.assertNotIn("/private/project/path", serialized)

        error = io.StringIO()
        with contextlib.redirect_stderr(error):
            repeated = main(arguments)
        self.assertEqual(2, repeated)
        self.assertIn("already exists", error.getvalue())

    def test_cli_freezes_the_required_three_scenario_suite(self):
        paths = {}
        for index, kind in enumerate(("micro", "engineering", "recovery")):
            item = protocol(
                benchmark_id=f"{kind}-scenario",
                dag_fingerprint=str(index + 1) * 64,
                input_fingerprint=str(index + 4) * 64,
            )
            path = self.root / f"{kind}.json"
            path.write_text(json.dumps(item.to_dict()), encoding="utf-8")
            paths[kind] = path
        output_path = self.root / "suite.json"
        arguments = [
            "benchmark",
            "freeze-suite",
            "--output",
            str(output_path),
            "--suite-id",
            "roi-core",
            "--suite-version",
            "v1",
            "--micro-protocol",
            str(paths["micro"]),
            "--engineering-protocol",
            str(paths["engineering"]),
            "--recovery-protocol",
            str(paths["recovery"]),
        ]
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            status = main(arguments)

        self.assertEqual(0, status)
        value = json.loads(output.getvalue())
        self.assertEqual("roi-core", value["suite_id"])
        self.assertEqual(
            {"engineering", "micro", "recovery"}, set(value["scenarios"])
        )
        self.assertEqual(value, BenchmarkSuite.load(output_path).to_dict())
        serialized = json.dumps(value, ensure_ascii=False)
        for path in paths.values():
            self.assertNotIn(str(path), serialized)

        error = io.StringIO()
        with contextlib.redirect_stderr(error):
            repeated = main(arguments)
        self.assertEqual(2, repeated)
        self.assertIn("already exists", error.getvalue())

    def test_suite_rejects_an_incomplete_concurrency_or_repetition_matrix(self):
        complete = protocol(benchmark_id="complete")

        with self.assertRaisesRegex(ContractViolation, "concurrency 1 and 2"):
            BenchmarkSuite(
                "roi-core",
                "v1",
                protocol(benchmark_id="micro", concurrency_matrix=(1,)),
                complete,
                protocol(benchmark_id="recovery"),
            )
        with self.assertRaisesRegex(ContractViolation, "at least 5 repetitions"):
            BenchmarkSuite(
                "roi-core",
                "v1",
                protocol(benchmark_id="micro"),
                protocol(benchmark_id="engineering", repetitions_per_cell=4),
                protocol(benchmark_id="recovery"),
            )

    def test_cli_records_a_reproducible_snapshot(self):
        protocol_path = self.root / "protocol.json"
        run_path = self.root / "run.json"
        prices_path = self.root / "prices.json"
        protocol_path.write_text(json.dumps(protocol().to_dict()), encoding="utf-8")
        run_path.write_text(
            json.dumps(run("cli-run", complete_usage()).to_dict()), encoding="utf-8"
        )
        prices_path.write_text(json.dumps(prices().to_dict()), encoding="utf-8")
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            status = main(
                [
                    "benchmark",
                    "record",
                    "--root",
                    str(self.root / "economics"),
                    "--protocol",
                    str(protocol_path),
                    "--run",
                    str(run_path),
                    "--prices",
                    str(prices_path),
                ]
            )

        self.assertEqual(0, status)
        snapshot = json.loads(output.getvalue())
        self.assertEqual("cli-run", snapshot["run_id"])
        self.assertEqual("estimated", snapshot["cost"]["source"])
        self.assertTrue((self.root / "economics" / "runs" / "cli-run.json").exists())

    def test_cli_executes_the_complete_protocol_matrix_in_one_command(self):
        class FakeExecutor:
            def __init__(self):
                self.calls = 0
                self.reasoning_efforts = []

            @property
            def capabilities(self):
                return ExecutorCapabilities(
                    "fake",
                    (
                        "model_selection",
                        "reasoning_control",
                        "structured_output",
                    ),
                    (),
                )

            def execute(self, request):
                self.calls += 1
                self.reasoning_efforts.append(request.reasoning_effort)
                return AgentResult(
                    "fake",
                    {"answer": self.calls},
                    "done",
                    tokens_used=10,
                    usage=exact_usage(10),
                )

        graph_value = {
            "id": "matrix-benchmark",
            "max_concurrency": 1,
            "require_reality_anchor": False,
            "nodes": [
                {
                    "id": "work",
                    "kind": "agent",
                    "writes": ["answer"],
                    "agent": {
                        "prompt": "private matrix prompt",
                        "executor": "fake",
                        "model": "test-model",
                    },
                }
            ],
        }
        graph = GraphSpec.from_dict(graph_value)
        inputs = {"objective": "private matrix input"}
        benchmark = protocol(
            executor="fake",
            model="test-model",
            dag_fingerprint=BenchmarkProtocol.graph_fingerprint(graph),
            input_fingerprint=BenchmarkProtocol.inputs_fingerprint(inputs),
            concurrency_matrix=(1, 2),
            repetitions_per_cell=2,
        )
        spec_path = self.root / "matrix-graph.json"
        inputs_path = self.root / "matrix-inputs.json"
        protocol_path = self.root / "matrix-protocol.json"
        workspace = self.root / "workspace"
        workspace.mkdir()
        spec_path.write_text(json.dumps(graph_value), encoding="utf-8")
        inputs_path.write_text(json.dumps(inputs), encoding="utf-8")
        protocol_path.write_text(
            json.dumps(benchmark.to_dict()), encoding="utf-8"
        )
        executor = FakeExecutor()
        executors = ExecutorRegistry()
        executors.register(executor)
        output = io.StringIO()

        with mock.patch(
            "grapheng.cli.discover_local_executors", return_value=executors
        ), contextlib.redirect_stdout(output):
            status = main(
                [
                    "benchmark",
                    "execute",
                    "--root",
                    str(self.root / "economics"),
                    "--protocol",
                    str(protocol_path),
                    "--spec",
                    str(spec_path),
                    "--inputs",
                    str(inputs_path),
                    "--workspace",
                    str(workspace),
                    "--verified",
                    "--timeout-seconds",
                    "2",
                ]
            )

        result = json.loads(output.getvalue())
        self.assertEqual(0, status)
        self.assertEqual(4, executor.calls)
        self.assertEqual(["low"] * 4, executor.reasoning_efforts)
        self.assertEqual(4, result["runs"])
        self.assertEqual({"1": 2, "2": 2}, result["runs_by_concurrency"])
        snapshots = tuple((self.root / "economics" / "runs").glob("*.json"))
        artifacts = tuple(
            (self.root / "economics" / "artifacts" / "runs").iterdir()
        )
        self.assertEqual(4, len(snapshots))
        self.assertEqual(4, len(artifacts))
        serialized = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("private matrix prompt", serialized)
        self.assertNotIn("private matrix input", serialized)

    def test_cli_records_real_control_run_artifacts(self):
        graph, run_dir, expected_usage = completed_control_run(self.root)
        benchmark = protocol(
            dag_fingerprint=graph.fingerprint(), concurrency_matrix=(2,)
        )
        protocol_path = self.root / "artifact-protocol.json"
        protocol_path.write_text(
            json.dumps(benchmark.to_dict()), encoding="utf-8"
        )
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            status = main(
                [
                    "benchmark",
                    "record",
                    "--root",
                    str(self.root / "economics"),
                    "--protocol",
                    str(protocol_path),
                    "--run-artifacts",
                    str(run_dir),
                    "--verified",
                ]
            )

        self.assertEqual(0, status)
        snapshot = json.loads(output.getvalue())
        self.assertTrue(snapshot["outcome"]["verified"])
        self.assertEqual(expected_usage.to_dict(), snapshot["usage"])
        self.assertEqual(2, snapshot["concurrency"])
        self.assertEqual(1, snapshot["model_calls"])
        self.assertEqual(
            {
                "bypassed": 0,
                "coalesced": 0,
                "hit": 0,
                "miss": 1,
                "none": 0,
                "saved_tokens": 0,
            },
            snapshot["reuse"],
        )
        self.assertEqual(0, snapshot["intervention"]["recoveries"])
        self.assertIsNone(snapshot["intervention"]["human_actions"])
        self.assertIsNone(snapshot["intervention"]["repair_loops"])
        self.assertGreaterEqual(snapshot["latency"]["wall_clock_seconds"], 0)
        self.assertGreaterEqual(snapshot["latency"]["queue_wait_seconds"], 0)
        self.assertGreaterEqual(snapshot["latency"]["model_seconds"], 0)
        self.assertGreaterEqual(
            snapshot["latency"]["critical_path_seconds"], 0
        )
        self.assertEqual(1, len(snapshot["nodes"]))
        node = snapshot["nodes"][0]
        self.assertEqual("work", node["node_id"])
        self.assertEqual("agent", node["kind"])
        self.assertEqual(expected_usage.to_dict(), node["usage"])
        self.assertEqual(1, node["attempts"])
        self.assertEqual(1, node["reuse"]["miss"])
        serialized = json.dumps(snapshot, ensure_ascii=False)
        self.assertNotIn("private benchmark prompt", serialized)
        self.assertNotIn(str(run_dir), serialized)

    def test_cli_reprices_an_existing_snapshot(self):
        root = self.root / "economics"
        RunEconomics(root).record(
            protocol(), run("cli-scenario", complete_usage()), prices()
        )
        catalog = prices(
            version="2026-10-01",
            effective_at="2026-10-01T00:00:00Z",
            input_usd_per_million=2,
            cached_input_usd_per_million=1,
            output_usd_per_million=4,
        )
        catalog_path = self.root / "scenario-prices.json"
        catalog_path.write_text(
            json.dumps(catalog.to_dict()), encoding="utf-8"
        )
        output = io.StringIO()

        with contextlib.redirect_stdout(output):
            status = main(
                [
                    "benchmark",
                    "reprice",
                    "--root",
                    str(root),
                    "--run-id",
                    "cli-scenario",
                    "--prices",
                    str(catalog_path),
                ]
            )

        value = json.loads(output.getvalue())
        self.assertEqual(0, status)
        self.assertAlmostEqual(
            0.000200, value["scenario_estimate"]["amount_usd"]
        )
        self.assertEqual(
            "2026-09-01",
            value["original_cost"]["estimated"]["catalog"]["version"],
        )

    def test_one_protocol_accepts_concurrency_one_and_two_artifacts(self):
        graph_one, run_one, _ = completed_control_run(self.root, concurrency=1)
        graph_two, run_two, _ = completed_control_run(self.root, concurrency=2)
        self.assertEqual(
            BenchmarkProtocol.graph_fingerprint(graph_one),
            BenchmarkProtocol.graph_fingerprint(graph_two),
        )
        benchmark = protocol(
            dag_fingerprint=BenchmarkProtocol.graph_fingerprint(graph_one),
            concurrency_matrix=(1, 2),
        )
        economics = RunEconomics(self.root / "economics")

        first = economics.record_control_run(benchmark, run_one, verified=True)
        second = economics.record_control_run(benchmark, run_two, verified=True)

        self.assertEqual(1, first["concurrency"])
        self.assertEqual(2, second["concurrency"])

    def test_control_artifact_latency_is_recomputed_from_event_boundaries(self):
        graph, run_dir, _ = completed_control_run(self.root)
        state_path = run_dir / "state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["submitted_at"] = 1_800_000_000.0
        state_path.write_text(json.dumps(state), encoding="utf-8")
        events_path = run_dir / "runtime" / "events.jsonl"
        events = [
            json.loads(line)
            for line in events_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        offsets = {
            "run_started": 0,
            "node_started": 2,
            "node_completed": 5,
            "run_completed": 7,
        }
        for event in events:
            offset = offsets.get(event["event"], 0)
            event["time"] = f"2027-01-15T08:00:0{offset}Z"
        events_path.write_text(
            "".join(json.dumps(event) + "\n" for event in events),
            encoding="utf-8",
        )

        snapshot = RunEconomics(self.root / "economics").record_control_run(
            protocol(dag_fingerprint=graph.fingerprint(), concurrency_matrix=(2,)),
            run_dir,
            verified=True,
        )

        self.assertEqual(7.0, snapshot["latency"]["wall_clock_seconds"])
        self.assertEqual(2.0, snapshot["latency"]["queue_wait_seconds"])
        self.assertEqual(3.0, snapshot["latency"]["model_seconds"])
        self.assertEqual(3.0, snapshot["latency"]["critical_path_seconds"])
        self.assertEqual(2.0, snapshot["nodes"][0]["queue_wait_seconds"])
        self.assertEqual(3.0, snapshot["nodes"][0]["execution_seconds"])

    def test_node_metric_boundaries_reject_duplicates_unknown_nodes_and_gaps(self):
        graph = GraphSpec.from_dict(
            {
                "id": "metric-boundaries",
                "require_reality_anchor": False,
                "nodes": [{"id": "work", "kind": "test"}],
            }
        )
        started = {
            "event": "node_started",
            "node_id": "work",
            "attempt": 1,
            "time": "2027-01-15T08:00:01Z",
        }
        failed = {
            "event": "node_failed",
            "node_id": "work",
            "attempt": 1,
            "time": "2027-01-15T08:00:02Z",
        }

        with self.assertRaisesRegex(ContractViolation, "duplicate boundaries"):
            RunEconomics._node_metrics(graph, [started, dict(started), failed], 0)
        with self.assertRaisesRegex(ContractViolation, "unknown node_id"):
            RunEconomics._node_metrics(
                graph,
                [{**started, "node_id": "unknown"}],
                0,
            )
        with self.assertRaisesRegex(ContractViolation, "incomplete boundaries"):
            RunEconomics._node_metrics(graph, [started], 0)

    def test_artifact_record_rejects_usage_disagreement(self):
        graph, run_dir, _ = completed_control_run(self.root)
        state_path = run_dir / "state.json"
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["result"]["usage"]["output_tokens"] += 1
        state_path.write_text(json.dumps(state), encoding="utf-8")

        with self.assertRaisesRegex(ContractViolation, "usage disagree"):
            RunEconomics(self.root / "economics").record_control_run(
                protocol(
                    dag_fingerprint=graph.fingerprint(), concurrency_matrix=(2,)
                ),
                run_dir,
                verified=True,
            )

    def test_cli_renders_report_and_supports_json(self):
        economics_root = self.root / "economics"
        economics = RunEconomics(economics_root, clock=lambda: 100.0)
        baseline = protocol(executor_version="baseline")
        candidate = protocol(executor_version="candidate")
        economics.record(
            baseline,
            run("baseline", exact_usage(100)),
            prices(
                effective_at="1970-01-01T00:00:00Z",
                valid_until="1970-01-01T00:00:01Z",
            ),
        )
        economics.record(candidate, run("candidate", exact_usage(70)))
        baseline_path = self.root / "baseline.json"
        candidate_path = self.root / "candidate.json"
        baseline_path.write_text(json.dumps(baseline.to_dict()), encoding="utf-8")
        candidate_path.write_text(json.dumps(candidate.to_dict()), encoding="utf-8")

        human_output = io.StringIO()
        with contextlib.redirect_stdout(human_output):
            status = main(
                [
                    "benchmark",
                    "report",
                    "--root",
                    str(economics_root),
                    "--baseline-protocol",
                    str(baseline_path),
                    "--candidate-protocol",
                    str(candidate_path),
                ]
            )
        self.assertEqual(0, status)
        self.assertIn("token reduction: 30.00%", human_output.getvalue())
        self.assertIn("cost ROI: cost sources are unknown", human_output.getvalue())
        self.assertIn(
            "warning: price catalog expired before benchmark time "
            "(baseline=1, candidate=0)",
            human_output.getvalue(),
        )

        json_output = io.StringIO()
        with contextlib.redirect_stdout(json_output):
            status = main(
                [
                    "benchmark",
                    "report",
                    "--root",
                    str(economics_root),
                    "--baseline-protocol",
                    str(baseline_path),
                    "--candidate-protocol",
                    str(candidate_path),
                    "--json",
                ]
            )
        self.assertEqual(0, status)
        self.assertEqual(0.3, json.loads(json_output.getvalue())["roi"]["token_reduction_fraction"])


if __name__ == "__main__":
    unittest.main()
