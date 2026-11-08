#!/bin/bash
# Pool side. Runs one environment's Harbor 0.24.0 jobs for its v0.1 package.
#   campaign.sh <env-id> verify            Oracle, two verifier repeats on its artifacts, nop, integrity probe
#   campaign.sh <env-id> fable|astra N TAG model runs: N attempts in job "<model><TAG>"
set -u
ROOT=/Users/aashirvad/pipeline-non-coding-clitasks-envs
cd "$ROOT" || exit 1
ENV_ID=$1; WHAT=$2
PKG=production/state/v01/build/$ENV_ID
J=production/state/v01/jobs/$ENV_ID
RUN="$HOME/harbor24-env/.venv/bin/python -I production/tools/v01/runner/run24.py"
mkdir -p "$J"
# Every job runs on the runtime tree convert.py recorded; refuse anything else.
TREE=$("$HOME/harbor24-env/.venv/bin/python" -I -c "import sys; sys.path.insert(0, 'production/tools/v01'); from convert import task_tree_sha256; print(task_tree_sha256(sys.argv[1]))" "$PKG")
WANT=$(cat "production/state/v01/build/$ENV_ID.task_tree_sha256")
echo "$(date -u +%FT%TZ) $WHAT ${4:-} tree=$TREE" >> "$J/tree-hashes.log"
[ "$TREE" = "$WANT" ] || { echo "runtime tree $TREE differs from recorded $WANT" >&2; exit 3; }
case "$WHAT" in
  verify)
    $RUN job --package "$PKG" --kind oracle --runs 1 --jobs-dir "$J" --name oracle || exit 1
    $RUN regrade --source "$J/oracle" --package "$PKG" --jobs-dir "$J" --name repeat-1 || exit 1
    $RUN regrade --source "$J/oracle" --package "$PKG" --jobs-dir "$J" --name repeat-2 || exit 1
    $RUN job --package "$PKG" --kind nop --runs 1 --jobs-dir "$J" --name nop || exit 1
    TAMPER=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['entries'][0]['path'])" "$PKG/provenance/ledger.json")
    $RUN job --package "$PKG" --kind probe --runs 1 --jobs-dir "$J" --name probe --tamper-path "$TAMPER" || exit 1
    ;;
  fable|astra)
    $RUN job --package "$PKG" --kind "$WHAT" --runs "$3" --jobs-dir "$J" --name "$WHAT$4" || exit 1
    ;;
  *) echo "unknown action $WHAT" >&2; exit 2 ;;
esac
echo "CAMPAIGN-DONE $ENV_ID $WHAT"
