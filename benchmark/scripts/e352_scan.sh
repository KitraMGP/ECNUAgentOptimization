#!/bin/bash
# E3.5.2：ABBA + placebo 测量编排（后台运行，~2 小时）
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT/benchmark"
export E352_LOG_DIR=/tmp
A="$ROOT/llama-baseline/build-cuda/bin/llama-server"
B="$ROOT/llama.cpp/build-cuda/bin/llama-server"

run_one() { # $1=binary $2=tag $3=block $4=seq
  uv run python scripts/e352_overhead_gate.py --binary "$1" --tag "$2" --block "$3" \
    --output "results/e352/block_${3}_${2}_${4}.json" > /dev/null 2>&1
  pkill -x llama-server 2>/dev/null; sleep 2
}

# 30 ABBA blocks（A B B A / B A A B 交替；每 server 独立文件）
for i in $(seq 0 29); do
  if [ $((i % 2)) -eq 0 ]; then order="A1 B1 B2 A2"; else order="B1 A1 A2 B2"; fi
  for slot in $order; do
    tag="${slot:0:1}"; seq="${slot:1}"
    if [ "$tag" = "A" ]; then run_one "$A" "A" "$i" "$seq"; else run_one "$B" "B" "$i" "$seq"; fi
  done
  echo "  [ABBA block $i done]"
done

# 10 placebo blocks（A A 配对）
for i in $(seq 0 9); do
  run_one "$A" "A" "p$i" "1"
  run_one "$A" "A" "p$i" "2"
  echo "  [placebo block $i done]"
done
pkill -x llama-server 2>/dev/null
echo "=== E3.5.2 DONE ==="
