# E8.6：上游 llama.cpp 历史与 issue/PR 对比（Upstream History Comparison）

- 复核时间：2026-08-08
- 方法：`git fetch --shallow-since=2024-01-01`（8584 commits）+ GitHub Search API（issues/PRs，关键词：shared prefix / cross-slot / kv clone / prefix reuse / lora / prompt cache / seq_cp）
- 状态：**UPSTREAM_HISTORY_AND_ISSUE_REVIEW_COMPLETE**（历史 + issue/PR 检索完成，网络可用）

## 1. 上游相关实现/讨论清单（GitHub API 实查）

| # | 类型 | 标题 | 状态 | 相关性 |
|---|---|---|---|---|
| 26204 | PR | server: add `/slots endpoint action=clone_to` (KV clone between slots) | **open（未合并）** | **高**：跨 slot KV 复制共享前缀（目标与 C1 重叠）|
| 26207 | issue | server: prompt cache is reused across requests with different per-request `lora` — output… | open | **高**：上游承认 lora-cache-identity 缺陷（本地 C1 已修复同题）|
| 25913 | issue | /slots save/restore silently loses all prompt reuse on hybrid/recurrent models | open | 中：hybrid/recurrent prompt reuse 限制（与本地 hybrid 禁用一致）|
| 26128 | issue | server prompt cache incompatible with RPC backend (state_seq_get_data assert) | open | 低：prompt cache 序列化路径 bug |
| 26676 | issue | slot KV state restore is a no-op | open | 低 |
| 24956 / 24143 / 24891 | PR | checkpoint/restore 相关修复 | open | 低-中 |

## 2. 与上游最接近方案（PR #26204 clone_to）对比

| 维度 | 本地 C1（kv-prefix-share）| 上游 PR #26204（clone_to）|
|---|---|---|
| 机制 | **seq_cp 元数据共享**（cell bitset 关联，零数据复制，内存不重复）| **KV 数据克隆**（state copy，物理复制到目标 slot）|
| 触发 | 自动：idle slot LCP 扫描（请求级）| 显式：`/slots/{id}?action=clone_to&source_id=...` API |
| identity | lora（are_lora_equal）+ 架构能力（capability 判断）| 未披露明确 identity 检查 |
| hybrid/recurrent | **禁用**（capability 默认 false）| 作者自述"On hybrid SSM models the speedup is lower"（未禁用）|
| 前缀来源 | 任意 idle slot 缓存 prompt | 指定 source slot |
| 状态 | merged（本地）| **open 未合并** |
| 加速数据 | TinyLlama：recompute -99.1%、e2e 311→10ms | gemma-4-26B：wall 42.9→20.2s（2.12x，冷缓存）|

**判定：本地 C1 与上游 clone_to 是 independent feature**——机制不同（元数据共享 vs 数据克隆）、触发不同（自动 vs 显式）、上游未合并。非 duplicate、非 fork；上游也无 merged 的跨 slot 前缀共享（`git log FETCH_HEAD --oneline | grep -i "clone_to\|cross-slot\|shared prefix"` 为空）。

## 3. 上游 cache identity 对比

- 上游 server prompt cache（`--cache-ram`）：**#26207 证实存在 lora identity 缺陷**（不同 per-request lora 复用缓存 → 输出错误风险），issue open 未修复
- 本地 C1：`are_lora_equal`（ptr + scale）已修复同题（E7.2/E8.2）；**但本地继承的上游 server prompt cache（cache_ram）路径仍有 #26207 描述的缺陷**（不在 C1 范围，C1 共享扫描已跳过 lora 不同源）——记录为已知上游缺陷
- 上游无 radix tree / 无统一 cache identity 结构（C1 的 capability 判断为本地独有）

## 4. hybrid/recurrent 对比

- 上游 #25913：save/restore 在 hybrid/recurrent 上丢失 prompt reuse（open）——与本地"hybrid 禁用 C1"同向：**hybrid 上跨请求前缀复用本身就不被上游支持**（本地 C1 的禁用是正确行为而非保守过度）

## 5. 结论

1. **本地 C1 是独立功能（independent feature）**：上游无 merged 等价实现；最近似方案 clone_to（PR #26204，open）机制不同
2. 上游 lora-cache-identity 缺陷（#26207）印证本地 C1 的 identity 修复必要且正确；本地 server prompt cache 继承该缺陷（记录）
3. 上游 hybrid/recurrent 不支持 prompt reuse（#25913）与本地 hybrid 禁用一致
4. **无可直接移植修复**（上游相关 PR 均未合并/未完成）
5. **推荐保留本地 C1**（目标重叠但机制独特：元数据共享零复制 + 自动触发 + 正向能力判断）
6. 命名保持 "Cross-slot exact-prefix KV metadata sharing"（与 clone_to 数据克隆区分）

## 6. 复现

```bash
cd llama.cpp
git fetch https://github.com/ggml-org/llama.cpp.git master --shallow-since=2024-01-01
# GitHub Search API:
#   q=repo:ggml-org/llama.cpp "kv clone" OR "cross-slot" OR "shared prefix" in:title → #26204
#   q=repo:ggml-org/llama.cpp prefix reuse kv server in:title,body → #26207, #25913, ...
```
