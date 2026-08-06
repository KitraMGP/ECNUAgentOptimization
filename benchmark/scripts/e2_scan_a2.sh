#!/bin/bash
# E2.1-A2：B0/B1/B2 实验矩阵（independent replicate 协议）
#
# 配置（ABBA 交叉顺序）：
#   parallel=1：B0(cr0,default) B1(cr256,default) B2-0(cr0,prefix-branch) B2-1(cr256,prefix-branch)
#     × multi_turn(20轮) + long_life(12轮)
#   parallel=2：B1(cr256,default) vs B2-1(cr256,prefix-branch) × multi_turn(20轮)（机制收益场景）
#
# 每 replicate 前：slot erase + /metrics/kv 断言 used_cells=0 && active_sequences=0
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SERVER="$ROOT/llama.cpp/build-cuda/bin/llama-server"
MODEL="$ROOT/models/qwen3-5-4B-Q4_K_M.gguf"
SLOT_SAVE="$ROOT/llama.cpp/tmp/e20_slot_save"
OUT="$ROOT/benchmark/results/e21"
mkdir -p "$SLOT_SAVE" "$OUT"
cd "$ROOT/benchmark"

BIN_HASH="$(sha256sum "$SERVER" | cut -d' ' -f1)"
MODEL_HASH="$(sha256sum "$MODEL" | cut -d' ' -f1)"
LLAMA_HEAD="$(git -C "$ROOT/llama.cpp" rev-parse HEAD)"
TS="$(date +%Y%m%d_%H%M%S)"
echo "{\"ts\":\"$TS\",\"server_binary_sha256\":\"$BIN_HASH\",\"model_sha256\":\"$MODEL_HASH\",\"llama_commit\":\"$LLAMA_HEAD\",\"ctx\":2048,\"temperature\":0,\"seed\":42,\"warmup\":1,\"repeat\":5,\"replicate_mode\":\"independent\"}" > "$OUT/manifest_${TS}.json"

run_cfg() { # $1=wl $2=cr $3=policy $4=parallel $5=rounds
  local wl="$1" cr="$2" pol="$3" par="$4" rds="$5"
  pkill -x llama-server; sleep 3
  nohup "$SERVER" -m "$MODEL" --host 127.0.0.1 --port 8080 -ngl 99 \
    --ctx-size 2048 --parallel "$par" --cache-reuse "$cr" --seed 42 --temp 0 \
    --slot-routing-policy "$pol" --slot-save-path "$SLOT_SAVE" \
    > "$OUT/srv_${wl}_cr${cr}_${pol}_p${par}.log" 2>&1 &
  for i in $(seq 1 60); do
    curl -s -m 2 http://127.0.0.1:8080/health 2>/dev/null | grep -q ok && break
    sleep 1
  done
  local tag="${wl}_cr${cr}_${pol}_p${par}"
  if [ "$wl" = "long_life" ]; then
    timeout 3000 uv run python agent_bench.py --scenario long_life --long-rounds "$rds" \
      --warmup 1 --repeat 5 --kv-probe --seed 42 --ctx-size 2048 --parallel "$par" \
      --replicate-mode independent --output-dir "$OUT/$tag" > /dev/null 2>&1
  else
    timeout 3000 uv run python agent_bench.py --scenario multi_turn --rounds "$rds" \
      --warmup 1 --repeat 5 --kv-probe --seed 42 --ctx-size 2048 --parallel "$par" \
      --replicate-mode independent --output-dir "$OUT/$tag" > /dev/null 2>&1
  fi
  echo "=== $tag done: $(ls "$OUT/$tag" 2>/dev/null | tail -1)"
}

# parallel=1 正式矩阵（B0/B1/B2-0/B2-1 × 两 workload）
for cfg in "multi_turn 0 default" "multi_turn 256 default" "multi_turn 0 prefix-branch" "multi_turn 256 prefix-branch" \
           "long_life 0 default" "long_life 256 default" "long_life 0 prefix-branch" "long_life 256 prefix-branch"; do
  set -- $cfg
  if [ "$1" = "long_life" ]; then RDS=12; else RDS=20; fi
  run_cfg "$1" "$2" "$3" 1 "$RDS"
done
# parallel=2 机制收益场景（B1 vs B2-1）
run_cfg multi_turn 256 default 2 20
run_cfg multi_turn 256 prefix-branch 2 20
echo "=== E2.1-A2 SCAN DONE ==="
