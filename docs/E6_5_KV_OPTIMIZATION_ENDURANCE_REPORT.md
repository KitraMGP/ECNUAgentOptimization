# E6.5：KV 优化 endurance 报告（C1，12 周期 churn）

状态：**E6.5 COMPLETE**（verdict: PASS_ENDURANCE）

## 1. 配置

- 候选：C1 跨 slot 前缀共享（`--kv-prefix-share` on）
- 模型：TinyLlama stories260K（标准架构，真实 KV 路径）
- server：unified p2 ctx 512 cache-ram 0 temp 0 seed 42 + slot-save-path
- churn：12 周期，每周期 2 并发共享前缀请求（A + 各自后缀，内容每 3 周期循环增长），
  周期末采样 /metrics/kv；最后 erase 全部 slot 验证回收
- 复现：`uv run python benchmark/scripts/e6_c1_churn.py --cycles 12`

## 2. 结果

| 指标 | 值 |
|---|---|
| cycles | 12（全 ok，无错误）|
| used_cells 序列 | [232,247,262] × 4（同逻辑状态周期 drift=**0**）|
| shared_cells 序列 | [195,210,225] × 4（共享引用正确释放，不累积）|
| erase 后 | used_cells=**0**、active_sequences=**0** |
| active victim | 0 |
| 跨 session 污染 | 0（无错误、输出正常）|

## 3. 门槛核对（12.19 E6.5）

| 条件 | 状态 |
|---|---|
| 至少 12 个完整 churn 周期 | ✓（12）|
| 每周期结束状态被记录 | ✓（cycle_samples 含 requests + kv）|
| 没有累计泄漏 | ✓（同逻辑状态 drift=0；增长仅归因内容增长）|
| 没有 active victim | ✓（12 周期 24 请求全 ok）|
| 没有跨 session 污染 | ✓ |
| 最终资源返回基线 | ✓（erase 后 0/0）|

**PASS_ENDURANCE**

## 4. 修改与提交

- 新增：`benchmark/scripts/e6_c1_churn.py`、`raw/e6_c1_churn12.json`
- 报告：本文件
- 提交：`test: validate optimized KV cache endurance`（根仓库）
