"""Deterministic runtime for manifest-governed outbound control loops."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping

from .manifest import Manifest


class RuntimeEscalation(RuntimeError):
    """Raised when the loop reaches a mechanical escalation condition."""


@dataclass(frozen=True)
class ActionResult:
    """Evidence emitted by a registered executor action."""

    files: tuple[str, ...] = ()
    metrics: Mapping[str, object] = field(default_factory=dict)
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class VerificationReport:
    """Verifier-only report over concrete evidence."""

    approved: bool
    missing_evidence: tuple[str, ...]
    failed_metrics: tuple[str, ...]


@dataclass(frozen=True)
class EvaluationReport:
    """Evaluator-only pass/fail decision against manifest criteria."""

    approved: bool
    incomplete_criteria: tuple[str, ...]


@dataclass(frozen=True)
class LoopResult:
    """Terminal state for a runtime run."""

    status: str
    iterations: int
    completed_criteria: tuple[str, ...]
    evidence: Mapping[str, object]
    memory_file: Path


ActionHandler = Callable[[str, Path], ActionResult]


class ControlLoopRuntime:
    """Runs a manifest through planner, executor, verifier, and evaluator gates."""

    def __init__(
        self,
        manifest: Manifest,
        *,
        workspace: str | Path,
        action_registry: Mapping[str, ActionHandler] | None = None,
    ) -> None:
        self.manifest = manifest
        self.workspace = Path(workspace)
        self.action_registry = dict(action_registry or {})
        self.memory_path = self.workspace / self.manifest.memory_file
        self.evidence_files: set[str] = set()
        self.metrics: dict[str, object] = {}
        self.completed_criteria: set[str] = set()
        self.escalations = 0

    def run(self) -> LoopResult:
        """Run until manifest completion, escalation, or iteration exhaustion."""

        self.workspace.mkdir(parents=True, exist_ok=True)
        self._load_memory()
        self._write_memory("started")

        for iteration in range(1, self.manifest.budget.max_iterations + 1):
            next_criterion = self._plan_next_criterion()
            if next_criterion is None:
                return self._complete(iteration - 1)

            action_name = self.manifest.actions.get(next_criterion.id)
            if action_name:
                self._execute(action_name, next_criterion.id)

            verification = self._verify()
            evaluation = self._evaluate(verification)
            self._write_memory("running")

            if verification.approved and evaluation.approved:
                return self._complete(iteration)

            if not action_name:
                self._escalate(
                    "no action registered for incomplete criterion "
                    f"{next_criterion.id}"
                )

        self._escalate("iteration limit reached before completion")
        raise AssertionError("unreachable")

    def _plan_next_criterion(self):
        for criterion in self.manifest.success_criteria:
            if criterion.id not in self.completed_criteria:
                return criterion
        return None

    def _execute(self, action_name: str, criterion_id: str) -> None:
        action = self.action_registry.get(action_name)
        if action is None:
            self._escalate(f"action is not registered: {action_name}")

        result = action(criterion_id, self.workspace)
        for path in result.files:
            self.evidence_files.add(_normalize_path(path))
        self.metrics.update(result.metrics)

    def _verify(self) -> VerificationReport:
        missing: set[str] = set()
        failed_metrics: set[str] = set()
        passed_evidence: set[str] = set()

        for evidence in self.manifest.evidence_required.values():
            if evidence.type == "file":
                expected = _normalize_path(evidence.path or "")
                evidence_path = self.workspace / expected
                if expected not in self.evidence_files or not evidence_path.is_file():
                    missing.add(evidence.id)
                else:
                    passed_evidence.add(evidence.id)
            elif evidence.type == "metric":
                metric_name = evidence.metric or ""
                if metric_name not in self.metrics:
                    missing.add(evidence.id)
                elif evidence.equals is not None and self.metrics[metric_name] != evidence.equals:
                    failed_metrics.add(evidence.id)
                else:
                    passed_evidence.add(evidence.id)
            else:
                missing.add(evidence.id)

        for criterion in self.manifest.success_criteria:
            if set(criterion.evidence) <= passed_evidence:
                self.completed_criteria.add(criterion.id)

        if missing or failed_metrics:
            return VerificationReport(
                approved=False,
                missing_evidence=tuple(sorted(missing)),
                failed_metrics=tuple(sorted(failed_metrics)),
            )

        return VerificationReport(
            approved=True,
            missing_evidence=(),
            failed_metrics=(),
        )

    def _evaluate(self, verification: VerificationReport) -> EvaluationReport:
        incomplete = tuple(
            criterion.id
            for criterion in self.manifest.success_criteria
            if criterion.id not in self.completed_criteria
        )
        return EvaluationReport(
            approved=verification.approved and not incomplete,
            incomplete_criteria=incomplete,
        )

    def _complete(self, iterations: int) -> LoopResult:
        verification = self._verify()
        evaluation = self._evaluate(verification)
        if not verification.approved or not evaluation.approved or self.escalations:
            self._escalate("completion requested before stop conditions passed")
        self._write_memory("complete")
        return LoopResult(
            status="complete",
            iterations=iterations,
            completed_criteria=tuple(sorted(self.completed_criteria)),
            evidence={
                "files": tuple(sorted(self.evidence_files)),
                "metrics": dict(sorted(self.metrics.items())),
            },
            memory_file=self.memory_path,
        )

    def _escalate(self, reason: str) -> None:
        self.escalations += 1
        self._write_memory("escalated", blocker=reason)
        raise RuntimeEscalation(reason)

    def _load_memory(self) -> None:
        if not self.memory_path.is_file():
            return
        with self.memory_path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        self.completed_criteria = set(data.get("completed_criteria", []))
        self.evidence_files = set(data.get("evidence", {}).get("files", []))
        self.metrics = dict(data.get("evidence", {}).get("metrics", {}))

    def _write_memory(self, status: str, blocker: str | None = None) -> None:
        memory = {
            "status": status,
            "objective": self.manifest.objective,
            "completed_criteria": sorted(self.completed_criteria),
            "in_progress": self._next_criterion_id(),
            "evidence": {
                "files": sorted(self.evidence_files),
                "metrics": dict(sorted(self.metrics.items())),
            },
            "blockers": [blocker] if blocker else [],
        }
        self.memory_path.parent.mkdir(parents=True, exist_ok=True)
        with self.memory_path.open("w", encoding="utf-8") as handle:
            json.dump(memory, handle, indent=2, sort_keys=True)
            handle.write("\n")

    def _next_criterion_id(self) -> str | None:
        criterion = self._plan_next_criterion()
        return criterion.id if criterion else None


def _normalize_path(path: str) -> str:
    return Path(path).as_posix().lstrip("/")
