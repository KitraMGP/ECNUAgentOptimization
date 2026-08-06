#!/bin/bash
# E2.5 正式矩阵（配对区组顺序：D P | P D | D P | P D | D P）
# RAM off 5×2 + RAM pressure(128MiB) 5×2 + RAM default 补 1 smoke + unified 1
set -u
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT/benchmark"
export E25_LOG_DIR=/tmp
ORDER=(default prefix-branch prefix-branch default default prefix-branch prefix-branch default default prefix-branch)

run_ram() { # $1=ram  $2=mib
  local i
  for i in "${!ORDER[@]}"; do
    uv run python scripts/e2_scan_e25.py --ram "$1" --ram-mib "$2" --policy "${ORDER[$i]}" --reps 1 --output-dir "results/e25/${1}_full" > /dev/null 2>&1
    echo "[$1 ${ORDER[$i]} rep$i] done"
  done
}

pkill -x llama-server 2>/dev/null; sleep 2
run_ram off 0
pkill -x llama-server 2>/dev/null; sleep 2
run_ram pressure 128
# RAM default 补第 2 个 smoke（default 策略，已有 prefix-branch 1 个）
pkill -x llama-server 2>/dev/null; sleep 2
uv run python scripts/e2_scan_e25.py --ram default --policy default --reps 1 --output-dir results/e25 > /dev/null 2>&1
echo "[default default smoke2] done"
# unified smoke（prefix-branch + RAM pressure）
pkill -x llama-server 2>/dev/null; sleep 2
uv run python scripts/e2_scan_e25.py --ram pressure --ram-mib 128 --policy prefix-branch --reps 1 --unified --output-dir results/e25 > /dev/null 2>&1
echo "[unified smoke] done"
pkill -x llama-server 2>/dev/null
echo "=== E2.5 DONE ==="
