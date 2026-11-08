"""Run one Harbor 0.24.0 job on a v0.1 package (pool side).

    run24.py job --package DIR --kind oracle|nop|probe|fable|astra --runs N --jobs-dir DIR --name NAME
    run24.py regrade --source DIR/NAME --package DIR --jobs-dir DIR --name NAME

Every job runs the pinned Harbor (`HARBOR_BIN`, default ~/harbor24-env/.venv/bin/harbor)
as this non-root user, with the package's own task.toml: its declared agent user,
resources, timeouts and network policy. Model jobs add only the model API host to
the agent phase (`extra_allowed_hosts`); Oracle, nop and the integrity probe get no
credentials and no network. The capturing agents (clitasks_v01.capture_agents) record
the workspace before and after each agent step. `regrade` re-grades a finished job's
collected artifacts under the same verifier: the repeatability evidence.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
HARBOR = Path(os.environ.get("HARBOR_BIN", Path.home() / "harbor24-env/.venv/bin/harbor"))
MODELS = {
    "fable": {"model": "anthropic/claude-fable-5-1", "agent": "clitasks_v01.capture_agents:CapturingClaudeCode",
              "version": "2.1.283", "host": "api.anthropic.com", "key": "ANTHROPIC_API_KEY"},
    "astra": {"model": "openai/gpt-6-astra", "agent": "clitasks_v01.capture_agents:CapturingCodex",
              "version": "0.157.1", "host": "api.openai.com", "key": "OPENAI_API_KEY"},
}
MAX_OUTPUT_TOKENS = 128000
EFFORT = "high"
SECRET_SUFFIXES = ("API_KEY", "ACCESS_TOKEN", "AUTH_TOKEN", "SECRET_KEY")


def load_keys(path: Path) -> dict[str, str]:
    keys = {}
    for line in path.read_text().splitlines():
        line = line.strip().removeprefix("export ").strip()
        if "=" in line and not line.startswith("#"):
            name, value = line.split("=", 1)
            keys[name.strip()] = value.strip().strip("'\"")
    return keys


def job_config(args) -> tuple[dict, dict]:
    env = {k: v for k, v in os.environ.items() if not k.endswith(SECRET_SUFFIXES)}
    env["PYTHONPATH"] = str(HERE)
    package = str(Path(args.package).resolve())
    agent: dict = {"kwargs": {}}
    if args.kind in MODELS:
        m = MODELS[args.kind]
        keys = load_keys(Path(args.keys))
        env[m["key"]] = keys[m["key"]]
        agent.update(import_path=m["agent"], model_name=m["model"], extra_allowed_hosts=[m["host"]],
                     kwargs={"reasoning_effort": EFFORT, "version": m["version"]})
        if args.kind == "fable":
            env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(MAX_OUTPUT_TOKENS)
        else:
            agent["kwargs"]["config"] = {"model_max_output_tokens": MAX_OUTPUT_TOKENS}
    elif args.kind in ("oracle", "probe"):
        agent.update(import_path="clitasks_v01.capture_agents:CapturingOracle", kwargs={"task_dir": package})
        if args.kind == "probe":
            agent["kwargs"]["tamper_path"] = args.tamper_path
    elif args.kind == "nop":
        agent.update(import_path="clitasks_v01.capture_agents:CapturingNop")
    else:
        raise SystemExit(f"unknown kind {args.kind}")
    config = {"job_name": args.name, "jobs_dir": str(Path(args.jobs_dir).resolve()),
              "n_attempts": args.runs, "n_concurrent_trials": args.concurrency or args.runs, "quiet": True,
              "retry": {"max_retries": 0}, "environment": {"type": "docker", "delete": True},
              "agents": [agent], "tasks": [{"path": package}]}
    return config, env


def regrade(args) -> int:
    """Re-run the package's own verifier on a recorded job's collected artifacts."""
    env = {k: v for k, v in os.environ.items() if not k.endswith(SECRET_SUFFIXES)}
    env["PYTHONPATH"] = str(HERE)  # the recorded agent class is re-imported
    jobs = Path(args.jobs_dir).resolve()
    if (jobs / args.name).exists():
        raise SystemExit(f"job {args.name} already exists in {jobs}")
    return subprocess.run([str(HARBOR), "job", "regrade", str(Path(args.source).resolve()),
                           "--task-path", str(Path(args.package).resolve()), "--env", "docker",
                           "--job-name", args.name, "--jobs-dir", str(jobs), "-n", "1"], env=env).returncode


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    j = sub.add_parser("job")
    j.add_argument("--package", required=True)
    j.add_argument("--kind", required=True)
    j.add_argument("--runs", type=int, default=1)
    j.add_argument("--concurrency", type=int)
    j.add_argument("--jobs-dir", required=True)
    j.add_argument("--name", required=True)
    j.add_argument("--keys", default=str(Path.cwd() / "keys.env"))
    j.add_argument("--tamper-path", default=None)
    r = sub.add_parser("regrade")
    r.add_argument("--source", required=True)
    r.add_argument("--package", required=True)
    r.add_argument("--jobs-dir", required=True)
    r.add_argument("--name", required=True)
    args = ap.parse_args()
    if args.cmd == "regrade":
        return regrade(args)
    config, env = job_config(args)
    jobs = Path(config["jobs_dir"])
    jobs.mkdir(parents=True, exist_ok=True)
    path = jobs / f"{args.name}.config.json"
    if path.exists() or (jobs / args.name).exists():
        raise SystemExit(f"job {args.name} already exists in {jobs}")
    path.write_text(json.dumps(config, indent=2) + "\n")
    os.chmod(path, 0o600)
    proc = subprocess.run([str(HARBOR), "run", "-c", str(path)], env=env)
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
