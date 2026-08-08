# E8.2：C1 正向能力边界审计（Capability Boundary Audit）

- 复核时间：2026-08-08
- 范围：C1 共享条件从"负向架构黑名单"升级为"正向能力声明"；min-lcp 参数语义；n_past 回退审计

## 1. 能力判断实现（代码改动）

### 1.1 memory 层正向能力声明（默认不支持）

`llama_memory_i`（src/llama-memory.h）新增虚拟方法：

```cpp
virtual bool supports_cross_slot_prefix_sharing() const { return false; }
```

**默认行为：不支持（false）**。仅 `llama_kv_cache`（标准 unified attention KV 实现，src/llama-kv-cache.h）override 为 true，附审计注释（4 条安全依据）：

1. seq_cp 同 stream 为纯 bitset 元数据共享（不碰数据 buffer）；
2. find_slot 的 `can_use` 要求 `is_empty || seq_count==1` → 共享 cell 不被覆盖；
3. seq_rm 只删本 seq 位 → 无悬空引用；
4. server 层另行校验 n_swa==0 → 无 SWA purge 破坏共享前缀问题。

**全部特殊 memory 实现保持默认 false（拒绝共享）**（审计于 llama-model.cpp `create_memory` 的架构映射）：

| memory 实现 | 对应架构 | 能力 |
|---|---|---|
| `llama_kv_cache`（标准）| TinyLlama、MOE 等 default 分支 | **true**（唯一）|
| `llama_kv_cache_msa` | MINIMAX_M3 | false |
| `llama_kv_cache_dsa` | GLM_DSA / DEEPSEEK32（主 context）| false |
| `llama_kv_cache_iswa` | DEEPSEEK4 MTP | false |
| `llama_kv_cache_dsv4` | DEEPSEEK4 | false |
| `llama_memory_hybrid` | Qwen3.5-4B 等 | false |
| `llama_memory_hybrid_iswa` | Qwen3.5-SWA | false |
| `llama_memory_recurrent` | Mamba / RWKV | false |

C API：`llama_memory_supports_cross_slot_prefix_sharing(llama_memory_t)`（llama.h + llama-context.cpp）。

### 1.2 server 层共享条件（tools/server/server-context.cpp）

```cpp
llama_memory_supports_cross_slot_prefix_sharing(llama_get_memory(ctx_tgt)) &&
!llama_model_is_hybrid(llama_get_model(ctx_tgt)) &&
!llama_model_is_recurrent(llama_get_model(ctx_tgt)) &&
llama_model_n_swa(llama_get_model(ctx_tgt)) == 0
```

正向能力（memory 声明）与架构级防御（hybrid/recurrent/SWA）同时通过才允许共享。任何未审计实现自动拒绝。

### 1.3 启用日志（--kv-prefix-share 时）

- capability accepted：`E8-C1: capability accepted: standard unified attention KV, non-hybrid, non-recurrent, n_swa=0`
- capability rejected（分级原因）：memory is null / memory 不支持 / hybrid / recurrent / SWA(n_swa=%d)
- 共享尝试拒绝（lora）：`E8-C1: skip source slot %d: lora identity mismatch`
- 既有共享日志保留：`E6-C1: shared N-token prefix from slot M`（source/target/LCP）
- **参数关闭时新增逻辑完全不执行**（测试验证：off 时日志无 `E6-C1: shared` 也无 `E8-C1: capability`）

## 2. min-lcp 参数语义（修正）

- **负值 → 启动明确失败**：`throw std::invalid_argument("--kv-prefix-share-min-lcp must be >= 0")`（server-context.cpp 构造时校验），测试 `test_min_lcp_negative_rejected` 验证启动失败 + 日志含拒绝原因
- **0 允许**：语义 = "LCP > 0 即共享"，测试 `test_min_lcp_zero_allowed` 验证
- 不再存在"无崩溃但语义含糊"状态

## 3. n_past == n_tokens 回退审计（[TAG_PROMPT_LOGITS]）

- 位置：server-context.cpp:4005（既有代码，**非 C1 引入**）
- 必要性：确保每个 active slot 至少评估 1 个 token（prompt logits 缓存机制要求，防空 batch）
- 影响：全前缀命中时回退 1 个 token（最大重算 1 token，非额外大段 prompt eval）；打 WRN 日志显式说明
- 边界测试：`test_full_prefix_recompute_one_token`（相同 prompt 二次请求 → prompt_n ≤ 1 + 输出一致）
- E7 矩阵的 recompute 数据不受影响（M01 等为 partial 共享，非全命中场景）

## 4. 新增测试（test_kv_prefix_share_capability.py，6 例）

| 测试 | 验证 |
|---|---|
| test_min_lcp_negative_rejected | 负 min-lcp → 启动明确失败 |
| test_min_lcp_zero_allowed | min-lcp=0 → LCP>0 即共享 |
| test_full_prefix_recompute_one_token | 全前缀命中 → 回退 ≤1 token + 无损 |
| test_partial_prefix_min_lcp_boundary | LCP < min_lcp → 不共享 |
| test_off_full_baseline | 关闭时无共享/无能力日志（完全基线）|
| test_moe_attention_share | MOE（标准 attention KV）→ 能力 accepted + 可共享 |

## 5. 验证结果

- llama.cpp 全量 47/47（新增 6）+ E1 5/5 + 根 pytest 177，退出码 0
- CPU + CUDA 构建通过

## 6. NOT_TESTED（E8.4 运行时验证覆盖）

- hybrid（Qwen3.5-4B）/ SWA / recurrent 能力拒绝的实际运行（4B 实测在 E8.4）
- 未识别 memory 实现拒绝：默认 false 路径（代码审计确认；无未识别实现模型可测）
- state save/load、cancel/retry/resume 生命周期（E8.4）
