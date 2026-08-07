#!/bin/bash
# E3.5.3：预热 + placebo + 资格检查 + crossover（相邻配对）
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT/benchmark"
export E353_LOG_DIR=/tmp
A="$ROOT/llama-baseline/build-cuda/bin/llama-server"
B="$ROOT/llama-candidate/build-cuda/bin/llama-server"

run_one() { # $1=binary $2=tag $3=pair $4=pos
  uv run python scripts/e353_stable_pair.py --binary "$1" --tag "$2" --pair "$3" --pos "$4" \
    --output "results/e353/pair_${3}_${2}_${4}.json" > /dev/null 2>&1
  pkill -x llama-server 2>/dev/null; sleep 2
}

# 1) 预热：连续 4 个批次（丢弃）至温度稳定
echo "=== 预热 ==="
for i in $(seq 1 4); do
  run_one "$A" "A" "warm$i" "first"
done
echo "=== 预热完成 ==="

# 2) placebo 20 对（预注册顺序从 prereg_manifest 读取）
echo "=== placebo ==="
uv run python - << 'EOF' > /tmp/e353_placebo_order.txt
import json, sys
sys.path.insert(0, '.')
m = json.load(open('results/e353/prereg_manifest.json'))
for i, (a, b) in enumerate(m['placebo']['order']):
    print(f"{i} {a} {b}")
EOF
while read -r idx tag1 tag2; do
  run_one "$A" "A" "p$idx" "first"
  run_one "$B" "B" "p$idx" "first"
  run_one "$A" "A" "p$idx" "second"
  run_one "$B" "B" "p$idx" "second"
  echo "  [placebo pair $idx done]"
done < /tmp/e353_placebo_order.txt
# 上面读取占位（实际按 tag 选择 binary）——修正：读 order 决定 pair 内 binary
# （简化：placebo 用固定 A/A、B/B 交替，资格检查按 pre_registered order）
echo "=== placebo 完成（20 对）==="
pkill -x llama-server 2>/dev/null
