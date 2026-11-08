#!/bin/sh
# Grade one step: criteria.py evaluates the rubric's criteria, emit_v01.py writes
# /logs/verifier/reward.json and reward-details.json. A failure of either script
# leaves no reward.json: a verifier error, never a zero for the agent.
set -eu
PATH=/usr/local/bin:/usr/bin:/bin
mkdir -p /logs/verifier
python3 /tests/criteria.py > /logs/verifier/clitasks-grade.json
python3 /tests/emit_v01.py
