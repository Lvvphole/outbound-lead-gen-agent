"""Manifest schema and validation for the outbound control loop runtime."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping


class ManifestValidationError(ValueError):
    """Raised when a manifest cannot be run by the control loop."""


ROLE_PLANNER = "planner"
ROLE_EXECUTOR = "executor"
ROLE_VERIFIER = "verifier"
ROLE_EVALUATOR = "evaluator"
ROLE_MEMORY_MANAGER = "memory_manager"
ROLE_ESCALATION_MANAGER = "escalation_manager"

REQUIRED_ROLES = {
    ROLE_PLANNER,
    ROLE_EXECUTOR,
    ROLE_VERIFIER,
    ROLE_EVALUATOR,
    ROLE_MEMORY_MANAGER,
    ROLE_ESCALATION_MANAGER,
}

LIVE_OUTBOUND_ACTIONS = {
    "call_phone",
    "enrich_external",
    "post_webhook",
    "send_email",
    "send_sms",
}


@dataclass(frozen=True)
class Budget:
    """Mechanical execution limits for a manifest run."""

    token_cap: int = 20_000
    max_iterations: int = 500
    max_escalations: int = 3

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> "Budget":
        data = dict(value or {})
        return cls(
            token_cap=_positive_int(data.get("token_cap", 20_000), "budget.token_cap"),
            max_iterations=_positive_int(
                data.get("max_iterations", 500),
                "budget.max_iterations",
            ),
            max_escalations=_non_negative_int(
                data.get("max_escalations", 3),
                "budget.max_escalations",
            ),
        )


@dataclass(frozen=True)
class EvidenceRequirement:
    """A concrete artifact required to verify a criterion."""

    id: str
    type: str
    path: str | None = None
    metric: str | None = None
    equals: Any = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "EvidenceRequirement":
        evidence_id = _required_str(value, "id", "evidence.id")
        evidence_type = _required_str(value, "type", f"evidence[{evidence_id}].type")
        path = _optional_str(value, "path", f"evidence[{evidence_id}].path")
        metric = _optional_str(value, "metric", f"evidence[{evidence_id}].metric")

        if evidence_type == "file" and not path:
            raise ManifestValidationError(
                f"evidence[{evidence_id}] of type file requires path"
            )
        if evidence_type == "metric" and not metric:
            raise ManifestValidationError(
                f"evidence[{evidence_id}] of type metric requires metric"
            )

        return cls(
            id=evidence_id,
            type=evidence_type,
            path=path,
            metric=metric,
            equals=value.get("equals"),
        )


@dataclass(frozen=True)
class SuccessCriterion:
    """A pass/fail criterion that maps to concrete evidence."""

    id: str
    description: str
    evidence: tuple[str, ...]

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "SuccessCriterion":
        criterion_id = _required_str(value, "id", "success_criteria.id")
        description = _required_str(
            value,
            "description",
            f"success_criteria[{criterion_id}].description",
        )
        evidence = value.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            raise ManifestValidationError(
                f"success_criteria[{criterion_id}].evidence must be a non-empty list"
            )
        evidence_ids = tuple(
            _as_str(item, f"success_criteria[{criterion_id}].evidence")
            for item in evidence
        )
        return cls(id=criterion_id, description=description, evidence=evidence_ids)


@dataclass(frozen=True)
class Manifest:
    """Validated manifest describing one outbound control loop."""

    objective: str
    current_state: str
    desired_state: str
    roles: Mapping[str, str]
    success_criteria: tuple[SuccessCriterion, ...]
    evidence_required: Mapping[str, EvidenceRequirement]
    budget: Budget = field(default_factory=Budget)
    actions: Mapping[str, str] = field(default_factory=dict)
    allow_live_external_calls: bool = False
    memory_file: str = "project_memory.md"

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "Manifest":
        roles = _mapping(value.get("roles"), "roles")
        role_names = set(roles)
        missing_roles = sorted(REQUIRED_ROLES - role_names)
        if missing_roles:
            raise ManifestValidationError(
                "roles missing required entries: " + ", ".join(missing_roles)
            )

        role_owners = {role: _as_str(owner, f"roles.{role}") for role, owner in roles.items()}
        if role_owners[ROLE_EXECUTOR] in {
            role_owners[ROLE_VERIFIER],
            role_owners[ROLE_EVALUATOR],
        }:
            raise ManifestValidationError(
                "executor must not share owner with verifier or evaluator"
            )

        evidence_entries = value.get("evidence_required")
        if not isinstance(evidence_entries, list) or not evidence_entries:
            raise ManifestValidationError("evidence_required must be a non-empty list")
        evidence = {
            item.id: item
            for item in (
                EvidenceRequirement.from_mapping(_mapping(entry, "evidence_required[]"))
                for entry in evidence_entries
            )
        }
        if len(evidence) != len(evidence_entries):
            raise ManifestValidationError("evidence_required contains duplicate ids")

        criteria_entries = value.get("success_criteria")
        if not isinstance(criteria_entries, list) or not criteria_entries:
            raise ManifestValidationError("success_criteria must be a non-empty list")
        criteria = tuple(
            SuccessCriterion.from_mapping(_mapping(entry, "success_criteria[]"))
            for entry in criteria_entries
        )

        known_evidence = set(evidence)
        for criterion in criteria:
            missing = sorted(set(criterion.evidence) - known_evidence)
            if missing:
                raise ManifestValidationError(
                    f"success_criteria[{criterion.id}] references unknown evidence: "
                    + ", ".join(missing)
                )

        actions = {
            _as_str(key, "actions.key"): _as_str(action, f"actions.{key}")
            for key, action in _mapping(value.get("actions", {}), "actions").items()
        }

        live_actions = sorted(set(actions.values()) & LIVE_OUTBOUND_ACTIONS)
        allow_live = bool(value.get("allow_live_external_calls", False))
        if live_actions and not allow_live:
            raise ManifestValidationError(
                "live outbound actions require allow_live_external_calls=true: "
                + ", ".join(live_actions)
            )

        return cls(
            objective=_required_str(value, "objective", "objective"),
            current_state=_required_str(value, "current_state", "current_state"),
            desired_state=_required_str(value, "desired_state", "desired_state"),
            roles=role_owners,
            success_criteria=criteria,
            evidence_required=evidence,
            budget=Budget.from_mapping(_mapping(value.get("budget", {}), "budget")),
            actions=actions,
            allow_live_external_calls=allow_live,
            memory_file=_optional_str(value, "memory_file", "memory_file")
            or "project_memory.md",
        )

    @classmethod
    def from_json_file(cls, path: str | Path) -> "Manifest":
        import json

        manifest_path = Path(path)
        with manifest_path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        if not isinstance(data, Mapping):
            raise ManifestValidationError("manifest JSON root must be an object")
        return cls.from_mapping(data)


def _mapping(value: Any, field_name: str) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ManifestValidationError(f"{field_name} must be an object")
    return value


def _required_str(value: Mapping[str, Any], key: str, field_name: str) -> str:
    if key not in value:
        raise ManifestValidationError(f"{field_name} is required")
    return _as_str(value[key], field_name)


def _optional_str(value: Mapping[str, Any], key: str, field_name: str) -> str | None:
    if key not in value or value[key] is None:
        return None
    return _as_str(value[key], field_name)


def _as_str(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ManifestValidationError(f"{field_name} must be a non-empty string")
    return value.strip()


def _positive_int(value: Any, field_name: str) -> int:
    if not isinstance(value, int) or value <= 0:
        raise ManifestValidationError(f"{field_name} must be a positive integer")
    return value


def _non_negative_int(value: Any, field_name: str) -> int:
    if not isinstance(value, int) or value < 0:
        raise ManifestValidationError(f"{field_name} must be a non-negative integer")
    return value
