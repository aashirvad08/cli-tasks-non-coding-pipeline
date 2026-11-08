"""Write /logs/verifier/reward.json and reward-details.json from the grader's result.

`criteria.py` grades the step and prints its result to `clitasks-grade.json`; this
file maps that result onto the step's rubric (`rubrics/<step>.json`) and applies
the rubric's aggregation: the weighted mean of the scored criteria, forced to 0
when a gate fails. It also cross-checks that aggregation against the grader's
own reward and refuses to write a grade when the two disagree, so a rubric that
drifted from `checks.json` is a verifier error, never a silent score.

The run's identity fields (`task_tree_sha256`, `input_snapshot`) are attached
when the run is archived: the verifier container holds neither the package tree
nor the workspace capture they name.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = Path(os.environ.get("CLITASKS_VERIFIER_DIR", "/logs/verifier"))
CONTRACT_GATE = "g_output_contract"


def main() -> int:
    result = json.loads((OUT / "clitasks-grade.json").read_text())
    step = result["step"]
    rubric_path = HERE / "rubrics" / f"{step}.json"
    rubric_bytes = rubric_path.read_bytes()
    rubric = json.loads(rubric_bytes)
    nodes = {node["id"]: node for node in rubric["nodes"]}
    root = nodes[rubric["root"]]
    weights = {child["node"]: child["weight"] for child in root["children"]}
    evidence_dir = OUT / "checks"
    evidence_dir.mkdir(parents=True, exist_ok=True)

    criteria, values = [], {}
    for node in rubric["nodes"]:
        if node["kind"] != "criterion":
            continue
        name = node["id"]
        gate = node["scoring"]["method"] == "hard_gate"
        if name == CONTRACT_GATE:
            errors = result.get("contract_errors") or []
            raw = 0.0 if errors else 1.0
            reason = ("every declared deliverable is present and valid against its published schema"
                      if not errors else "output contract failed: " + "; ".join(map(str, errors)))
            record = {"id": name, "passed": not errors, "contract_errors": errors}
        else:
            record = result["checks"][name]
            if record.get("gated_out"):
                raise ValueError(f"{name}: prerequisite gating is not declared in this rubric")
            raw = 1.0 if record.get("passed") else 0.0
            reason = record.get("reason") or ("criterion met" if raw else "criterion not met")
        (evidence_dir / f"{name}.json").write_text(json.dumps(record, indent=2, ensure_ascii=False) + "\n")
        values[name] = raw
        criteria.append({"name": name, "kind": "gate" if gate else "scored",
                         "weight": 0 if gate else weights[name], "raw_value": raw, "value": raw,
                         "status": "evaluated", "reason": reason, "evidence": [f"checks/{name}.json"]})

    base = sum(weights[n] * values[n] for n in weights) / sum(weights.values())
    before_gates = min(1.0, max(0.0, base))
    gates_failed = [g for g in rubric["gates"] if values[g] < 1]
    reward = 0.0 if gates_failed else before_gates
    if not math.isclose(reward, float(result["reward"]), rel_tol=0, abs_tol=1e-9):
        raise SystemExit(f"rubric aggregation {reward} disagrees with the grader's reward {result['reward']}")

    details = {
        "schema": "clitasks-delivery/reward-details/1",
        "step": step,
        "reward": reward,
        "formula": ("0 if any gate fails, else the weighted mean of the scored criteria: "
                    f"sum(weight * value) / {sum(weights.values())}"),
        "gates_failed": gates_failed,
        "criteria": criteria,
        "rubric_sha256": hashlib.sha256(rubric_bytes).hexdigest(),
        "execution_profile": rubric["execution_profile"],
        "groups": [{"name": root["id"], "value": base}],
        "aggregation": {"base": base, "deduction": 0.0, "reward_before_gates": before_gates},
    }
    (OUT / "reward-details.json").write_text(json.dumps(details, indent=2, ensure_ascii=False) + "\n")
    (OUT / "reward.json").write_text(json.dumps({"reward": reward}) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
