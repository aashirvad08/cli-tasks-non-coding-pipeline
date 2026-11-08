"""Assemble a CLI Tasks delivery-format v0.1 batch folder around finished packages, then run the batch check.

    <checker-venv>/python -I production/tools/v01/batch.py production/tools/v01/batches/<batch-id>@<version>.json [--force]

The batch spec names the supplier, batch ID and version, the output folder and, per environment, its zip
(already passed by the format checker), its change class in this delivery and a task summary of at most
300 characters (no answers). Everything else is read from the packages themselves, so the manifest and the
spreadsheet agree with the zips by construction:

  <out_dir>/CliTasks-Environments-<supplier>-<batch-id>@<batch-version>/
      COVER-LETTER.md / .pdf   section 3.4 headings, facts from the packages (PDF via headless Chrome)
      MANIFEST.json            section 3.2
      MANIFEST.xlsx            Models / Summary / Tasks tabs (section 3.5; Changes when it supersedes a batch)
      SHA256SUMS               every file but itself
      tasks/<env-id>@<version>.zip

Then `clitasks-rle-formatcheck --batch` runs with the source archive; the report goes to
production/state/v01/reports/. Nothing here uploads.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import io
import json
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import tomllib
import zipfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
CHECKER = Path.home() / "clitasks-rle-formatcheck-external"
sys.path.insert(0, str(CHECKER / "src"))
from clitasks_rle_formatcheck import checker as fc  # noqa: E402
import openpyxl  # noqa: E402  (the checker's venv has it)

REPORTS = REPO / "production/state/v01/reports"
ARCHIVES = Path.home() / ".cache/clitasks-v01/source-archives"
CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
STATS = [("n", "n"), ("Pass@1", "pass_at_1"), ("Pass@k", "pass_at_k"), ("Mean reward", "mean_reward"), ("Reward std", "std_reward")]
LEVEL_TEXT = {"F0": "F0 (Inspired), which takes the goal and kind of work",
              "F1": "F1 (Pattern), which reuses a failure pattern or rule shape from the session in authored material",
              "F2": "F2 (Scenario), which keeps the session's scenario and requests with authored records",
              "F3": "F3 (Replay), which replays the session's own records"}


def sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


class Pkg:
    """One delivered zip, read without unpacking except for the tree hash."""

    def __init__(self, zip_path: Path, entry: dict):
        self.zip, self.entry = zip_path, entry
        z = zipfile.ZipFile(zip_path)
        self.names = z.namelist()
        self.env_id = self.names[0].split("/")[0]
        read = lambda rel: z.read(f"{self.env_id}/{rel}")
        self.cfg = tomllib.loads(read("task.toml").decode())
        self.md = self.cfg["metadata"]
        self.version = self.cfg["task"]["version"]
        self.steps = [s["name"] for s in self.cfg["steps"]]
        self.metrics = json.loads(read("calibration/metrics.json"))
        self.runs = list(csv.DictReader(io.StringIO(read("calibration/runs.csv").decode())))
        self.rubrics = {s: json.loads(read(f"tests/rubrics/{s}.json")) for s in self.steps}
        self.instructions = {s: read(f"steps/{s}/instruction.md").decode() for s in self.steps}
        self.receipt = json.loads(read("verification/receipt.json"))
        self.declarations = json.loads(read("declarations.json"))["declarations"]
        self.q26 = json.loads(read("verification/supplement/q26-q30.json"))
        with tempfile.TemporaryDirectory(prefix="batch-") as tmp:
            root = fc.materialize(zip_path, Path(tmp))
            self.tree = fc.task_tree_sha256(root)
        self.models = {m["model_id"]: m for m in self.metrics["models"]}

    def timeout(self, step: str) -> int:
        s = next(x for x in self.cfg["steps"] if x["name"] == step)
        return s.get("agent", {}).get("timeout_sec", self.cfg["agent"]["timeout_sec"])

    def root_weight(self, step: str):
        rub = self.rubrics[step]
        root = next(n for n in rub["nodes"] if n["id"] == rub["root"])
        return 1 if root["kind"] == "criterion" else sum(c["weight"] for c in root["children"])

    def finalized(self, model: str, step: str) -> list[dict]:
        return [r for r in self.runs if r["model_id"] == model and r["step"] == step and r["outcome"] != "excluded"]

    def why(self, model: str, step: str) -> str:
        rows = self.finalized(model, step)
        counts = Counter(c for r in rows for c in filter(None, r["failed_checks"].split(";")))
        top = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:2]
        if not top:
            return f"No criterion failed in any of {len(rows)} runs."
        return "; ".join(f"{n} of {len(rows)} runs fail `{c}`" for c, n in top) + "."

    def reference_trace(self, step: str, model_ids: list[str]) -> str:
        for mid in model_ids:
            for r in sorted(self.finalized(mid, step), key=lambda r: r["run_id"]):
                path = f"calibration/trials/{mid}/{r['run_id']}/"
                if r["step_pass"] in ("1", "true", "True") and any(n.startswith(f"{self.env_id}/{path}") for n in self.names):
                    return path
        return "oracle"


def mean_of(rows: list[dict], fields: list[str]):
    if not rows or any(r.get(f) in (None, "") for r in rows for f in fields):
        return None
    return statistics.mean(sum(float(r[f]) for f in fields) for r in rows)


def manifest(spec: dict, pkgs: list[Pkg], created: str) -> dict:
    first = pkgs[0].metrics["models"]
    models = [{"model_id": m["model_id"], "provider_model": m["provider_model"], "harness": m["harness"],
               "reasoning_effort": m["reasoning_effort"], "runs_per_task": m["runs_requested"]} for m in first]
    k = {p.metrics["k"] for p in pkgs}
    if len(k) != 1:
        raise SystemExit(f"REFUSED: packages use different k: {sorted(k)}")
    for p in pkgs:
        mine = [{"model_id": m["model_id"], "provider_model": m["provider_model"], "harness": m["harness"],
                 "reasoning_effort": m["reasoning_effort"], "runs_per_task": m["runs_requested"]} for m in p.metrics["models"]]
        if mine != models:
            raise SystemExit(f"REFUSED: {p.env_id} was calibrated with other models or settings than {pkgs[0].env_id}")
    man = {"schema": "clitasks-delivery/batch-manifest/1", "delivery_format": "0.1", "batch_id": spec["batch_id"],
           "supplier": spec["supplier"], "supplier_aliases": spec.get("supplier_aliases", []),
           "batch_version": spec["batch_version"], "created_at": created, "supersedes_batch": spec.get("supersedes_batch"),
           "calibration_models": models, "pass_at_k": k.pop(), "tasks": []}
    for p in pkgs:
        man["tasks"].append({"env_id": p.env_id, "zip": f"tasks/{p.zip.name}", "zip_sha256": sha256(p.zip),
                             "zip_bytes": p.zip.stat().st_size, "task_version": p.version, "task_tree_sha256": p.tree,
                             "type": p.md["type"], "domain": p.md["domain"], "faithfulness": p.md["faithfulness"],
                             "source_session": p.md["source_session"], "reward_strategy": p.cfg["multi_step_reward_strategy"],
                             "steps": [{"name": s, "step_id": f"{p.env_id}/{s}"} for s in p.steps],
                             "calibration_status": p.metrics["status"], "change": p.entry["change"]})
    return man


def workbook(spec: dict, man: dict, pkgs: list[Pkg], path: Path) -> None:
    mids = [m["model_id"] for m in man["calibration_models"]]
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Models"
    for key in ("supplier", "batch_id", "batch_version"):
        ws.append([key, man[key]])
    ws.append([])
    ws.append(["Model ID", "Provider model", "Harness", "Harness version", "Reasoning effort", "Runs per task", "Runner",
               "Runner version", "Runtime", "Runtime version", "Agent user", "Run dates"])
    envs = [p.metrics["run_environment"] for p in pkgs]
    one = lambda field: (lambda u: u.pop() if len(u) == 1 else (_ for _ in ()).throw(SystemExit(f"REFUSED: packages differ in {field}: {u}")))({e[field] for e in envs})
    days = sorted({e[t][:10] for e in envs for t in ("started_at", "finished_at")})
    dates = days[0] if len(days) == 1 else f"{days[0]} to {days[-1]}"
    for m in man["calibration_models"]:
        ws.append([m["model_id"], m["provider_model"], m["harness"]["name"], m["harness"]["version"], m["reasoning_effort"],
                   m["runs_per_task"], one("runner"), one("runner_version"), one("runtime"), one("runtime_version"),
                   one("agent_user"), dates])

    ws = wb.create_sheet("Summary")
    header = ["Env ID", "Task version", "Source session", "Type", "Reward strategy", "Calibration status"]
    header += [h for _ in mids for h, _ in STATS] + ["Pooled mean reward"]
    ws.append(header)
    rows = []
    for p in pkgs:
        vals = [p.models[mid]["task"][key] for mid in mids for _, key in STATS]
        n = sum(p.models[mid]["task"]["n"] for mid in mids)
        pooled = sum((p.models[mid]["task"]["mean_reward"] or 0) * p.models[mid]["task"]["n"] for mid in mids) / n if n else None
        rows.append([p.env_id, p.version, p.md["source_session"], p.md["type"], p.cfg["multi_step_reward_strategy"],
                     p.metrics["status"], *vals, pooled])
    every = ["ALL"] + [None] * 5
    for col in range(6, len(header)):
        nums = [r[col] for r in rows if r[col] is not None]
        every.append(None if header[col] == "Reward std" or not nums else statistics.mean(nums))
    for r in rows + [every]:
        ws.append(r)

    ws = wb.create_sheet("Tasks")
    header = ["Step ID", "Env ID", "Task version", "Step", "Official", "Type", "Domain", "Faithfulness", "Source session",
              "Task summary", "Complete task instruction", "Root weight total", "Agent timeout (s)", "Calibration status"]
    for _ in mids:
        header += [h for h, _ in STATS] + ["Refusals", "Excluded", "Mean agent steps", "Mean tokens"]
    header += ["Task ZIP", "Reference trace"] + ["Why it fails"] * len(mids)
    ws.append(header)
    for p in pkgs:
        summary = p.entry["summary"]
        if not summary.strip() or len(summary) > 300:
            raise SystemExit(f"REFUSED: {p.env_id} summary must be 1-300 characters (has {len(summary)})")
        strategy = p.cfg["multi_step_reward_strategy"]
        for i, s in enumerate(p.steps):
            text = p.instructions[s]
            row = [f"{p.env_id}/{s}", p.env_id, p.version, f"{i + 1} of {len(p.steps)}",
                   "yes" if strategy == "mean" or i == len(p.steps) - 1 else "no",
                   {"coding": "Coding", "non-coding": "Non-Coding"}[p.md["type"]], p.md["domain"], p.md["faithfulness"],
                   p.md["source_session"], summary, text if len(text) <= 32767 else f"see zip: steps/{s}/instruction.md",
                   p.root_weight(s), p.timeout(s), p.metrics["status"]]
            for mid in mids:
                row += [p.models[mid]["per_step"][s][key] for _, key in STATS]
                recs = [r for r in p.runs if r["model_id"] == mid and r["step"] == s]
                fin = p.finalized(mid, s)
                row += [sum(r["outcome"] == "model_refusal" for r in recs), sum(r["outcome"] == "excluded" for r in recs),
                        mean_of(fin, ["agent_steps"]), mean_of(fin, ["input_tokens", "output_tokens"])]
            row += [f"tasks/{p.zip.name}", p.reference_trace(s, mids)] + [p.why(mid, s) for mid in mids]
            ws.append(row)

    if man.get("supersedes_batch"):
        ws = wb.create_sheet("Changes")
        ws.append(["Env ID", "Previous version", "New version", "Change class", "Summary", "Calibration status"])
        for p in pkgs:
            if p.entry["change"] != "unchanged":
                ws.append([p.env_id, p.entry.get("previous_version"), p.version, p.entry["change"],
                           p.entry.get("change_summary", ""), p.metrics["status"]])
    wb.save(path)


def cover_letter(spec: dict, man: dict, pkgs: list[Pkg]) -> str:
    mids = [m["model_id"] for m in man["calibration_models"]]
    steps = sum(len(p.steps) for p in pkgs)
    multi = sum(len(p.steps) > 1 for p in pkgs)
    types = sorted({p.md["type"] for p in pkgs})
    domains = ", ".join(sorted({p.md["domain"] for p in pkgs}))
    levels = Counter(p.md["faithfulness"] for p in pkgs)
    level_text = ", ".join(f"{levels[k]} {k}" for k in sorted(levels))
    rt = pkgs[0].receipt["runtime"]
    envs = [p.metrics["run_environment"] for p in pkgs]
    days = sorted({e[t][:10] for e in envs for t in ("started_at", "finished_at")})
    outcomes = Counter(r["outcome"] for p in pkgs for r in p.runs)
    settings = pkgs[0].metrics["models"][0]["settings"]
    non_root = all(not p.receipt["executed_as"]["host_user_is_root"] for p in pkgs)
    agent = {(p.receipt["executed_as"]["agent_user"], p.receipt["executed_as"]["agent_uid"]) for p in pkgs}
    if not non_root or len(agent) != 1:
        raise SystemExit("REFUSED: receipts were not all produced by a non-root runner with one agent user")
    agent_user, agent_uid = agent.pop()
    date = man["created_at"][:10]
    q26_values = sum(p.q26["q26"]["values_checked"] for p in pkgs)
    q26_hits = sum(len(p.q26["q26"]["hits"]) for p in pkgs)
    decl = Counter(d.get("kind", "?") for p in pkgs for d in p.declarations)

    L = [f"# CLI Tasks environments: {man['batch_id']} @ {man['batch_version']}", "", "## Summary", "",
         "| Field | Value |", "|---|---|",
         f"| Batch ID | `{man['batch_id']}` |", f"| Batch version | {man['batch_version']} |", f"| Date | {date} |",
         f"| Supplier | `{man['supplier']}` |", f"| Environments | {len(pkgs)} ({multi} with more than one step) |",
         f"| Steps | {steps} |", f"| Types | {', '.join(types)} |", f"| Domains | {domains} |",
         f"| Faithfulness levels | {level_text} |", f"| Delivery format | {man['delivery_format']} |", "",
         "Every environment is one zip, `tasks/<env-id>@<version>.zip`, holding all of its steps. `MANIFEST.json` is the "
         "machine-readable list and `MANIFEST.xlsx` the spreadsheet; `SHA256SUMS` covers every file in this folder.", "",
         "## What changed", ""]
    if man.get("supersedes_batch"):
        L += [f"This revision supersedes `{man['supersedes_batch']}`. " + spec.get("what_changed", ""), ""]
    else:
        L += ["New batch. " + spec.get("what_changed", ""), ""]
    L += ["| Environment | Previous version | Version in this batch | Change | Calibration |", "|---|---|---|---|---|"]
    for p in pkgs:
        L.append(f"| `{p.env_id}` | {p.entry.get('previous_version') or '—'} | {p.version} | {p.entry['change']} | {p.metrics['status']} |")
    L += ["", "## How these were built", "",
          "Each environment is reconstructed from one real CLI Tasks session. The session supplies the kind of work and the goal; "
          "the documents, records, people and organisations in the starting workspace are synthetic and authored. The "
          "faithfulness level records how much of the session is kept; this batch uses "
          + " and ".join(LEVEL_TEXT[k] for k in sorted(levels)) + ". Each package's `provenance/source-scope.json` names the "
          "session messages every step uses and how, and `provenance/ledger.json` the origin of every starting file. Graders "
          "are deterministic: checks are data (`tests/checks.json`) whose targets are recomputed from the starting workspace at "
          "grade time, and each step's rubric is `tests/rubrics/<step>.json`. Each step's reference solution "
          "(`steps/<step>/solution/solve.sh`) derives the answer from the workspace; it does not store it.", "",
          "## How to run", "",
          f"Pinned runner and platform for the receipts and the calibration: Harbor {pkgs[0].receipt['runner']['version']}, "
          f"Docker {rt['version']} on {rt['host_os']}, {rt['arch']} (native, no emulation). From the folder that holds the "
          "unpacked environment:", "", "```",
          "harbor run -p <env-id> -a oracle                          # expect 1.0 on every step",
          "harbor run -p <env-id> -a nop                             # expect 0",
          "harbor run -p <env-id> -a <harness> -m <model> -k <runs>  # a model run", "```", "",
          f"The receipts (`verification/receipt.json` in each package) were produced with the agent running as the declared "
          f"user `{agent_user}` (uid {agent_uid}) and the runner as a non-root host user.", "",
          "## Grading", "",
          "Rewards are in [0, 1]. A step's reward is the weighted mean of its rubric's scored criteria, which are binary; the "
          "weights of every rubric sum to 100. Every environment in this batch uses the `mean` reward strategy: the run reward "
          "is the mean of its step rewards, and a run passes when its reward is 1 (within 1e-9). Verification profile: local, "
          "deterministic; repeatability is checked by grading the same workspace twice (`verification/repeats/`). The "
          "integrity gate: modifying, moving or deleting any protected input under `/app/environment/files` sets the step "
          "reward to 0, as does an output that breaks the published output contract; `verification/integrity-probe/` shows a "
          "modified input scoring 0.", "",
          "## Calibration", "",
          "| Model ID | Provider model | Harness | Reasoning effort | Runs per environment |", "|---|---|---|---|---|"]
    for m in man["calibration_models"]:
        L.append(f"| `{m['model_id']}` | `{m['provider_model']}` | {m['harness']['name']} {m['harness']['version']} | "
                 f"{m['reasoning_effort']} | {m['runs_per_task']} |")
    span = days[0] if len(days) == 1 else f"between {days[0]} and {days[-1]}"
    L += ["", f"`k` = {man['pass_at_k']}. Every environment was calibrated on its delivered version (status `current`) with "
          f"Harbor {pkgs[0].receipt['runner']['version']} on {rt['arch']}, runs made {span}. Settings: output cap "
          f"{settings['max_output_tokens'] // 1000}k tokens, the task's own agent timeout ({settings['agent_timeout_sec'] // 3600} "
          "hours), no turn limit, and an agent phase with no network except the harness's own model API host. Outcomes over all "
          f"runs: {outcomes['complete']} complete, {outcomes['model_limit']} model limit, {outcomes['model_refusal']} refusal, "
          f"{outcomes['excluded']} excluded. The exclusion policy is the format's (infrastructure failures only, with evidence in "
          "`run.json`); refusals are kept at their recorded rewards and reported beside a refusal-free companion. "
          "Per-environment results are on the Summary tab of `MANIFEST.xlsx`; per-step results, the failing criteria and a "
          "reference trace per step are on its Tasks tab.", "",
          "| Environment | " + " | ".join(f"{mid} pass@1 (mean)" for mid in mids) + " |", "|---|" + "---|" * len(mids)]
    for p in pkgs:
        cells = [f"{p.models[mid]['task']['pass_at_1']:.2f} ({p.models[mid]['task']['mean_reward']:.3f})" for mid in mids]
        L.append(f"| `{p.env_id}` | " + " | ".join(cells) + " |")
    L += ["", "## Declarations", ""]
    if decl:
        L += ["; ".join(f"{n} declaration(s) of kind `{k}`" for k, n in sorted(decl.items())) +
              ". See each package's `declarations.json`.", ""]
    else:
        L += ["No environment declares an exception: every package's `declarations.json` lists none.", ""]
    L += ["## Known limitations", "",
          "- Task images ship pinned Claude Code and Codex CLIs, because the agent phase has no network to install a harness; a "
          "runner that installs its own harness may ignore them.",
          "- Harness-provided web tools (provider-side search) were not disabled during calibration; no task needs them, "
          "and the task container itself has no egress.",
          "- Task containers run with `TZ=UTC`; every date a task needs comes from its files and instruction, never from the clock.",
          "- The format checker reports Q26 (answer leaks from Oracle outputs) and Q30 (collected-artifact estimate) as SKIP, "
          "not implemented. Both were run with the supplier's implementation of their section-18 definitions "
          f"(`verification/supplement/q26-q30.json` in each package): Q26 searched {q26_values} expected values from the "
          f"Oracle outputs in every README and instruction and recorded a review verdict for each of its {q26_hits} hits "
          "(allowed values, schema keys and bounds, rubric weights, segment indices, dates and other given input facts; none "
          "states an answer); Q30 found every deliverable and protected path collected. No numeric ceiling is approved in v0.1.",
          "", "## Provenance and terms", "",
          "The environments are synthetic reconstructions of real CLI Tasks sessions. No personal data from the sessions is "
          "included: every person, organisation and record in a workspace is fictional, internal links and local paths were "
          "removed, and session text appears only where the provenance files quote the messages a step uses. Source sessions "
          "are identified by their CLI Tasks session IDs. Use is governed by the contract between the parties.", ""]
    return "\n".join(L)


def md_to_html(md: str, title: str) -> str:
    """The subset cover_letter() writes: headings, paragraphs, pipe tables, bullets, fenced code, inline code."""
    inline = lambda s: re.sub(r"`([^`]+)`", lambda m: f"<code>{m.group(1)}</code>", html.escape(s, quote=False))
    out, lines, i = [], md.splitlines(), 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("```"):
            j = i + 1
            while not lines[j].startswith("```"):
                j += 1
            out.append("<pre>" + html.escape("\n".join(lines[i + 1:j])) + "</pre>")
            i = j + 1
        elif line.startswith("#"):
            level = len(line) - len(line.lstrip("#"))
            out.append(f"<h{level}>{inline(line[level:].strip())}</h{level}>")
            i += 1
        elif line.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].startswith("|"):
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            body = [r for r in rows[1:] if not all(re.fullmatch(r":?-+:?", c) for c in r)]
            out.append("<table><tr>" + "".join(f"<th>{inline(c)}</th>" for c in rows[0]) + "</tr>"
                       + "".join("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in r) + "</tr>" for r in body) + "</table>")
        elif line.startswith("- "):
            items = []
            while i < len(lines) and lines[i].startswith("- "):
                items.append(lines[i][2:])
                i += 1
            out.append("<ul>" + "".join(f"<li>{inline(x)}</li>" for x in items) + "</ul>")
        elif line.strip():
            para = []
            while i < len(lines) and lines[i].strip() and not lines[i].startswith(("#", "|", "- ", "```")):
                para.append(lines[i])
                i += 1
            out.append(f"<p>{inline(' '.join(para))}</p>")
        else:
            i += 1
    css = ("body{font-family:-apple-system,Helvetica,Arial,sans-serif;font-size:10.5pt;line-height:1.45;margin:0;color:#111}"
           "h1{font-size:17pt;margin:0 0 10px}h2{font-size:13pt;margin:18px 0 6px;border-bottom:1px solid #ccc;padding-bottom:2px}"
           "table{border-collapse:collapse;margin:6px 0 10px;font-size:9.5pt}td,th{border:1px solid #bbb;padding:3px 6px;"
           "text-align:left;vertical-align:top}th{background:#f2f2f2}code{font-family:Menlo,monospace;font-size:9pt}"
           "pre{background:#f6f6f6;padding:8px;font-size:9pt;white-space:pre-wrap}@page{size:A4;margin:18mm 16mm}")
    return (f"<!doctype html><html><head><meta charset='utf-8'><title>{html.escape(title)}</title><style>{css}</style></head>"
            f"<body>{''.join(out)}</body></html>")


def pdf(html_text: str, out: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="cover-") as tmp:
        page = Path(tmp, "cover.html")
        page.write_text(html_text, encoding="utf-8")
        p = subprocess.run([str(CHROME), "--headless=new", "--disable-gpu", "--no-pdf-header-footer", "--no-first-run",
                            f"--user-data-dir={tmp}/profile", f"--print-to-pdf={out}", page.as_uri()],
                           capture_output=True, text=True, timeout=120)
        if p.returncode or not out.is_file():
            raise SystemExit(f"REFUSED: PDF rendering failed: {p.stderr[-400:]}")
    if b"/Users/" in out.read_bytes() or b"/private/" in out.read_bytes():
        raise SystemExit("REFUSED: the cover letter PDF carries a local path")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("spec")
    ap.add_argument("--force", action="store_true", help="replace an existing batch folder of this ID and version")
    args = ap.parse_args()
    spec = json.loads(Path(args.spec).read_text())
    for key in ("supplier", "batch_id"):
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", spec[key]):
            raise SystemExit(f"REFUSED: {key} {spec[key]!r} must match ^[a-z0-9][a-z0-9-]*$")
    name = f"CliTasks-Environments-{spec['supplier']}-{spec['batch_id']}@{spec['batch_version']}"
    out = (REPO / spec["out_dir"] / name).resolve()
    if out.exists():
        if not args.force:
            raise SystemExit(f"REFUSED: {out} exists (use --force to rebuild it)")
        shutil.rmtree(out)
    (out / "tasks").mkdir(parents=True)
    pkgs = []
    for entry in spec["tasks"]:
        src = REPO / entry["zip"]
        dest = out / "tasks" / src.name
        shutil.copyfile(src, dest)
        if sha256(dest) != sha256(src):
            raise SystemExit(f"REFUSED: copy of {src.name} differs")
        pkgs.append(Pkg(dest, entry))
    created = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    man = manifest(spec, pkgs, created)
    (out / "MANIFEST.json").write_text(json.dumps(man, indent=2, ensure_ascii=False) + "\n")
    workbook(spec, man, pkgs, out / "MANIFEST.xlsx")
    letter = cover_letter(spec, man, pkgs)
    (out / "COVER-LETTER.md").write_text(letter)
    pdf(md_to_html(letter, f"CLI Tasks environments: {man['batch_id']} @ {man['batch_version']}"), out / "COVER-LETTER.pdf")
    files = sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file() and p.name != "SHA256SUMS")
    (out / "SHA256SUMS").write_text("".join(f"{sha256(out / f)}  {f}\n" for f in files))
    print(f"built {out}")

    archives = [str(a) for a in sorted(ARCHIVES.glob("*.zip"))]
    REPORTS.mkdir(parents=True, exist_ok=True)
    report = REPORTS / f"{name}.batch.formatcheck.json"
    cmd = [str(CHECKER / ".venv/bin/clitasks-rle-formatcheck"), "--batch", str(out), "--require-source", "--quiet", "--json", str(report)]
    for a in archives:
        cmd += ["--source-archive", a]
    c = subprocess.run(cmd, capture_output=True, text=True, cwd=CHECKER)
    print(c.stdout.strip())
    print(f"report {report}")
    return c.returncode


if __name__ == "__main__":
    sys.exit(main())
