# E6.1：KV Cache 优化基线、instrumentation 与最小复现报告

状态：**E6.1 COMPLETE**（阶段退出条件 12.19 全部满足）

## 1. 冻结配置（与 environment.json 一致）

- server：`llama.cpp/build-cuda/bin/llama-server`，`--kv-unified --ctx-size 8192 --parallel 4 --cache-ram 0 --temp 0 --seed 42 --metrics --slots --slot-save-path`
- 模型：`qwen3-5-4B-Q4_K_M.gguf`（sha256 `de8e96cd…`），`-ngl 99`
- llama.cpp commit：`69711a2d6`（E6.0 冻结时）
- 注意：`--parallel 4` 平分 ctx → 每 slot 2048（此限制已在 workload_manifest.json 注明并适配 prompt 预算）

## 2. instrumentation

- **已有**：`GET /metrics/kv`（capacity_cells/used_cells/active_sequences/shared_cells/used_bytes/used_bytes_valid/physical_sharing）+ OAI `usage.prompt_tokens_details.cached_tokens`（= n_past 公共前缀命中）+ server 日志 `purging slot` 计数
- **12.17 字段映射**：
  - `prefix_hit_tokens` = OAI cached_tokens
  - `recompute_tokens` = prompt_tokens − cached_tokens
  - `unique_physical_kv_tokens` = /metrics/kv used_cells（无物理共享，physical_sharing=false）
  - `purge_count` = server 日志 `purging slot` 正则计数
- **instrumentation gap（字段 = null，不得用 0 代替）**：
  - `internal_fragmentation_bytes`：架构无空洞 cell 计数（架构审计 §13）
  - `stranded_capacity_bytes`：无连续性/所有权受限统计
  - `shared_blocks` / `cow_operations`：无 paged block / 无 COW（physical_sharing=false）
  - `peak_physical_kv_bytes`：本阶段未加峰值采样器（E6.4 paired benchmark 补）

## 3. 最小 correctness reproducer 结果（基线，未优化 llama.cpp）

| workload | 描述 | reps | completed | failed | prompt | cached | recompute | used_cells |
|---|---|---|---|---|---|---|---|---|
| W01_single_short | 单短 | 1 | 1/1 | 0 | 214 | 0 | 214 | 245 |
| W02_single_long | 单长 | 1 | 1/1 | 0 | 1427 | 0 | 1427 | 1437 |
| W07_exact_prefix_4_sessions | 4 并发相同前缀 | 3 | 4/4 | 0 | 5708 | 5692 | 16 | 5877 |
| W10_idle_reuse | idle 复用 | 3 | 2/2 | 0 | 2248 | 1216 | 1032 | 1180 |
| W11_active_pressure | active 压力 | 3 | 5/5 | 0 | 5750 | 4194 | 1556 | 4851 |
| W16_churn_12_cycles | 12 周期 churn | 1 | 48/48 | 0 | — | — | — | 0（erase 后）|

全部确定性（temp 0 seed 42）；并发/生命周期 workload 按 12.9 ≥3 次；无失败。

**关键观察**：W07（4 并发 exact prefix）基线即显示高前缀命中（cached 5692/5708 ≈ 99.7%）——llama.cpp 现有 cache_prompt + n_past 已能复用简单相同前缀。**C1（radix）的收益点不在简单 4 路相同前缀，而在多级树状共享（few-shot+多选+树搜索）与跨 session 长时间保留**（radix 树 + LRU 保留策略），需在 E6.4 用更复杂的 prefix 模式 workload 测量。

## 4. 原始数据与汇总

- 原始 JSON：`benchmark/results/kv_optimization/raw/e6_baseline_min_*.json`（12 条，12.17 格式）
- 汇总：`uv run python scripts/e6_summarize.py` → `raw/summary.json`（12 文件全部 READ_OK）
- server 日志：`raw/server_baseline_min.log`

## 5. 阶段退出条件核对（12.19 E6.1）

| 条件 | 状态 |
|---|---|
| 未优化基线可复现 | ✓（确定性 prompt + 冻结配置；W01-W11-W16 全部完成）|
| 必需 instrumentation 可采集 | ✓（/metrics/kv + cached_tokens + purge 计数）|
| 所有最小 correctness reproducer 可运行 | ✓（W01/W02/W07/W10/W11/W16 全通过）|
| 原始 JSON 能被汇总脚本读取 | ✓（12/12 READ_OK → summary.json）|
| 基线结果已保存 | ✓（raw/ 12 条 JSON）|

**E6.1 COMPLETE**

## 6. 修改与提交

- 新增：`benchmark/scripts/e6_runner.py`（统一实验 runner，12.17 输出）、`benchmark/scripts/e6_summarize.py`（汇总）
- 更新：`benchmark/results/kv_optimization/workload_manifest.json`（prompt 预算适配 parallel-4 slot ctx 2048）
- 提交：`test: establish KV cache optimization baseline`（根仓库）
