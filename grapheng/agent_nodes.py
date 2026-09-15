from pathlib import Path
from typing import Optional

from .agents import AgentExecution, AgentRequest, ExecutorRegistry
from .errors import ContractViolation
from .model import GraphSpec
from .runtime import NodeContext, NodeOutcome


class AgentNodeHandler:
    def __init__(
        self,
        graph: GraphSpec,
        executors: ExecutorRegistry,
        workspace: Path,
        reasoning_effort: Optional[str] = None,
    ):
        if not workspace.is_dir():
            raise ContractViolation(f"agent workspace does not exist: {workspace}")
        self._nodes = graph.node_map()
        self._executors = executors
        self._workspace = workspace.resolve()
        self._reasoning_effort = reasoning_effort

    @property
    def workspace_identity(self):
        stat = self._workspace.stat()
        return {
            "path": str(self._workspace),
            "device": stat.st_dev,
            "inode": stat.st_ino,
        }

    def __call__(self, context: NodeContext) -> NodeOutcome:
        node = self._nodes[context.node_id]
        spec = node.agent
        if spec is None:
            raise ContractViolation(f"node {node.id} has no agent configuration")
        if spec.workspace.mode != "shared":
            raise ContractViolation(
                f"agent node {node.id} requires an isolated workspace; "
                "use the Orca orchestration backend"
            )
        request = AgentRequest(
            task_id=context.effect_id or f"{context.run_id}:{node.id}:{context.attempt}",
            prompt=spec.prompt,
            inputs=context.inputs(),
            output_keys=node.writes,
            workspace=self._workspace,
            model=spec.model,
            tools=spec.tools,
            timeout_seconds=spec.timeout_seconds,
            max_tokens=node.max_tokens,
            max_cost_usd=spec.max_cost_usd,
            data_classification=spec.data_classification,
            task_type=spec.task_type,
            model_family=spec.model_family,
            reuse_scope=spec.reuse_scope,
            reasoning_effort=self._reasoning_effort,
        )
        result = self._executors.execute(
            request,
            executor_id=spec.executor,
            required_features=spec.required_capabilities,
        )
        return NodeOutcome(
            result.outputs,
            tokens_used=result.tokens_used,
            cost_usd=result.cost_usd,
            metadata={
                "executor_id": result.executor_id,
                "cost_usd": result.cost_usd,
                "session_id": result.session_id,
                "reuse_status": result.reuse_status,
                "reuse_saved_tokens": (
                    result.reuse_saved_tokens
                    if result.reuse_saved_tokens is not None
                    else 0
                    if result.reuse_status in ("none", "miss", "bypassed")
                    else None
                ),
                "source_task_id": result.source_task_id,
                "source_run_id": result.source_run_id,
                "verification_id": result.verification_id,
            },
            agent_execution=AgentExecution(request, result),
            usage=result.usage,
        )
