"""Grade positive and negative probes with a v0.1 package's own grader.

    python3 -I production/tools/v01/probes.py <package-dir>

Writes `verification/probes/report.json` (and the probe deliverables under
`verification/probes/<probe>/`). Every probe runs the package's `tests/criteria.py`
and `tests/emit_v01.py` exactly as the verifier does, on a copy of the supplied
files, so the report is evidence about the delivered grader, not about a model:

  positive   the reference solution's output, re-serialised with other key order and
             whitespace and with every `unordered` array reversed: must score 1.0
  misread    the reference solution with one authored trap misread (CLITASKS_MISREAD):
             must fail that trap's criteria and pass every other one
  empty      no deliverable: the output-contract gate fails, reward 0
  extra      an undeclared extra file beside the deliverable: the contract gate fails, reward 0
  tamper     one protected source file changed: the fixture gate fails, reward 0
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

STEP = "01-task"


def grade(pkg: Path, source: Path, submitted: Path) -> dict:
    with tempfile.TemporaryDirectory() as logs:
        env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", CLITASKS_STEP=STEP, CLITASKS_SOURCE=str(source), CLITASKS_SUBMITTED=str(submitted),
                   CLITASKS_VERIFIER_DIR=logs)
        grade = subprocess.run([sys.executable, "-I", "-B", str(pkg / "tests/criteria.py")], env=env,
                               capture_output=True, text=True, check=True)
        Path(logs, "clitasks-grade.json").write_text(grade.stdout)
        subprocess.run([sys.executable, "-I", "-B", str(pkg / "tests/emit_v01.py")], env=env,
                       capture_output=True, text=True, check=True)
        details = json.loads(Path(logs, "reward-details.json").read_text())
    return {"reward": details["reward"], "gates_failed": details["gates_failed"],
            "failed": sorted(c["name"] for c in details["criteria"] if c["value"] < 1)}


def solve(pkg: Path, source: Path, out: Path, misread: str = "") -> None:
    out.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", CLITASKS_SOURCE=str(source), CLITASKS_OUT=str(out), CLITASKS_MISREAD=misread)
    subprocess.run([sys.executable, "-I", "-B", str(pkg / f"steps/{STEP}/solution/solve.py")], env=env,
                   capture_output=True, text=True, check=True)


def reorder(value, schema):
    """Reverse arrays the schema marks unordered and rebuild objects in reverse key order."""
    if isinstance(value, dict):
        props = (schema or {}).get("properties", {})
        return {k: reorder(value[k], props.get(k)) for k in reversed(list(value))}
    if isinstance(value, list):
        items = [reorder(v, (schema or {}).get("items")) for v in value]
        return list(reversed(items)) if (schema or {}).get("unordered") else items
    return value


def main() -> int:
    pkg = Path(sys.argv[1]).resolve()
    outputs = json.loads((pkg / "tests/outputs.json").read_text())["outputs"]
    traps = json.loads((pkg / "authoring/traps.json").read_text())["traps"]
    rubric = json.loads((pkg / f"tests/rubrics/{STEP}.json").read_text())
    criteria = sorted(n["id"] for n in rubric["nodes"] if n["kind"] == "criterion")
    target = pkg / "verification/probes"
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    probes = []
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        source = tmp / "files"
        shutil.copytree(pkg / "environment/files", source)

        def record(name, kind, expect, result, out=None, detail=""):
            if expect == "pass":
                ok = result["reward"] == 1.0 and not result["failed"]
            elif expect == "zero":
                ok = result["reward"] == 0.0 and bool(result["gates_failed"])
            else:
                ok = result["failed"] == sorted(expect)
            stored = []
            if out is not None:
                for p in sorted(out.rglob("*")):
                    if p.is_file():
                        dest = target / name / p.relative_to(out)
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(p, dest)
                        stored.append(dest.relative_to(pkg).as_posix())
            probes.append({"probe": name, "kind": kind, "detail": detail,
                           "expected": expect if isinstance(expect, str) else {"failed": sorted(expect)},
                           "observed": result, "as_expected": ok, "deliverables": stored})

        ref = tmp / "ref"
        solve(pkg, source, ref)
        record("reference", "positive", "pass", grade(pkg, source, ref), ref, "reference solution output")
        alt = tmp / "alt"
        alt.mkdir()
        for o in outputs:
            data = json.loads((ref / o["file"]).read_text())
            (alt / o["file"]).write_text(json.dumps(reorder(data, o["schema"]), indent=1, ensure_ascii=True) + "\n")
        record("reordered", "positive", "pass", grade(pkg, source, alt), alt,
               "reference output with reversed key order, unordered arrays reversed, other whitespace and ASCII escapes")
        for trap in traps:
            out = tmp / f"misread-{trap['id']}"
            solve(pkg, source, out, trap["id"])
            # a trap graded one value per criterion may declare the subset its reference misreading fails
            expect = (trap.get("extensions") or {}).get("x-clitasks-pipeline", {}).get("misread_fails") or trap["criteria"]
            record(f"misread-{trap['id']}", "negative", expect, grade(pkg, source, out), out,
                   f"reference solution with trap {trap['id']} misread"
                   + ("" if expect == trap["criteria"] else f" (fails {len(expect)} of the trap's {len(trap['criteria'])} criteria)"))
        empty = tmp / "empty"
        empty.mkdir()
        record("empty", "negative", "zero", grade(pkg, source, empty), None, "no deliverable written")
        extra = tmp / "extra"
        shutil.copytree(ref, extra)
        (extra / "notes.txt").write_text("scratch\n")
        record("extra-file", "negative", "zero", grade(pkg, source, extra), None, "reference output plus an undeclared file")
        first = sorted(p for p in source.rglob("*") if p.is_file())[0]
        original = first.read_bytes()
        first.write_bytes(original + b"\n")
        try:
            record("tamper", "negative", "zero", grade(pkg, source, ref), None,
                   f"reference output with /app/environment/files/{first.relative_to(source).as_posix()} changed")
        finally:
            first.write_bytes(original)
    report = {"schema": "x-clitasks-pipeline/grader-probes/1", "step": STEP, "criteria": criteria,
              "all_as_expected": all(p["as_expected"] for p in probes), "probes": probes}
    (target / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    print(f"{pkg.name}: {sum(p['as_expected'] for p in probes)}/{len(probes)} probes as expected")
    for p in probes:
        if not p["as_expected"]:
            print("  UNEXPECTED", p["probe"], p["observed"])
    return 0 if report["all_as_expected"] else 1


if __name__ == "__main__":
    sys.exit(main())
