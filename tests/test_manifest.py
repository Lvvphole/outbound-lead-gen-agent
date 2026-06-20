import pytest

from outbound_control_loop import Manifest, ManifestValidationError


def base_manifest():
    return {
        "objective": "Run a manifest controlled outbound loop in mock mode.",
        "current_state": "No runtime has produced evidence.",
        "desired_state": "Runtime stops after verifier and evaluator approve evidence.",
        "roles": {
            "planner": "planner-agent",
            "executor": "executor-agent",
            "verifier": "verifier-agent",
            "evaluator": "evaluator-agent",
            "memory_manager": "memory-agent",
            "escalation_manager": "escalation-agent",
        },
        "success_criteria": [
            {
                "id": "criterion-1",
                "description": "Evidence file exists.",
                "evidence": ["evidence-file"],
            }
        ],
        "evidence_required": [
            {
                "id": "evidence-file",
                "type": "file",
                "path": "evidence/result.json",
            }
        ],
        "actions": {"criterion-1": "produce-result"},
        "budget": {"max_iterations": 2, "max_escalations": 1, "token_cap": 1000},
    }


def test_manifest_requires_role_separation():
    data = base_manifest()
    data["roles"]["verifier"] = "executor-agent"

    with pytest.raises(ManifestValidationError, match="executor must not share"):
        Manifest.from_mapping(data)


def test_manifest_blocks_live_outbound_actions_by_default():
    data = base_manifest()
    data["actions"]["criterion-1"] = "send_email"

    with pytest.raises(ManifestValidationError, match="live outbound actions"):
        Manifest.from_mapping(data)


def test_manifest_requires_criteria_to_reference_known_evidence():
    data = base_manifest()
    data["success_criteria"][0]["evidence"] = ["missing"]

    with pytest.raises(ManifestValidationError, match="unknown evidence"):
        Manifest.from_mapping(data)
