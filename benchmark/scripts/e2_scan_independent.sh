#!/bin/bash
# E2.0.5：B0/B1 独立基线扫描（Independent replicate 协议）
#
# 用途：重做最小可信 B0/B1 基线 —— 每个正式 replicate 前清除 KV 并断言
# /metrics/kv used_cells=0 且 active_sequences=0（--replicate-mode independent）。
#
# 配置矩阵（ABBA 交叉顺序对抗环境漂移）：
#   multi_turn: cr=0 -> 256 -> 256 -> 0
#   long_life : cr=0 -> 256 -> 256 -> 0
#   另各跑一个 stateful soak（--replicate-mode soak）展示 KV 跨 cycle 累积。
#
# 用法：bash scripts/e2_scan_independent.sh [--workload multi_turn|long_life]
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SERVER="$ROOT/llama.cpp/build-cuda/bin/llama-server"
MODEL="$ROOT/models/qwen3-5-4B-Q4_K_M.gguf"
SLOT_SAVE="$ROOT/llama.cpp/tmp/e20_slot_save"
OUT="$ROOT/benchmark/results/e205"
mkdir -p "$SLOT_SAVE" "$OUT"
cd "$ROOT/benchmark"

WORKLOAD="${1:-all}"   # multi_turn | long_life | all

BIN_HASH="$(sha256sum "$SERVER" | cut -d' ' -f1)"
MODEL_HASH="$(sha256sum "$MODEL" | cut -d' ' -f1)"
LLAMA_HEAD="$(git -C "$ROOT/llama.cpp" rev-parse HEAD)"
ROOT_HEAD="$(git -C "$ROOT" rev-parse HEAD)"
TS="$(date +%Y%m%d_%H%M%S)"
MANIFEST="$OUT/manifest_${TS}.json"

run_cfg() { # $1=workload $2=cache_reuse $3=mode($4=rounds)
  local wl="$1" cr="$2" mode="$3" rds="$4"
  pkill -x llama-server; sleep 3
  nohup "$SERVER" -m "$MODEL" --host 127.0.0.1 --port 8080 -ngl 99 \
    --ctx-size 2048 --parallel 1 --cache-reuse "$cr" --seed 42 --temp 0 \
    --slot-save-path "$SLOT_SAVE" > "$OUT/srv_${wl}_cr${cr}_${mode}.log" 2>&1 &
  for i in $(seq 1 60); do
    curl -s -m 2 http://127.0.0.1:8080/health 2>/dev/null | grep -q ok && break
    sleep 1
  done
  local tag="${wl}_cr${cr}_${mode}"
  if [ "$wl" = "long_life" ]; then
    timeout 3000 uv run python agent_bench.py --scenario long_life --long-rounds "$rds" \
      --warmup 1 --repeat 5 --kv-probe --seed 42 --ctx-size 2048 --parallel 1 \
      --replicate-mode "$mode" --output-dir "$OUT/$tag" > /dev/null 2>&1
  else
    timeout 3000 uv run python agent_bench.py --scenario multi_turn --rounds "$rds" \
      --warmup 1 --repeat 5 --kv-probe --seed 42 --ctx-size 2048 --parallel 1 \
      --replicate-mode "$mode" --output-dir "$OUT/$tag" > /dev/null 2>&1
  fi
  echo "=== $tag done: $(ls "$OUT/$tag" 2>/dev/null | tail -1)"
}

echo "{\"ts\":\"$TS\",\"server_binary_sha256\":\"$BIN_HASH\",\"model_sha256\":\"$MODEL_HASH\",\"llama_commit\":\"$LLAMA_HEAD\",\"root_commit\":\"$ROOT_HEAD\",\"ctx\":2048,\"parallel\":1,\"temperature\":0,\"seed\":42,\"warmup\":1,\"repeat\":5,\"configs\":[]}" > "$MANIFEST"

if [ "$WORKLOAD" = "all" ] || [ "$WORKLOAD" = "multi_turn" ]; then
  # ABBA：cr=0 -> 256 -> 256 -> 0
  run_cfg multi_turn 0    independent 20
  run_cfg multi_turn 256  independent 20
  run_cfg multi_turn 256  independent 20
  run_cfg multi_turn 0    independent 20
  run_cfg multi_turn 256  soak 20
fi
if [ "$WORKLOAD" = "all" ] || [ "$WORKLOAD" = "long_life" ]; then
  run_cfg long_life 0    independent 12
  run_cfg long_life 256  independent 12
  run_cfg long_life 256  independent 12
  run_cfg long_life 0    independent 12
  run_cfg long_life 256  soak 12
fi
echo "=== E2.0.5 SCAN ALL DONE ==="
