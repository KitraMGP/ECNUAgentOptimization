# E7.1：上游 llama.cpp 对比报告（Upstream Comparison）

- 复核时间：2026-08-08
- 对比对象：本地 llama.cpp `f5837427`（E7.2 修复后）vs 上游 `https://github.com/ggml-org/llama.cpp.git` master `3653e6d6d`（fetch 成功，网络可用）
- 状态：**UPSTREAM_COMPARISON_COMPLETE**（浅克隆限制注明：fetch 为 `--depth 1` 最新 master 快照，未遍历完整提交历史）

## 1. 本地与上游基线

| 项 | 本地 | 上游 master |
|---|---|---|
| HEAD | `f5837427b`（E7.2 修复）| `3653e6d6d`（tts timings #26733）|
| remote | `KitraMGP/ECNUAgentOptimization-llama.cpp.git` | `ggml-org/llama.cpp.git` |
| server-context.cpp 行数 | 6151 | 5434 |
| 关键 API | `llama_model_is_hybrid/is_recurrent` 均有 | 均有（本地 backport 或同源）|

## 2. 功能对比

| 功能 | 上游 | 本地 | 判定 |
|---|---|---|---|
| `--kv-prefix-share`（跨 slot 前缀共享）| **无**（grep kv_prefix_share/prefix_share = 0）| 有（C1）| **本地新功能，非重复实现** |
| 跨 slot `seq_cp` | `copy_state_to`（server-context.cpp:719）：`seq_cp(id, other.id, -1, -1)` **全量状态复制**（用于 fork/分支复制）| C1：`seq_cp(src, dst, 0, best_lcp)` **部分前缀共享** | 语义不同：上游全量复制（`-1,-1`），本地部分共享（`[0,lcp)`）；C1 是上游没有的部分前缀元数据共享 |
| 跨请求前缀复用 | `get_common_prefix`（1627/3221）+ idle slot LCP + RAM prompt cache（`cache_ram`）| 相同基线机制（E6.1 已记录）| 两者一致（均为上游原生功能，非 C1）|
| cache identity（lora）| `are_lora_equal`（server-common.cpp）用于 batch 合并 | C1 共享扫描用 `are_lora_equal`（E7.2 加）| 上游有比较函数，但**不用于跨 slot 前缀共享**（上游无此功能）|
| hybrid/recurrent 检测 | `llama_model_is_hybrid/is_recurrent` 存在 | 使用（C1 禁用条件）| 同源 API |
| unified KV / slot routing | 有 `kv_unified`、slot routing（5 处引用）| 有 + E2/E3 扩展（unified idle policy、routing events、lifecycle trace）| 本地扩展基于上游基础 |
| radix tree / 前缀树 | 无（server 层）| 无（C1 是线性 LCP 扫描，非 radix）| 一致：两者都无 radix tree |

## 3. 结论

1. **C1 是本地新实现**，上游 llama.cpp（master `3653e6d6d`）没有 `--kv-prefix-share` 或等价的部分前缀跨 slot 共享功能——**非重复实现**
2. 上游最接近的机制是 `copy_state_to`（全量 seq_cp，用于子任务/分支），语义与 C1 不同（全量复制 vs 部分共享）
3. 上游的跨请求复用 = idle slot LCP + RAM prompt cache（本地基线已有，C1 在其上增量）
4. **无可直接移植的修复**：上游无对应 identity/生命周期处理可借鉴；C1 的 E7.2 修复（lora/SWA/recurrent 防御）为本地独有
5. **推荐保留本地 C1**（作为独立功能），但名称必须为 "Cross-slot exact-prefix KV metadata sharing"（非上游功能、非 radix）
6. 上游对比的浅层限制：仅最新 master 快照；未遍历提交历史（如上游近期是否讨论过 cross-slot sharing 的 issue/PR）——若需完整历史对比，需 `git fetch --unshallow`（约数百 MB）

## 4. 复现

```bash
cd llama.cpp
git fetch https://github.com/ggml-org/llama.cpp.git master --depth 1
git show FETCH_HEAD:tools/server/server-context.cpp | grep -c "kv_prefix_share"   # 0
git show FETCH_HEAD:tools/server/server-context.cpp | sed -n '715,725p'          # copy_state_to
```
