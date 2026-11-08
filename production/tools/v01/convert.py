"""Build the runtime tree of a CLI Tasks delivery-format v0.1 package from a shipped task tree.

    python3 -I production/tools/v01/convert.py <spec.json> [--archive-dir DIR] [--force]

Writes `production/state/v01/build/<env-id>/` with everything whose bytes must be
fixed before any Oracle or calibration run: task.toml, declarations.json,
environment/, tests/ (rubric, expected data, the v0.1 reward emitter), steps/,
plus the evidence that does not depend on runs (provenance/, authoring/traps.json,
verification/reward-hacking.md). The runtime-tree hash it prints is the identity
every later run must be made on; `assemble.py` adds the run evidence, README,
self-assessment and manifest, and zips the package.

The task's instruction, fixture files, checks and reference solution are copied
byte for byte. What changes is packaging: file layout, the agent Dockerfile's
permission step (no owner-write removal under an artifacts source), TZ, the grader
entry point and its reward-details output.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tomllib
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
OVERLAY = HERE / "overlay"
BUILD = REPO / "production/state/v01/build"
STEP = "01-task"
CONTRACT_GATE = "g_output_contract"
JUNK = {"__pycache__", ".DS_Store", ".pytest_cache", ".mypy_cache", ".git"}
APT_PINS = ("tmux=3.3a-3", "ripgrep=13.0.0-4+b2", "procps=2:4.0.2-3", "ca-certificates=20250419~deb12u1")
HOME_ALLOW = ("agent", "user", "node", "ubuntu", "root", "app", "runner", "worker", "harbor", "e2b")
LEAK = re.compile(r"/Users/[A-Za-z0-9_.-]+|/home/(?!(?:%s)\b)[A-Za-z0-9_.-]+/|/private/tmp/|scaler" % "|".join(HOME_ALLOW), re.I)


def task_tree_sha256(root: Path) -> str:
    """Delivery format §13.2, verbatim."""
    lines = []
    for top in ("task.toml", "declarations.json", "environment", "steps", "tests"):
        p = os.path.join(root, top)
        files = [p] if os.path.isfile(p) else [os.path.join(d, f) for d, _, fs in os.walk(p) for f in fs]
        for f in files:
            rel = os.path.relpath(f, root).replace(os.sep, "/")
            sha = hashlib.sha256(open(f, "rb").read()).hexdigest()
            x = "x" if os.stat(f).st_mode & 0o111 else "-"
            lines.append(f"{sha}  {x}  {rel}\n")
    return hashlib.sha256("".join(sorted(lines, key=lambda l: l.split("  ", 2)[2].encode())).encode()).hexdigest()


def copy_tree(src: Path, dst: Path, skip=()) -> None:
    for path in sorted(src.rglob("*")):
        rel = path.relative_to(src)
        if any(part in JUNK for part in rel.parts) or any(rel.parts[0] == s or rel.as_posix() == s for s in skip):
            continue
        if path.is_symlink():
            raise ValueError(f"symlink in source tree: {path}")
        target = dst / rel
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
            os.chmod(target, 0o755 if os.stat(path).st_mode & 0o111 else 0o644)


def toml_str(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def agent_dockerfile(text: str) -> str:
    """Same base images, pins and harness CLIs; protected inputs root-owned, never owner-write-stripped."""
    lines = text.splitlines()
    froms = [i for i, line in enumerate(lines) if line.startswith("FROM ")]
    if len(froms) != 2 or " AS node" not in lines[froms[0]]:
        raise ValueError("unexpected agent Dockerfile shape")
    old_perm = "RUN chmod -R a-w /app/environment && mkdir -p /app/deliverables && chown agent:agent /app/deliverables"
    if lines.count(old_perm) != 1:
        raise ValueError("agent Dockerfile permission step not found")
    apt_old = "apt-get install -y --no-install-recommends tmux=3.3a-3 ripgrep procps ca-certificates"
    if sum(apt_old in line for line in lines) != 1:
        raise ValueError("agent Dockerfile apt step not found")
    out = []
    for i, line in enumerate(lines):
        if line in ("USER agent",) or line.startswith("CMD "):
            continue  # metadata-only: the sandbox drops them and [agent] user sets the agent account
        # Exact versions the 2026-09-19 snapshot resolves (read from the built image).
        line = line.replace(apt_old, "apt-get install -y --no-install-recommends " + " ".join(APT_PINS))
        if line == old_perm:
            line = ("RUN chown -R root:root /app/environment && chmod -R go-w /app/environment"
                    " && mkdir -p /app/deliverables && chown agent:agent /app/deliverables")
        out.append(line)
        if i == froms[1]:
            out.append("ENV TZ=UTC")
    return "\n".join(out) + "\n"


def verifier_dockerfile(text: str) -> str:
    first = text.splitlines()[0]
    if not re.fullmatch(r"FROM \S+@sha256:[0-9a-f]{64}", first):
        raise ValueError("verifier Dockerfile is not digest-pinned")
    return (f"{first}\nENV TZ=UTC\nWORKDIR /app\nCOPY . /tests/\n"
            "RUN mkdir -p /logs/verifier /app/deliverables /app/environment/files && chmod 0755 /tests/test.sh\n")


ASSERTION_RULES = {
    "equals": "equal to the target",
    "set_equals": "equal to the target as a set (order and layout ignored; no missing or extra members)",
    "contains_all": "containing every member of the target",
    "absent": "free of every value of the forbidden form the target names",
    "present": "present as the target requires",
    "row_count": "with exactly the target number of rows",
    "numeric_within": "a number within the declared tolerance of the target",
}


def rubric_for(checks: list[dict], step: str) -> dict:
    objectives = [c for c in checks if c["kind"] == "objective" and c.get("step", step) == step]
    guardrails = [c for c in checks if c["kind"] == "guardrail" and c.get("step", step) == step]
    if [g["id"] for g in guardrails] != ["g_fixture_intact"] or sum(c["weight"] for c in objectives) != 100:
        raise ValueError("expected one fixture guardrail and objective weights summing to 100")
    nodes = [{"id": "score", "kind": "group", "description": "Weighted mean of the step's scored criteria",
              "aggregation": "weighted_mean",
              "children": [{"node": c["id"], "weight": c["weight"]} for c in objectives]}]
    for c in objectives:
        if c.get("requires"):
            raise ValueError(f"{c['id']}: prerequisites are not mapped to depends_on")
        rule = ASSERTION_RULES.get(c["assertion"], f"satisfying the '{c['assertion']}' assertion against the target")
        tol = c.get("tolerance")
        tolerance = (f" Numeric values may differ from the target by at most {tol}." if isinstance(tol, (int, float)) and tol
                     else " Values are compared exactly after the parsing the published schema implies.")
        nodes.append({
            "id": c["id"], "kind": "criterion",
            "description": c["basis"],
            "acceptance": (f"The part of the deliverable this criterion reads must be {rule}. The target is recomputed "
                           "at grade time from the supplied files under the rules the instruction and the supplied files state; "
                           "every reading those files support is accepted." + tolerance),
            "scoring": {"method": "binary"},
            "verification": {"method": "reference_check", "implementation": "criteria.py",
                             "references": ["checks.json"],
                             "comparison": {"method": "custom",
                                            "rule": f"Evaluate this criterion's declared '{c['assertion']}' assertion in checks.json "
                                                    "between the expression read from the submission and the target expression "
                                                    "recomputed from the supplied files and their frozen projections in expected/<step>/."}},
            "depends_on": []})
    nodes.append({
        "id": "g_fixture_intact", "kind": "criterion",
        "description": "The supplied source files are left unchanged.",
        "acceptance": ("No file under /app/environment/files is added, deleted or modified relative to the private baseline "
                       "tests/fixture-manifest.json, and the tree holds no symlink or special file."),
        "scoring": {"method": "hard_gate"},
        "verification": {"method": "deterministic_test", "implementation": "criteria.py"},
        "depends_on": []})
    nodes.append({
        "id": CONTRACT_GATE, "kind": "criterion",
        "description": "The declared deliverable exists and follows its published schema.",
        "acceptance": ("Every declared deliverable exists in /app/deliverables, is UTF-8 JSON without duplicate keys or "
                       "nonfinite numbers and validates against its published schema (tests/outputs.json); no undeclared "
                       "file, directory or symlink is present. A missing or invalid deliverable fails this gate."),
        "scoring": {"method": "hard_gate"},
        "verification": {"method": "deterministic_test", "implementation": "criteria.py"},
        "depends_on": []})
    return {"schema": "clitasks-delivery/rubric/1", "step": step, "execution_profile": "local_deterministic",
            "root": "score", "nodes": nodes, "gates": ["g_fixture_intact", CONTRACT_GATE], "penalties": [],
            "repeatability": {"mode": "deterministic", "runs": 2}}


def source_scope(spec: dict, data: bytes, sha: str, session: str, model: str | None) -> dict:
    record = [t for t in json.loads(data)["traces"] if t["session_id"] == session]
    if len(record) != 1:
        raise ValueError("source export must hold exactly one record for the session")
    messages = record[0]["messages"]
    segments, uses = {}, []
    for name, seg in spec["segments"].items():
        start, end = messages[seg["start"]], messages[seg["end"]]
        span = messages[seg["start"]:seg["end"] + 1]
        segments[name] = {"start": {"message_index": seg["start"], "node_id": start["node_ids"][0]},
                          "end": {"message_index": seg["end"], "node_id": end["node_ids"][0]}}
        # A segment may serve several uses (for example failure_mode and context).
        for u in seg.get("uses") or [seg]:
            if u["use"] in ("request", "standing_rule", "correction") and not any(m["role"] == "user" for m in span):
                raise ValueError(f"segment {name}: a {u['use']} use needs a user message")
            use = {"segment": name, "use": u["use"], "adoption": u["adoption"]}
            if u["adoption"] == "partial":
                use.update(adopted=u["adopted"], not_adopted=u["not_adopted"])
            uses.append(use)
    scope = {"schema": "clitasks-source-scope/v1", "session_id": session,
             "export": {"archive": spec["export"]["archive"], "member": spec["export"]["member"],
                        "data_json_sha256": sha},
             "segments": segments,
             "steps": {STEP: {"start_workspace": {"origin": "synthetic"}, "authored": spec["authored"], "uses": uses}}}
    if model:
        scope["extensions"] = {"x-clitasks": {"source_model": model}}
    return scope


def traps_v01(tr: dict, step: str) -> dict:
    out = []
    for p in tr["premises"]:
        quotes = [q for q in [p.get("resolving_quote"), *(p.get("anchors") or [])] if q]
        trap = {"id": p["id"], "summary": p["misreading"], "steps": [step], "criteria": p["checks"],
                "public_basis": [{"path": f"environment/files/{q['file']}", "quote": q["text"]} for q in quotes]}
        # `type` is optional in traps/1; a premise authored without a template (a stated filler) has none.
        if (p.get("template") or "").split():
            trap["type"] = p["template"].split()[0]
        trap["extensions"] = {"x-clitasks-pipeline": {"decisive": bool(p.get("decisive")), "family": p.get("family"),
                                                   "mechanism": p.get("mechanism")}}
        # misread_fails: the criteria the reference misreading fails, when that is a strict subset of the
        # trap's criteria (one criterion per graded value, and the misreading moves only some values)
        if p.get("misread_fails"):
            if not set(p["misread_fails"]) <= set(p["checks"]):
                raise ValueError(f"{p['id']}: misread_fails names a criterion outside the trap's checks")
            trap["extensions"]["x-clitasks-pipeline"]["misread_fails"] = p["misread_fails"]
        out.append(trap)
    return {"schema": "clitasks-delivery/traps/1", "traps": out}


def build(spec_path: Path, archive_dir: Path, force: bool) -> Path:
    spec = json.loads(spec_path.read_text())
    env_id, version = spec["env_id"], spec["version"]
    src = REPO / spec["source_tree"]
    old = tomllib.loads((src / "task.toml").read_text())
    # source_version: the source tree's own version when it is not the previous delivered version
    # (a revision of an already-delivered v0.1 package is built from a new frozen tree).
    if old["task"]["version"] != spec.get("source_version", spec["previous_version"]):
        raise ValueError("source tree is not the declared source version")
    out = BUILD / env_id
    if out.exists():
        if not force:
            raise FileExistsError(f"{out} exists; pass --force to rebuild (every run made on it becomes stale)")
        shutil.rmtree(out)
    out.mkdir(parents=True)

    # environment/: the Dockerfile and the fixture files only (tools.json and the
    # negation .dockerignore are not build inputs).
    (out / "environment").mkdir()
    (out / "environment/Dockerfile").write_text(agent_dockerfile((src / "environment/Dockerfile").read_text()))
    copy_tree(src / "environment/files", out / "environment/files")

    # tests/: grader data unchanged; derived projections become expected/<step>/;
    # the step rubric replaces rubric.json/reward.toml; v0.1 entry points overlay.
    tests = out / "tests"
    copy_tree(src / "tests", tests, skip=("derived", "rubric.json", "reward.toml", "criteria.py", "test.sh", "Dockerfile"))
    copy_tree(src / "tests/derived", tests / "expected" / STEP)
    for name in ("criteria.py", "emit_v01.py", "test.sh"):
        shutil.copyfile(OVERLAY / "tests" / name, tests / name)
    os.chmod(tests / "test.sh", 0o755)
    (tests / "Dockerfile").write_text(verifier_dockerfile((src / "tests/Dockerfile").read_text()))
    checks = json.loads((src / "tests/checks.json").read_text())["checks"]
    old_rubric = json.loads((src / "tests/rubric.json").read_text())
    if list(old_rubric) != [STEP] or set(old_rubric[STEP]) != {c["id"] for c in checks}:
        raise ValueError("source rubric.json does not list exactly the checks of one step")
    (tests / "rubrics").mkdir()
    (tests / "rubrics" / f"{STEP}.json").write_text(json.dumps(rubric_for(checks, STEP), indent=2, ensure_ascii=False) + "\n")

    # steps/: instruction and reference solution byte for byte; solve.sh executable.
    copy_tree(src / "steps", out / "steps")
    os.chmod(out / "steps" / STEP / "solution/solve.sh", 0o755)
    if not (out / "steps" / STEP / "solution/solve.sh").read_text().startswith(("#!/bin/sh", "#!/bin/bash")):
        raise ValueError("solve.sh needs a /bin/sh or /bin/bash shebang")

    outputs = json.loads((src / "tests/outputs.json").read_text())["outputs"]
    deliverables = [f"/app/deliverables/{o['file']}" for o in outputs]
    session = old["metadata"]["source_trace"]
    env, agent, verifier = old["environment"], old["agent"], old["verifier"]
    toml = f'''schema_version = "1.4"
multi_step_reward_strategy = "mean"
artifacts = ["/app/deliverables", "/app/environment/files"]

[task]
name = "clitasks/{env_id}"
version = "{version}"
description = {toml_str(old["task"]["description"])}
keywords = {json.dumps(old["task"].get("keywords", []), ensure_ascii=False)}

[metadata]
delivery_format = "0.1"
type = "non-coding"
domain = "{spec["domain"]}"
faithfulness = "{old["metadata"]["faithfulness"]}"
prompt_language = "{spec["prompt_language"]}"
agent_workspace = "/app"
deliverables = {json.dumps(deliverables)}
protected_paths = ["/app/environment/files"]
source_session = "{session}"
difficulty_disclosures = {json.dumps(spec["difficulty_disclosures"], ensure_ascii=False)}

[environment]
build_timeout_sec = {int(env["build_timeout_sec"])}
cpus = {env["cpus"]}
memory_mb = {env["memory_mb"]}
storage_mb = {env["storage_mb"]}
network_mode = "no-network"
workdir = "/app"

[agent]
user = "{agent["user"]}"
timeout_sec = {int(agent["timeout_sec"])}
network_mode = "no-network"

[verifier]
timeout_sec = {int(verifier["timeout_sec"])}
network_mode = "no-network"
environment_mode = "separate"

[[steps]]
name = "{STEP}"
[steps.verifier]
env = {{ CLITASKS_STEP = "{STEP}" }}
'''
    (out / "task.toml").write_text(toml)
    (out / "declarations.json").write_text(json.dumps({"schema": "clitasks-delivery/declarations/1", "declarations": []}, indent=2) + "\n")

    # provenance/: exact source references, hashed from the recipient-held export.
    # export.archive is the name the package declares; local_archive is this machine's copy of it.
    local = spec["export"].get("local_archive", spec["export"]["archive"])
    with zipfile.ZipFile(archive_dir / local) as archive:
        data = archive.read(spec["export"]["member"])
    sha = hashlib.sha256(data).hexdigest()
    record = [t for t in json.loads(data)["traces"] if t["session_id"] == session][0]
    (out / "provenance").mkdir()
    (out / "provenance/source-scope.json").write_text(json.dumps(
        source_scope(spec, data, sha, session, record.get("model_name")), indent=2, ensure_ascii=False) + "\n")
    fixture = out / "environment/files"
    ledger = {"schema": "clitasks-source-ledger/v1", "session_id": session, "export_data_json_sha256": sha,
              "entries": [{"path": "/app/environment/files/" + p.relative_to(fixture).as_posix(),
                           "sha256": hashlib.sha256(p.read_bytes()).hexdigest(), "origin": "authored"}
                          for p in sorted(fixture.rglob("*")) if p.is_file()]}
    (out / "provenance/ledger.json").write_text(json.dumps(ledger, indent=2, ensure_ascii=False) + "\n")

    (out / "authoring").mkdir()
    (out / "authoring/traps.json").write_text(json.dumps(
        traps_v01(json.loads((src / "authoring/traps.json").read_text()), STEP), indent=2, ensure_ascii=False) + "\n")
    (out / "verification").mkdir()
    note = (src / "REWARD-HACKING.md").read_text()
    if LEAK.search(note):
        raise ValueError(f"REWARD-HACKING.md carries a local path or supplier name: {LEAK.search(note).group(0)}")
    (out / "verification/reward-hacking.md").write_text(note)

    for path in out.rglob("*"):
        if path.is_file() and LEAK.search(path.read_text(errors="replace")) and "environment/files" not in path.as_posix():
            hit = LEAK.search(path.read_text(errors="replace")).group(0)
            raise ValueError(f"{path.relative_to(out)}: local path or supplier name {hit!r}")
    tree = task_tree_sha256(out)
    (BUILD / f"{env_id}.task_tree_sha256").write_text(tree + "\n")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("spec", type=Path)
    ap.add_argument("--archive-dir", type=Path, default=Path.home() / "Downloads")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    out = build(args.spec, args.archive_dir, args.force)
    print(f"{out.relative_to(REPO)} task_tree_sha256={(BUILD / f'{out.name}.task_tree_sha256').read_text().strip()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
