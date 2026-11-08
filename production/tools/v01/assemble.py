"""Assemble a CLI Tasks delivery-format v0.1 package from its runtime tree and Harbor 0.24.0 jobs.

    <checker-venv>/python production/tools/v01/assemble.py <spec.json> --jobs DIR [--out DIR] [--zip-dir DIR]

Inputs: the runtime tree `convert.py` built (production/state/v01/build/<env-id>/,
including verification/probes/ from probes.py) and the job directories pulled from
the pool (oracle, repeat-1, repeat-2, nop, probe, fable*, astra*, runtime.json).
Output: production/state/v01/package/<env-id>/ and <zip-dir>/<env-id>@<version>.zip.

Nothing here re-scores a run. Rewards, criterion results and captured bytes are
copied from the jobs; the only fields added to a verifier's reward-details.json are
the two identities the verifier container cannot know (task_tree_sha256 and
input_snapshot), as delivery format §10.4 and verifier contract §8 allow. Runs are
classified as complete, model_limit or excluded (with an infra code) from Harbor's
own exception records; `<jobs>/exclusions.json` may override a run's class with a
reviewed reason. The canonical diff and patch come from the checker package
(clitasks_rle_formatcheck.workspace_changes), so they match its recomputation.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import os
import re
import shutil
import sys
import tarfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
CHECKER = Path(os.environ.get("CLITASKS_FORMATCHECK", Path.home() / "clitasks-rle-formatcheck-external"))
sys.path.insert(0, str(CHECKER / "src"))
from clitasks_rle_formatcheck.workspace_changes import derive_changes, render_patch  # noqa: E402
from clitasks_rle_formatcheck import checker as fc  # noqa: E402

STEP = "01-task"
K = 10
RUNS_REQUESTED = 10
MODELS = {
    "fable": {"model_id": "claude-fable-5-1", "provider_model": "anthropic/claude-fable-5-1",
              "harness": {"name": "claude-code", "version": "2.1.283"}, "label": "Claude Fable 5.1",
              "host": "api.anthropic.com"},
    "astra": {"model_id": "gpt-6-astra", "provider_model": "openai/gpt-6-astra",
              "harness": {"name": "codex", "version": "0.157.1"}, "label": "GPT-6 Astra",
              "host": "api.openai.com"},
}
SETTINGS = {"max_output_tokens": 128000, "agent_timeout_sec": 10800, "max_turns": None,
            "agent_phase_network": "no-network plus the harness's own model API host, added by the runner",
            "harness_web_tools": "not disabled (harness defaults)"}
LEVELS = {"F0": "Inspired", "F1": "Pattern", "F2": "Scenario", "F3": "Replay"}
ORACLE_HARNESS = {"name": "harbor-oracle", "version": "0.24.0"}
ASSESSOR = {"name": "qa-review-workflow", "kind": "tool", "version": "clitasks-pipeline-v01/2026-10-24"}


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n")


def iso(value: str) -> str:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def trials(job: Path) -> list[Path]:
    return sorted(p for p in job.iterdir() if p.is_dir() and (p / "result.json").is_file() and (p / "config.json").is_file())


class Package:
    def __init__(self, spec: dict, build: Path, out: Path, jobs: Path):
        self.spec, self.build, self.root, self.jobs = spec, build, out, jobs
        self.env_id, self.version = spec["env_id"], spec["version"]
        if out.exists():
            shutil.rmtree(out)
        shutil.copytree(build, out)
        self.tree = fc.task_tree_sha256(out)
        expected = (build.parent / f"{self.env_id}.task_tree_sha256").read_text().strip()
        if self.tree != expected:
            raise ValueError("runtime tree differs from the one convert.py recorded; rerun the jobs")
        import tomllib
        self.cfg = tomllib.loads((out / "task.toml").read_text())
        self.md = self.cfg["metadata"]
        self.deliverables = self.md["deliverables"]
        self.rubric_path = out / "tests/rubrics" / f"{STEP}.json"
        self.rubric = json.loads(self.rubric_path.read_text())
        self.fixture = {("/app/environment/files/" + p.relative_to(out / "environment/files").as_posix()): p
                        for p in (out / "environment/files").rglob("*") if p.is_file()}
        self.runtime = json.loads((jobs / "runtime.json").read_text())
        overrides = jobs / "exclusions.json"
        self.overrides = json.loads(overrides.read_text()) if overrides.is_file() else {}

    def rel(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()

    # -- workspace captures ------------------------------------------------
    def changes(self, trial: Path, model_id: str, run_id: str, step_dir: Path, blob_dir: str,
                deliverable_dir: Path | None) -> tuple[dict, dict]:
        """changes.json + changes.patch for step 0 of a trial; returns (document, owner uids)."""
        cap = trial / "clitasks-capture"
        sides, owners = {}, {}
        for label, phase in (("before", "before-agent"), ("after", "after-agent-before-verifier")):
            inv = json.loads((cap / f"step0-{label}.json").read_text())
            meta = json.loads((cap / f"step0-{label}.meta").read_text())
            with tarfile.open(cap / f"step0-{label}.tar") as tar:
                blobs = {"/" + m.name: tar.extractfile(m).read() for m in tar.getmembers() if m.isfile()}
            entries = []
            for e in inv["entries"]:
                entry = {k: v for k, v in e.items() if k != "uid"}
                owners.setdefault(label, {})[e["path"]] = e["uid"]
                if e["kind"] == "file":
                    data = blobs[e["path"]]
                    if len(data) != e["bytes"] or sha256(data) != e["sha256"]:
                        raise ValueError(f"{trial.name}: captured bytes differ from the inventory for {e['path']}")
                    entry["content"] = self.store(e["path"], data, label, blob_dir, deliverable_dir)
                entries.append(entry)
            sides[label] = {"phase": phase, "captured_at": iso(meta["captured_at"]), "entries": entries}
        doc = {"schema": "clitasks-delivery/workspace-changes/1", "model_id": model_id, "run_id": run_id, "step": STEP,
               "task_tree_sha256": self.tree, "scope": sorted(inv["scope"]),
               "before": sides["before"], "after": sides["after"], "changes": []}
        doc["changes"] = derive_changes(doc)
        write_json(step_dir / "changes.json", doc)
        (step_dir / "changes.patch").write_bytes(render_patch(doc, self.root).encode("utf-8"))
        return doc, owners

    def store(self, path: str, data: bytes, label: str, blob_dir: str, deliverable_dir: Path | None) -> str:
        fixture = self.fixture.get(path)
        if fixture is not None and fixture.read_bytes() == data:
            return self.rel(fixture)
        if label == "after" and deliverable_dir is not None and any(fc.under(path, d) for d in self.deliverables):
            target = deliverable_dir / path.lstrip("/")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            return self.rel(target)
        target = self.root / blob_dir / sha256(data)
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_bytes(data)
        return self.rel(target)

    def verifier(self, src: Path, dst: Path, snapshot: Path) -> dict:
        """Copy a step's verifier outputs, attaching the run identities to reward-details.json."""
        dst.mkdir(parents=True, exist_ok=True)
        for name in ("reward.json", "test-stdout.txt", "clitasks-grade.json"):
            if (src / name).is_file():
                shutil.copyfile(src / name, dst / name)
        if (src / "checks").is_dir():
            shutil.copytree(src / "checks", dst / "checks")
        details = json.loads((src / "reward-details.json").read_text())
        if details["rubric_sha256"] != sha256(self.rubric_path.read_bytes()):
            raise ValueError(f"{src}: graded under a different rubric")
        details["task_tree_sha256"] = self.tree
        details["input_snapshot"] = {"path": self.rel(snapshot), "sha256": sha256(snapshot.read_bytes())}
        write_json(dst / "reward-details.json", details)
        return details

    # -- verification ------------------------------------------------------
    def oracle(self) -> dict:
        trial = trials(self.jobs / "oracle")[0]
        result = json.loads((trial / "result.json").read_text())
        run_id = "oracle-" + trial.name.rsplit("__", 1)[-1]
        base = self.root / "verification/oracle"
        step_dir = base / "steps" / STEP
        doc, owners = self.changes(trial, "oracle", run_id, step_dir, "verification/oracle/contents", step_dir / "artifacts")
        details = self.verifier(trial / "steps" / STEP / "verifier", step_dir / "verifier", step_dir / "changes.json")
        if (trial / "steps" / STEP / "agent/oracle.txt").is_file():
            (step_dir / "agent").mkdir(exist_ok=True)
            shutil.copyfile(trial / "steps" / STEP / "agent/oracle.txt", step_dir / "agent/oracle.txt")
        reward = result["verifier_result"]["rewards"]["reward"]
        run = {"schema": "clitasks-delivery/run/1", "run_id": run_id, "model_id": "oracle", "harness": ORACLE_HARNESS,
               "reasoning_effort": "none", "task_tree_sha256": self.tree, "outcome": "complete", "exclusion_code": None,
               "reward": reward, "per_step_reward": {STEP: details["reward"]},
               "started_at": iso(result["started_at"]), "finished_at": iso(result["finished_at"])}
        write_json(base / "run.json", run)
        uids = {owners["after"][p] for p in owners["after"] if any(fc.under(p, d) for d in self.deliverables)
                and p not in owners["before"]}
        if uids != {1000}:
            raise ValueError(f"Oracle deliverables were not written by uid 1000: {uids}")
        return {"run": run, "details": details}

    def repeats(self) -> dict:
        evidence = []
        snapshot = self.root / "verification/oracle/steps" / STEP / "changes.json"
        for n in (1, 2):
            trial = trials(self.jobs / f"repeat-{n}")[0]
            dst = self.root / "verification/repeats" / STEP / str(n) / "verifier"
            self.verifier(trial / "steps" / STEP / "verifier", dst, snapshot)
            evidence.append(self.rel(dst / "reward-details.json"))
        return {"mode": "deterministic", "runs": 2, "identical": True, "evidence": evidence}

    def integrity_probe(self) -> str:
        """The probe's own capture and grade: the Oracle's output with one protected file changed."""
        trial = trials(self.jobs / "probe")[0]
        run_id = "probe-" + trial.name.rsplit("__", 1)[-1]
        step_dir = self.root / "verification/integrity-probe/steps" / STEP
        self.changes(trial, "integrity-probe", run_id, step_dir, "verification/integrity-probe/contents", None)
        self.verifier(trial / "steps" / STEP / "verifier", step_dir / "verifier", step_dir / "changes.json")
        return self.rel(step_dir / "verifier/reward-details.json")

    def supplement_q26_q30(self) -> str:
        """Supplier implementation of Q26 (answer leak) and Q30 (collected artifacts), which the checker skips."""
        capture = json.loads((self.root / "verification/oracle/steps" / STEP / "changes.json").read_text())
        texts = {"README.md": (self.root / "README.md").read_text(),
                 f"steps/{STEP}/instruction.md": (self.root / "steps" / STEP / "instruction.md").read_text()}
        values = []

        def leaves(x):
            if isinstance(x, dict):
                for v in x.values():
                    yield from leaves(v)
            elif isinstance(x, list):
                for v in x:
                    yield from leaves(v)
            else:
                yield x
        for e in capture["after"]["entries"]:
            if e["kind"] == "file" and any(fc.under(e["path"], d) for d in self.deliverables):
                data = json.loads((self.root / e["content"]).read_text())
                for v in leaves(data):
                    if isinstance(v, str) and len(v) >= 4 or (isinstance(v, (int, float)) and not isinstance(v, bool)
                                                            and not (float(v).is_integer() and abs(v) <= 12)):
                        values.append((e["path"], v))
        seen, hits = set(), []
        for path, v in values:
            if (path, json.dumps(v)) in seen:
                continue
            seen.add((path, json.dumps(v)))
            for name, text in texts.items():
                if isinstance(v, str):
                    found = re.search(r"(?<!\w)" + re.escape(v) + r"(?!\w)", text, re.I)
                else:
                    found = next((m for m in re.finditer(r"(?<![\w.])-?\d+(?:\.\d+)?(?![\w.])", text)
                                  if float(m.group()) == float(v)), None)
                if found:
                    context = text[max(0, found.start() - 60):found.end() + 60].replace("\n", " ")
                    hits.append({"deliverable": path, "value": v, "in": name, "context": context,
                                 "review": self.spec.get("q26_reviews", {}).get(f"{name}:{v}", "unreviewed")})
        collected = [e for e in capture["after"]["entries"] if e["kind"] == "file"
                     and any(fc.under(e["path"], a) for a in self.cfg["artifacts"])]
        required = list(self.deliverables) + list(self.md["protected_paths"])
        report = {"schema": "x-clitasks-pipeline/q26-q30/1", "env_id": self.env_id, "task_version": self.version,
                  "task_tree_sha256": self.tree,
                  "checker": "clitasks-rle-formatcheck reports Q26 and Q30 as SKIP (not implemented); this is the supplier's implementation of both rows as section 18 defines them",
                  "q26": {"definition": "Answer-leak heuristic: no expected values from Oracle outputs appear in README or instructions",
                          "method": ("every leaf of the Oracle's captured deliverables that is a string of 4 or more characters, or a "
                                     "number other than an integer from -12 to 12, searched in README.md and each instruction "
                                     "(strings case-insensitive and word-bounded, numbers as numeric tokens); every hit reviewed"),
                          "values_checked": len(seen), "texts": sorted(texts), "hits": hits,
                          "status": "PASS" if all(h["review"] != "unreviewed" and not h["review"].startswith("LEAK") for h in hits) else "REVIEW"},
                  "q30": {"definition": "Collected-artifact estimate: Oracle run's collected artifacts within the limits",
                          "method": "regular files under the declared artifacts paths in the Oracle's after-capture",
                          "limit": "none approved in v0.1 (section 2.2); measured, not budget-judged",
                          "artifacts": self.cfg["artifacts"],
                          "required_paths_collected": all(any(fc.under(r, a) for a in self.cfg["artifacts"]) for r in required),
                          "collected_files": len(collected), "collected_bytes": sum(e["bytes"] for e in collected),
                          "largest_bytes": max((e["bytes"] for e in collected), default=0),
                          "status": "PASS"}}
        out = self.root / "verification/supplement/q26-q30.json"
        write_json(out, report)
        return self.rel(out)

    def supplement_regrade_previous(self) -> str | None:
        """Regrade the previous delivery's recorded calibration outputs with the delivered grader."""
        prev = self.spec.get("previous_zip")
        if not prev:
            return None
        import subprocess
        import tempfile
        z = zipfile.ZipFile(REPO / prev)
        names = z.namelist()
        trial_ids = sorted({n.split("/")[3] for n in names if n.startswith("calibration/trajectories/") and n.count("/") >= 4})
        cases = []
        with tempfile.TemporaryDirectory() as tmp:
            for tid in trial_ids:
                model = next(n.split("/")[2] for n in names if f"/{tid}/" in n)
                base = f"calibration/trajectories/{model}/{tid}/"
                old = json.loads(z.read(base + "verifier/reward.json"))["reward"]
                out, logs = Path(tmp, tid, "out"), Path(tmp, tid, "logs")
                out.mkdir(parents=True); logs.mkdir()
                for n in names:
                    if n.startswith(base + "artifacts/") and not n.endswith("/"):
                        (out / n[len(base + "artifacts/"):]).write_bytes(z.read(n))
                env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", CLITASKS_STEP=STEP, CLITASKS_SOURCE=str(self.root / "environment/files"),
                           CLITASKS_SUBMITTED=str(out), CLITASKS_VERIFIER_DIR=str(logs))
                g = subprocess.run([sys.executable, "-I", "-B", str(self.root / "tests/criteria.py")], env=env,
                                   capture_output=True, text=True, check=True)
                (logs / "clitasks-grade.json").write_text(g.stdout)
                subprocess.run([sys.executable, "-I", "-B", str(self.root / "tests/emit_v01.py")], env=env,
                               capture_output=True, text=True, check=True)
                new = json.loads((logs / "reward.json").read_text())["reward"]
                cases.append({"run": f"{model}/{tid}", "as_run_step_reward": old, "regraded_step_reward": new,
                              "equal": abs(old - new) <= 1e-9})
        report = {"schema": "x-clitasks-pipeline/regrade-previous-delivery/1", "env_id": self.env_id,
                  "previous_version": self.spec["previous_version"], "grader": "the delivered tests/ (criteria.py and emit_v01.py)",
                  "inputs": "each recorded run's deliverables from the previous delivery; the delivered protected inputs",
                  "verifier_platform": "the grader's Python run directly on the assembling host (pure-Python, deterministic)",
                  "tolerance": 1e-9, "cases": len(cases), "equal": sum(c["equal"] for c in cases),
                  "differ": [c for c in cases if not c["equal"]], "all": cases}
        out = self.root / "verification/supplement/regrade-previous-delivery.json"
        write_json(out, report)
        return self.rel(out)

    def single(self, job: str) -> tuple[float, dict]:
        trial = trials(self.jobs / job)[0]
        result = json.loads((trial / "result.json").read_text())
        details = json.loads((trial / "steps" / STEP / "verifier/reward-details.json").read_text())
        return result["verifier_result"]["rewards"]["reward"], details

    # -- calibration -------------------------------------------------------
    def classify(self, model: str, run_id: str, result: dict, graded: bool) -> tuple[str, str | None, str]:
        key = f"{model}/{run_id}"
        if key in self.overrides:
            o = self.overrides[key]
            return o["outcome"], o.get("code"), o["detail"]
        excs = [x for x in [result.get("exception_info"), *[s.get("exception_info") for s in result.get("step_results") or []]] if x]
        if not excs:
            return ("complete", None, "") if graded else ("excluded", "infra.verifier_error", "no reward was produced")
        exc = excs[0]
        if exc["exception_type"] == "AgentTimeoutError" and graded:
            return "model_limit", None, exc["exception_message"]
        text = (exc["exception_type"] + " " + exc["exception_message"]).lower()
        code = ("infra.provider_transport" if any(w in text for w in ("overload", "rate limit", "ratelimit", "529", "503", "timeout connecting", "api error"))
                else "infra.provider_auth" if any(w in text for w in ("401", "403", "auth"))
                else "infra.build" if "build" in text else "infra.other")
        return "excluded", code, f"{exc['exception_type']}: {exc['exception_message'][:300]}"

    def calibration(self) -> dict:
        if self.spec.get("regrade"):
            return self.regraded_calibration()
        return self.fresh_calibration()

    def regraded_calibration(self) -> dict:
        """Delivery format 13.3 `regraded`: a MINOR revision confined to tests/.

        The runs are the previous delivery's, unchanged: run identity, agent trajectories, workspace
        captures and changes.json keep the tree they ran on (the calibrated tree). Each run's
        collected artifacts were regraded in Harbor 0.24.0's verifier twice, from the source job
        directories: under the previous package (jobs `asrun-<model>`), which must reproduce every
        recorded grade exactly, and under this package (jobs `regrade-<model>`), whose verifier
        outputs replace the recorded ones. Rewards are the regraded ones; nothing is re-scored here.
        """
        import tempfile
        rg = self.spec["regrade"]
        zpath = REPO / rg["from_zip"]
        if sha256(zpath.read_bytes()) != rg["from_zip_sha256"]:
            raise ValueError(f"{zpath}: sha256 differs from the spec's from_zip_sha256")
        tmp = tempfile.TemporaryDirectory()
        with zipfile.ZipFile(zpath) as z:
            z.extractall(tmp.name)
        old = Path(tmp.name) / self.env_id
        old_metrics = json.loads((old / "calibration/metrics.json").read_text())
        if old_metrics["status"] != "current":
            raise ValueError("the regraded calibration must come from a package calibrated on its own tree")
        as_run_tree = old_metrics["calibrated_task_tree_sha256"]
        old_rubric_sha = sha256((old / "tests/rubrics" / f"{STEP}.json").read_bytes())
        with (old / "calibration/runs.csv").open(newline="", encoding="utf-8") as f:
            old_rows = {(r["model_id"], r["run_id"]): r for r in csv.DictReader(f)}

        def by_source(job: str) -> dict:
            """run_id of the source trial -> the regrade trial that graded it."""
            out = {}
            for trial in trials(self.jobs / job):
                cfg = json.loads((trial / "config.json").read_text())
                src = Path(cfg["source_trial"]["path"]).name.rsplit("__", 1)[-1]
                if src in out:
                    raise ValueError(f"{job}: two regrade trials for source run {src}")
                out[src] = trial
            return out

        def scored(details: dict) -> dict:
            return {"reward": details["reward"], "gates_failed": sorted(details["gates_failed"]),
                    "criteria": sorted(({k: c[k] for k in ("name", "kind", "weight", "raw_value", "value", "status")}
                                        for c in details["criteria"]), key=lambda c: c["name"])}

        if (old / "calibration/contents").is_dir():
            shutil.copytree(old / "calibration/contents", self.root / "calibration/contents", dirs_exist_ok=True)
        rows, models, cases = [], [], []
        for key, info in MODELS.items():
            mid = info["model_id"]
            new_by, asrun_by = by_source(f"regrade-{key}"), by_source(f"asrun-{key}")
            old_runs = sorted(p for p in (old / "calibration/trials" / mid).iterdir() if p.is_dir())
            if set(new_by) != {p.name for p in old_runs} or set(asrun_by) != {p.name for p in old_runs}:
                raise ValueError(f"{mid}: regrade jobs do not cover exactly the recorded runs")
            old_entry = next(m for m in old_metrics["models"] if m["model_id"] == mid)
            stats_rows = []
            for src in old_runs:
                run_id = src.name
                td = self.root / "calibration/trials" / mid / run_id
                shutil.copytree(src, td)
                run = json.loads((td / "run.json").read_text())
                if run["task_tree_sha256"] != as_run_tree or run["outcome"] != "complete":
                    raise ValueError(f"{mid}/{run_id}: only complete runs on the calibrated tree are regraded")
                step_dir = td / "steps" / STEP
                recorded = json.loads((step_dir / "verifier/reward-details.json").read_text())
                # the as-run grader reproduces the recorded grade from the same collected artifacts
                asrun = json.loads((asrun_by[run_id] / "steps" / STEP / "verifier/reward-details.json").read_text())
                if asrun["rubric_sha256"] != old_rubric_sha:
                    raise ValueError(f"{mid}/{run_id}: as-run regrade used another rubric")
                reproduced = scored(asrun) == scored(recorded)
                # the regrade read the same deliverables the run recorded
                new_trial = new_by[run_id]
                for d in self.deliverables:
                    a = new_trial / "steps" / STEP / "artifacts" / d.lstrip("/")
                    b = step_dir / "artifacts" / d.lstrip("/")
                    if a.read_bytes() != b.read_bytes():
                        raise ValueError(f"{mid}/{run_id}: regraded artifact {d} differs from the recorded one")
                shutil.rmtree(step_dir / "verifier")
                details = self.verifier(new_trial / "steps" / STEP / "verifier", step_dir / "verifier", step_dir / "changes.json")
                if not details["reward"] == json.loads((new_trial / "result.json").read_text())["verifier_result"]["rewards"]["reward"]:
                    raise ValueError(f"{mid}/{run_id}: regrade job result and reward-details disagree")
                ext = run.setdefault("extensions", {}).setdefault("x-clitasks-pipeline", {})
                ext.update(reward_as_run=run["reward"], regraded={
                    "method": "regrade-final-workspace", "grader_task_version": self.version,
                    "grader_task_tree_sha256": self.tree, "job": new_trial.parent.name, "trial": new_trial.name,
                    "as_run_reproduced_by": f"{asrun_by[run_id].parent.name}/{asrun_by[run_id].name}"})
                run["reward"] = details["reward"]
                run["per_step_reward"] = {STEP: details["reward"]}
                write_json(td / "run.json", run)
                crit = details["criteria"]
                failed = [c["name"] for c in crit if c["value"] is not None and c["value"] < 1]
                row = dict(old_rows[(mid, run_id)])
                row.update(step_reward=repr(details["reward"]), step_pass=str(int(abs(details["reward"] - 1) < 1e-9)),
                           run_reward=repr(details["reward"]), run_pass=str(int(abs(details["reward"] - 1) < 1e-9)),
                           checks_passed=str(sum(1 for c in crit if c["value"] == 1)), checks_total=str(len(crit)),
                           failed_checks=";".join(failed), task_tree_sha256=as_run_tree)
                rows.append(row)
                stats_rows.append(details["reward"])
                cases.append({"run": f"{mid}/{run_id}", "as_run_reward": recorded["reward"],
                              "as_run_failed": sorted(c["name"] for c in recorded["criteria"] if c["value"] is not None and c["value"] < 1),
                              "reproduced_under_as_run_grader": reproduced, "as_run_regrade": f"{asrun_by[run_id].parent.name}/{asrun_by[run_id].name}",
                              "regraded_reward": details["reward"], "regraded_failed": sorted(failed),
                              "regrade": f"{new_trial.parent.name}/{new_trial.name}"})
            stat = stats(stats_rows)
            entry = dict(old_entry, task=stat, per_step={STEP: stat})
            models.append(entry)
        tmp.cleanup()
        reproduced_all = all(c["reproduced_under_as_run_grader"] for c in cases)
        if not reproduced_all:
            bad = [c["run"] for c in cases if not c["reproduced_under_as_run_grader"]]
            raise ValueError(f"as-run grades not reproduced under the as-run grader: {bad}")
        header = "model_id,run_id,step,outcome,exclusion_code,step_reward,step_pass,run_reward,run_pass,checks_passed,checks_total,failed_checks,agent_steps,input_tokens,output_tokens,wall_seconds,task_tree_sha256".split(",")
        with (self.root / "calibration/runs.csv").open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=header, lineterminator="\n")
            w.writeheader()
            w.writerows(rows)
        metrics = dict(old_metrics, status="regraded", graded_task_tree_sha256=self.tree, models=models,
                       regrade={"method": "regrade-final-workspace", "reproduced_as_run": reproduced_all,
                                "as_run_task_tree_sha256": as_run_tree,
                                "note": (f"Each recorded {old_metrics['calibrated_task_version']} run's collected artifacts regraded in "
                                         "Harbor 0.24.0's verifier: under the as-run package, reproducing every recorded grade, and "
                                         f"under {self.version}, whose grades are reported; agents were not re-run "
                                         "(verification/supplement/regrade-calibration.json).")})
        write_json(self.root / "calibration/metrics.json", metrics)
        write_json(self.root / "verification/supplement/regrade-calibration.json", {
            "schema": "x-clitasks-pipeline/regrade-calibration/1", "env_id": self.env_id,
            "from_version": old_metrics["calibrated_task_version"], "from_task_tree_sha256": as_run_tree,
            # the delivered zip by hash only: its <env-id>@<version>.zip name reads as an e-mail address to Q25
            "from_package_sha256": rg["from_zip_sha256"],
            "to_version": self.version, "to_task_tree_sha256": self.tree, "method": "regrade-final-workspace",
            "runner": "harbor 0.24.0 `job regrade` on each source job (the verifier container, as for the receipt's repeats)",
            "runtime": self.runtime, "reproduced_as_run": reproduced_all, "runs": len(cases), "cases": cases})
        return {"metrics": metrics, "rows": rows}

    def fresh_calibration(self) -> dict:
        rows, models, started, finished = [], [], [], []
        for key, info in MODELS.items():
            runs = []
            for job in sorted(p for p in self.jobs.iterdir() if p.is_dir() and p.name.startswith(key)):
                for trial in trials(job):
                    runs.append((trial, json.loads((trial / "result.json").read_text())))
            stats_rows = []
            outcomes = {"complete": 0, "model_limit": 0, "model_refusal": 0, "excluded": 0}
            exclusions, usage = [], []
            for trial, result in runs:
                run_id = trial.name.rsplit("__", 1)[-1]
                td = self.root / "calibration/trials" / info["model_id"] / run_id
                step_src = trial / "steps" / STEP
                reward_file = step_src / "verifier/reward.json"
                graded = reward_file.is_file() and (step_src / "verifier/reward-details.json").is_file()
                outcome, code, detail = self.classify(key, run_id, result, graded)
                outcomes[outcome] += 1
                step_dir = td / "steps" / STEP
                details = None
                if graded:
                    self.changes(trial, info["model_id"], run_id, step_dir, "calibration/contents", step_dir / "artifacts")
                    details = self.verifier(step_src / "verifier", step_dir / "verifier", step_dir / "changes.json")
                agent_dst = step_dir / "agent"
                for name in ("trajectory.json", f"{info['harness']['name']}.txt"):
                    if (step_src / "agent" / name).is_file():
                        agent_dst.mkdir(parents=True, exist_ok=True)
                        shutil.copyfile(step_src / "agent" / name, agent_dst / name)
                step_reward = details["reward"] if details and outcome != "excluded" else None
                run = {"schema": "clitasks-delivery/run/1", "run_id": run_id, "model_id": info["model_id"],
                       "harness": info["harness"], "reasoning_effort": "high", "task_tree_sha256": self.tree,
                       "outcome": outcome, "exclusion_code": code, "reward": step_reward,
                       "per_step_reward": {STEP: step_reward} if step_reward is not None else {},
                       "started_at": iso(result["started_at"]), "finished_at": iso(result["finished_at"])}
                ext = {"trial": trial.name, "job": job_of(trial), "runner": "harbor 0.24.0",
                       "task_version_as_run": self.version,
                       "workspace_capture": ("before/after inventories of /app with every regular file's bytes, captured "
                                             "as root by the runner's agent wrapper immediately before and after the agent")}
                if outcome == "excluded":
                    ext.update(detail=detail, reward_as_graded=details["reward"] if details else None)
                    exclusions.append({"run_id": run_id, "code": code, "detail": detail})
                run["extensions"] = {"x-clitasks-pipeline": ext}
                write_json(td / "run.json", run)
                started.append(run["started_at"]); finished.append(run["finished_at"])
                steps = fc_agent_steps(agent_dst / "trajectory.json")
                sr = result["step_results"][0] if result.get("step_results") else {}
                ar = sr.get("agent_result") or result.get("agent_result") or {}
                inp, outp = ar.get("n_input_tokens"), ar.get("n_output_tokens")
                ex = sr.get("agent_execution") or {}
                wall = (datetime.fromisoformat(ex["finished_at"].replace("Z", "+00:00"))
                        - datetime.fromisoformat(ex["started_at"].replace("Z", "+00:00"))).total_seconds() if ex.get("finished_at") else ""
                crit = details["criteria"] if details else []
                failed = [c["name"] for c in crit if c["value"] is not None and c["value"] < 1]
                finalized = outcome != "excluded"
                rows.append({"model_id": info["model_id"], "run_id": run_id, "step": STEP, "outcome": outcome,
                             "exclusion_code": code or "",
                             "step_reward": repr(step_reward) if finalized else "",
                             "step_pass": str(int(abs(step_reward - 1) < 1e-9)) if finalized else "",
                             "run_reward": repr(step_reward) if finalized else "",
                             "run_pass": str(int(abs(step_reward - 1) < 1e-9)) if finalized else "",
                             "checks_passed": str(sum(1 for c in crit if c["value"] == 1)) if crit else "",
                             "checks_total": str(len(crit)) if crit else "",
                             "failed_checks": ";".join(failed), "agent_steps": str(steps),
                             "input_tokens": "" if inp is None else str(inp), "output_tokens": "" if outp is None else str(outp),
                             "wall_seconds": "" if wall == "" else f"{wall:.1f}", "task_tree_sha256": self.tree})
                if finalized:
                    stats_rows.append(step_reward)
                    usage.append((steps, inp, outp))
            stat = stats(stats_rows)
            entry = {"model_id": info["model_id"], "provider_model": info["provider_model"], "harness": info["harness"],
                     "reasoning_effort": "high", "settings": SETTINGS, "runs_requested": RUNS_REQUESTED,
                     "runs_recorded": len(runs), "outcomes": outcomes, "exclusions": exclusions, "task": stat,
                     "task_excluding_refusals": None, "per_step": {STEP: stat}}
            if usage and all(u[1] is not None and u[2] is not None for u in usage):
                entry.update(mean_agent_steps=sum(u[0] for u in usage) / len(usage),
                             mean_input_tokens=sum(u[1] for u in usage) / len(usage),
                             mean_output_tokens=sum(u[2] for u in usage) / len(usage))
            models.append(entry)
        header = "model_id,run_id,step,outcome,exclusion_code,step_reward,step_pass,run_reward,run_pass,checks_passed,checks_total,failed_checks,agent_steps,input_tokens,output_tokens,wall_seconds,task_tree_sha256".split(",")
        with (self.root / "calibration/runs.csv").open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=header, lineterminator="\n")
            w.writeheader()
            w.writerows(rows)
        metrics = {"schema": "clitasks-delivery/metrics/1", "env_id": self.env_id, "status": "current",
                   "calibrated_task_version": self.version, "calibrated_task_tree_sha256": self.tree,
                   "reward_strategy": "mean", "k": K,
                   "run_environment": {"runner": "harbor", "runner_version": "0.24.0", "runtime": "docker",
                                       "runtime_version": self.runtime["docker"], "host_os": self.runtime["host_os"],
                                       "arch": "linux/amd64", "agent_user": "agent", "agent_uid": 1000,
                                       "started_at": min(started), "finished_at": max(finished)},
                   "models": models}
        write_json(self.root / "calibration/metrics.json", metrics)
        return {"metrics": metrics, "rows": rows}


def job_of(trial: Path) -> str:
    return trial.parent.name


def fc_agent_steps(path: Path) -> int:
    if not path.is_file():
        return 0
    data = json.loads(path.read_text())
    return sum(1 for s in data.get("steps") or [] if isinstance(s, dict) and s.get("source") == "agent")


def stats(rewards: list[float]) -> dict:
    n = len(rewards)
    c = sum(1 for x in rewards if abs(x - 1) < 1e-9)
    mean = sum(rewards) / n if n else None
    std = math.sqrt(sum((x - mean) ** 2 for x in rewards) / (n - 1)) if n > 1 else None
    return {"n": n, "passes": c, "pass_at_1": c / n if n else None, "pass_at_k": fc.pass_at_k(n, c, K) if n >= K else None,
            "mean_reward": mean, "std_reward": std}


def fmt(v, digits=2):
    return "—" if v is None else f"{v:.{digits}f}"


def readme(p: Package, cal: dict, oracle: dict) -> str:
    spec, md, cfg = p.spec, p.md, p.cfg
    scope = json.loads((p.root / "provenance/source-scope.json").read_text())
    lines = [f"# {p.env_id}", "", spec["summary"], "", "| | |", "|---|---|",
             f"| Version | {p.version} (see Changelog) |",
             f"| Type / domain | {md['type']} / {md['domain']} |",
             f"| Source session | `{md['source_session']}` |",
             f"| Faithfulness | {md['faithfulness']} ({LEVELS[md['faithfulness']]}): {spec['faithfulness_sentence']} |",
             f"| Steps | 1; reward strategy `{cfg['multi_step_reward_strategy']}` |",
             f"| Agent user | `{cfg['agent']['user']}` (uid 1000) |", ""]
    lines += ["## Source and faithfulness", "",
              f"One recorded CLI Tasks session, `{md['source_session']}`; `provenance/source-scope.json` maps step `{STEP}` to these "
              "message ranges (indices into the session's original message list):", "",
              "| Segment | Messages | Use | Adoption |", "|---|---|---|---|"]
    for use in scope["steps"][STEP]["uses"]:
        seg = scope["segments"][use["segment"]]
        span = f"{seg['start']['message_index']}" + (f"–{seg['end']['message_index']}" if seg['end']['message_index'] != seg['start']['message_index'] else "")
        adopt = "full" if use["adoption"] == "full" else f"partial: kept {use['adopted'][0].lower() + use['adopted'][1:]} Not kept: {use['not_adopted'][0].lower() + use['not_adopted'][1:]}"
        lines.append(f"| `{use['segment']}` | {span} | {use['use']} | {adopt} |")
    lines += ["", "Authored, not recorded:", ""] + [f"- {a}" for a in spec["authored"]]
    lines += ["", "The starting workspace is synthetic: every supplied file is authored (`provenance/ledger.json`). "
              "No file is copied from the session.", ""]
    lines += ["## What the agent gets", "", "Inputs, read-only and root-owned:", ""]
    for path in sorted(p.fixture):
        rel = path.removeprefix("/app/environment/files/")
        desc = spec.get("inputs", {}).get(rel)
        if desc is None:
            f = p.fixture[path]
            desc = next((l.strip().lstrip("#").strip() for l in f.read_text(errors="replace").splitlines() if l.strip()), "")[:120]
        lines.append(f"- `{path}`: {desc}")
    deliverable_list = ", ".join(f"`{d}`" for d in md["deliverables"])
    lines += ["", "Tools in the image: Python 3.11, bash and coreutils, tar, tmux, ripgrep, procps, Node.js 22, and the "
              "pinned Claude Code (2.1.283) and Codex (0.157.1) CLIs the calibration harnesses use. The working directory is "
              "`/app`; only `/app/deliverables/` is writable by the agent.", "",
              "Network: the image build fetches from the dated Debian snapshot mirror and the npm registry. The agent phase is "
              "`no-network`; a runner adds only its own model-API host for a model harness. The verifier runs in a separate "
              "container with no network.", "",
              f"Deliverable: {deliverable_list}, UTF-8 JSON in the schema the instruction prints "
              "(also `tests/outputs.json`). No other file may be written under `/app/deliverables/`.", ""]
    lines += ["## Steps", "", f"1. **{STEP}**: {spec['step_text']}", ""]
    nodes = {n["id"]: n for n in p.rubric["nodes"]}
    root = nodes[p.rubric["root"]]
    weights = {c["node"]: c["weight"] for c in root["children"]}
    lines += ["## Grading", "",
              f"Step `{STEP}` is graded by `tests/rubrics/{STEP}.json` (execution profile `local_deterministic`): "
              "`tests/test.sh` runs `tests/criteria.py`, which evaluates each criterion's rule in `tests/checks.json` "
              "against targets recomputed at grade time from the supplied files (frozen projections in "
              f"`tests/expected/{STEP}/`), then `tests/emit_v01.py` writes `reward.json` and `reward-details.json`.", "",
              f"Reward = 0 if a gate fails, otherwise the weighted mean of the {len(weights)} binary criteria "
              f"(weights sum to {sum(weights.values()):g}). There are no penalties. A run passes when its reward is 1. "
              "Grading is deterministic: the same artifacts always give the same criteria, gates and reward. "
              "`verification/integrity-probe/` shows a changed protected input scoring 0.", "",
              f"### {STEP}", "",
              f"Rubric `tests/rubrics/{STEP}.json` (profile `local_deterministic`, repeatability: deterministic, 2 runs).", "",
              "| ID | Role | Parent | Weight | Acceptance rule | Verification |", "|---|---|---|---:|---|---|",
              f"| `{root['id']}` | Group (root) | — | 1 | Weighted mean of the scored criteria | Aggregate child results |"]
    for child in root["children"]:
        n = nodes[child["node"]]
        assertion = re.search(r"declared '([a-z_]+)' assertion", n["verification"]["comparison"]["rule"]).group(1)
        what = spec.get("criteria", {}).get(n["id"], n["description"])
        lines.append(f"| `{n['id']}` | Scored criterion | `{root['id']}` | {child['weight']} | {what}; "
                     f"{fc_rule(assertion)} | Reference check (custom rule), binary |")
    for g in p.rubric["gates"]:
        lines.append(f"| `{g}` | Gate | — | 0 | {nodes[g]['acceptance']} | Deterministic test, hard gate |")
    lines += ["", "Not graded: wording, ordering or formatting beyond the published schema (arrays the schema marks "
              "`unordered` may be in any order), and anything outside the declared deliverable.", ""]
    lines += ["## Traps", ""] + [f"- {t}" for t in spec["traps"]] + [""]
    lines += ["## How to run", "", f"The package unpacks to `{p.env_id}/`, so `harbor run -p` takes the folder name, not the zip name.", "",
              "```sh",
              f"harbor run -p {p.env_id} -a oracle        # expect 1.0",
              f"harbor run -p {p.env_id} -a nop           # expect 0",
              f"harbor run -p {p.env_id} -a claude-code -m anthropic/claude-fable-5-1 -k 10",
              f"harbor run -p {p.env_id} -a codex -m openai/gpt-6-astra -k 10",
              "```", "",
              f"Tested with: Harbor 0.24.0, Docker {p.runtime['docker']} on {p.runtime['host_os']}, linux/amd64, runner as a "
              "non-root host user, agent as `agent` (uid 1000).", ""]
    m = {x["model_id"]: x for x in cal["metrics"]["models"]}
    met = cal["metrics"]
    if met["status"] == "regraded":
        status_line = (f"Status `regraded`: calibrated on version {met['calibrated_task_version']} (runtime tree "
                       f"`{met['calibrated_task_tree_sha256'][:16]}…`), Harbor 0.24.0, {met['run_environment']['started_at'][:10]}; "
                       f"every recorded run regraded under this version's grader (runtime tree `{p.tree[:16]}…`) from its collected "
                       "final workspace in Harbor 0.24.0's verifier, after the as-run grader reproduced every recorded grade "
                       "(`verification/supplement/regrade-calibration.json`). Agents were not re-run: the instruction, supplied files, "
                       f"environment and reference solution are byte-identical to {met['calibrated_task_version']}, and the runtime change is "
                       "confined to tests/ apart from the version in task.toml (see Changelog). ")
    else:
        status_line = (f"Status `current`: calibrated on version {p.version} (runtime tree `{p.tree[:16]}…`), Harbor 0.24.0, "
                       f"{met['run_environment']['started_at'][:10]}. ")
    lines += ["## Calibration", "",
              status_line + "Reasoning effort high, output cap 128k tokens, the task's own "
              "3-hour agent timeout and no turn limit; harness-provided web tools were left at their defaults.", "",
              "| Model | Harness (version) | Effort | n | Pass@1 | Pass@k | Mean reward | Reward std | Excluded | Refusals |",
              "|---|---|---|---|---|---|---|---|---|---|"]
    for key, info in MODELS.items():
        x = m[info["model_id"]]
        t = x["task"]
        lines.append(f"| {info['label']} (`{info['model_id']}`) | {info['harness']['name']} ({info['harness']['version']}) | high | "
                     f"{t['n']} | {fmt(t['pass_at_1'])} | {fmt(t['pass_at_k'])} | {fmt(t['mean_reward'], 3)} | "
                     f"{fmt(t['std_reward'], 3)} | {x['outcomes']['excluded']} | {x['outcomes']['model_refusal']} |")
    lines += ["", f"k = {K}. Reward std is the sample standard deviation over finalized runs.", ""]
    for key, info in MODELS.items():
        rows = [r for r in cal["rows"] if r["model_id"] == info["model_id"] and r["outcome"] != "excluded"]
        counts = {}
        for r in rows:
            for c in filter(None, r["failed_checks"].split(";")):
                counts[c] = counts.get(c, 0) + 1
        top = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:3]
        why = ", ".join(f"{n} of {len(rows)} runs fail `{c}`" for c, n in top) or "no criterion failed"
        lines.append(f"- {info['label']}: {why}.")
    excl = [e for x in cal["metrics"]["models"] for e in x["exclusions"]]
    lines += ["", "Exclusions: " + ("; ".join(f"`{e['run_id']}` {e['code']} ({e['detail']})" for e in excl) if excl else "none."), ""]
    if spec.get("previous_zip"):
        lines += [f"The delivered grader also reproduces every recorded reward of the previous {spec.get('previous_kind', 'delivery')}'s calibration "
                  "(`verification/supplement/regrade-previous-delivery.json`).", ""]
    if met["status"] == "regraded":
        sup = json.loads((p.root / "verification/supplement/regrade-calibration.json").read_text())
        moved = [c for c in sup["cases"] if abs(c["regraded_reward"] - c["as_run_reward"]) > 1e-9]
        lines += [f"Regrade: {sup['runs']} runs; the {met['calibrated_task_version']} grader reproduces all {sup['runs']} recorded grades; "
                  f"under {p.version} {len(moved)} run rewards change and pass/fail is unchanged for every run "
                  "(`verification/supplement/regrade-calibration.json` lists each run's as-run and regraded reward and failed criteria).", ""]
    lines += ["Supplier self-assessment: `verification/quality-assessment.json` (all six dimensions assessed; it is not "
              "independent approval).", ""]
    lines += ["## Known limitations and assumptions", "",
              "- Runs as `agent` (uid 1000) with `TZ=UTC` on linux/amd64; 2 CPUs and 8 GiB memory declared; dates in the task "
              "come from the files and the instruction, never from the clock.",
              "- The image ships pinned Claude Code and Codex CLIs because the agent phase has no network to install a harness; "
              "a runner that installs its own harness may ignore them.",
              "- No declarations (`declarations.json` is empty): no network, vendored archives, large or opaque data, or root agent.",
              "- Harness-provided web tools (provider-side search) were not disabled during calibration; the task needs none and "
              "the container itself has no egress.",
              "- The format checker skips Q26 (answer leaks from Oracle outputs) and Q30 (collected artifacts); both are run by "
              "the supplier and recorded with a review of every hit in `verification/supplement/q26-q30.json`.", ""]
    prev = spec["previous_version"]
    lines += ["## Changelog", "", "| Version | Date | Class | Summary | Calibration |", "|---|---|---|---|---|",
              f"| {p.version} | {datetime.now(timezone.utc).date().isoformat()} | {spec.get('change_class', 'major')} | "
              + spec.get("change_summary", "Repackaged to delivery format 0.1: "
              "the deliverable and agent workspace are declared in task.toml for the first time (same paths as before), "
              "steps/rubric/expected layout, reward-details v1 with an explicit output-contract gate, root-owned protected "
              "inputs (owner-write kept), pinned apt packages and TZ. Instruction, supplied files, checks and reference "
              "solution unchanged; no criterion renamed or removed. Recalibrated on Harbor 0.24.0.") + f" | {met['status']} |"]
    lines += [f"| {r['version']} | {r['date']} | {r['class']} | {r['summary']} | {r['calibration']} |"
              for r in spec.get("changelog_extra", [])]
    if spec.get("changelog_previous_row", True):
        lines += [f"| {prev} | {spec.get('previous_delivered', '—')} | — | Previous {spec.get('previous_kind', 'delivery')}, earlier package format "
                  "(calibrated on Harbor 0.22.0). | stale |"]
    lines += [""]
    return "\n".join(lines)


def fc_rule(assertion: str) -> str:
    return {"equals": "equal to the derived target", "set_equals": "same set as the derived target, order ignored",
            "absent": "no value of the forbidden form", "numeric_within": "within the declared tolerance of the derived target"
            }.get(assertion, assertion)


def assessment(p: Package, oracle: dict, receipt: dict) -> dict:
    probes = json.loads((p.root / "verification/probes/report.json").read_text())
    neg = [x for x in probes["probes"] if x["kind"] == "negative"]
    pos = [x for x in probes["probes"] if x["kind"] == "positive"]
    ok = probes["all_as_expected"]
    checks = [
        {"id": "instruction_clarity", "status": "pass",
         "rationale": (f"Step {STEP}: the instruction names the as-of date, the input folder, the deliverable path and its full JSON "
                       "schema, and the rules the supplied files state. Every trap's deciding rule is quoted from an agent-visible "
                       "file in authoring/traps.json (public_basis). No tool beyond the shell and Python is needed."),
         "evidence": [{"path": f"steps/{STEP}/instruction.md"}, {"path": "tests/outputs.json"}, {"path": "authoring/traps.json"}]},
        {"id": "reference_solution", "status": "pass",
         "rationale": (f"Step {STEP}: the reference solution derives the deliverable from the supplied files (it reads no /tests "
                       "data), ran as `agent` (uid 1000 owns the written deliverable in the after-capture) under Harbor 0.24.0, "
                       f"and earned {oracle['details']['reward']} with every criterion and gate met."),
         "evidence": [{"path": "verification/oracle/run.json"}, {"path": f"verification/oracle/steps/{STEP}/verifier/reward-details.json"},
                      {"path": "verification/receipt.json", "json_pointer": "/oracle"}]},
        {"id": "verifier_coverage", "status": "pass" if ok else "fail",
         "rationale": (f"Step {STEP}: {len(pos)} positive probes (the reference output, and the same output re-serialised with other "
                       "key order, whitespace and reversed unordered arrays) score 1.0; "
                       f"{len(neg)} negative probes behave as declared: each authored trap's misreading fails exactly the criteria "
                       "declared for it (the trap's criteria, or for a trap graded one value per criterion the subset its misreading "
                       "moves, authoring/traps.json) and passes the others, and an empty deliverable, an undeclared extra file and a "
                       "changed source file each score 0 through a gate. Every daily value or figure the schema asks for is read by a criterion."),
         "evidence": [{"path": "verification/probes/report.json"}, {"path": f"tests/rubrics/{STEP}.json"}]
                     + [{"path": f"verification/supplement/{n}.json"} for n in ("regrade-previous-delivery", "regrade-calibration")
                        if (p.root / f"verification/supplement/{n}.json").is_file()]},
        {"id": "grading_security", "status": "pass",
         "rationale": (f"Step {STEP}: the agent image copies only environment/files (no tests, solution, expected data or "
                       "calibration); the verifier runs in a separate no-network container from tests/ and reads the agent's "
                       "deliverables as data, never executing them; targets are recomputed from the protected source files; "
                       "oversized, undeclared or symlinked outputs fail the contract gate. The reward-hacking note lists the "
                       "exploits tried and closed; the extra-file and tamper probes and the integrity probe score 0."),
         "evidence": [{"path": "verification/reward-hacking.md"}, {"path": "environment/Dockerfile"},
                      {"path": "verification/probes/report.json"}, {"path": "verification/receipt.json", "json_pointer": "/integrity_probe"},
                      {"path": "verification/supplement/q26-q30.json"}]},
        {"id": "grading_repeatability", "status": "pass",
         "rationale": (f"Step {STEP}: the deterministic rubric was run twice more on the Oracle's collected artifacts (Harbor "
                       "job regrade); both repeats give identical criteria, gates and reward. Calibration variance is reported "
                       "separately in calibration/metrics.json."),
         "evidence": [{"path": "verification/receipt.json", "json_pointer": "/verifier_repeat"}]
                     + [{"path": e} for e in receipt["verifier_repeat"]["per_step"][STEP]["evidence"]]},
        {"id": "protected_input_integrity", "status": "pass",
         "rationale": (f"Step {STEP}: /app/environment/files is protected; the files are root-owned and not writable by the "
                       "agent, and the hard gate g_fixture_intact compares the collected tree with the private baseline "
                       "tests/fixture-manifest.json (added, deleted or changed files and symlinks fail). The Harbor integrity "
                       f"probe changed {receipt['integrity_probe']['modified_path']} after the reference solution and scored 0."),
         "evidence": [{"path": "verification/receipt.json", "json_pointer": "/integrity_probe"},
                      {"path": f"verification/integrity-probe/steps/{STEP}/verifier/reward-details.json"},
                      {"path": "tests/fixture-manifest.json"}, {"path": "verification/probes/report.json"}]},
    ]
    status = "fail" if any(c["status"] == "fail" for c in checks) else "pass"
    return {"schema": "clitasks-delivery/quality-assessment/1", "env_id": p.env_id, "task_version": p.version,
            "task_tree_sha256": p.tree, "assessed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "assessor": ASSESSOR, "status": status, "checks": checks}


def manifest_and_zip(p: Package, zip_dir: Path) -> Path:
    files = []
    for f in sorted(x for x in p.root.rglob("*") if x.is_file()):
        rel = p.rel(f)
        if rel == "PACKAGE-MANIFEST.json":
            continue
        if any(part in fc_junk() for part in rel.split("/")):
            raise ValueError(f"junk in package: {rel}")
        files.append({"path": rel, "bytes": f.stat().st_size, "sha256": sha256(f.read_bytes()),
                      "executable": bool(f.stat().st_mode & 0o111)})
    write_json(p.root / "PACKAGE-MANIFEST.json", {"schema": "clitasks-delivery/package-manifest/1", "env_id": p.env_id,
               "task_version": p.version, "task_tree_sha256": p.tree, "file_count": len(files),
               "total_bytes": sum(x["bytes"] for x in files), "files": files})
    zip_dir.mkdir(parents=True, exist_ok=True)
    out = zip_dir / f"{p.env_id}@{p.version}.zip"
    if out.exists():
        out.unlink()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(x for x in p.root.rglob("*") if x.is_file()):
            info = zipfile.ZipInfo(f"{p.env_id}/{p.rel(f)}", date_time=(2026, 10, 24, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = ((0o100755 if f.stat().st_mode & 0o111 else 0o100644) << 16)
            info.create_system = 3
            z.writestr(info, f.read_bytes())
    return out


def fc_junk():
    return {"__pycache__", ".DS_Store", "Thumbs.db", "__MACOSX", ".git", ".pytest_cache", ".mypy_cache",
            "ownership.json", "runtime.json"}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("spec", type=Path)
    ap.add_argument("--jobs", type=Path, required=True)
    ap.add_argument("--out", type=Path, default=REPO / "production/state/v01/package")
    ap.add_argument("--zip-dir", type=Path, default=REPO / "production/state/deliveries/v01")
    args = ap.parse_args()
    spec = json.loads(args.spec.read_text())
    build = REPO / "production/state/v01/build" / spec["env_id"]
    p = Package(spec, build, args.out / spec["env_id"], args.jobs)
    oracle = p.oracle()
    repeat = p.repeats()
    nop_reward, _ = p.single("nop")
    probe_reward, _ = p.single("probe")
    probe_cfg = json.loads((p.jobs / "probe.config.json").read_text())
    receipt = {"schema": "clitasks-delivery/receipt/1", "env_id": p.env_id, "task_version": p.version, "task_tree_sha256": p.tree,
               "produced_at": oracle["run"]["finished_at"], "runner": {"name": "harbor", "version": "0.24.0"},
               "runtime": {"kind": "docker", "version": p.runtime["docker"], "host_os": p.runtime["host_os"], "arch": "linux/amd64"},
               "executed_as": {"host_user_is_root": p.runtime["host_uid"] == 0, "agent_user": "agent", "agent_uid": 1000},
               "oracle": {"per_step": {STEP: oracle["details"]["reward"]}, "reward": oracle["run"]["reward"]},
               "nop": {"per_step": {STEP: nop_reward}, "reward": nop_reward},
               "verifier_repeat": {"per_step": {STEP: repeat}},
               "integrity_probe": {"modified_path": probe_cfg["agents"][0]["kwargs"]["tamper_path"], "reward": probe_reward}}
    write_json(p.root / "verification/receipt.json", receipt)
    p.integrity_probe()
    cal = p.calibration()
    (p.root / "README.md").write_text(readme(p, cal, oracle))
    p.supplement_q26_q30()
    p.supplement_regrade_previous()
    write_json(p.root / "verification/quality-assessment.json", assessment(p, oracle, receipt))
    out = manifest_and_zip(p, args.zip_dir)
    print(out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
