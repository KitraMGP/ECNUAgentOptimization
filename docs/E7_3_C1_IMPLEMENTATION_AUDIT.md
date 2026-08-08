# E7.3：C1 实现审计报告（Implementation Audit）

- 复核时间：2026-08-08
- 审计对象：C1 跨 slot 前缀 KV 共享（`--kv-prefix-share`，llama.cpp `02541f72` 基础 + E7.2 修复后）

## 1. 机制结论（20 问核心答案）

| # | 问题 | 结论 | 证据 |
|---|---|---|---|
| 1 | 是否作用于实际推理请求 | **是**：`update_slots → pre_decode → cache_prompt 分支`，共享后 `n_past=best_lcp`，batch 只渲染 `[lcp, n)` | server-context.cpp:3740-3780 |
| 2 | 共享数据还是元数据 | **纯元数据**：`seq_cp` 同 stream 只 `cells.seq_add` 加 bitset 位，不碰 K/V buffer | llama-kv-cache.cpp:459-488 |
| 3 | seq_cp unified 语义 | unified（n_stream=1）永远同 stream 分支；跨 stream 的 `is_full` 断言被 `kv_unified` 条件排除 | llama-kv-cache.cpp:82,502；server-context.cpp:3749 |
| 4 | 共享源保护 | `get_available_slot` + `try_clear_idle_slots` 用 `seq_cell_stats.shared>0` 跳过；**但仅 `llama_kv_cache` override，msa/iswa/dsa/dsv4/hybrid/recurrent 返回 false → 保护静默失效**（靠 find_slot 免疫兜底） | server-context.cpp:1901-1912,2055-2062；llama-memory.h:111-116 |
| 5 | 目标 slot 分配 | launch 不碰 KV；共享前 `seq_rm(slot.id,-1,-1)` 清 target 旧 KV 防同 pos 冲突 | server-context.cpp:3768 |
| 6 | 共享 cell 覆盖处理 | `find_slot` 的 `can_use` 要求 `is_empty || seq_count==1` → 共享 cell（count≥2）永不被覆盖；`seq_rm` 只删本 seq 位 | llama-kv-cache.cpp:1142-1144,1220-1229,406 |
| 7 | partial prefix 安全 | 非 SWA 安全（source 连续性 + cell pos 显式）；SWA 有 hole 风险 | server-context.cpp:3857-3863 |
| 8 | 追加写破坏 | 非 SWA 不破坏（新写进空 cell）；**SWA（iswa）purge 会删 target 共享前缀关联 → 静默输出错误** | llama-kv-cache.cpp:1250-1265 |
| 9 | 清理/retry/cancel/resume/restore | `seq_rm(seq_id)` 只删本 seq 位，不破坏其他引用；checkpoint restore 会把共享前缀实体化为 target 独占拷贝（数据一致、双份占用） | server-context.cpp:301-307,4002；llama-kv-cache.cpp:2088-2102,2323-2370 |
| 10 | generation 变化 | `slot_generation` 仅 trace 用，不绑定 KV bitset；共享引用跨 generation 存活（设计意图） | server-context.cpp:2196 |
| 11 | 悬空引用 | **无悬空**：source 清只删 source 位；物理数据仅在全部引用清除后回收 | （负向声明） |
| 12 | bitset 关联数 | cell 级计数（3 seq 共享 1 cell 算 1），非引用计数；作保护判断语义够，作 metrics 低估 | llama-kv-cache.cpp:716-726 |
| 13 | 跨 stream/context/backend | C1 只处理 ctx_tgt（draft KV 不共享，正确性 OK）；跨 stream 被排除 | server-context.cpp:3769 |
| 14 | 并发竞态 | **无**：server 单线程事件循环（callback_update_slots 单线程）| server-queue.cpp:139-163 |
| 15 | LCP 是否比 identity | **修复前只比 token ID；E7.2 已加 lora/adapter 检查**（are_lora_equal）；rope/kv type/context 同 ctx_tgt 固定 | server-context.cpp:3753-3765 |
| 16 | 输出相同但状态污染 | 污染源：a) lora 不匹配（已修）；b) SWA purge（已禁用 SWA）；c) dsv4 comp state 全量 seq_cp（DEEPSEEK4 是 hybrid 已排除） | — |
| 17 | min-lcp 边界 | `lcp >= min` 允许、`lcp < min` 跳过；无 off-by-one；负值无崩溃 | server-context.cpp:3761 |
| 18 | 关闭回基线 | `kv_prefix_share=false` 时共享与保护全部跳过，完全基线 | server-context.cpp:3748,1903,2056 |
| 19 | hybrid 覆盖 | `llama_model_is_hybrid` 覆盖 QWEN35/DEEPSEEK4/JAMBA 等；**recurrent（Mamba/RWKV）非 hybrid 漏检 → E7.2 已加 `llama_model_is_recurrent`** | llama-arch.cpp:958-977；include/llama.h:647 |
| 20 | 检测失败 | `llama_get_model` 不为空；DFLASH（iswa+压缩参数）非 hybrid 曾有风险 → **E7.2 已加 `llama_model_n_swa==0` 覆盖 SWA/iswa** | llama-model.cpp:2198 |

## 2. E7.2 代码修正（本审计驱动的修复）

在 `llama.cpp/tools/server/server-context.cpp` C1 共享逻辑：

1. **共享条件收紧**（三处防御）：
   ```cpp
   !llama_model_is_hybrid(llama_get_model(ctx_tgt)) &&
   !llama_model_is_recurrent(llama_get_model(ctx_tgt)) &&
   llama_model_n_swa(llama_get_model(ctx_tgt)) == 0
   ```
   - 覆盖 hybrid（Qwen3.5-4B）、纯 recurrent（Mamba/RWKV）、SWA/iswa（Qwen3-SWA、DFLASH）
2. **lora/adapter identity**：source 扫描加 `are_lora_equal(slot.lora, other.lora)`（ptr + scale 比较，与 `can_batch_with` 同语义）——不同 adapter 的缓存 KV 不共享

## 3. Cache identity 审计结论

| 字段 | 是否参与共享判断 | 结论 |
|---|---|---|
| model identity | 隐含（同 ctx_tgt/model）| 同一 server 内单一模型，无跨模型共享路径 |
| model revision/hash | 隐含 | 同上 |
| adapter identity | **E7.2 已加**（are_lora_equal：ptr+scale）| 修复前缺失（高危），已修复 |
| adapter scale | E7.2 已加 | 同上 |
| RoPE 类型/配置 | 隐含（同 ctx_tgt）| 同一 context 固定，无差异 |
| context 配置 | 隐含 | server 级固定 |
| KV type | 隐含（同 ctx_tgt）| 同 context 固定 |
| flash attention | 隐含 | 同 context 固定 |
| batch/continuous batching | 不参与 | 不影响 KV 内容（仅调度） |
| grammar/tool-call | 不参与 | 影响解码不直接影响已存 KV |
| sampler | 不参与 | 显式确认不耦合 KV |
| tenant/session/generation | **不参与**（设计意图：跨请求前缀复用）；canary 测试验证无泄漏 | 共享仅限相同 token 前缀 + 相同 adapter |
| prompt template | 已 tokenized（LCP 在 token 层）| 相同 token = 相同 template 效果 |

**结论**：identity 判断 = `token LCP ≥ min_lcp` + `adapter(lora) 相同` + `模型非 hybrid/recurrent/SWA`。不足：无显式 model hash 比较（同 server 单模型下安全，跨 model 加载场景未覆盖——llama.cpp server 单模型实例，不适用）。

## 4. 隔离风险清单（修复后）

**高（已修复）**
- ~~lora/adapter 不匹配共享~~ → E7.2 are_lora_equal 检查
- ~~SWA purge 删 target 共享前缀~~ → E7.2 n_swa==0 条件禁用
- ~~recurrent 模型漏检~~ → E7.2 is_recurrent 条件禁用

**中（残余）**
- `seq_cell_stats` 保护在 msa/dsa/dsv4/iswa 上返回 false（保护失效，靠 find_slot 免疫兜底；这些架构 E7.2 后均被 n_swa 或非 attention-only 排除或未受影响）
- 默认 `slot_prompt_similarity=0` 时 LRU 分支无共享源检查（不悬空，仅保护不一致）

**低（已知）**
- checkpoint restore 双份占用（数据一致）
- `n_past==n_tokens` 时 `n_past--` 浪费末个共享 cell
- 负 `--kv-prefix-share-min-lcp` 语义含糊（无崩溃）

**NOT_TESTED（无对应模型文件）**
- lora 隔离实测（需 lora GGUF；代码级 are_lora_equal 已实现）
- SWA 模型禁用实测（需 iswa 模型）
- recurrent 模型禁用实测（需 Mamba 模型）

## 5. 名称修正（指令八）

C1 实际实现 = **Cross-slot exact-prefix KV metadata sharing**（idle slot 线性 LCP 扫描 + seq_cp 元数据共享 + 共享源保护；无 radix tree、无节点分裂/合并、无节点级 LRU、无 cache-aware scheduling）。文档/报告统一使用该名称，可注明 "Inspired by RadixAttention"，**不得称 RadixAttention implementation**。

## 6. 结论

- C1 机制在 **attention-only、非 SWA、非 recurrent、unified、相同 adapter** 范围内正确（20 问核心确认 + 10 审计测试 + canary 无泄漏）
- E7.2 修复了审计发现的 3 个高危缺陷（identity/SWA/recurrent）
- 主模型 Qwen3.5-4B（hybrid）仍被禁用（`QWEN3.5_4B_STATUS: CORRECT_NO_OPTIMIZATION`，待 E7.4/7.6 实证）
