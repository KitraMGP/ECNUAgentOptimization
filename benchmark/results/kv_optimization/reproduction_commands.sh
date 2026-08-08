#!/usr/bin/env bash
# E6 KV Cache 优化——关键结果复现命令
# 环境：RTX 4060 Laptop 8GB / Qwen3.5-4B-Q4_K_M.gguf / llama.cpp build + build-cuda
# 两个 git 仓库：根（benchmark-enhance）+ llama.cpp（子仓库）

set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"

echo "== 0. 仓库状态 =="
git -C "$ROOT" rev-parse HEAD
git -C "$ROOT/llama.cpp" rev-parse HEAD
git -C "$ROOT" status --short
git -C "$ROOT/llama.cpp" status --short

echo "== 1. E6.1 未优化基线（4B GPU）=="
# 需要先启动 build-cuda llama-server（--kv-unified --ctx-size 8192 --parallel 4 --cache-ram 0）
cd "$ROOT/benchmark"
uv run python scripts/e6_runner.py --workload W01,W02,W07 --repetitions 1 --tag baseline_min
uv run python scripts/e6_runner.py --workload W10,W11 --repetitions 1 --tag baseline_min
uv run python scripts/e6_runner.py --workload W16 --cycles 12 --tag baseline_min
uv run python scripts/e6_summarize.py

echo "== 2. E6.2 C1 跨 slot 前缀共享（llama.cpp 代码改动）=="
cd "$ROOT/llama.cpp/tools/server/tests"
LLAMA_SERVER_BIN_PATH="$ROOT/llama.cpp/build/bin/llama-server" \
HF_ENDPOINT=https://hf-mirror.com LLAMA_CACHE="$ROOT/llama.cpp/tmp" \
python3 -m pytest unit/test_kv_prefix_share.py --noconftest -v

echo "== 3. E6.3 C2 离线 oracle（4B GPU）=="
cd "$ROOT/benchmark"
uv run python scripts/e6_c2_oracle.py

echo "== 4. E6.4 paired benchmark（TinyLlama 标准架构）=="
uv run python scripts/e6_c1_bench.py --reps 5

echo "== 5. E6.5 endurance 12 周期 =="
uv run python scripts/e6_c1_churn.py --cycles 12

echo "== 6. 测试 =="
cd "$ROOT/benchmark" && uv run pytest -q
cd "$ROOT/llama.cpp/tools/server/tests" && \
LLAMA_SERVER_BIN_PATH="$ROOT/llama.cpp/build/bin/llama-server" \
HF_ENDPOINT=https://hf-mirror.com LLAMA_CACHE="$ROOT/llama.cpp/tmp" \
python3 -m pytest unit/test_kv_prefix_share.py unit/test_active_pressure.py \
  unit/test_unified_idle_lifecycle.py unit/test_slot_routing.py unit/test_lifecycle_trace.py \
  --noconftest -q
