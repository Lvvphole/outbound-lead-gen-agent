import json

import pytest

from outbound_control_loop import ActionResult, ControlLoopRuntime, Manifest, RuntimeEscalation
from test_manifest import base_manifest


def test_runtime_completes_after_verifier_and_evaluator_approve(tmp_path):
    manifest = Manifest.from_mapping(base_manifest())

    def produce_result(criterion_id, workspace):
        del criterion_id
        evidence_path = workspace / "evidence" / "result.json"
        evidence_path.parent.mkdir(parents=True)
        evidence_path.write_text('{"ok": true}\n', encoding="utf-8")
        return ActionResult(files=("evidence/result.json",))

    runtime = ControlLoopRuntime(
        manifest,
        workspace=tmp_path,
        action_registry={"produce-result": produce_result},
    )

    result = runtime.run()

    assert result.status == "complete"
    assert result.completed_criteria == ("criterion-1",)
    assert result.evidence["files"] == ("evidence/result.json",)
    memory = json.loads(result.memory_file.read_text(encoding="utf-8"))
    assert memory["status"] == "complete"
    assert memory["completed_criteria"] == ["criterion-1"]


def test_runtime_escalates_when_required_evidence_is_missing(tmp_path):
    manifest = Manifest.from_mapping(base_manifest())

    def produce_nothing(criterion_id, workspace):
        del criterion_id, workspace
        return ActionResult()

    runtime = ControlLoopRuntime(
        manifest,
        workspace=tmp_path,
        action_registry={"produce-result": produce_nothing},
    )

    with pytest.raises(RuntimeEscalation, match="iteration limit reached"):
        runtime.run()

    memory = json.loads((tmp_path / "project_memory.md").read_text(encoding="utf-8"))
    assert memory["status"] == "escalated"
    assert memory["blockers"] == ["iteration limit reached before completion"]


def test_runtime_verifies_metric_evidence(tmp_path):
    data = base_manifest()
    data["success_criteria"][0]["evidence"] = ["score"]
    data["evidence_required"] = [
        {"id": "score", "type": "metric", "metric": "accepted", "equals": True}
    ]
    manifest = Manifest.from_mapping(data)

    def produce_metric(criterion_id, workspace):
        del criterion_id, workspace
        return ActionResult(metrics={"accepted": True})

    runtime = ControlLoopRuntime(
        manifest,
        workspace=tmp_path,
        action_registry={"produce-result": produce_metric},
    )

    result = runtime.run()

    assert result.status == "complete"
    assert result.evidence["metrics"] == {"accepted": True}
