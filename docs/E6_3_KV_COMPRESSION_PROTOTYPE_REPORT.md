# E6.3：KV 压缩原型报告（C2：SnapKV 式 prompt KV 压缩离线 oracle）

状态：**E6.3 COMPLETE**（离线 oracle + 明确判断；未伪装正式收益）

## 1. 目标

第二候选 C2 = SnapKV 式 prompt KV 压缩（P1 有损，prefill 后一次性压缩 prompt KV）。E6.3 要求"至少有可运行原型或离线 oracle，有实际输出数据，明确判断是否值得进入在线路径"。

## 2. 离线 oracle（benchmark/scripts/e6_c2_oracle.py）

在真实 Qwen3.5-4B（CUDA）上采集数据 + 离线模拟压缩收益上界：

| 指标 | 值 |
|---|---|
| prompt tokens（真实 tokenizer） | 3265 |
| attention KV used_cells | 3265 |
| attention KV used_bytes | 106,987,520 bytes（~102 MB）|
| capacity_cells / capacity_bytes | 8192 / 268,435,456 |

### 压缩模拟（SnapKV 式预算，保留最近 window 32 + 选中 prefix）

| budget | keep | attention KV 压缩 | 说明 |
|---|---|---|---|
| 1024 | 1024 | **-68.64%** | attention-only 估算 |
| 2048 | 2048 | -37.27% | |
| 3072 | 3072 | -5.91% | |

## 3. 架构分析（为什么 hybrid 上不进入在线路径）

Qwen3.5-4B 是 **hybrid（attention + recurrent）**：

1. **recurrent state 不可 token 级压缩**：recurrent state 是随 token 序列连续更新的不可分割状态，删除中间 attention token 会使 recurrent/attention 的位置对应错位（E6.2 已证同源限制：`llama_memory_hybrid::seq_pos_min = max(attn, recr)`）
2. 上述压缩率仅针对 attention KV 部分；**recurrent state + 模型权重不随压缩减少**，端到端显存收益被稀释
3. SnapKV 完整实现需要：FA 注意力分数输出通道（FA 下分数不物化，需新 kernel/图节点）+ KV 紧凑重排（`ggml_set_rows` 索引 copy + cell 元数据迁移）+ server 触发记账 + recurrent 兼容改造——工程量与 hybrid 上的受限收益不匹配
4. SnapKV 论文在 LLaMA/Mistral（标准 attention-only）上验证，其映射到 llama.cpp 的可行性分析见 E6.0（论文笔记 snapkv.md）

## 4. 判断

- **enter_online_path: false**（主模型 Qwen3.5-4B hybrid 上）
- **quality_gate: NOT_APPLICABLE**（离线 oracle 无法测量生成质量；不做任何质量声明，不伪装收益）
- **deferred_to**：标准 attention-only 架构模型（LLaMA/Mistral 系列 GGUF）上作未来工作
- 候选状态：`DEFERRED_AFTER_SCORING`（C2；E6.0 已评分 3.1，本阶段确认 hybrid 主模型上收益受限）

## 5. 阶段退出条件核对（12.19 E6.3）

| 条件 | 状态 |
|---|---|
| 第二候选至少有可运行原型或离线 oracle | ✓（e6_c2_oracle.py，真实 4B 数据）|
| 有实际输出数据 | ✓（raw/e6_c2_oracle.json）|
| 明确判断是否值得进入在线路径 | ✓（false，架构+工程量理由充分）|
| 未伪装成正式优化收益 | ✓（quality_gate NOT_APPLICABLE，无质量声明）|

**E6.3 COMPLETE**

## 6. 修改与提交

- 新增：`benchmark/scripts/e6_c2_oracle.py`、`raw/e6_c2_oracle.json`
- 报告：本文件
- 提交：`perf: prototype adaptive KV cache compression`（根仓库）
