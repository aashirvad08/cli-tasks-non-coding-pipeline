"""Grading entry point.

Deliberately thin. Every scored decision lives in `checks.json` as data, and one
reviewed evaluator runs them, so this file has nothing task-specific to get
wrong. `rubrics/<step>.json` declares which criteria grade a step and how they
aggregate; `basis.toml` holds the public wording each check rests on;
`expected/<step>/` holds the frozen projections of the supplied files that
targets read. `emit_v01.py` turns this result into the two reward files.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from _clitasks_runtime.checks_runtime import Workspace, grade, load  # noqa: E402

#: Rubric gate for the output contract. It is evaluated by the runtime's
#: submission checks, not by a `checks.json` entry.
CONTRACT_GATE = "g_output_contract"


def step_name() -> str:
    step = os.environ.get("CLITASKS_STEP", "").strip()
    rubrics = sorted((HERE / "rubrics").glob("*.json"))
    if not step and len(rubrics) == 1:
        step = rubrics[0].stem
    if not (HERE / "rubrics" / f"{step}.json").is_file():
        # An unknown step is an execution error, never a request to grade everything.
        raise ValueError("CLITASKS_STEP must name a declared step")
    return step


def main() -> int:
    step = step_name()
    source = Path(os.environ.get("CLITASKS_SOURCE", "/app/environment/files"))
    submitted = Path(os.environ.get("CLITASKS_SUBMITTED", "/app/deliverables"))
    rubric = json.loads((HERE / "rubrics" / f"{step}.json").read_text())
    wanted = {node["id"] for node in rubric["nodes"]
              if node["kind"] == "criterion" and node["id"] != CONTRACT_GATE}
    checks = [c for c in load(HERE / "checks.json") if c.id in wanted]
    if {c.id for c in checks} != wanted:
        raise ValueError("rubric criteria and checks.json disagree")

    expected = HERE / "expected" / step
    projections = json.loads((HERE / "projections.json").read_text())["projections"]
    if any(expected.glob("*.jsonl")) and not projections:
        raise RuntimeError("expected targets require projections metadata")
    outputs = json.loads((HERE / "outputs.json").read_text())["outputs"]
    manifest = json.loads((HERE / "fixture-manifest.json").read_text())
    result = grade(checks, Workspace(
        source=source, submitted=submitted, derived=expected, projections=projections,
        fixture_manifest=manifest, outputs=outputs, step=step))
    result["step"] = step
    json.dump(result, sys.stdout, indent=2)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
