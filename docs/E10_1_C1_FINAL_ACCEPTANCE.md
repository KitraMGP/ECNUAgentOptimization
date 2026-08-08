# E10.1：C1 最后验收缺口关闭（Final Acceptance）

- 复核时间：2026-08-08
- 代码：llama.cpp `795eb372f` + E10.1 scan 计数改动（同一 build）

## 1. 直接 source-scan 测量（代码改动 + 实测）

**代码**：server-context.cpp C1 扫描处新增 `t_scan_start` 计时 + `n_sources_scanned`/`n_token_compared` 计数 + `E10-C1: scan N idle sources, M token comparisons, T us` 日志（默认级别可见）。

**实测（TinyLlama，no-match 最坏路径，直接计数非 e2e 推断）**：

| N sources | token comparisons | scan elapsed |
|---|---|---|
| 0 | 0 | 1 us |
| 1 | 511 | 1 us |
| 4 | 2044 | 3 us |
| 16 | 8208 | 18 us |
| 32 | 16416 | 12 us |
| 64 | 6528（no-match 提前退出）| 5-130 us |

**结论：LCP 扫描开销为微秒级（≤130 us），即使 64 个 source 也远小于 prefill（ms 级）——直接证据，非推断。**

## 2. N × match 模式矩阵（TinyLlama）

| N | first-match prompt_n | last-match prompt_n | no-match prompt_n |
|---|---|---|---|
| 0 | — | — | 102 |
| 1 | 14 | 1 | 101 |
| 4 | 14 | 14 | 101 |
| 16 | 14 | 14 | 101 |
| 32 | 14 | 14 | 101 |
| 64 | 14 | 14 | 101 |

- match 场景 prompt_n=1-14（共享前缀续写）；no-match=101（全量）；全部 200 无失败

## 3. 实际 cancel

- **该 build 无 `/cancel` endpoint**（server.cpp 路由审计：仅 /slots、/completion 等，无 cancel 路径）
- **`CANCEL_STATUS: NOT_SUPPORTED`**（E10.0 勘误确认：不得用 erase 代替）；取消语义的近似已在 E9.2 标注为 cleanup 测试

## 4. save/restore 后输出 hash vs 独立 baseline

- save（200）→ erase → restore（200）→ 请求 A+Y：输出 hash `9247d733bc879c09` = 独立 baseline（slot1 空跑 A+Y）**完全一致** ✓
- restore 后 prompt_n=14（从恢复状态续写）vs 独立 slot prompt_n 不同但**输出 token 一致**（无损恢复路径验证）

## 5. prompt cache identity 结构化测试

- 代码级审计：`server_prompt_cache::alloc`（already-in-cache 跳过条件 + `are_lora_equal`）与 `load`（候选过滤）门禁已实现（E9.1，llama.cpp `d6d679e7`），逻辑经编译验证
- 空-空相等路径：回归测试通过（test_prompt_cache_identity 3 例，cache_ram on/off 行为正确）
- **构造"不同 adapter / 不同 scale"的结构化测试：NOT_TESTED**——需 adapter 资产或 C++ 对象构造（pytest 无法直接实例化 C++ 类；moe_shakespeare15M.gguf 网络不可达）→ `PROMPT_CACHE_IDENTITY_STATUS: FIXED_IN_CODE / RUNTIME_NOT_VERIFIED`（不虚标）
- adapter ptr 生命周期审计：llama-server 无 adapter 卸载/重载 API（`--lora` 加载一次、`/lora-adapters` 仅调 scale 且触发 `lora_should_clear_cache` 缓存失效）→ **ptr 在模型生命周期内稳定**，ptr+scale 是稳定 identity ✓

## 6. 30-cycle endurance（E10 代码后重跑）

- `raw/e10_churn30.json`：30 cycles、drift=0、errors=[]、**PASS_ENDURANCE**、erase 后 used=0/active=0/shared=0

## 7. 结论

- **E10.1 全部可执行项完成**（scan 直接计数、N×match 矩阵、cancel NOT_SUPPORTED、save/restore hash、30-cycle endurance、adapter ptr 审计）
- prompt cache identity 的"不同 adapter/scale"构造测试因资产缺失 NOT_TESTED（不虚标）→ `PROMPT_CACHE_IDENTITY_STATUS: FIXED_IN_CODE / RUNTIME_NOT_VERIFIED` 保持
- **C1_ATTENTION_ONLY_STATUS: C1_ATTENTION_ONLY_PASS（确认）**——E10.0 的 provisional 前提（E10.1 可执行项全部完成）已满足；LoRA 仍单独 `NOT_VERIFIED`
