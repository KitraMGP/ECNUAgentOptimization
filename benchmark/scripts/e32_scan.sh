#!/bin/bash
# E3.2 正式矩阵（配对区组：D L | L D | D L | L D | D L）
# A. p4_hot_cold_order 5×2；B. p2_single_candidate 3×2；C. active_protection 3×2
# D. non_unified_smoke 1×2；E. RAM default smoke（p4_hot_cold, cache-ram 8192）2×2
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT/benchmark"
export E32_LOG_DIR=/tmp

run_pairs() { # $1=scenario  $2=n_pairs  $3=cache_ram
  local i pol n_pairs=$2
  for i in $(seq 0 $((n_pairs - 1))); do
    if [ $((i % 2)) -eq 0 ]; then
      pol="default lru"
    else
      pol="lru default"
    fi
    for p in $pol; do
      uv run python scripts/e32_unified_lifecycle_value_probe.py --scenario "$1" --policy "$p" \
        --reps 1 --cache-ram "$3" --output "results/e32/${1}_${p}_rep${i}.json" > /dev/null 2>&1
      echo "  [$1 $p rep$i] done"
    done
  done
}

pkill -x llama-server 2>/dev/null; sleep 3
run_pairs p4_hot_cold_order 5 0
pkill -x llama-server 2>/dev/null; sleep 3
run_pairs p2_single_candidate 3 0
pkill -x llama-server 2>/dev/null; sleep 3
run_pairs active_protection 3 0
pkill -x llama-server 2>/dev/null; sleep 3
run_pairs non_unified_smoke 1 0
pkill -x llama-server 2>/dev/null; sleep 3
run_pairs p4_hot_cold_order 2 8192
pkill -x llama-server 2>/dev/null
echo "=== E3.2 DONE ==="
