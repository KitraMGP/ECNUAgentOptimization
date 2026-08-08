# E12.0：E11 状态冻结与事实核对（Status Freeze）

- 复核时间：2026-08-08
- 目的：冻结 E11 状态，核对事实与代码位置，为 E12.1 诊断定位基线

## 1. 仓库与构建状态

| 项 | 值 |
|---|---|
| 根仓库 HEAD | `231cded`（benchmark-enhance，clean）|
| llama.cpp HEAD | `0bddfbc7f`（llama.cpp，clean）|
| E11 server build | 对应 `0bddfbc7f`（build 14:48:59）|

## 2. checkpoint 三入口与关键代码位置

| 组件 | 位置 |
|---|---|
| `--checkpoint-reuse` 参数 | common/arg.cpp（默认 off）；server-context.cpp:1470 赋值 |
| checkpoint save 入口 | server-context.cpp:2663（send_final_response，用 `slot.ckpt_prefix_tokens` 快照）|
| checkpoint restore 入口 | server-context.cpp:2261（launch_slot_with_task，`find_prefix` + `set_data`）|
| `find_prefix` | server-checkpoint.h:171（identity 全字段 + token 前缀 FNV-1a hash）|
| `cache_prompt` / `n_past` | server-context.cpp:3842（`n_past = slot.prompt.tokens.get_common_prefix(input_tokens)`）|
| `llama_memory_seq_pos_min` | server-context.cpp:3628/3989/4291 |
| recurrent `seq_pos_min` | llama-memory-recurrent.cpp:364（hybrid pos_min = max(attn, recr)，E6 已知）|

## 3. 关键日志（E11.3）

**4B restore 成功但全量 prefill**（`llama.cpp/tmp/e11_4b6.log`）：
```
E11-CKPT: saved checkpoint 1 (363 tokens, 64692164 bytes)   ← save 成功
E11-CKPT: restored checkpoint 2 (362 tokens, 64659376 bytes) ← restore 执行成功（P.rstrip 场景）
→ 但请求 prompt_n = 384（全量 prefill）→ cache_prompt 未命中
```
**TinyLlama restore 命中**（`e11_integ3.log`）：
```
E11-CKPT: saved checkpoint 1 (113 tokens, 73820 bytes)
E11-CKPT: restored checkpoint 1 → prompt_n = 17（只 prefill 后缀）
```

## 4. E11 raw 字段核对（缺口）

| raw | 记录 | 缺失 |
|---|---|---|
| e11_lifecycle.json | save_code、prompt_n、output hash、baseline hash、kv | **无 token vector、无 checkpoint/restore token_count、无 state position** |
| e11_perf.json | e2e/prompt_n/失败率 | 同上 |
| e11_switch_combos.json | 组合结果 | 同上 |

→ E12.1 需新增结构化诊断 raw（token IDs、token_count、position、roundtrip）。

## 5. E11 状态确认

```text
E11_CHECKPOINT_STATUS: SERVER_INTEGRATED_PARTIAL
QWEN35_HYBRID_CACHE_HIT: FALSE（4B restore 执行成功但 cache_prompt 未命中 → 全量 prefill）
QWEN35_HYBRID_CORRECTNESS: TRUE_BY_SAFE_FALLBACK（全量 prefill 输出与 baseline 一致，无错误复用）
QWEN35_HYBRID_OPTIMIZATION: NOT_ACHIEVED
```

## 6. E12 起点假设（E12.1 验证）

1. **token prefix mismatch**（BPE 边界）：已证（P 末尾空格合并）——但 P.rstrip 场景前缀一致仍 miss → 非唯一原因
2. **recurrent position mismatch**（候选主因）：hybrid pos_min=max(attn,recr)；restore 后 recurrent position 与 attention 位置可能不一致 → cache_prompt 的 pos_min 校验失败 → n_past=0
3. **cache_prompt 误判 / restore 后 state 被清**：需 E12.1 诊断确认
