# E9.1：server prompt-cache LoRA identity 审计与修复（Prompt Cache Identity Audit）

- 复核时间：2026-08-08
- 背景：E8.6 确认本地继承上游 prompt cache/cache_ram 的 LoRA identity 缺陷（上游 issue #26207，open 未修复）

## 1. 审计结论（6 问）

| # | 问题 | 结论（修复前） |
|---|---|---|
| 1 | 不同 LoRA 的相同 token prompt 是否命中同一 RAM cache | **是**：`server_prompt_cache::load` 仅按 token LCP（f_keep/f_sim）匹配，`server_prompt_cache_state` 无 lora 字段 → 恢复错误权重下的 KV state |
| 2 | 不同 LoRA scale 是否命中 | **是**：scale 不参与比较 |
| 3 | 无 LoRA 与有 LoRA 是否命中 | **是**（无 lora 的缓存可被有 lora 请求恢复）|
| 4 | restore 后 cache identity 是否保留 | **否**：state 序列化不含 identity |
| 5 | C1 off 时是否仍存在错误复用 | **是**：这是 server prompt cache（cache_ram）独立于 C1 的缺陷 |
| 6 | C1 on + cache_ram on 时两条路径是否一致 | 修复前：C1 扫描有 lora 门禁（E7.2）、RAM cache 无门禁 → **不一致** |

**风险确认成立**（与 #26207 一致，本地继承）。

## 2. 修复实现（E9.1）

代码位置：`tools/server/server-task.{h,cpp}` + `tools/server/server-context.cpp`

1. **`server_prompt_cache_state` 增加结构化 adapter identity**：`std::vector<common_adapter_lora_info> lora`（ptr + scale，server-task.h）
2. **`alloc` 门禁**：already-in-cache 跳过条件加 `are_lora_equal(it->lora, lora)`——同 token 但 lora 不同不视为已缓存；push_back 保存 lora
3. **`load` 门禁**：候选循环先 `are_lora_equal(it->lora, lora)`，不匹配跳过（拒绝复用）；identity 缺失/不同 → 安全默认拒绝
4. **调用点**：`prompt_save` 传 `slot.lora`（保存当前任务 lora）；`get_available_slot` 的 `prompt_load` 传 `construct_lora_list(task.params.lora)`（目标请求 lora）
5. **不用 prompt 字符串/HTTP header 替代 identity**（ptr 是模型加载后的稳定 adapter 指针 + per-request scale）

## 3. 测试

新增 `tools/server/tests/unit/test_prompt_cache_identity.py`（3 例）：
- `test_cache_ram_on_reuses_same_prompt`：cache_ram on 相同 prompt 行为回归（输出一致、无错误）
- `test_cache_ram_off_no_reuse`：cache_ram off 对照（无复用）
- `test_lora_identity_gate_runtime_not_verified`：状态占位（无真 LoRA 资产）

**LoRA 差异门禁的运行时验证**：`RUNTIME_NOT_VERIFIED`（moe_shakespeare15M.gguf 网络不可达，无法构造非空 lora 请求；alloc/load 的 `are_lora_equal` 过滤已实现并经编译 + 代码级审计）。`test_kv_prefix_share_lora.py` 3 例保持 skipif。

## 4. 验证结果

- llama.cpp 全量：**50 passed + 3 skipped**（+3 新测试，LoRA 3 例 skipped 不计 passed）
- E1：5/5；根 pytest：177
- CPU + CUDA 构建通过（exit 0）

## 5. 结论

- **PROMPT_CACHE_IDENTITY_STATUS: FIXED_IN_CODE / RUNTIME_NOT_VERIFIED**（修复已实现并过编译+回归；真 LoRA 运行时验证待资产）
- 无 lora 场景（本项目默认）下，RAM cache 复用行为与修复前一致（回归测试通过）
- 该修复同时使 C1 on + cache_ram on 的两条复用路径 identity 语义一致
