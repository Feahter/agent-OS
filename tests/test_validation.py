import unittest

from grapheng import GraphSpec, GraphValidationError, validate_graph


def graph(nodes, **overrides):
    value = {
        "id": "test-graph",
        "require_reality_anchor": False,
        "nodes": nodes,
    }
    value.update(overrides)
    return GraphSpec.from_dict(value)


class ValidationTests(unittest.TestCase):
    def test_unknown_effect_type_fails_closed(self):
        with self.assertRaisesRegex(GraphValidationError, "must be one of"):
            graph(
                [
                    {
                        "id": "work",
                        "kind": "work",
                        "writes": ["result"],
                        "effect": "best_effort",
                    }
                ]
            )

    def test_shared_mutating_agent_requires_explicit_effect_contract(self):
        with self.assertRaisesRegex(
            GraphValidationError, "explicit effect contract"
        ):
            validate_graph(
                GraphSpec.from_dict(
                    {
                    "id": "unsafe-shared-effect",
                    "require_reality_anchor": False,
                    "nodes": [
                        {
                            "id": "change",
                            "kind": "agent",
                            "writes": ["result"],
                            "agent": {
                                "prompt": "change it",
                                "tools": ["write"],
                                "workspace": {"mode": "shared"},
                            },
                        }
                    ],
                    }
                )
            )

    @staticmethod
    def controlled_merge_nodes(**overrides):
        source = {
            "id": "change",
            "kind": "agent",
            "writes": ["change_result"],
            "agent": {
                "prompt": "make change",
                "workspace": {"mode": "isolated"},
            },
            "controlled_merge": {
                "verifier": "verify",
                "target_branch": "main",
            },
        }
        verifier = {
            "id": "verify",
            "kind": "agent",
            "deps": ["change"],
            "reads": ["change_result"],
            "writes": ["verification"],
            "gate": "merge-approval",
            "verifier_for": "change",
            "reality_anchor": True,
            "verified_reuse": {
                "decision_artifact": "verification",
                "passed_path": ["passed"],
                "quality_path": ["quality"],
            },
            "agent": {"prompt": "verify change"},
        }
        if "source_workspace" in overrides:
            source["agent"]["workspace"] = overrides["source_workspace"]
        if "verifier_gate" in overrides:
            verifier["gate"] = overrides["verifier_gate"]
        if "verifier_for" in overrides:
            verifier["verifier_for"] = overrides["verifier_for"]
        return [source, verifier]

    def test_accepts_valid_verifier_topology(self):
        spec = graph(
            [
                {"id": "build", "kind": "build", "writes": ["draft"]},
                {
                    "id": "verify",
                    "kind": "verify",
                    "deps": ["build"],
                    "reads": ["draft"],
                    "writes": ["verified"],
                    "verifier_for": "build",
                },
            ]
        )
        validate_graph(spec)

    def test_rejects_cycle(self):
        spec = graph(
            [
                {"id": "a", "kind": "work", "deps": ["b"]},
                {"id": "b", "kind": "work", "deps": ["a"]},
            ]
        )
        with self.assertRaisesRegex(GraphValidationError, "cycle"):
            validate_graph(spec)

    def test_rejects_unordered_writers(self):
        spec = graph(
            [
                {"id": "a", "kind": "work", "writes": ["shared"]},
                {"id": "b", "kind": "work", "writes": ["shared"]},
            ]
        )
        with self.assertRaisesRegex(GraphValidationError, "unordered nodes"):
            validate_graph(spec)

    def test_requires_reality_anchor_by_default(self):
        spec = GraphSpec.from_dict({"id": "grounded", "nodes": [{"id": "a", "kind": "work"}]})
        with self.assertRaisesRegex(GraphValidationError, "reality_anchor"):
            validate_graph(spec)

    def test_rejects_read_without_upstream_producer(self):
        spec = graph(
            [{"id": "consumer", "kind": "work", "reads": ["missing"]}]
        )

        with self.assertRaisesRegex(GraphValidationError, "without an upstream producer"):
            validate_graph(spec)

    def test_verifier_must_read_verified_output(self):
        spec = graph(
            [
                {"id": "build", "kind": "build", "writes": ["draft"]},
                {
                    "id": "verify",
                    "kind": "verify",
                    "deps": ["build"],
                    "writes": ["verdict"],
                    "verifier_for": "build",
                },
            ]
        )

        with self.assertRaisesRegex(GraphValidationError, "must read an artifact"):
            validate_graph(spec)

    def test_verified_reuse_requires_explicit_grounded_contract(self):
        spec = graph(
            [
                {
                    "id": "build",
                    "kind": "agent",
                    "writes": ["draft", "notes"],
                    "agent": {"prompt": "build"},
                },
                {
                    "id": "verify",
                    "kind": "verify",
                    "deps": ["build"],
                    "reads": ["draft"],
                    "writes": ["decision"],
                    "verifier_for": "build",
                    "verified_reuse": {
                        "decision_artifact": "decision",
                        "passed_path": ["passed"],
                        "quality_path": ["quality_score"],
                    },
                },
            ]
        )

        with self.assertRaisesRegex(
            GraphValidationError, "must read all outputs.*notes"
        ):
            validate_graph(spec)
        with self.assertRaisesRegex(GraphValidationError, "must be a reality_anchor"):
            validate_graph(spec)

    def test_verified_reuse_rejects_sensitive_source(self):
        spec = graph(
            [
                {
                    "id": "build",
                    "kind": "agent",
                    "writes": ["draft"],
                    "agent": {
                        "prompt": "build",
                        "data_classification": "restricted",
                    },
                },
                {
                    "id": "verify",
                    "kind": "verify",
                    "deps": ["build"],
                    "reads": ["draft"],
                    "writes": ["decision"],
                    "verifier_for": "build",
                    "reality_anchor": True,
                    "verified_reuse": {
                        "decision_artifact": "decision",
                        "passed_path": ["passed"],
                        "quality_path": ["quality_score"],
                    },
                },
            ]
        )

        with self.assertRaisesRegex(GraphValidationError, "classification=restricted"):
            validate_graph(spec)

    def test_all_terminal_paths_must_be_grounded(self):
        spec = GraphSpec.from_dict(
            {
                "id": "partly-grounded",
                "nodes": [
                    {"id": "anchor", "kind": "work", "reality_anchor": True},
                    {"id": "ungrounded", "kind": "work"},
                ],
            }
        )

        with self.assertRaisesRegex(GraphValidationError, "terminal node ungrounded"):
            validate_graph(spec)

    def test_agent_node_requires_configuration(self):
        spec = graph([{"id": "worker", "kind": "agent", "writes": ["result"]}])

        with self.assertRaisesRegex(GraphValidationError, "requires an agent configuration"):
            validate_graph(spec)

    def test_agent_node_rejects_non_canonical_tool(self):
        spec = graph(
            [
                {
                    "id": "worker",
                    "kind": "agent",
                    "writes": ["result"],
                    "agent": {"prompt": "work", "tools": ["sudo"]},
                }
            ]
        )

        with self.assertRaisesRegex(GraphValidationError, "unsupported canonical tools"):
            validate_graph(spec)

    def test_mutating_agent_defaults_to_isolated_workspace(self):
        spec = graph(
            [
                {
                    "id": "writer",
                    "kind": "agent",
                    "writes": ["result"],
                    "agent": {"prompt": "write", "tools": ["write"]},
                }
            ]
        )

        self.assertEqual("isolated", spec.nodes[0].agent.workspace.mode)

    def test_unordered_agents_cannot_share_mutable_workspace(self):
        spec = graph(
            [
                {
                    "id": "writer",
                    "kind": "agent",
                    "writes": ["a"],
                    "agent": {
                        "prompt": "write",
                        "tools": ["write"],
                        "workspace": {"mode": "shared"},
                    },
                },
                {
                    "id": "reader",
                    "kind": "agent",
                    "writes": ["b"],
                    "agent": {"prompt": "read", "tools": ["read"]},
                },
            ]
        )

        with self.assertRaisesRegex(GraphValidationError, "may race in a shared workspace"):
            validate_graph(spec)

    def test_unordered_agents_may_use_isolated_workspaces(self):
        spec = graph(
            [
                {
                    "id": "one",
                    "kind": "agent",
                    "writes": ["one"],
                    "agent": {
                        "prompt": "write one",
                        "tools": ["write"],
                        "workspace": {"mode": "isolated"},
                    },
                },
                {
                    "id": "two",
                    "kind": "agent",
                    "writes": ["two"],
                    "agent": {
                        "prompt": "write two",
                        "tools": ["write"],
                        "workspace": {"mode": "isolated", "lineage": "top-level"},
                    },
                },
            ]
        )

        validate_graph(spec)

    def test_graph_dollar_budget_requires_agent_hard_cap(self):
        spec = graph(
            [
                {
                    "id": "worker",
                    "kind": "agent",
                    "writes": ["result"],
                    "agent": {"prompt": "work"},
                }
            ],
            max_cost_usd=1.0,
        )

        with self.assertRaisesRegex(GraphValidationError, "requires max_cost_usd"):
            validate_graph(spec)

    def test_graph_token_budget_requires_agent_hard_cap(self):
        spec = graph(
            [
                {
                    "id": "worker",
                    "kind": "agent",
                    "writes": ["result"],
                    "agent": {"prompt": "work"},
                }
            ],
            max_tokens=100,
        )

        with self.assertRaisesRegex(GraphValidationError, "requires max_tokens"):
            validate_graph(spec)

    def test_controlled_merge_requires_isolated_source_workspace(self):
        spec = graph(
            self.controlled_merge_nodes(source_workspace={"mode": "shared"})
        )

        with self.assertRaisesRegex(GraphValidationError, "isolated agent workspace"):
            validate_graph(spec)

    def test_controlled_merge_defaults_to_reconcilable_effect(self):
        spec = graph(self.controlled_merge_nodes())

        self.assertEqual("reconcilable", spec.node_map()["change"].effect)

    def test_controlled_merge_requires_existing_gate_on_exact_verifier(self):
        missing_gate = graph(
            self.controlled_merge_nodes(verifier_gate=None)
        )
        wrong_verifier = graph(
            self.controlled_merge_nodes(verifier_for="other")
        )

        with self.assertRaisesRegex(GraphValidationError, "requires an approval gate"):
            validate_graph(missing_gate)
        with self.assertRaisesRegex(GraphValidationError, "must verify change"):
            validate_graph(wrong_verifier)

    def test_controlled_merge_rejects_ambiguous_shared_verifier(self):
        nodes = self.controlled_merge_nodes()
        nodes.insert(
            1,
            {
                "id": "second-change",
                "kind": "agent",
                "writes": ["second_result"],
                "agent": {
                    "prompt": "make another change",
                    "workspace": {"mode": "isolated"},
                },
                "controlled_merge": {
                    "verifier": "verify",
                    "target_branch": "main",
                },
            },
        )
        nodes[-1]["deps"].append("second-change")
        nodes[-1]["reads"].append("second_result")

        with self.assertRaisesRegex(GraphValidationError, "ambiguous for sources"):
            validate_graph(graph(nodes))


if __name__ == "__main__":
    unittest.main()
