"""Harbor 0.24 agents that capture the agent workspace around each agent step.

Delivery format v0.1 §12.4 needs, for every graded step, the workspace inventory
immediately before the agent starts and after it stops, before the verifier runs,
with the exact bytes of every regular file. Each class here wraps a stock Harbor
agent: it captures `SCOPE` as root, runs the stock agent unchanged, and captures
again in a `finally` block, so a failed or timed-out agent still leaves its
"after" state. Captures are written beside the trial (`<trial>/clitasks-capture/`),
never into a directory mounted into the container.

    capture  = <label>.json  inventory: path, kind, mode, bytes, sha256, target
               <label>.tar   the regular files' bytes, by path without the leading /
               <label>.meta  captured_at (UTC, taken just before the capture command)
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from harbor.agents.installed.claude_code import ClaudeCode
from harbor.agents.installed.codex import Codex
from harbor.agents.nop import NopAgent
from harbor.agents.oracle import OracleAgent
from harbor.models.trial.paths import TrialPaths

SCOPE = ("/app",)
REMOTE = "/tmp/clitasks-capture"

#: Runs inside the agent container. Walks the scope without following links,
#: records every entry, and tars the regular files. Any other file type is an
#: error: the v0.1 inventory cannot represent it and must not omit it.
CAPTURE_SCRIPT = r'''
import hashlib, json, os, stat, sys, tarfile
out, roots = sys.argv[1], sys.argv[2:]
entries = []
with tarfile.open(out + ".tar", "w", format=tarfile.PAX_FORMAT) as tar:
    def record(path):
        st = os.lstat(path)
        mode = "%04o" % stat.S_IMODE(st.st_mode)
        if stat.S_ISLNK(st.st_mode):
            entries.append({"path": path, "kind": "symlink", "mode": mode, "uid": st.st_uid, "target": os.readlink(path)})
        elif stat.S_ISDIR(st.st_mode):
            entries.append({"path": path, "kind": "directory", "mode": mode, "uid": st.st_uid})
        elif stat.S_ISREG(st.st_mode):
            with open(path, "rb") as f:
                data = f.read()
            entries.append({"path": path, "kind": "file", "mode": mode, "uid": st.st_uid, "bytes": len(data),
                            "sha256": hashlib.sha256(data).hexdigest()})
            info = tarfile.TarInfo(path.lstrip("/"))
            info.size, info.mode = len(data), stat.S_IMODE(st.st_mode)
            import io
            tar.addfile(info, io.BytesIO(data))
        else:
            raise SystemExit("unrepresentable file type in scope: " + path)
    for root in roots:
        if not os.path.lexists(root):
            continue
        record(root)
        if os.path.islink(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            dirnames.sort()
            for name in sorted(dirnames + filenames):
                record(os.path.join(dirpath, name))
            dirnames[:] = [d for d in dirnames if not os.path.islink(os.path.join(dirpath, d))]
entries.sort(key=lambda e: e["path"])
with open(out + ".json", "w") as f:
    json.dump({"scope": list(roots), "entries": entries}, f)
'''


async def capture(environment, dest: Path, label: str) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    script = dest / "capture.py"
    script.write_text(CAPTURE_SCRIPT)
    remote_script, remote_out = f"{REMOTE}-{label}.py", f"{REMOTE}-{label}"
    await environment.upload_file(source_path=script, target_path=remote_script)
    captured_at = datetime.now(timezone.utc)
    result = await environment.exec(
        command=f"python3 -I {remote_script} {remote_out} {' '.join(SCOPE)}", user="root", timeout_sec=600)
    if result.return_code != 0:
        raise RuntimeError(f"workspace capture {label} failed: {(result.stderr or result.stdout or '')[-500:]}")
    await environment.download_file(source_path=f"{remote_out}.json", target_path=dest / f"{label}.json")
    await environment.download_file(source_path=f"{remote_out}.tar", target_path=dest / f"{label}.tar")
    await environment.exec(command=f"rm -f {remote_script} {remote_out}.json {remote_out}.tar", user="root")
    (dest / f"{label}.meta").write_text(json.dumps({"captured_at": captured_at.isoformat().replace("+00:00", "Z")}))


class _Capturing:
    """Mixin: capture before and after the wrapped agent's run(), once per step."""

    _clitasks_step = 0

    def _capture_dir(self) -> Path:
        # logs_dir is <trial>/agent, which Docker mounts at /logs/agent; write beside it.
        return Path(self.logs_dir).parent / "clitasks-capture"

    async def run(self, instruction, environment, context):
        index = self._clitasks_step
        self._clitasks_step += 1
        dest = self._capture_dir()
        await capture(environment, dest, f"step{index}-before")
        try:
            await super().run(instruction, environment, context)
            await self._after_agent(environment)
        finally:
            await capture(environment, dest, f"step{index}-after")


class CapturingClaudeCode(_Capturing, ClaudeCode):
    async def _after_agent(self, environment):
        return None


class CapturingCodex(_Capturing, Codex):
    async def _after_agent(self, environment):
        return None


class CapturingNop(_Capturing, NopAgent):
    async def _after_agent(self, environment):
        return None


class CapturingOracle(_Capturing, OracleAgent):
    """The stock Oracle (step solution as the declared agent user), with captures.

    Constructed by import path, so Harbor does not pass the Oracle-only arguments;
    they are rebuilt from the trial directory and the `task_dir` kwarg. With
    `tamper_path`, a protected file is modified as root after the solution ran:
    the integrity probe, which must score 0.
    """

    def __init__(self, logs_dir, task_dir, tamper_path: str | None = None, **kwargs):
        kwargs.pop("trial_paths", None)
        super().__init__(logs_dir=logs_dir, task_dir=Path(task_dir),
                         trial_paths=TrialPaths(trial_dir=Path(logs_dir).parent), **kwargs)
        self._tamper_path = tamper_path

    async def _after_agent(self, environment):
        if self._tamper_path:
            result = await environment.exec(
                command=f"printf '\\n' >> '{self._tamper_path}'", user="root")
            if result.return_code != 0:
                raise RuntimeError("integrity probe could not modify the protected file")
