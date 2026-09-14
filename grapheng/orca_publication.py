"""Verified-result publication for Orca-coordinated runs.

Publication is advisory: a result enters the verified cache only after a
Reality Anchor accepted it, and a publication failure must never fail the run
that produced the result. That "best effort, always report, never raise"
policy is easy to get wrong when it is interleaved with scheduling code, so it
lives here behind one small collaborator.

The recorder depends on the graph, the publisher and two callbacks - how to
emit a coordinator event and how to resolve a node's workspace. It never
touches the coordinator's state document beyond passing it back to ``emit``,
which keeps the seam narrow enough to test on its own.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping, Optional, Sequence

from .agents import AgentExecution, AgentRequest
from .artifacts import ArtifactRecord, ArtifactStore
from .model import GraphSpec, NodeSpec
from .publication import PublicationEvent, VerifiedResultPublisher

#: Event emitted when publication could not complete. The run is unaffected.
DEFERRED_EVENT = "verified_result_publish_deferred"

EmitCallback = Callable[
    [Mapping[str, Any], str, Optional[str], Optional[int], Optional[Mapping[str, Any]]],
    None,
]


class OrcaPublicationRecorder:
    """Stages, verifies and reconciles verified results for one run."""

    def __init__(
        self,
        graph: GraphSpec,
        publisher: Optional[VerifiedResultPublisher],
        workspace_for: Callable[[NodeSpec, Optional[str]], Any],
        emit: EmitCallback,
    ):
        self.graph = graph
        self.publisher = publisher
        self._workspace_for = workspace_for
        self._emit = emit

    @property
    def enabled(self) -> bool:
        return self.publisher is not None

    def record(
        self,
        state: Mapping[str, Any],
        node: NodeSpec,
        attempt: int,
        result: Any,
        input_records: Sequence[ArtifactRecord],
        output_records: Sequence[ArtifactRecord],
        artifacts: ArtifactStore,
        workspace_id: Optional[str],
    ) -> None:
        """Stage one node result and let the verifier observe it."""

        if self.publisher is None or node.agent is None:
            return
        try:
            execution = AgentExecution(
                self._request(state, node, attempt, input_records, workspace_id),
                result,
            )
            self._publish(
                state,
                self.publisher.stage(
                    self.graph,
                    state["run_id"],
                    node,
                    attempt,
                    execution,
                    input_records,
                    output_records,
                ),
            )
            self._publish(
                state,
                self.publisher.observe_verifier(
                    self.graph,
                    artifacts,
                    state["run_id"],
                    node,
                    attempt,
                    input_records,
                    output_records,
                ),
            )
        except Exception as error:
            self._defer(state, error, node.id, attempt)

    def reconcile(self, state: Mapping[str, Any], artifacts: ArtifactStore) -> None:
        """Replay publications that a previous process left unfinished."""

        if self.publisher is None:
            return
        try:
            for event in self.publisher.reconcile(
                self.graph, artifacts, state["run_id"]
            ):
                self._publish(state, event)
        except Exception as error:
            self._defer(state, error, None, None)

    def _request(
        self,
        state: Mapping[str, Any],
        node: NodeSpec,
        attempt: int,
        input_records: Sequence[ArtifactRecord],
        workspace_id: Optional[str],
    ) -> AgentRequest:
        assert node.agent is not None
        return AgentRequest(
            task_id=f"{state['run_id']}:{node.id}:{attempt}",
            prompt=node.agent.prompt,
            inputs={record.key: record.value for record in input_records},
            output_keys=node.writes,
            workspace=self._workspace_for(node, workspace_id),
            model=node.agent.model,
            tools=node.agent.tools,
            timeout_seconds=node.agent.timeout_seconds,
            max_tokens=node.max_tokens,
            max_cost_usd=node.agent.max_cost_usd,
            data_classification=node.agent.data_classification,
            task_type=node.agent.task_type,
            model_family=node.agent.model_family,
            reuse_scope=node.agent.reuse_scope,
            reuse_allowed=(
                node.controlled_merge is None
                and node.agent.workspace.mode == "shared"
            ),
        )

    def _publish(
        self, state: Mapping[str, Any], event: Optional[PublicationEvent]
    ) -> None:
        if event is not None:
            self._emit(state, event.event, event.node_id, event.attempt, event.payload)

    def _defer(
        self,
        state: Mapping[str, Any],
        error: BaseException,
        node_id: Optional[str],
        attempt: Optional[int],
    ) -> None:
        self._emit(
            state,
            DEFERRED_EVENT,
            node_id,
            attempt,
            {"reason": f"{type(error).__name__}: {error}"},
        )
