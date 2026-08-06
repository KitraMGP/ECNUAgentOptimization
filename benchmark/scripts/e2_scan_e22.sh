#!/bin/bash
# E2.2：A2 快速验证实验（3 reps 快速验证 + unified smoke）
# - non-unified：default/prefix-branch 各 3 个 independent replicates（带 output hash）
# - unified：default/prefix-branch 各 1 个 smoke（--kv-unified，验证边界）
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SERVER="$ROOT/llama.cpp/build-cuda/bin/llama-server"
MODEL="$ROOT/models/qwen3-5-4B-Q4_K_M.gguf"
SLOT_SAVE="$ROOT/llama.cpp/tmp/e20_slot_save"
OUT="$ROOT/benchmark/results/e22"
mkdir -p "$SLOT_SAVE" "$OUT"
cd "$ROOT/benchmark"

start_srv() { # $1=policy $2=unified(0/1)
  pkill -x llama-server; sleep 3
  local unified=""
  [ "$2" = "1" ] && unified="--kv-unified"
  nohup "$SERVER" -m "$MODEL" --host 127.0.0.1 --port 8080 -ngl 99 \
    --ctx-size 2048 --parallel 2 --cache-reuse 0 --seed 42 --temp 0 \
    --slot-routing-policy "$1" --slot-routing-stats $unified \
    --slot-save-path "$SLOT_SAVE" > "$OUT/srv_${1}_u${2}.log" 2>&1 &
  for i in $(seq 1 60); do
    curl -s -m 2 http://127.0.0.1:8080/health 2>/dev/null | grep -q ok && break
    sleep 1
  done
}

# non-unified 快速验证（3 reps）
start_srv default 0
uv run python scripts/e2_a2_probe.py --port 8080 --policy default --reps 3 \
  --output "$OUT/branch_probe_nonunified_default_r3.json"
start_srv prefix-branch 0
uv run python scripts/e2_a2_probe.py --port 8080 --policy prefix-branch --reps 3 \
  --output "$OUT/branch_probe_nonunified_prefix_branch_r3.json"

# unified smoke（1 rep）
start_srv default 1
uv run python scripts/e2_a2_probe.py --port 8080 --policy default --reps 1 \
  --output "$OUT/branch_probe_unified_default_smoke.json"
start_srv prefix-branch 1
uv run python scripts/e2_a2_probe.py --port 8080 --policy prefix-branch --reps 1 \
  --output "$OUT/branch_probe_unified_prefix_branch_smoke.json"
pkill -x llama-server
echo "=== E2.2 PROBE DONE ==="
