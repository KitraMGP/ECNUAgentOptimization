#!/bin/bash
# E2.4：Long-Branch + RAM cache 边界实验
# B: tinyllama bc2 RAM off（default/prefix-branch）
# C: tinyllama bc4 RAM on（default/prefix-branch）
# E: 4B unified long smoke（prefix-branch）
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
TL_SRV="$ROOT/llama.cpp/build/bin/llama-server"
TL_MODEL="$(find "$ROOT/llama.cpp/tmp/models--ggml-org--test-model-stories260K" -name 'stories260K-f32.gguf' | head -1)"
CUDA_SRV="$ROOT/llama.cpp/build-cuda/bin/llama-server"
MODEL="$ROOT/models/qwen3-5-4B-Q4_K_M.gguf"
SLOT_SAVE="$ROOT/llama.cpp/tmp/e20_slot_save"
OUT="$ROOT/benchmark/results/e24"
mkdir -p "$SLOT_SAVE" "$OUT"
cd "$ROOT/benchmark"

start_srv() { # $1=server $2=model $3=policy $4=extra args...
  pkill -x llama-server; sleep 2
  nohup "$1" -m "$2" --host 127.0.0.1 --port 8080 --ctx-size 2048 --parallel 2 \
    --cache-reuse 0 --seed 42 --temp 0 --slot-routing-policy "$3" \
    --slot-routing-stats --slot-save-path "$SLOT_SAVE" "${@:4}" > "$OUT/srv_$3.log" 2>&1 &
  for i in $(seq 1 90); do
    curl -s -m 2 http://127.0.0.1:8080/health 2>/dev/null | grep -q ok && break
    sleep 1
  done
}

# B: tinyllama bc2 RAM off
for pol in default prefix-branch; do
  start_srv "$TL_SRV" "$TL_MODEL" "$pol" --cache-ram 0
  timeout 1200 uv run python scripts/e2_long_branch_probe.py --port 8080 --policy "$pol" \
    --branches 2 --reps 3 --output "$OUT/tl_bc2_ramoff_${pol}.json" > /dev/null 2>&1
  echo "=== B tl_bc2_ramoff_$pol done"
done

# C: tinyllama bc4 RAM on（默认 8192）
for pol in default prefix-branch; do
  start_srv "$TL_SRV" "$TL_MODEL" "$pol"
  timeout 1200 uv run python scripts/e2_long_branch_probe.py --port 8080 --policy "$pol" \
    --branches 4 --reps 3 --output "$OUT/tl_bc4_ramon_${pol}.json" > /dev/null 2>&1
  echo "=== C tl_bc4_ramon_$pol done"
done

# E: 4B unified long smoke（prefix-branch，bc2）
start_srv "$CUDA_SRV" "$MODEL" prefix-branch --kv-unified
timeout 900 uv run python scripts/e2_long_branch_probe.py --port 8080 --policy prefix-branch \
  --branches 2 --reps 1 --output "$OUT/4b_unified_bc2_smoke.json" > /dev/null 2>&1
echo "=== E 4b_unified_bc2_smoke done"
pkill -x llama-server
echo "=== E2.4 DONE ==="
