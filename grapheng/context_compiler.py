"""Deterministic, budgeted context compilation for every Agent Adapter.

Adapters own provider transport.  They do not own context selection, JSON
encoding, fingerprints, or truncation decisions: those are centralized here
so one request has one explainable context contract across providers.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Optional, Tuple

from .agents import AgentRequest
from .errors import ContractViolation

CONTEXT_CONTRACT_VERSION = 1
DEFAULT_MAX_CONTEXT_BYTES = 512 * 1024

_ARTIFACT_PREFIX = "\n\nArtifacts:"
_TEXT_OUTPUT_PREFIX = "\nReturn only JSON with keys:"
_OUTPUT_CONTRACTS = frozenset({"prompt", "provider_schema"})


def _utf8_bytes(value: str) -> int:
    return len(value.encode("utf-8"))


def _canonical_json(value: Any, *, label: str) -> str:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError) as error:
        raise ContractViolation(
            f"context {label} must be finite JSON data: {error}"
        ) from error


@dataclass(frozen=True)
class ContextPolicy:
    """Compilation policy supplied by the provider-neutral Adapter seam."""

    max_context_bytes: Optional[int] = DEFAULT_MAX_CONTEXT_BYTES
    output_contract: str = "prompt"

    def __post_init__(self) -> None:
        if self.max_context_bytes is not None and (
            isinstance(self.max_context_bytes, bool)
            or not isinstance(self.max_context_bytes, int)
            or self.max_context_bytes < 1
        ):
            raise ContractViolation(
                "context max_context_bytes must be a positive integer or null"
            )
        if self.output_contract not in _OUTPUT_CONTRACTS:
            raise ContractViolation(
                "context output_contract must be prompt or provider_schema"
            )


@dataclass(frozen=True)
class ContextInclusion:
    item: str
    location: str
    reason: str


@dataclass(frozen=True)
class ContextOmission:
    item: str
    reason: str


@dataclass(frozen=True)
class ContextBudgetDecision:
    status: str
    used_bytes: int
    limit_bytes: Optional[int]
    remaining_bytes: Optional[int]


@dataclass(frozen=True)
class CompiledContext:
    """One immutable context body plus its attribution and decision record."""

    body: str
    output_schema_json: Optional[str]
    fingerprint: str
    total_bytes: int
    prompt_bytes: int
    artifact_bytes: int
    contract_bytes: int
    inclusions: Tuple[ContextInclusion, ...]
    omissions: Tuple[ContextOmission, ...]
    budget: ContextBudgetDecision


class ContextBudgetExceeded(ContractViolation):
    """A complete context did not fit; callers must not send a truncation."""

    def __init__(self, used_bytes: int, limit_bytes: int):
        self.used_bytes = used_bytes
        self.limit_bytes = limit_bytes
        super().__init__(
            f"compiled context requires {used_bytes} bytes, exceeding the "
            f"{limit_bytes} byte budget; context was not truncated"
        )


class ContextCompiler:
    """Compile an :class:`AgentRequest` into a compact provider payload."""

    def compile(
        self, request: AgentRequest, policy: ContextPolicy
    ) -> CompiledContext:
        artifact_json = _canonical_json(request.inputs, label="artifacts")
        output_keys = sorted(request.output_keys)
        output_keys_json = _canonical_json(output_keys, label="output keys")

        body = f"{request.prompt}{_ARTIFACT_PREFIX}{artifact_json}"
        schema_json: Optional[str] = None
        inclusions = [
            ContextInclusion("task_prompt", "body", "required task objective"),
            ContextInclusion(
                "input_artifacts", "body", "declared AgentRequest inputs"
            ),
        ]
        omissions = []
        contract_bytes = _utf8_bytes(_ARTIFACT_PREFIX)

        if policy.output_contract == "provider_schema":
            schema_json = _canonical_json(
                {
                    "type": "object",
                    "properties": {key: {} for key in output_keys},
                    "required": output_keys,
                    "additionalProperties": False,
                },
                label="output schema",
            )
            contract_bytes += _utf8_bytes(schema_json)
            inclusions.append(
                ContextInclusion(
                    "output_contract",
                    "provider_schema",
                    "provider enforces the exact output keys",
                )
            )
            omissions.append(
                ContextOmission(
                    "body.output_contract",
                    "already represented by the provider output schema",
                )
            )
        else:
            output_contract = f"{_TEXT_OUTPUT_PREFIX}{output_keys_json}"
            body += output_contract
            contract_bytes += _utf8_bytes(output_contract)
            inclusions.append(
                ContextInclusion(
                    "output_contract",
                    "body",
                    "provider has no native output-schema transport",
                )
            )

        prompt_bytes = _utf8_bytes(request.prompt)
        artifact_bytes = _utf8_bytes(artifact_json)
        total_bytes = prompt_bytes + artifact_bytes + contract_bytes
        encoded_fingerprint = _canonical_json(
            {
                "body": body,
                "contract_version": CONTEXT_CONTRACT_VERSION,
                "output_schema_json": schema_json,
            },
            label="fingerprint envelope",
        )
        fingerprint = hashlib.sha256(
            encoded_fingerprint.encode("utf-8")
        ).hexdigest()

        limit = policy.max_context_bytes
        if limit is not None and total_bytes > limit:
            raise ContextBudgetExceeded(total_bytes, limit)
        budget = ContextBudgetDecision(
            status="accepted" if limit is not None else "unbounded",
            used_bytes=total_bytes,
            limit_bytes=limit,
            remaining_bytes=None if limit is None else limit - total_bytes,
        )
        return CompiledContext(
            body=body,
            output_schema_json=schema_json,
            fingerprint=fingerprint,
            total_bytes=total_bytes,
            prompt_bytes=prompt_bytes,
            artifact_bytes=artifact_bytes,
            contract_bytes=contract_bytes,
            inclusions=tuple(inclusions),
            omissions=tuple(omissions),
            budget=budget,
        )
