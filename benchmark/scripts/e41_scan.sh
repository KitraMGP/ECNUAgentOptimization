#!/bin/bash
# E4.1 场景矩阵（correctness 模式 trace on + production 模式 trace off）
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT/benchmark"
export E41_LOG_DIR=/tmp

run_one() { # $1=scenario $2=policy $3=rep $4=trace(0/1)
  tr=""; [ "$4" = "1" ] && tr="--trace"
  uv run python scripts/e41_kv_saturation.py --scenario "$1" --policy "$2" --reps 1 $tr \
    --output "results/e41/${1}_${2}_r${3}_trace${4}.json" > /dev/null 2>&1
  pkill -x llama-server 2>/dev/null; sleep 2
}

for sc in A B C D E; do
  for pol in default lru; do
    for i in 0 1; do
      run_one $sc $pol $i 1
    done
  done
  echo "  [$sc done]"
done
# production 模式（trace off）：A 最小饱和确认
for pol in default lru; do
  run_one A $pol prod 0
done
echo "=== E4.1 DONE ==="
