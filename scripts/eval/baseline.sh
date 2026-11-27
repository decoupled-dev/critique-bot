#!/bin/sh
# SPDX-License-Identifier: Apache-2.0
# The Stage 0 baseline in one command, for the machine that can reach the model server.
#
#   scripts/eval/baseline.sh CRIT_CONFIG OPENCODE_MODEL [RUNS] [OUT] [NOTE]
#
#   CRIT_CONFIG     crit config file naming the server, e.g. {"backend": "local",
#                   "url": "http://192.168.1.50:8080", "model": "qwen-27b", "allowed_hosts": ["192.168.1.50"]}
#   OPENCODE_MODEL  opencode's provider/model for the same server (set up in opencode.json)
#   RUNS            trials per task (default 3)
#   OUT             results folder (default plan/results/00)
#   NOTE            server, quant, context size, flags: saved in meta.json
#
# Run it from a checkout of the commit you want to measure. The text-protocol baseline
# (before native tool calls) is commit 0d8204e on feat/aaos-scale.
set -eu
here=$(cd "$(dirname "$0")/../.." && pwd)
config=${1:?crit config file}
model=${2:?opencode provider/model}
runs=${3:-3}
out=${4:-$here/plan/results/00}
note=${5:-}
py=${PYTHON:-python3}
cd "$here"
export PYTHONPATH="$here/src${PYTHONPATH:+:$PYTHONPATH}"

echo "== grader audit"
"$py" -m critique_bot.eval_harness audit
echo "== crit ($runs runs per task)"
"$py" -m critique_bot.eval_harness run --config "$config" --runs "$runs" --out "$out/crit" --note "$note" || true
if command -v opencode >/dev/null 2>&1; then
    echo "== opencode ($runs runs per task)"
    "$py" -m critique_bot.eval_harness run --runner opencode --model "$model" --runs "$runs" \
        --out "$out/opencode" --note "$note" || true
    "$py" -m critique_bot.eval_harness compare "$out/opencode" "$out/crit" --out "$out/baseline.md"
else
    echo "opencode is not on PATH; only crit was measured"
fi
echo "results in $out"
