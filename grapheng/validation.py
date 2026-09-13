from collections import defaultdict, deque
from typing import Dict, List, Mapping, Set, Tuple

from .errors import GraphValidationError
from .model import GraphSpec, NodeSpec

CANONICAL_AGENT_TOOLS = {"edit", "read", "shell", "write"}


def _topological_order(nodes: Mapping[str, NodeSpec]) -> Tuple[List[str], List[str]]:
    indegree = dict.fromkeys(nodes, 0)
    children = defaultdict(list)
    issues = []
    for node in nodes.values():
        for dep in node.deps:
            if dep not in nodes:
                issues.append(f"node {node.id} depends on missing node {dep}")
                continue
            indegree[node.id] += 1
            children[dep].append(node.id)

    queue = deque(sorted(node_id for node_id, degree in indegree.items() if degree == 0))
    order = []
    while queue:
        node_id = queue.popleft()
        order.append(node_id)
        for child in sorted(children[node_id]):
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)
    if len(order) != len(nodes) and not issues:
        cyclic = sorted(node_id for node_id, degree in indegree.items() if degree > 0)
        issues.append(f"graph contains a cycle involving: {', '.join(cyclic)}")
    return order, issues


def _ancestors(order: List[str], nodes: Mapping[str, NodeSpec]) -> Dict[str, Set[str]]:
    result: Dict[str, Set[str]] = {node_id: set() for node_id in nodes}
    for node_id in order:
        for dep in nodes[node_id].deps:
            result[node_id].add(dep)
            result[node_id].update(result[dep])
    return result


def validation_issues(graph: GraphSpec) -> Tuple[str, ...]:
    issues = []
    node_ids = tuple(graph.node_ids())
    if len(node_ids) != len(set(node_ids)):
        issues.append("node ids must be unique")
    if issues:
        return tuple(issues)

    nodes = graph.node_map()
    order, topology_issues = _topological_order(nodes)
    issues.extend(topology_issues)
    if topology_issues:
        return tuple(issues)

    ancestor_map = _ancestors(order, nodes)
    writers = defaultdict(set)
    children = defaultdict(set)
    merge_sources_by_verifier = defaultdict(set)
    for node in graph.nodes:
        for artifact in node.writes:
            writers[artifact].add(node.id)
        for dependency in node.deps:
            children[dependency].add(node.id)

    for node in graph.nodes:
        if node.id in node.deps:
            issues.append(f"node {node.id} cannot depend on itself")
        if set(node.reads) & set(node.writes):
            overlap = sorted(set(node.reads) & set(node.writes))
            issues.append(f"node {node.id} reads and writes the same artifacts: {', '.join(overlap)}")
        if node.max_tokens is not None and node.estimated_tokens > node.max_tokens:
            issues.append(f"node {node.id} estimated_tokens exceeds max_tokens")
        if (
            node.agent is not None
            and node.agent.max_cost_usd is not None
            and node.estimated_cost_usd > node.agent.max_cost_usd
        ):
            issues.append(f"node {node.id} estimated_cost_usd exceeds agent.max_cost_usd")
        if node.kind == "agent" and node.agent is None:
            issues.append(f"agent node {node.id} requires an agent configuration")
        if node.kind != "agent" and node.agent is not None:
            issues.append(f"non-agent node {node.id} cannot have an agent configuration")
        if node.agent is not None:
            if graph.max_tokens is not None and node.max_tokens is None:
                issues.append(
                    f"agent node {node.id} requires max_tokens when graph max_tokens is set"
                )
            if graph.max_cost_usd is not None and node.agent.max_cost_usd is None:
                issues.append(
                    f"agent node {node.id} requires max_cost_usd when graph max_cost_usd is set"
                )
            unsupported_tools = sorted(set(node.agent.tools) - CANONICAL_AGENT_TOOLS)
            if unsupported_tools:
                issues.append(
                    f"agent node {node.id} uses unsupported canonical tools: "
                    f"{', '.join(unsupported_tools)}"
                )
            if not node.writes:
                issues.append(f"agent node {node.id} must declare at least one output")
            if (
                node.agent.workspace.mode == "shared"
                and node.agent.workspace.lineage != "child"
            ):
                issues.append(
                    f"agent node {node.id} cannot set workspace.lineage for shared mode"
                )
        for artifact in node.reads:
            upstream_writers = writers[artifact] & ancestor_map[node.id]
            if not upstream_writers:
                issues.append(
                    f"node {node.id} reads artifact {artifact} without an upstream producer"
                )
        if node.verifier_for is not None:
            if node.verifier_for not in nodes:
                issues.append(f"node {node.id} verifies missing node {node.verifier_for}")
            elif node.verifier_for not in ancestor_map[node.id]:
                issues.append(f"verifier {node.id} must depend on {node.verifier_for}")
            elif not (set(node.reads) & set(nodes[node.verifier_for].writes)):
                issues.append(
                    f"verifier {node.id} must read an artifact written by {node.verifier_for}"
                )
        if node.verified_reuse is not None:
            publication = node.verified_reuse
            if node.verifier_for is None:
                issues.append(
                    f"verified reuse publisher {node.id} requires verifier_for"
                )
            elif node.verifier_for in nodes:
                source = nodes[node.verifier_for]
                if source.agent is None:
                    issues.append(
                        f"verified reuse publisher {node.id} must verify an agent node"
                    )
                else:
                    missing_outputs = sorted(set(source.writes) - set(node.reads))
                    if missing_outputs:
                        issues.append(
                            f"verified reuse publisher {node.id} must read all outputs of "
                            f"{source.id}: {', '.join(missing_outputs)}"
                        )
                    if source.agent.data_classification not in ("public", "internal"):
                        issues.append(
                            f"verified reuse publisher {node.id} cannot publish "
                            f"classification={source.agent.data_classification}"
                        )
                    for artifact in source.writes:
                        intervening = sorted(
                            writer
                            for writer in writers[artifact]
                            if writer != source.id
                            and source.id in ancestor_map[writer]
                            and writer in ancestor_map[node.id]
                        )
                        if intervening:
                            issues.append(
                                f"verified reuse publisher {node.id} cannot verify overwritten "
                                f"artifact {artifact}: {', '.join(intervening)}"
                            )
            if not node.reality_anchor:
                issues.append(
                    f"verified reuse publisher {node.id} must be a reality_anchor"
                )
            if publication.decision_artifact not in node.writes:
                issues.append(
                    f"verified reuse publisher {node.id} must write decision artifact "
                    f"{publication.decision_artifact}"
                )
        if node.controlled_merge is not None:
            merge = node.controlled_merge
            merge_sources_by_verifier[merge.verifier].add(node.id)
            verifier = nodes.get(merge.verifier)
            if node.agent is None or node.agent.workspace.mode != "isolated":
                issues.append(
                    f"controlled merge source {node.id} must use an isolated agent workspace"
                )
            if verifier is None:
                issues.append(
                    f"controlled merge source {node.id} names missing verifier {merge.verifier}"
                )
            else:
                if verifier.verifier_for != node.id:
                    issues.append(
                        f"controlled merge verifier {verifier.id} must verify {node.id}"
                    )
                if verifier.verified_reuse is None or not verifier.reality_anchor:
                    issues.append(
                        f"controlled merge verifier {verifier.id} must be a verified Reality Anchor"
                    )
                if verifier.gate is None:
                    issues.append(
                        f"controlled merge verifier {verifier.id} requires an approval gate"
                    )

    for verifier_id, source_ids in sorted(merge_sources_by_verifier.items()):
        if len(source_ids) > 1:
            issues.append(
                f"controlled merge verifier {verifier_id} is ambiguous for sources: "
                f"{', '.join(sorted(source_ids))}"
            )

    for index, left in enumerate(graph.nodes):
        for right in graph.nodes[index + 1 :]:
            overlap = sorted(set(left.writes) & set(right.writes))
            ordered = left.id in ancestor_map[right.id] or right.id in ancestor_map[left.id]
            if overlap and not ordered:
                issues.append(
                    f"unordered nodes {left.id} and {right.id} write the same artifacts: {', '.join(overlap)}"
                )
            left_mutates = left.agent is not None and bool(
                set(left.agent.tools) & {"edit", "shell", "write"}
            )
            right_mutates = right.agent is not None and bool(
                set(right.agent.tools) & {"edit", "shell", "write"}
            )
            if (
                left.agent is not None
                and right.agent is not None
                and left.agent.workspace.mode == "shared"
                and right.agent.workspace.mode == "shared"
                and (left_mutates or right_mutates)
                and not ordered
            ):
                issues.append(
                    f"unordered agent nodes {left.id} and {right.id} may race in a shared workspace"
                )

    if graph.require_reality_anchor:
        anchors = {node.id for node in graph.nodes if node.reality_anchor}
        if not anchors:
            issues.append("graph requires at least one reality_anchor node")
        else:
            for terminal in sorted(node.id for node in graph.nodes if not children[node.id]):
                if not (anchors & (ancestor_map[terminal] | {terminal})):
                    issues.append(
                        f"terminal node {terminal} is not grounded by a reality_anchor"
                    )
    return tuple(issues)


def validate_graph(graph: GraphSpec) -> None:
    issues = validation_issues(graph)
    if issues:
        raise GraphValidationError(issues)
