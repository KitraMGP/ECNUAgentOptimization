#!/bin/bash
# E3.3 正式矩阵（配对区组：D L | L D | D L | L D | D L）
# A. multi_turn p2 pressure 5×2；B. long_life p2 pressure 5×2
# C. long_life p4 integration_pressure 5×2；D. branch p2 baseline 3×2
# E. branch_pressure p2 baseline 3×2；F. non-unified smoke；G. parallel=1 smoke
# RAM smoke：multi_turn pressure cache-ram 8192 1×2
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT/benchmark"
export E33_LOG_DIR=/tmp

run_pairs() { # $1=workload $2=variant $3=parallel $4=n_pairs $5=unified $6=cache_ram
  local i pol n_pairs=$4
  for i in $(seq 0 $((n_pairs - 1))); do
    if [ $((i % 2)) -eq 0 ]; then pol="default lru"; else pol="lru default"; fi
    for p in $pol; do
      unif=""
      [ "$5" = "1" ] && unif="--unified"
      uv run python scripts/e33_existing_workload_integration.py --workload "$1" --variant "$2" \
        --policy "$p" --parallel "$3" $unif --reps 1 --cache-ram "$6" \
        --output "results/e33/${1}_${2}_p${3}_${p}_rep${i}.json" > /dev/null 2>&1
      echo "  [$1 $2 p$3 $p rep$i] done"
    done
  done
}

pkill -x llama-server 2>/dev/null; sleep 3
run_pairs multi_turn pressure 2 5 1 0
pkill -x llama-server 2>/dev/null; sleep 3
run_pairs long_life pressure 2 5 1 0
pkill -x llama-server 2>/dev/null; sleep 3
run_pairs long_life integration_pressure 4 5 1 0
pkill -x llama-server 2>/dev/null; sleep 3
run_pairs branch baseline 2 3 1 0
pkill -x llama-server 2>/dev/null; sleep 3
run_pairs branch_pressure baseline 2 3 1 0
pkill -x llama-server 2>/dev/null; sleep 3
run_pairs multi_turn baseline 2 1 0 0   # F. non-unified smoke（lru 惰性）
pkill -x llama-server 2>/dev/null; sleep 3
run_pairs multi_turn baseline 1 1 1 0   # G. parallel=1 smoke
pkill -x llama-server 2>/dev/null; sleep 3
run_pairs multi_turn pressure 2 1 1 8192  # RAM smoke
pkill -x llama-server 2>/dev/null
echo "=== E3.3 DONE ==="
