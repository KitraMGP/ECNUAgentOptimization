#!/bin/bash
# E3.4 正式矩阵（配对区组 D L | L D 交替）
# 1. multi_session multi_turn pressure 5×2；2. long_life pressure 5×2
# 3/4. no_pressure 3×2；7. non-unified smoke 1×2；8. parallel=1 smoke 1×2
# （branch/branch_pressure regression 复用 E3.3，不重跑）
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT/benchmark"
export E34_LOG_DIR=/tmp

run_pairs() { # $1=workload $2=pressure(0/1) $3=n_pairs $4=extra
  local i pol n_pairs=$3
  for i in $(seq 0 $((n_pairs - 1))); do
    if [ $((i % 2)) -eq 0 ]; then pol="default lru"; else pol="lru default"; fi
    for p in $pol; do
      pr=""; [ "$2" = "1" ] && pr="--pressure"
      uv run python scripts/e34_multi_session_workload_gate.py --workload "$1" $pr \
        --policy "$p" --reps 1 $4 \
        --output "results/e34/${1}_${2}_${p}_rep${i}.json" > /dev/null 2>&1
      echo "  [$1 pressure=$2 $p rep$i] done"
    done
  done
}

pkill -x llama-server 2>/dev/null; sleep 3
run_pairs multi_turn 1 5 ""
pkill -x llama-server 2>/dev/null; sleep 3
run_pairs long_life 1 5 ""
pkill -x llama-server 2>/dev/null; sleep 3
run_pairs multi_turn 0 3 ""
pkill -x llama-server 2>/dev/null; sleep 3
run_pairs long_life 0 3 ""
pkill -x llama-server 2>/dev/null; sleep 3
run_pairs multi_turn 0 1 "--parallel 2 --non-unified"
pkill -x llama-server 2>/dev/null; sleep 3
run_pairs multi_turn 0 1 "--parallel 1"
pkill -x llama-server 2>/dev/null
echo "=== E3.4 DONE ==="
