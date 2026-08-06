#!/bin/bash
# E2.3：TinyLlama 标准架构逻辑收益量化（3 配置 × 3 reps，non-unified）
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SERVER="$ROOT/llama.cpp/build/bin/llama-server"
MODEL="$(find "$ROOT/llama.cpp/tmp/models--ggml-org--test-model-stories260K" -name 'stories260K-f32.gguf' | head -1)"
SLOT_SAVE="$ROOT/llama.cpp/tmp/e20_slot_save"
OUT="$ROOT/benchmark/results/e23"
mkdir -p "$SLOT_SAVE" "$OUT"
cd "$ROOT/benchmark"

run_tl() { # $1=parallel $2=policy $3=tag
  pkill -x llama-server; sleep 2
  nohup "$SERVER" -m "$MODEL" --host 127.0.0.1 --port 8080 --ctx-size 2048 \
    --parallel "$1" --cache-reuse 0 --seed 42 --temp 0 \
    --slot-routing-policy "$2" --slot-routing-stats \
    --slot-save-path "$SLOT_SAVE" > "$OUT/srv_$3.log" 2>&1 &
  for i in $(seq 1 60); do
    curl -s -m 2 http://127.0.0.1:8080/health 2>/dev/null | grep -q ok && break
    sleep 1
  done
  timeout 1200 uv run python scripts/e2_a2_probe.py --port 8080 --policy "$2" --reps 3 \
    --output "$OUT/tl_$3.json" > /dev/null 2>&1
  echo "=== tl_$3 done"
}

run_tl 1 default tl_p1_default
run_tl 2 default tl_p2_default
run_tl 2 prefix-branch tl_p2_prefix_branch
pkill -x llama-server
echo "=== TL DONE ==="
