# E12.1：Qwen3.5-4B hybrid cache miss 根因定位（Root Cause）

- 复核时间：2026-08-08
- 方法：结构化诊断（不改逻辑，仅加 E12-DIAG 日志），真实 4B 运行
- raw：`llama.cpp/tmp/e12_diag.log`（4B 诊断日志）

## 1. 诊断数据（4B，P=362 tokens 前缀稳定场景）

| 诊断点 | 值 |
|---|---|
| checkpoint save | 362 tokens、64,659,376 bytes（P.rstrip 前缀稳定）|
| restore 执行 | `restored checkpoint 1 (362 tokens)`——set_data 成功 |
| **restore 后 prompt.tokens** | **362**（正确）|
| **restore 后 pos_min**（llama_memory_seq_pos_min）| **364**（≠ 362！）|
| cache_prompt n_past | **362**（LCP 计算正确）|
| pos_next | 362 |
| pos_min_thold（pos_next - n_swa - 0）| 362 |
| **pos_min(364) ≥ pos_min_thold(362)** | **TRUE → 进入 context checkpoint 搜索** |
| slot.prompt.checkpoints | **空**（restore 未创建 checkpoint）|
| do_reset | **TRUE → n_past = 0** |
| 实际 prompt_n | **384（全量 prefill）** |

**根因链**：`restore 成功 → hybrid pos_min=364（recurrent 位置语义）≥ pos_min_thold=362 → cache_prompt 的 context checkpoint 搜索（无）→ do_reset → n_past=0 → 全量 prefill`。

## 2. 为什么 TinyLlama 命中

- TinyLlama（attention）：restore 后 **pos_min=0**（attention KV 从 0 起）
- pos_min(0) < pos_min_thold(362) → **不进入 checkpoint 搜索** → n_past=362 保留 → 只 prefill 后缀（prompt_n=17）✓

**差异本质**：hybrid 的 `llama_memory_seq_pos_min = max(attn_pos_min, recr_pos_min)`——**recurrent 位置（364）≠ attention token 位置（362）**（recurrent convs 的内部偏移），导致 pos_min 判定偏大 → 触发 do_reset。

## 3. 6 类原因区分

| 原因 | 判定 |
|---|---|
| token prefix mismatch | **存在但非本场景原因**（P.rstrip 前缀一致；BPE 边界是独立问题——P 末尾空格时 find_prefix 拒绝，安全降级）|
| checkpoint identity mismatch | **否**（identity 全字段匹配，find_prefix 命中）|
| **recurrent state position mismatch** | **是（根因）**：restore 后 pos_min=364 ≠ 362（recurrent 位置语义），触发 do_reset |
| cache_prompt 误判 | **是（根因的机制面）**：pos_min ≥ pos_min_thold 时无 checkpoint → do_reset 全量（对普通路径合理，对 restore 路径误判）|
| restore 后 state 被清 | **否**（顺序已修：prompt_clear 在 set_data 前）|
| suffix 调度错误 | **否**（n_past=362 时后缀调度正确——TinyLlama 证明）|

## 4. 三情况记录

| 情况 | 结果 |
|---|---|
| 1. P 单独 tokenize 保存，P+X 整体 tokenize | BPE 边界：P 末尾空格被合并 → find_prefix 拒绝（安全 fallback 全量）|
| 2. P 末尾无空格，前缀 token 完全一致 | **restore 成功但 do_reset 全量**（本根因场景）|
| 3. P 末尾有 BPE 合并风险，前缀不一致 | 同情况 1（安全拒绝）|

## 5. recurrent position 正确性评估（E12.2 前置）

- **state 数值正确**：E10.4 探针证明 set_data 往返逐字节一致（roundtrip YES）+ 恢复后追加 X 输出与 baseline 一致（PREFIX_ALIGNED_CHECKPOINT_LOSSLESS）——**restore 的 recurrent state 精确**
- **pos_min=364 是位置元数据语义**（recurrent convs 偏移），**非状态错误**——恢复后从 P.size() 追加（= 探针路径）在 4B 上已验证正确（探针）
- **结论**：restore state 位于精确 P 边界（数值正确、追加正确）；server 的 pos_min_thold 校验对 restore 路径误判 → **E12.2 允许的最小修复：restore 后跳过该 do_reset 路径（保留 n_past=ck->token_count）**

## 6. 结论

```text
QWEN35_HYBRID_CACHE_MISS_ROOT_CAUSE: recurrent_position_mismatch_triggering_do_reset
（辅助因素：BPE token 前缀边界——独立场景，安全拒绝）
```
