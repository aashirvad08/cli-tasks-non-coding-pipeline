"""Ship a frozen task as a CLI Tasks delivery-format v0.1 package (Harbor 0.24.0 evidence).

    python3 -I production/tools/v01/ship_v01.py spec <frozen-tree> --version X.Y.Z   draft specs/<env-id>.json
    python3 -I production/tools/v01/ship_v01.py build <spec>                         runtime tree + grader probes
    python3 -I production/tools/v01/ship_v01.py run <spec> --ws POOL [--what verify,fable,astra]
    python3 -I production/tools/v01/ship_v01.py status <spec> --ws POOL
    python3 -I production/tools/v01/ship_v01.py pull <spec> --ws POOL
    python3 -I production/tools/v01/ship_v01.py assemble <spec>                      package, zip, format check

The flow after `b6ctl ship` (a task that met the bar and passed its audit):

  1. spec      drafts the per-task spec from the frozen tree: source segments from the
               trace seeds, domain, export member and hash. The prose fields (summary,
               step text, segment adoption, authored list, trap kinds, difficulty
               disclosures) are marked TODO and must be written by hand from the trace
               and traps.json: high level, no answers, no file locations.
  2. build     convert.py (runtime tree; its hash is fixed from here on) and probes.py.
  3. run       syncs the tree and the runner to the pool, stages the API keys in the
               pool's /dev/shm (mode 600, as coder_run does), then runs the verification
               set (Oracle, two verifier repeats, nop, integrity probe) and 10 Fable 5.1
               (Claude Code) + 10 GPT-6 Astra (Codex) runs on Harbor 0.24.0. The runner
               refuses to start a job whose runtime-tree hash differs from build's.
  4. pull      copies the job directories back (agent session dirs excluded).
  5. assemble  assemble.py, then the CLI Tasks format checker (with the source archive).

A changed runtime tree (task.toml, declarations.json, environment/, steps/, tests/)
makes every earlier run stale: rebuild, rerun, reassemble. Nothing here uploads.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
HERE = Path(__file__).resolve().parent
SPECS = HERE / "specs"
BUILD = REPO / "production/state/v01/build"
JOBS = REPO / "production/state/v01/jobs"
REPORTS = REPO / "production/state/v01/reports"
REMOTE_ROOT = "/Users/aashirvad/pipeline-non-coding-clitasks-envs"
CHECKER = Path.home() / "clitasks-rle-formatcheck-external"
CHECKER_PY = CHECKER / ".venv/bin/python"
ARCHIVE_DIR = Path.home() / "Downloads"
PROJECT_PY = REPO / ".venv/bin/python"
TODO = "TODO"


def sh(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, text=True, capture_output=True, **kw)


def ssh(ws: str, script: str, stdin=None, timeout=600) -> str:
    p = subprocess.run(["coder", "ssh", ws, "--", script], input=stdin, capture_output=True, timeout=timeout)
    out = (p.stdout + p.stderr).decode(errors="replace")
    return "\n".join(l for l in out.splitlines() if "version mismatch" not in l.lower() and "download v2" not in l
                     and "LIBARCHIVE.xattr" not in l)


def spec_cmd(tree: Path, version: str) -> int:
    cfg = tomllib.loads((tree / "task.toml").read_text())
    md = cfg["metadata"]
    env_id = cfg["task"]["name"].split("/", 1)[1]
    session = md["source_trace"]
    member = None
    for archive in sorted(ARCHIVE_DIR.glob("*.zip")):
        try:
            with zipfile.ZipFile(archive) as z:
                hits = [n for n in z.namelist() if n.endswith(f"sessions/{session}/data.json")]
            if len(hits) == 1:
                member = {"archive": re.sub(r"^\[[^\]]*\]\s*", "", archive.name), "member": hits[0],
                          "local_archive": archive.name}
                with zipfile.ZipFile(archive) as z:
                    data = json.loads(z.read(hits[0]))
                break
        except zipfile.BadZipFile:
            continue
    if member is None:
        print(f"REFUSED: no archive in {ARCHIVE_DIR} holds sessions/{session}/data.json")
        return 2
    messages = [t for t in data["traces"] if t["session_id"] == session][0]["messages"]
    seeds = sorted({int(s.split("::")[1]) for s in md.get("source_seeds", [])})
    segments = {}
    for i in seeds:
        role = messages[i]["role"]
        segments[f"msg-{i}"] = {"start": i, "end": i, "use": "request" if role == "user" else "context",
                                "adoption": "partial", "adopted": TODO, "not_adopted": TODO}
    spec = {"env_id": env_id, "source_tree": str(tree.relative_to(REPO)), "previous_version": cfg["task"]["version"],
            "version": version, "domain": re.sub(r"[^a-z0-9]+", "_", md.get("category", TODO).lower()).strip("_"),
            "prompt_language": "en", "export": member, "summary": TODO, "faithfulness_sentence": TODO,
            "step_text": TODO, "segments": segments, "authored": [TODO], "traps": [TODO], "difficulty_disclosures": [TODO],
            "x-notes": {"source_intent": md.get("source_intent"), "trace_correction": md.get("trace_correction")}}
    out = SPECS / f"{env_id}.json"
    if out.exists():
        print(f"REFUSED: {out} exists")
        return 2
    out.write_text(json.dumps(spec, indent=2, ensure_ascii=False) + "\n")
    print(f"{out}: fill every {TODO} by hand (merge or split msg-N segments; use request/standing_rule/"
          "correction only for spans with a user message), then: build")
    return 0


def load(spec: Path) -> dict:
    s = json.loads(spec.read_text())
    if TODO in json.dumps({k: v for k, v in s.items() if k != "x-notes"}):
        raise SystemExit(f"{spec}: unfinished {TODO} fields")
    return s


def build_cmd(spec: Path) -> int:
    s = load(spec)
    for step in (["convert.py", str(spec), "--force"], ["probes.py", str(BUILD / s["env_id"])]):
        p = sh([str(PROJECT_PY), "-I", str(HERE / step[0]), *step[1:]], cwd=REPO)
        print((p.stdout + p.stderr).strip())
        if p.returncode:
            return p.returncode
    return 0


def run_cmd(spec: Path, ws: str, what: list[str], runs: int) -> int:
    s = load(spec)
    env = s["env_id"]
    tar_bytes = subprocess.run(["tar", "--no-xattrs", "--no-mac-metadata", "-cf", "-", "--exclude=__pycache__",
                                "--exclude=verification/probes", "production/tools/v01", f"production/state/v01/build/{env}",
                                f"production/state/v01/build/{env}.task_tree_sha256"], cwd=REPO, capture_output=True,
                               env={"COPYFILE_DISABLE": "1", "PATH": "/usr/bin:/bin"}).stdout
    print(ssh(ws, f"cd {REMOTE_ROOT} && rm -rf production/state/v01/build/{env} && tar -xf - && echo synced", stdin=tar_bytes))
    keys = (REPO / "keys.env").read_bytes()
    print(ssh(ws, "python3 -c \"import os,sys; d=sys.stdin.buffer.read(); fd=os.open('/dev/shm/clitasks-keys.env', "
                  "os.O_WRONLY|os.O_CREAT|os.O_TRUNC, 0o600); os.write(fd, d); os.close(fd)\" && "
                  f"ln -sfn /dev/shm/clitasks-keys.env {REMOTE_ROOT}/keys.env && echo keys staged", stdin=keys))
    runtime = ("D=$(sg docker -c 'docker version --format {{.Server.Version}}'); OS=$(. /etc/os-release && echo \"$PRETTY_NAME\"); "
               f"mkdir -p {REMOTE_ROOT}/production/state/v01/jobs/{env}; "
               "printf '{\"docker\": \"%s\", \"host_os\": \"%s\", \"arch\": \"%s\", \"host_uid\": %s, \"host_user\": \"%s\"}\\n' "
               f"\"$D\" \"$OS\" \"$(uname -m)\" \"$(id -u)\" \"$(id -un)\" > {REMOTE_ROOT}/production/state/v01/jobs/{env}/runtime.json")
    print(ssh(ws, runtime))
    launches = []
    for w in what:
        args = "verify" if w == "verify" else f"{w} {runs} ''"
        launches.append(f"nohup setsid sg docker -c \"bash production/tools/v01/runner/campaign.sh {env} {args}\" "
                        f"> production/state/v01/jobs/{env}/{w}.log 2>&1 < /dev/null &")
    # `cd X; A & B &`: each launch is its own backgrounded command with its own redirections, so
    # no subshell keeps the ssh channel open, and every job starts from the repo root.
    print(ssh(ws, f"cd {REMOTE_ROOT} || exit 1; " + " ".join(launches)
              + f" sleep 5; tail -n {len(launches)} production/state/v01/jobs/{env}/tree-hashes.log"))
    return 0


def status_cmd(spec: Path, ws: str) -> int:
    env = load(spec)["env_id"]
    script = (f"cd {REMOTE_ROOT}/production/state/v01/jobs/{env} 2>/dev/null || exit 0; for l in *.log; do "
              "echo \"$l: $(grep -c CAMPAIGN-DONE $l) done, $(grep -oE '[0-9]+/[0-9]+ Mean: [0-9.]+' $l | tail -1)\"; done; "
              "for j in fable astra; do [ -d $j ] && echo \"$j: $(ls $j/*/result.json 2>/dev/null | wc -l) results\"; done")
    print(ssh(ws, script))
    return 0


def pull_cmd(spec: Path, ws: str) -> int:
    env = load(spec)["env_id"]
    JOBS.mkdir(parents=True, exist_ok=True)
    p = subprocess.run(["coder", "ssh", ws, "--", f"cd {REMOTE_ROOT}/production/state/v01/jobs && "
                        f"tar -cf - --exclude='*/sessions/*' --exclude='*/setup/*' {env}"], capture_output=True)
    x = subprocess.run(["tar", "-xf", "-"], input=p.stdout, cwd=JOBS, capture_output=True)
    print(f"pulled {JOBS / env} ({len(p.stdout)} bytes, tar exit {x.returncode})")
    return x.returncode


def assemble_cmd(spec: Path) -> int:
    s = load(spec)
    p = sh([str(CHECKER_PY), "-I", str(HERE / "assemble.py"), str(spec), "--jobs", str(JOBS / s["env_id"])], cwd=REPO)
    print((p.stdout + p.stderr).strip())
    if p.returncode:
        return p.returncode
    zip_path = Path(p.stdout.strip().splitlines()[-1])
    zip_path = zip_path if zip_path.is_absolute() else REPO / zip_path
    REPORTS.mkdir(parents=True, exist_ok=True)
    # The package declares the archive without the supplier tag (Q37); check Q23b against an
    # alias of the local copy that carries the declared name.
    # A hard link (the checker resolves symlinks), kept outside the repo: same bytes, no copy.
    alias = Path.home() / ".cache/clitasks-v01/source-archives" / s["export"]["archive"]
    alias.parent.mkdir(parents=True, exist_ok=True)
    if not alias.exists():
        os.link(ARCHIVE_DIR / s["export"].get("local_archive", s["export"]["archive"]), alias)
    archive = alias
    c = sh([str(CHECKER / ".venv/bin/clitasks-rle-formatcheck"), str(zip_path), "--source-archive", str(archive),
            "--require-source", "--supplier", "scaler", "--quiet", "--json", str(REPORTS / f"{zip_path.stem}.formatcheck.json")], cwd=CHECKER)
    print(c.stdout.strip())
    print(f"sha256 {hashlib.sha256(zip_path.read_bytes()).hexdigest()}  {zip_path.name}")
    q = json.loads((REPO / "production/state/v01/package" / s["env_id"] / "verification/supplement/q26-q30.json").read_text())
    pending = [h for h in q["q26"]["hits"] if h["review"] == "unreviewed"]
    print(f"Q26 (supplier) {q['q26']['status']}: {len(q['q26']['hits'])} hit(s), {len(pending)} unreviewed; Q30 {q['q30']['status']}")
    for h in pending:
        print(f"  REVIEW {h['in']}:{h['value']}  ...{h['context']}...  -> add to the spec's q26_reviews, then assemble again")
    return c.returncode or (4 if pending else 0)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("spec"); p.add_argument("tree", type=Path); p.add_argument("--version", required=True)
    for name in ("build", "assemble"):
        sub.add_parser(name).add_argument("spec", type=Path)
    for name in ("run", "status", "pull"):
        p = sub.add_parser(name); p.add_argument("spec", type=Path); p.add_argument("--ws", required=True)
        if name == "run":
            p.add_argument("--what", default="verify,fable,astra")
            p.add_argument("--runs", type=int, default=10)
    a = ap.parse_args()
    if a.cmd == "spec":
        return spec_cmd(a.tree.resolve(), a.version)
    if a.cmd == "build":
        return build_cmd(a.spec)
    if a.cmd == "run":
        return run_cmd(a.spec, a.ws, a.what.split(","), a.runs)
    if a.cmd == "status":
        return status_cmd(a.spec, a.ws)
    if a.cmd == "pull":
        return pull_cmd(a.spec, a.ws)
    return assemble_cmd(a.spec)


if __name__ == "__main__":
    sys.exit(main())
