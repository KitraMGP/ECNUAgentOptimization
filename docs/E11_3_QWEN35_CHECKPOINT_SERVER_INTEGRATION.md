# E11.3：Qwen3.5-4B server 集成最小路径（Checkpoint Server Integration）

- 复核时间：2026-08-08
- 代码：llama.cpp `8d1b2b1e1`（--checkpoint-reuse + checkpoint_save/restore 请求参数 + 池集成）
- 范围限制：单模型 / trusted single-tenant / 单 server / 单 source / ≤4 target（池可多 target）/ 无 LRU / 无跨重启持久化 / 无分布式 / 无 recurrent 近似 / 无 end-position clone

## 1. 集成实现

| 组件 | 实现 |
|---|---|
| 开关 | `--checkpoint-reuse`（默认 off；仅内部路径，无公共 HTTP action）|
| 触发 | completion 请求参数 `checkpoint_save` / `checkpoint_restore`（bool；--api-key 鉴权保护下）|
| 保存 | send_final_response 时用 **launch 快照**（`slot.ckpt_prefix_tokens`——task.tokens 可能在任务处理中被修改）→ `llama_state_seq_get_data` → `ckpt_pool.add` |
| 恢复 | launch 时 `find_prefix`（identity 全字段 + token 前缀 hash 精确匹配）→ **先 prompt_clear 再 set_data**（修复：原顺序 set 后 clear 会清掉恢复的 KV）→ prompt.tokens 同步 → cache_prompt LCP 续写 |
| 回滚 | set_data 失败 → release + prompt_clear（不留半恢复状态）|
| 释放 | target 完成 → release；refcount==0 → valid=false（E11.2 池）|

## 2. 集成流程 9 步验证（TinyLlama，生效）

| 步骤 | 结果 |
|---|---|
| 1. 请求 A 完整 prefill P（checkpoint_save）| checkpoint 1（110 tokens、73.8KB）保存 |
| 2. P 边界保存 | 精确 110 tokens（launch 快照）|
| 3. 请求 B = P+X（checkpoint_restore）| **restored checkpoint 1** |
| 4. 从 checkpoint 恢复 | prompt_n = **17**（仅 prefill X 的 17 tokens；baseline 127）|
| 5. 请求 C = P+Y（restore）| prompt_n = **14**（仅 Y）|
| 6. 对比 full-prefill baseline | **P+X、P+Y 输出 hash 完全一致（MATCH）** |
| 7. target 写入不改变 checkpoint | state 为字节拷贝（E11.2 测试验证）|
| 8. source 删除后 target 不损坏 | refcount 保护（池）|
| 9. 全清理后释放 | release → refcount==0 → 失效 |

## 3. 关键发现：BPE tokenizer 前缀边界

- **现象**：P 文本以空格结尾（"…12:00. "）时，`P+X` **整体 tokenize** 会把 P 末尾空格与 X 开头合并 → `P+X` 的前缀 token 序列 ≠ 单独 P 的 tokenize（@362：695 vs 220）
- **影响**：checkpoint（单独 P 的 token 序列）与 restore 请求（P+X 整体 tokenize）前缀不一致 → `find_prefix` hash 不匹配 → **安全拒绝（降级全量 prefill）**，输出与 baseline 一致（无损，无错误复用）
- **验证**：P 末尾无空格（rstrip）→ 前缀稳定 → 4B 集成恢复执行
- **这是 E11.2"prefix 不匹配必须拒绝"的真实触发场景**（BPE 本质特性，非实现缺陷）

## 4. Qwen3.5-4B（hybrid）集成结果

- **restore 执行成功**（checkpoint 2：362 tokens、64.66MB，restored 日志确认）
- **但 cache_prompt 未命中**（prompt_n=384 全量）——hybrid 的 recurrent state 位置语义使 server 的 n_past 判定未识别恢复状态（E6 已知：hybrid pos_min = max(attn, recr)）
- **输出与 baseline 完全一致（无损）**——恢复状态存在但被全量覆盖，无错误复用
- **hybrid 上集成收益未达成**（需 cache_prompt 命中逻辑适配恢复状态，E11.4/11.5 记录）

## 5. 结论

```text
SERVER_INTEGRATION_STATUS: PARTIAL（TinyLlama 完整生效：恢复 + 命中 + 无损；
  4B hybrid 恢复执行但未命中 cache——需 recurrent 位置适配，输出无损）
```

- 集成机制正确（保存/恢复/回滚/释放/refcount）
- BPE 前缀边界 + hybrid 位置语义为两个真实限制（均安全降级，无错误复用）
- 默认关闭；无公共 HTTP action
