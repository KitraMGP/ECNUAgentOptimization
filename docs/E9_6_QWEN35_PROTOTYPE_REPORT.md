# E9.6：Qwen3.5-4B 主模型最小 prototype 报告（Prototype Report）

- 复核时间：2026-08-08
- 候选：H4（跨 slot state clone，`/slots/{dst}?action=clone_from&source_id={src}`，experimental 显式 API）
- 代码：llama.cpp `d6d679e7` + 本阶段 `SLOT_CLONE` 任务/action（server-task.h/.cpp、server-context.cpp）

## 1. 实现

- `SERVER_TASK_TYPE_SLOT_CLONE` 任务：把 source slot 完整 state（attention KV + recurrent state，经 `llama_state_seq_get_data/set_data`，hybrid state_read/write 覆盖 attn+recr）复制到 target slot；target 的 prompt.tokens 同步为 source 的
- 默认 off（无 CLI 开关，仅显式 API 触发）；源/目标忙时 defer；无缓存源拒绝

## 2. TinyLlama（纯 attention）验证

| 项 | 值 |
|---|---|
| clone | 200，n_restored=259 tokens、n_read=169012 B、restore_ms=0.152ms |
| cloned tgt | prompt_n=**14**（克隆状态直接续写，仅算 14 新 token）|
| independent tgt | prompt_n=259（全量 prefill）|
| 输出 hash | **一致**（9247d733bc879c09）→ **无损** ✓ |

**纯 attention 上 clone 成立**：复制状态后 target 从克隆点续写，与独立 prefill 输出一致。

## 3. Qwen3.5-4B（hybrid）验证：**输出不一致 → 简单 clone 不适用（实证）**

| 项 | 值 |
|---|---|
| clone | 200，n_restored=1754、n_read=**110,201,908 B（110MB）**、restore_ms=63.9ms |
| cloned tgt 输出 | `' turbidity 2.8 NTU'`（hash a1a3e246...）|
| independent tgt 输出 | `'<think>...'`（hash 126357be...）|
| **hash 一致** | **False（不一致）**|

**根因（实证 E8.7 的 NO_GO 结论）**：recurrent state 是**末尾位置**的状态——source 完成 `P+X` 后，克隆的 recurrent state 是 **X 末尾**的；target 请求 `P+Y` 的续写起点是 **P 末尾**（LCP=1747）→ recurrent 状态与位置**错位** → 输出污染。TinyLlama 无 recurrent 所以无此问题。

**正确路径**（已证方向）：需在 **P 末尾**保存 checkpoint（prefill 过程中的中间状态），clone/restore 从匹配 LCP 的 checkpoint 恢复——即 E8.7 的 H2 前置条件（checkpoint 前缀对齐），独立中等工程。

## 4. 保护与 rollback

- **已加保护**：hybrid/recurrent 模型上 `clone_from` 拒绝（错误提示"recurrent state is end-position, not prefix-position; use checkpoint-aligned restore"）——rollback 条件（输出不一致）已触发并落实
- 参数关闭/无 API 调用 = 完全基线（无新增默认行为）

## 5. 结论

```text
QWEN35_OPTIMIZATION_PROTOTYPE_STATUS: NO_GO_WITH_EVIDENCE（简单 clone 路径）
```

- **两个候选的真实测量证据**（非仅 E8.7 引用）：
  1. **H1（KV 量化 q8_0）**：4B 实测 per-cell 32768→17408 B（-46.9%），输出 hash 与 F16 一致（39a0f0fc...）→ **真实收益成立**（但为上游既有开关，非新代码）
  2. **H4（简单 clone）**：TinyLlama 无损成立（prompt_n 259→14）；**hybrid 上实证输出不一致**（recurrent 位置错位）→ 已拒绝保护
- 主模型真正的新代码优化路径 = H1 收益自动化/验证层 + H2 checkpoint 对齐（独立工程）；本阶段以实证证据关闭简单路径

## 6. 性能对照（prototype 测量）

| 路径 | 耗时 | 说明 |
|---|---|---|
| 4B prefill 1747 tokens | 663ms（wall 0.79s）| 全量 prefill |
| 4B clone 110MB | 63.9ms | 状态复制（比 prefill 快 ~10×）——**但 hybrid 上语义错误** |
| TinyLlama clone 169KB | 0.15ms | 纯 attention 正确路径 |
| 4B q8_0 KV | per-cell -46.9% | H1 comparator |

## 7. 复现

```bash
# TinyLlama（正确）
curl -X POST localhost:8080/slots/1?action=clone_from -d '{"source_id": 0}'
# 4B hybrid（拒绝，E9.6 保护）
# → 400/500 "Clone_from is not supported on hybrid/recurrent models ..."
```
