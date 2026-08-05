#!/usr/bin/env bash
# download_models.sh — 下载本项目所需的 GGUF 模型（ModelScope 国内源，可复现）
#
# 用法:
#   ./download_models.sh          # 下载全部模型（默认 all）
#   ./download_models.sh 0.8b     # 只下载指定模型（0.5b / 0.8b / 4b）
#
# 说明:
#   - 模型文件较大（4b 约 2.7GB），不入 git 仓库，统一由本脚本拉取
#   - 幂等：已下载的文件自动跳过
#   - 来源: ModelScope（国内直连更快）
set -euo pipefail

BASE="https://www.modelscope.cn"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

# key -> "repo|file"
declare -A MODELS=(
  ["0.5b"]="Qwen/Qwen2.5-0.5B-Instruct-GGUF|qwen2.5-0.5b-instruct-q4_k_m.gguf"
  ["0.8b"]="unsloth/Qwen3.5-0.8B-GGUF|Qwen3.5-0.8B-Q4_K_M.gguf"
  ["4b"]="diodel/Qwen3.5-4B-Q4_K_M-GGUF|qwen3-5-4B-Q4_K_M.gguf"
)

download() {
  local repo="$1" file="$2"
  local url="$BASE/models/$repo/resolve/master/$file"
  if [[ -f "$file" ]]; then
    echo "[跳过] $file 已存在"
    return
  fi
  echo "[下载] $file"
  echo "  来源: $url"
  wget -q --show-progress -O "$file" "$url"
  echo "[完成] $file"
}

target="${1:-all}"
if [[ "$target" == "all" ]]; then
  for key in 0.5b 0.8b 4b; do
    IFS='|' read -r repo file <<< "${MODELS[$key]}"
    download "$repo" "$file"
  done
else
  if [[ -z "${MODELS[$target]:-}" ]]; then
    echo "未知目标: $target（可用: 0.5b 0.8b 4b all）" >&2
    exit 1
  fi
  IFS='|' read -r repo file <<< "${MODELS[$target]}"
  download "$repo" "$file"
fi

echo ""
echo "全部完成。当前模型文件："
ls -la *.gguf 2>/dev/null || echo "（无 gguf 文件）"
