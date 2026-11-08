#!/bin/sh
set -eu
mkdir -p /logs/verifier
export CLITASKS_REWARD=/logs/verifier/reward.json
export CLITASKS_DETAILS=/logs/verifier/reward-details.json
python3 /tests/criteria.py > /logs/verifier/clitasks-grade.json
