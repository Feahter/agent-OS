import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from .agents import (
    AgentRequest,
    AgentResult,
    ExecutorCapabilities,
    ExecutorRegistry,
    validate_agent_outputs,
)
from .errors import (
    AgentExecutionError,
    AgentProtocolError,
    AgentRateLimitError,
    AgentTimeoutError,
    ContractViolation,
)
from .routing import PolicyRouter
from .reuse import VerifiedArtifactCache


CANONICAL_TOOLS = ("edit", "read", "shell", "write")
_DISCOVERY_PROBE_TIMEOUT_SECONDS = 10


def _json_object(text: str) -> Mapping[str, Any]:
    candidate = text.strip()
    if candidate.startswith("```") and candidate.endswith("```"):
        lines = candidate.splitlines()
        if len(lines) >= 3:
            candidate = "\n".join(lines[1:-1]).strip()
    try:
        value = json.loads(candidate)
    except json.JSONDecodeError as error:
        raise AgentProtocolError(f"agent did not return a JSON object: {error}") from error
    if not isinstance(value, dict):
        raise AgentProtocolError("agent result must be a JSON object")
    return value


def _non_negative_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return 0
    return int(value)


def _prompt(request: AgentRequest) -> str:
    envelope = {
        "inputs": request.inputs,
        "required_outputs": list(request.output_keys),
    }
    return (
        f"{request.prompt}\n\n"
        "Graph execution contract:\n"
        f"{json.dumps(envelope, ensure_ascii=False, sort_keys=True)}\n"
        "Return only one JSON object whose keys exactly match required_outputs."
    )


class _CliExecutor:
    tool_map: Mapping[str, str] = {}

    def __init__(self, command: Sequence[str]):
        if not command:
            raise ContractViolation("agent command cannot be empty")
        self._command = tuple(command)

    def _mapped_tools(self, tools: Sequence[str]) -> Tuple[str, ...]:
        try:
            return tuple(self.tool_map[tool] for tool in tools)
        except KeyError as error:
            raise ContractViolation(
                f"executor {self.capabilities.executor_id} does not map tool {error.args[0]}"
            ) from error

    def _run(self, arguments: Sequence[str], request: AgentRequest) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(
                [*self._command, *arguments],
                cwd=str(request.workspace),
                capture_output=True,
                text=True,
                timeout=request.timeout_seconds,
                check=False,
            )
        except FileNotFoundError as error:
            raise AgentExecutionError(
                f"agent executable is unavailable: {self._command[0]}"
            ) from error
        except subprocess.TimeoutExpired as error:
            raise AgentTimeoutError(
                f"agent {self.capabilities.executor_id} timed out after "
                f"{request.timeout_seconds}s"
            ) from error

    @staticmethod
    def _require_success(completed: subprocess.CompletedProcess, executor_id: str) -> None:
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()
            if len(detail) > 1000:
                detail = detail[-1000:]
            normalized = detail.lower()
            if any(
                marker in normalized
                for marker in ("rate limit", "rate_limit", "too many requests", "429")
            ):
                raise AgentRateLimitError(
                    f"agent {executor_id} was rate limited: {detail}"
                )
            raise AgentExecutionError(
                f"agent {executor_id} exited with {completed.returncode}: {detail}"
            )


class ClaudeCodeExecutor(_CliExecutor):
    tool_map = {"read": "Read", "shell": "Bash", "edit": "Edit", "write": "Write"}

    @property
    def capabilities(self) -> ExecutorCapabilities:
        return ExecutorCapabilities(
            "claude-code",
            ("cost_budget", "model_selection", "structured_output", "token_usage", "tool_policy"),
            CANONICAL_TOOLS,
        )

    def execute(self, request: AgentRequest) -> AgentResult:
        schema = {
            "type": "object",
            "properties": {key: {} for key in request.output_keys},
            "required": list(request.output_keys),
            "additionalProperties": False,
        }
        mapped_tools = self._mapped_tools(request.tools)
        arguments = [
            "--print",
            "--output-format",
            "json",
            "--json-schema",
            json.dumps(schema, separators=(",", ":")),
            "--no-session-persistence",
            "--safe-mode",
            "--permission-mode",
            "dontAsk",
            "--tools",
            ",".join(mapped_tools),
        ]
        if request.model:
            arguments.extend(("--model", request.model))
        if request.max_cost_usd is not None:
            arguments.extend(("--max-budget-usd", str(request.max_cost_usd)))
        arguments.append(_prompt(request))
        completed = self._run(arguments, request)
        self._require_success(completed, self.capabilities.executor_id)
        try:
            envelope = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise AgentProtocolError(f"invalid Claude Code JSON result: {error}") from error
        if not isinstance(envelope, dict) or envelope.get("is_error") is True:
            raise AgentProtocolError("Claude Code returned an error result")
        structured = envelope.get("structured_output")
        raw_result = envelope.get("result", "")
        if isinstance(structured, dict):
            outputs = structured
            text = json.dumps(structured, ensure_ascii=False, sort_keys=True)
        elif isinstance(raw_result, str):
            outputs = _json_object(raw_result)
            text = raw_result
        else:
            raise AgentProtocolError("Claude Code result has no structured output")
        usage = envelope.get("usage") if isinstance(envelope.get("usage"), dict) else {}
        tokens = _non_negative_int(usage.get("total_tokens"))
        if not tokens:
            tokens = sum(
                _non_negative_int(usage.get(key))
                for key in (
                    "input_tokens",
                    "output_tokens",
                    "cache_creation_input_tokens",
                    "cache_read_input_tokens",
                )
            )
        cost = envelope.get("total_cost_usd")
        return AgentResult(
            self.capabilities.executor_id,
            validate_agent_outputs(outputs, request.output_keys),
            text,
            tokens,
            float(cost) if isinstance(cost, (int, float)) and not isinstance(cost, bool) else None,
            str(envelope["session_id"]) if envelope.get("session_id") else None,
        )


class PiAgentExecutor(_CliExecutor):
    tool_map = {"read": "read", "shell": "bash", "edit": "edit", "write": "write"}

    @property
    def capabilities(self) -> ExecutorCapabilities:
        return ExecutorCapabilities(
            "pi-agent",
            ("model_selection", "structured_output", "token_usage", "tool_policy"),
            CANONICAL_TOOLS,
        )

    def execute(self, request: AgentRequest) -> AgentResult:
        mapped_tools = self._mapped_tools(request.tools)
        arguments = [
            "--mode",
            "json",
            "--print",
            "--no-session",
            "--no-extensions",
            "--no-skills",
            "--no-prompt-templates",
            "--no-context-files",
            "--tools",
            ",".join(mapped_tools),
        ]
        if request.model:
            arguments.extend(("--model", request.model))
        arguments.append(_prompt(request))
        completed = self._run(arguments, request)
        self._require_success(completed, self.capabilities.executor_id)
        final_message: Optional[Mapping[str, Any]] = None
        for line in completed.stdout.splitlines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as error:
                raise AgentProtocolError(f"invalid Pi JSONL event: {error}") from error
            if (
                isinstance(event, dict)
                and event.get("type") == "message_end"
                and isinstance(event.get("message"), dict)
                and event["message"].get("role") == "assistant"
            ):
                final_message = event["message"]
        if final_message is None:
            raise AgentProtocolError("Pi event stream has no final assistant message")
        if final_message.get("stopReason") in ("error", "aborted"):
            raise AgentProtocolError(
                f"Pi returned {final_message.get('stopReason')}: "
                f"{final_message.get('errorMessage', '')}"
            )
        content = final_message.get("content")
        if not isinstance(content, list):
            raise AgentProtocolError("Pi assistant message has invalid content")
        text = "\n".join(
            item["text"]
            for item in content
            if isinstance(item, dict) and item.get("type") == "text" and isinstance(item.get("text"), str)
        )
        outputs = _json_object(text)
        usage = final_message.get("usage") if isinstance(final_message.get("usage"), dict) else {}
        tokens = _non_negative_int(usage.get("totalTokens"))
        cost_data = usage.get("cost") if isinstance(usage.get("cost"), dict) else {}
        cost = cost_data.get("total")
        return AgentResult(
            self.capabilities.executor_id,
            validate_agent_outputs(outputs, request.output_keys),
            text,
            tokens,
            float(cost) if isinstance(cost, (int, float)) and not isinstance(cost, bool) else None,
            None,
        )


class CodexExecutor(_CliExecutor):
    @property
    def capabilities(self) -> ExecutorCapabilities:
        return ExecutorCapabilities(
            "codex",
            ("model_selection", "structured_output", "token_usage", "tool_policy"),
            CANONICAL_TOOLS,
        )

    @staticmethod
    def _sandbox(tools: Sequence[str]) -> str:
        if set(tools) & {"edit", "shell", "write"}:
            return "workspace-write"
        return "read-only"

    def execute(self, request: AgentRequest) -> AgentResult:
        schema = {
            "type": "object",
            "properties": {key: {} for key in request.output_keys},
            "required": list(request.output_keys),
            "additionalProperties": False,
        }
        schema_path: Optional[Path] = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", suffix=".json", delete=False
            ) as handle:
                json.dump(schema, handle, ensure_ascii=False, separators=(",", ":"))
                schema_path = Path(handle.name)
            arguments = [
                "exec",
                "--json",
                "--ephemeral",
                "--sandbox",
                self._sandbox(request.tools),
                "--output-schema",
                str(schema_path),
            ]
            if request.model:
                arguments.extend(("--model", request.model))
            if not any((parent / ".git").exists() for parent in (request.workspace, *request.workspace.parents)):
                arguments.append("--skip-git-repo-check")
            arguments.append(_prompt(request))
            completed = self._run(arguments, request)
        finally:
            if schema_path is not None:
                schema_path.unlink(missing_ok=True)

        self._require_success(completed, self.capabilities.executor_id)
        final_text: Optional[str] = None
        session_id: Optional[str] = None
        usage: Mapping[str, Any] = {}
        for line in completed.stdout.splitlines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as error:
                raise AgentProtocolError(f"invalid Codex JSONL event: {error}") from error
            if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                raise AgentProtocolError("invalid Codex JSONL event envelope")
            event_type = event["type"]
            if event_type == "thread.started" and event.get("thread_id"):
                session_id = str(event["thread_id"])
            elif event_type == "item.completed" and isinstance(event.get("item"), dict):
                item = event["item"]
                if item.get("type") == "agent_message" and isinstance(item.get("text"), str):
                    final_text = item["text"]
            elif event_type == "turn.completed" and isinstance(event.get("usage"), dict):
                usage = event["usage"]
            elif event_type in ("turn.failed", "error"):
                detail = event.get("message") or event.get("error") or event_type
                raise AgentExecutionError(f"Codex reported {event_type}: {detail}")
        if final_text is None:
            raise AgentProtocolError("Codex event stream has no final agent message")
        tokens = _non_negative_int(usage.get("total_tokens"))
        if not tokens:
            tokens = _non_negative_int(usage.get("input_tokens")) + _non_negative_int(
                usage.get("output_tokens")
            )
        return AgentResult(
            self.capabilities.executor_id,
            validate_agent_outputs(_json_object(final_text), request.output_keys),
            final_text,
            tokens,
            None,
            session_id,
        )


def _codex_is_usable(command: str) -> bool:
    try:
        completed = subprocess.run(
            (command, "exec", "--help"),
            capture_output=True,
            text=True,
            timeout=_DISCOVERY_PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0


def discover_local_executors(
    router: Optional[PolicyRouter] = None,
    reuse_store: Optional[VerifiedArtifactCache] = None,
) -> ExecutorRegistry:
    registry = ExecutorRegistry(router, reuse_store)
    claude = shutil.which("claude")
    if claude:
        registry.register(ClaudeCodeExecutor((claude,)))
    pi = shutil.which("pi")
    if pi:
        registry.register(PiAgentExecutor((pi,)))
    codex = shutil.which("codex")
    if codex and _codex_is_usable(codex):
        registry.register(CodexExecutor((codex,)))
    return registry
