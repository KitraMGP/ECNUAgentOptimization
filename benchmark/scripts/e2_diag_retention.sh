#!/bin/bash
# E2.0.5：4B 保真诊断（诊断配置，不替换正式 baseline）
#
# 目标：判定 long_life state_retention_rate=0 的来源
#   - 模型能力（无论配置都记不住）
#   - ctx truncation（更大 ctx 后是否改善）
#   - evaluator 解析（子串匹配，理论无碍）
#
# 诊断矩阵（ctx-size × rounds 交叉，均 temperature=0 seed=42）：
#   ctx=2048, rounds=12   （正式配置，对照）
#   ctx=8192, rounds=12   （更大 ctx → 无截断）
#   ctx=2048, rounds=4    （更短会话：secret 轮 2 注入，轮 4 询问）
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SERVER="$ROOT/llama.cpp/build-cuda/bin/llama-server"
MODEL="$ROOT/models/qwen3-5-4B-Q4_K_M.gguf"
SLOT_SAVE="$ROOT/llama.cpp/tmp/e20_slot_save"
OUT="$ROOT/benchmark/results/e205_diag"
mkdir -p "$SLOT_SAVE" "$OUT"
cd "$ROOT/benchmark"

run_diag() { # $1=ctx $2=rounds
  local tag="ll_ctx${1}_r${2}"
  pkill -x llama-server; sleep 3
  nohup "$SERVER" -m "$MODEL" --host 127.0.0.1 --port 8080 -ngl 99 \
    --ctx-size "$1" --parallel 1 --cache-reuse 256 --seed 42 --temp 0 \
    --slot-save-path "$SLOT_SAVE" > "$OUT/srv_${tag}.log" 2>&1 &
  for i in $(seq 1 60); do
    curl -s -m 2 http://127.0.0.1:8080/health 2>/dev/null | grep -q ok && break
    sleep 1
  done
  timeout 3000 uv run python agent_bench.py --scenario long_life --long-rounds "$2" \
    --warmup 1 --repeat 3 --kv-probe --seed 42 --ctx-size "$1" --parallel 1 \
    --replicate-mode independent --output-dir "$OUT/$tag" > /dev/null 2>&1
  echo "=== $tag done"
}

run_diag 2048 12
run_diag 8192 12
run_diag 2048 4
echo "=== E2.0.5 DIAG DONE ==="
