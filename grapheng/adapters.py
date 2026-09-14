import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, Dict, Mapping, Optional, Sequence, Tuple

from . import telemetry
from .agents import (
    AgentRequest,
    AgentResult,
    ExecutorCapabilities,
    ExecutorRegistry,
    ModelUsage,
    validate_agent_outputs,
)
from .context_compiler import (
    DEFAULT_MAX_CONTEXT_BYTES,
    CompiledContext,
    ContextBudgetExceeded,
    ContextCompiler,
    ContextPolicy,
)
from .errors import (
    AgentExecutionError,
    AgentProtocolError,
    AgentRateLimitError,
    AgentTimeoutError,
    ContractViolation,
)
from .reuse import VerifiedArtifactCache
from .routing import PolicyRouter

CANONICAL_TOOLS = ("edit", "read", "shell", "write")
_DISCOVERY_PROBE_TIMEOUT_SECONDS = 10
_ENV_MAX_OUTPUT_BYTES = "AGENT_OS_MAX_AGENT_OUTPUT_BYTES"
_DEFAULT_MAX_OUTPUT_BYTES = 8 * 1024 * 1024
_DETAIL_CHARS = 1000
_RATE_LIMIT_MARKERS = (
    "rate limit",
    "rate_limit",
    "too many requests",
    "429",
)
_CLAUDE_REQUIRED_HELP_FLAGS = (
    "--print",
    "--output-format",
    "--json-schema",
    "--no-session-persistence",
    "--permission-mode",
    "--tools",
    "--max-budget-usd",
)


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


def _mapping(value: Any) -> Mapping[str, Any]:
    """Return ``value`` when it is a JSON object, otherwise an empty mapping."""

    return value if isinstance(value, dict) else {}


def _non_negative_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return 0
    return int(value)


def _optional_non_negative_int(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _max_output_bytes() -> int:
    """Return the per-stream output ceiling for one Agent call."""

    raw = os.environ.get(_ENV_MAX_OUTPUT_BYTES)
    if raw is None:
        return _DEFAULT_MAX_OUTPUT_BYTES
    try:
        value = int(raw)
    except ValueError as error:
        raise ContractViolation(
            f"{_ENV_MAX_OUTPUT_BYTES} must be a positive integer"
        ) from error
    if value < 1024:
        raise ContractViolation(
            f"{_ENV_MAX_OUTPUT_BYTES} must be at least 1024 bytes"
        )
    return value


class AgentOutputTooLargeError(AgentProtocolError):
    """An Agent produced more output than the configured ceiling allows."""


@dataclass(frozen=True)
class FailureClassification:
    """Why an Agent call is considered failed, and how confident that is.

    ``source`` records the evidence used:

    * ``exit_code`` - the Adapter declares this exit code's meaning,
    * ``structured`` - the Agent's own machine-readable error payload,
    * ``heuristic`` - a substring match on human-readable output, the last
      resort. Circuit-breaker and routing decisions consume ``kind``, so the
      weaker evidence is always recorded rather than hidden.
    """

    kind: str
    source: str
    detail: str

    def raise_for(self, executor_id: str) -> None:
        telemetry.emit_failure(
            "agent.failure_classified",
            executor_id=executor_id,
            failure_kind=self.kind,
            classification_source=self.source,
            detail=self.detail,
        )
        if self.kind == "rate_limit":
            raise AgentRateLimitError(
                f"agent {executor_id} was rate limited: {self.detail}"
            )
        if self.kind == "timeout":
            raise AgentTimeoutError(f"agent {executor_id} timed out: {self.detail}")
        raise AgentExecutionError(f"agent {executor_id} failed: {self.detail}")


def _bounded_detail(text: str) -> str:
    detail = text.strip()
    return detail[-_DETAIL_CHARS:] if len(detail) > _DETAIL_CHARS else detail


#: Exit codes with a POSIX or shell-defined meaning, shared by every Adapter.
_POSIX_EXIT_FAILURES: Mapping[int, str] = {
    124: "timeout",  # GNU timeout(1) convention
    126: "execution",  # found but not executable
    127: "execution",  # command not found
    -9: "execution",  # SIGKILL, commonly the OOM killer
    -15: "execution",  # SIGTERM
}


def _heuristic_kind(text: str) -> Optional[str]:
    normalized = text.lower()
    if any(marker in normalized for marker in _RATE_LIMIT_MARKERS):
        return "rate_limit"
    return None


def _read_stream_bounded(
    stream, limit: int, overflow: threading.Event
) -> Tuple[str, int]:
    """Drain ``stream`` up to ``limit`` bytes, flagging overflow.

    Reading incrementally keeps a runaway Agent from filling memory, and the
    shared ``overflow`` event lets the caller terminate the process as soon as
    either stream crosses the ceiling.
    """

    chunks = []
    total = 0
    while True:
        chunk = stream.read(65536)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            overflow.set()
            break
        chunks.append(chunk)
    return b"".join(chunks).decode("utf-8", errors="replace"), total


def _terminate(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


def _await_process(
    process: subprocess.Popen,
    executor_id: str,
    timeout_seconds: float,
    overflow: threading.Event,
) -> int:
    """Wait for the Agent, terminating it on timeout or output overflow."""

    deadline = time.monotonic() + float(timeout_seconds)
    while True:
        if overflow.is_set():
            _terminate(process)
            return process.wait()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _terminate(process)
            raise AgentTimeoutError(
                f"agent {executor_id} timed out after {timeout_seconds}s"
            )
        try:
            return process.wait(timeout=min(0.1, remaining))
        except subprocess.TimeoutExpired:
            continue


def run_bounded_process(
    command: Sequence[str],
    *,
    cwd: str,
    env: Mapping[str, str],
    timeout_seconds: float,
    executor_id: str,
    max_output_bytes: int,
) -> subprocess.CompletedProcess:
    """Run one Agent CLI call under a hard timeout and a hard output ceiling.

    ``subprocess.run(capture_output=True)`` buffers the whole stream in memory,
    so a looping Agent could exhaust the host before the timeout fired. Here
    both streams are drained incrementally by reader threads and the process is
    terminated as soon as either crosses ``max_output_bytes``. A truncated
    event stream is never returned as a result: crossing the ceiling raises,
    because a partial stream cannot be shown to carry a complete answer.

    This is the single process seam for every Adapter, which also makes Agent
    process behavior testable without spawning real Agents.
    """

    try:
        process = subprocess.Popen(
            list(command),
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=dict(env),
        )
    except FileNotFoundError as error:
        raise AgentExecutionError(
            f"agent executable is unavailable: {command[0]}"
        ) from error

    overflow = threading.Event()
    captured: Dict[str, Tuple[str, int]] = {}

    def drain(name: str, stream) -> None:
        try:
            captured[name] = _read_stream_bounded(stream, max_output_bytes, overflow)
        finally:
            stream.close()

    readers = [
        threading.Thread(
            target=drain, args=(name, stream), name=f"grapheng-{name}", daemon=True
        )
        for name, stream in (("stdout", process.stdout), ("stderr", process.stderr))
        if stream is not None
    ]
    for reader in readers:
        reader.start()
    try:
        returncode = _await_process(
            process, executor_id, timeout_seconds, overflow
        )
    finally:
        for reader in readers:
            reader.join(timeout=5)
    stdout, stdout_bytes = captured.get("stdout", ("", 0))
    stderr, stderr_bytes = captured.get("stderr", ("", 0))
    if overflow.is_set():
        raise AgentOutputTooLargeError(
            f"agent {executor_id} exceeded the {max_output_bytes} byte output "
            f"ceiling; raise {_ENV_MAX_OUTPUT_BYTES} only if the stream is trusted"
        )
    telemetry.emit(
        "agent.process_finished",
        executor_id=executor_id,
        returncode=returncode,
        stdout_bytes=stdout_bytes,
        stderr_bytes=stderr_bytes,
    )
    return subprocess.CompletedProcess(list(command), returncode, stdout, stderr)


class CliAgentAdapter(ABC):
    """Shared process, timeout, fault, environment, and tool-mapping Adapter kit."""

    tool_map: ClassVar[Mapping[str, str]] = {}
    native_output_schema: ClassVar[bool] = False

    #: Exit codes whose meaning this Adapter declares explicitly. Anything not
    #: listed here falls through to the Agent's structured error payload and
    #: then, only as a last resort, to substring heuristics.
    #:
    #: Only POSIX-defined codes are declared here. Per-Agent codes belong in
    #: the subclass and must be verified against that Agent before being added;
    #: an invented mapping is worse than an honest heuristic, because routing
    #: and the circuit breaker trust ``source == "exit_code"``.
    exit_code_failures: ClassVar[Mapping[int, str]] = _POSIX_EXIT_FAILURES

    def __init__(
        self,
        command: Sequence[str],
        *,
        max_context_bytes: Optional[int] = DEFAULT_MAX_CONTEXT_BYTES,
    ):
        if not command:
            raise ContractViolation("agent command cannot be empty")
        self._command = tuple(command)
        policy = ContextPolicy(max_context_bytes=max_context_bytes)
        self._max_context_bytes = policy.max_context_bytes
        self._context_compiler = ContextCompiler()

    @property
    def command(self) -> Tuple[str, ...]:
        return self._command

    @property
    @abstractmethod
    def capabilities(self) -> ExecutorCapabilities:
        """Describe the normalized features and tools provided by this Adapter."""

    @abstractmethod
    def execute(self, request: AgentRequest) -> AgentResult:
        """Execute one normalized Agent OS request."""

    def map_tools(self, tools: Sequence[str]) -> Tuple[str, ...]:
        try:
            return tuple(self.tool_map[tool] for tool in tools)
        except KeyError as error:
            raise ContractViolation(
                f"executor {self.capabilities.executor_id} does not map tool {error.args[0]}"
            ) from error

    def compile_context(self, request: AgentRequest) -> CompiledContext:
        """Compile and record one complete context before starting a provider."""

        transport = "provider_schema" if self.native_output_schema else "prompt"
        try:
            compiled = self._context_compiler.compile(
                request,
                ContextPolicy(
                    max_context_bytes=self._max_context_bytes,
                    output_contract=transport,
                ),
            )
        except ContextBudgetExceeded as error:
            telemetry.emit_failure(
                "agent.context_rejected",
                executor_id=self.capabilities.executor_id,
                task_id=request.task_id,
                context_total_bytes=error.used_bytes,
                context_budget_bytes=error.limit_bytes,
                context_budget_status="rejected",
            )
            raise
        telemetry.emit(
            "agent.context_compiled",
            executor_id=self.capabilities.executor_id,
            task_id=request.task_id,
            context_fingerprint=compiled.fingerprint,
            context_total_bytes=compiled.total_bytes,
            context_prompt_bytes=compiled.prompt_bytes,
            context_artifact_bytes=compiled.artifact_bytes,
            context_contract_bytes=compiled.contract_bytes,
            context_budget_bytes=compiled.budget.limit_bytes,
            context_budget_status=compiled.budget.status,
            output_contract_transport=transport,
            omitted_items=",".join(item.item for item in compiled.omissions),
        )
        return compiled

    def run_cli(
        self,
        arguments: Sequence[str],
        request: AgentRequest,
        environment: Optional[Mapping[str, str]] = None,
    ) -> subprocess.CompletedProcess:
        """Run the Agent CLI for one normalized request."""

        executor_id = self.capabilities.executor_id
        process_environment = dict(os.environ)
        if environment:
            process_environment.update(environment)
        limit = _max_output_bytes()
        started = time.monotonic()
        telemetry.emit(
            "agent.call_started",
            executor_id=executor_id,
            task_id=getattr(request, "task_id", None),
            timeout_seconds=request.timeout_seconds,
            max_output_bytes=limit,
        )
        completed = run_bounded_process(
            [*self._command, *arguments],
            cwd=str(request.workspace),
            env=process_environment,
            timeout_seconds=request.timeout_seconds,
            executor_id=executor_id,
            max_output_bytes=limit,
        )
        telemetry.emit(
            "agent.call_finished",
            executor_id=executor_id,
            task_id=getattr(request, "task_id", None),
            returncode=completed.returncode,
            duration_seconds=round(time.monotonic() - started, 6),
        )
        return completed

    def classify_exit(
        self, completed: subprocess.CompletedProcess
    ) -> Optional[FailureClassification]:
        """Classify a non-zero exit, preferring declared codes over guessing."""

        if completed.returncode == 0:
            return None
        detail = _bounded_detail(str(completed.stderr or "") or str(completed.stdout or ""))
        declared = self.exit_code_failures.get(completed.returncode)
        if declared is not None:
            return FailureClassification(
                declared,
                "exit_code",
                f"exit {completed.returncode}: {detail}" if detail else f"exit {completed.returncode}",
            )
        heuristic = _heuristic_kind(detail)
        if heuristic is not None:
            return FailureClassification(heuristic, "heuristic", detail)
        return FailureClassification(
            "execution",
            "exit_code",
            f"exit {completed.returncode}: {detail}" if detail else f"exit {completed.returncode}",
        )

    def require_success(
        self, completed: subprocess.CompletedProcess, executor_id: str
    ) -> None:
        classification = self.classify_exit(completed)
        if classification is not None:
            classification.raise_for(executor_id)


class ClaudeCodeExecutor(CliAgentAdapter):
    native_output_schema = True
    tool_map: ClassVar[Mapping[str, str]] = {
        "read": "Read",
        "shell": "Bash",
        "edit": "Edit",
        "write": "Write",
    }

    def __init__(
        self,
        command: Sequence[str],
        safe_mode_flag: Optional[bool] = None,
        *,
        max_context_bytes: Optional[int] = DEFAULT_MAX_CONTEXT_BYTES,
    ):
        super().__init__(command, max_context_bytes=max_context_bytes)
        detected = (
            _claude_safe_mode_flag(command)
            if safe_mode_flag is None
            else safe_mode_flag
        )
        self._safe_mode_flag = detected is True

    @property
    def capabilities(self) -> ExecutorCapabilities:
        return ExecutorCapabilities(
            "claude-code",
            (
                "cost_budget",
                "model_selection",
                "reasoning_control",
                "structured_output",
                "token_usage",
                "tool_policy",
            ),
            CANONICAL_TOOLS,
        )

    def execute(self, request: AgentRequest) -> AgentResult:
        compiled = self.compile_context(request)
        if compiled.output_schema_json is None:  # pragma: no cover - class invariant
            raise ContractViolation("Claude Code context is missing its output schema")
        mapped_tools = self.map_tools(request.tools)
        arguments = [
            "--print",
            "--output-format",
            "json",
            "--json-schema",
            compiled.output_schema_json,
            "--no-session-persistence",
            "--permission-mode",
            "dontAsk",
            "--tools",
            ",".join(mapped_tools),
        ]
        if self._safe_mode_flag:
            arguments.insert(arguments.index("--permission-mode"), "--safe-mode")
        if request.model:
            arguments.extend(("--model", request.model))
        if request.reasoning_effort:
            arguments.extend(("--effort", request.reasoning_effort))
        if request.max_cost_usd is not None:
            arguments.extend(("--max-budget-usd", str(request.max_cost_usd)))
        arguments.append(compiled.body)
        completed = self.run_cli(
            arguments,
            request,
            {"CLAUDE_CODE_SAFE_MODE": "1"},
        )
        self.require_success(completed, self.capabilities.executor_id)
        try:
            envelope = json.loads(completed.stdout)
        except json.JSONDecodeError as error:
            raise AgentProtocolError(f"invalid Claude Code JSON result: {error}") from error
        if not isinstance(envelope, dict) or envelope.get("is_error") is True:
            raise AgentProtocolError("Claude Code returned an error result")
        structured = envelope.get("structured_output")
        raw_result = envelope.get("result", "")
        outputs: Mapping[str, Any]
        if isinstance(structured, dict):
            outputs = structured
            text = json.dumps(structured, ensure_ascii=False, sort_keys=True)
        elif isinstance(raw_result, str):
            outputs = _json_object(raw_result)
            text = raw_result
        else:
            raise AgentProtocolError("Claude Code result has no structured output")
        usage = _mapping(envelope.get("usage"))
        input_tokens = _optional_non_negative_int(usage.get("input_tokens"))
        cached_input_tokens = _optional_non_negative_int(
            usage.get("cache_read_input_tokens")
        )
        output_tokens = _optional_non_negative_int(usage.get("output_tokens"))
        reported_total = _optional_non_negative_int(usage.get("total_tokens"))
        legacy_parts = tuple(
            _optional_non_negative_int(usage.get(key))
            for key in (
                "input_tokens",
                "output_tokens",
                "cache_creation_input_tokens",
                "cache_read_input_tokens",
            )
        )
        tokens = reported_total if reported_total is not None else sum(
            item for item in legacy_parts if item is not None
        )
        total_complete = reported_total is not None or all(
            item is not None for item in legacy_parts
        )
        cost = envelope.get("total_cost_usd")
        normalized_cost = (
            float(cost)
            if isinstance(cost, (int, float)) and not isinstance(cost, bool)
            else None
        )
        normalized_usage = ModelUsage(
            input_tokens=input_tokens,
            cached_input_tokens=cached_input_tokens,
            output_tokens=output_tokens,
            total_tokens=tokens,
            cost_usd=normalized_cost,
            input_tokens_complete=input_tokens is not None,
            cached_input_tokens_complete=cached_input_tokens is not None,
            output_tokens_complete=output_tokens is not None,
            total_tokens_complete=total_complete,
            cost_complete=normalized_cost is not None,
        )
        return AgentResult(
            self.capabilities.executor_id,
            validate_agent_outputs(outputs, request.output_keys),
            text,
            tokens,
            normalized_cost,
            str(envelope["session_id"]) if envelope.get("session_id") else None,
            usage=normalized_usage,
        )


class PiAgentExecutor(CliAgentAdapter):
    tool_map: ClassVar[Mapping[str, str]] = {
        "read": "read",
        "shell": "bash",
        "edit": "edit",
        "write": "write",
    }

    @property
    def capabilities(self) -> ExecutorCapabilities:
        return ExecutorCapabilities(
            "pi-agent",
            (
                "model_selection",
                "reasoning_control",
                "structured_output",
                "token_usage",
                "tool_policy",
            ),
            CANONICAL_TOOLS,
        )

    def execute(self, request: AgentRequest) -> AgentResult:
        compiled = self.compile_context(request)
        mapped_tools = self.map_tools(request.tools)
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
        if request.reasoning_effort:
            arguments.extend(("--thinking", request.reasoning_effort))
        arguments.append(compiled.body)
        completed = self.run_cli(arguments, request)
        self.require_success(completed, self.capabilities.executor_id)
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
        usage = _mapping(final_message.get("usage"))
        input_tokens = _optional_non_negative_int(usage.get("input"))
        cached_input_tokens = _optional_non_negative_int(usage.get("cacheRead"))
        cache_write_tokens = _optional_non_negative_int(usage.get("cacheWrite"))
        output_tokens = _optional_non_negative_int(usage.get("output"))
        reported_total = _optional_non_negative_int(usage.get("totalTokens"))
        components = (
            input_tokens,
            cached_input_tokens,
            cache_write_tokens,
            output_tokens,
        )
        if reported_total is not None:
            tokens = reported_total
            total_complete = True
        elif all(item is not None for item in components):
            tokens = sum(item for item in components if item is not None)
            total_complete = True
        else:
            tokens = 0
            total_complete = False
        cost_data = _mapping(usage.get("cost"))
        cost = cost_data.get("total")
        normalized_cost = (
            float(cost)
            if isinstance(cost, (int, float)) and not isinstance(cost, bool)
            else None
        )
        normalized_usage = ModelUsage(
            input_tokens=input_tokens,
            cached_input_tokens=cached_input_tokens,
            output_tokens=output_tokens,
            total_tokens=tokens,
            cost_usd=normalized_cost,
            input_tokens_complete=input_tokens is not None,
            cached_input_tokens_complete=cached_input_tokens is not None,
            output_tokens_complete=output_tokens is not None,
            total_tokens_complete=total_complete,
            cost_complete=normalized_cost is not None,
        )
        return AgentResult(
            self.capabilities.executor_id,
            validate_agent_outputs(outputs, request.output_keys),
            text,
            tokens,
            normalized_cost,
            None,
            usage=normalized_usage,
        )


class CodexExecutor(CliAgentAdapter):
    native_output_schema = True
    @property
    def capabilities(self) -> ExecutorCapabilities:
        return ExecutorCapabilities(
            "codex",
            (
                "model_selection",
                "reasoning_control",
                "structured_output",
                "token_usage",
                "tool_policy",
            ),
            CANONICAL_TOOLS,
        )

    @staticmethod
    def _sandbox(tools: Sequence[str]) -> str:
        if set(tools) & {"edit", "shell", "write"}:
            return "workspace-write"
        return "read-only"

    def execute(self, request: AgentRequest) -> AgentResult:
        compiled = self.compile_context(request)
        if compiled.output_schema_json is None:  # pragma: no cover - class invariant
            raise ContractViolation("Codex context is missing its output schema")
        schema_path: Optional[Path] = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", suffix=".json", delete=False
            ) as handle:
                handle.write(compiled.output_schema_json)
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
            if request.reasoning_effort:
                arguments.extend(
                    ("--config", f'model_reasoning_effort="{request.reasoning_effort}"')
                )
            if not any((parent / ".git").exists() for parent in (request.workspace, *request.workspace.parents)):
                arguments.append("--skip-git-repo-check")
            arguments.append(compiled.body)
            completed = self.run_cli(arguments, request)
        finally:
            if schema_path is not None:
                schema_path.unlink(missing_ok=True)

        self.require_success(completed, self.capabilities.executor_id)
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
        input_tokens = _optional_non_negative_int(usage.get("input_tokens"))
        cached_input_tokens = _optional_non_negative_int(
            usage.get("cached_input_tokens")
        )
        output_tokens = _optional_non_negative_int(usage.get("output_tokens"))
        reported_total = _optional_non_negative_int(usage.get("total_tokens"))
        if reported_total is not None:
            tokens = reported_total
            total_complete = True
        else:
            tokens = (input_tokens or 0) + (output_tokens or 0)
            total_complete = input_tokens is not None and output_tokens is not None
        normalized_usage = ModelUsage(
            input_tokens=input_tokens,
            cached_input_tokens=cached_input_tokens,
            output_tokens=output_tokens,
            total_tokens=tokens,
            input_tokens_complete=input_tokens is not None,
            cached_input_tokens_complete=cached_input_tokens is not None,
            output_tokens_complete=output_tokens is not None,
            total_tokens_complete=total_complete,
        )
        return AgentResult(
            self.capabilities.executor_id,
            validate_agent_outputs(_json_object(final_text), request.output_keys),
            final_text,
            tokens,
            None,
            session_id,
            usage=normalized_usage,
        )


class OpenCodeExecutor(CliAgentAdapter):
    """OpenCode JSONL Adapter with a deny-by-default tool permission envelope."""

    @property
    def capabilities(self) -> ExecutorCapabilities:
        return ExecutorCapabilities(
            "opencode",
            ("model_selection", "structured_output", "token_usage", "tool_policy"),
            CANONICAL_TOOLS,
        )

    @staticmethod
    def _permission_policy(tools: Sequence[str]) -> Mapping[str, str]:
        allowed = set(tools)
        unknown = allowed - set(CANONICAL_TOOLS)
        if unknown:
            raise ContractViolation(
                f"executor opencode does not map tool {sorted(unknown)[0]}"
            )
        return {
            "*": "deny",
            "read": "allow" if "read" in allowed else "deny",
            "glob": "allow" if "read" in allowed else "deny",
            "grep": "allow" if "read" in allowed else "deny",
            "list": "allow" if "read" in allowed else "deny",
            "bash": "allow" if "shell" in allowed else "deny",
            "edit": "allow" if allowed & {"edit", "write"} else "deny",
            "question": "deny",
            "plan_enter": "deny",
            "plan_exit": "deny",
            "webfetch": "deny",
        }

    @staticmethod
    def _event_error(executor_id: str, detail: Any) -> None:
        """Fail the call from an OpenCode ``error`` event.

        A structured ``name``/``code`` field is authoritative. Only free-form
        messages fall back to substring matching, and the weaker evidence is
        recorded as such.
        """

        structured_kind: Optional[str] = None
        if isinstance(detail, Mapping):
            marker = " ".join(
                str(detail.get(key, ""))
                for key in ("name", "code", "type", "status")
            ).lower()
            if "429" in marker or "ratelimit" in marker.replace("_", "").replace(
                " ", ""
            ):
                structured_kind = "rate_limit"
            elif "timeout" in marker:
                structured_kind = "timeout"
        if isinstance(detail, str):
            text = detail
        else:
            try:
                text = json.dumps(detail, ensure_ascii=False, sort_keys=True)
            except (TypeError, ValueError):
                text = repr(detail)
        bounded = _bounded_detail(text)
        if structured_kind is not None:
            FailureClassification(structured_kind, "structured", bounded).raise_for(
                executor_id
            )
        heuristic = _heuristic_kind(bounded)
        FailureClassification(
            heuristic or "execution", "heuristic" if heuristic else "structured", bounded
        ).raise_for(executor_id)

    def execute(self, request: AgentRequest) -> AgentResult:
        compiled = self.compile_context(request)
        arguments = ["run", "--format", "json", "--pure"]
        if request.model:
            arguments.extend(("--model", request.model))
        arguments.append(compiled.body)
        permissions = self._permission_policy(request.tools)
        completed = self.run_cli(
            arguments,
            request,
            {
                "OPENCODE_PERMISSION": json.dumps(
                    permissions,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                "OPENCODE_DISABLE_PROJECT_CONFIG": "true",
            },
        )
        self.require_success(completed, self.capabilities.executor_id)

        final_text: Optional[str] = None
        session_id: Optional[str] = None
        step_usages = []
        for line in completed.stdout.splitlines():
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as error:
                raise AgentProtocolError(
                    f"invalid OpenCode JSONL event: {error}"
                ) from error
            if not isinstance(event, dict) or not isinstance(event.get("type"), str):
                raise AgentProtocolError("invalid OpenCode JSONL event envelope")
            if event.get("sessionID"):
                session_id = str(event["sessionID"])
            event_type = event["type"]
            part = event.get("part")
            if event_type == "text" and isinstance(part, dict):
                if isinstance(part.get("text"), str):
                    final_text = part["text"]
            elif event_type == "step_finish" and isinstance(part, dict):
                usage = _mapping(part.get("tokens"))
                input_tokens = _optional_non_negative_int(usage.get("input"))
                output_tokens = _optional_non_negative_int(usage.get("output"))
                reasoning_tokens = _optional_non_negative_int(usage.get("reasoning"))
                reported_total = _optional_non_negative_int(usage.get("total"))
                if reported_total is not None:
                    step_tokens = reported_total
                    total_complete = True
                else:
                    legacy_parts = (input_tokens, output_tokens, reasoning_tokens)
                    step_tokens = sum(
                        item for item in legacy_parts if item is not None
                    )
                    total_complete = all(item is not None for item in legacy_parts)
                step_cost = part.get("cost")
                normalized_cost = (
                    float(step_cost)
                    if isinstance(step_cost, (int, float))
                    and not isinstance(step_cost, bool)
                    and step_cost >= 0
                    else None
                )
                step_usages.append(
                    ModelUsage(
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        total_tokens=step_tokens,
                        cost_usd=normalized_cost,
                        input_tokens_complete=input_tokens is not None,
                        output_tokens_complete=output_tokens is not None,
                        total_tokens_complete=total_complete,
                        cost_complete=normalized_cost is not None,
                    )
                )
            elif event_type == "error":
                self._event_error(
                    self.capabilities.executor_id, event.get("error", event)
                )
        if final_text is None:
            raise AgentProtocolError("OpenCode event stream has no final text result")
        normalized_usage = (
            ModelUsage.combine(step_usages)
            if step_usages
            else ModelUsage(total_tokens=0)
        )
        tokens = normalized_usage.total_tokens or 0
        return AgentResult(
            self.capabilities.executor_id,
            validate_agent_outputs(_json_object(final_text), request.output_keys),
            final_text,
            tokens,
            normalized_usage.cost_usd,
            session_id,
            usage=normalized_usage,
        )


def _claude_safe_mode_flag(command: Sequence[str]) -> Optional[bool]:
    try:
        completed = subprocess.run(
            (*command, "--help"),
            capture_output=True,
            text=True,
            timeout=_DISCOVERY_PROBE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    help_text = f"{completed.stdout or ''}\n{completed.stderr or ''}"
    if completed.returncode != 0 or any(
        flag not in help_text for flag in _CLAUDE_REQUIRED_HELP_FLAGS
    ):
        return None
    return "--safe-mode" in help_text


def _compatibility_probe(
    command: Sequence[str], timeout_seconds: int
) -> subprocess.CompletedProcess:
    return subprocess.run(
        list(command),
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        check=False,
    )


def _certified_adapter_commands() -> Mapping[str, str]:
    from .distribution import AgentOSDistribution

    return AgentOSDistribution(
        which=shutil.which,
        runner=_compatibility_probe,
    ).certified_adapter_commands()


def discover_local_executors(
    router: Optional[PolicyRouter] = None,
    reuse_store: Optional[VerifiedArtifactCache] = None,
) -> ExecutorRegistry:
    registry = ExecutorRegistry(router, reuse_store)
    certified = _certified_adapter_commands()
    claude = certified.get("claude-code")
    if claude:
        safe_mode_flag = _claude_safe_mode_flag((claude,))
        if safe_mode_flag is not None:
            registry.register(
                ClaudeCodeExecutor((claude,), safe_mode_flag=safe_mode_flag)
            )
    pi = certified.get("pi-agent")
    if pi:
        registry.register(PiAgentExecutor((pi,)))
    codex = certified.get("codex")
    if codex:
        registry.register(CodexExecutor((codex,)))
    opencode = certified.get("opencode")
    if opencode:
        registry.register(OpenCodeExecutor((opencode,)))
    return registry
