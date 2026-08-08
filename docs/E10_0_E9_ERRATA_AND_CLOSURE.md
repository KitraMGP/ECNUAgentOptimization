# E10.0：E9 勘误与证据关闭（Errata & Closure）

- 复核时间：2026-08-08
- 目的：逐项修正 E9 遗留矛盾，关闭证据缺口

## 1. E9 勘误清单

| # | E9 问题 | 勘误 |
|---|---|---|
| 1 | E9.4 "KV type 不可用"与"q8_0 可用"矛盾 | 根因 = 参数名笔误（`--ctk` 不存在，应为 `-ctk`/`--cache-type-k`）；E9.4 正文表格已修正（q8_0 实测可用）；本报告正式勘误：**E9.4 初版的"`-ctk/-ctv` 对 hybrid 无效"结论错误，撤销**——4B hybrid 实测支持 q8_0/q4_0 KV |
| 2 | 4B strict C1 off/on paired raw 缺失 | **本阶段补测完成**（见 §2）：同一 build `795eb372f`、同模型、同配置，仅 `--kv-prefix-share` 不同 → hash 全一致、capability rejected、shared=0、kv 资源一致 |
| 3 | E9.2 cancel 场景实为 cleanup 近似 | **勘误**：该 build 无 `/cancel` endpoint（E9.2 已注"cleanup 语义近似"）；本报告明确：**cleanup 近似测试不得称为 cancel 测试**——实际 cancel 验收见 E10.1（若无 API 标 NOT_SUPPORTED）|
| 4 | LoRA runtime skipped ≠ runtime identity 通过 | **确认**：`test_kv_prefix_share_lora.py` 3 例为 skipif（资产缺失），不计入 runtime identity 通过；`LORA_RUNTIME_STATUS: NOT_VERIFIED` 保持 |
| 5 | H4 NO_GO 范围 | **限定**：NO_GO 仅适用于 **end-position clone**（从 P+X 末尾复制状态到 P+Y 起点 → recurrent 错位）；**不适用于 prefix-aligned checkpoint restore**（E10.4 单独验证）|
| 6 | C1_ATTENTION_ONLY_PASS 状态 | E9 授予的 PASS 在本阶段（E10.0/E10.1 完成）前为 **provisional**：`C1_ATTENTION_ONLY_STATUS: PROVISIONAL_C1_ATTENTION_ONLY_PASS` |

## 2. 4B strict paired raw（证据关闭）

- 配置：build-cuda `795eb372f`、Qwen3.5-4B-Q4_K_M.gguf（de8e96cd...）、ctx=2048/parallel=2、kv-unified、cache-ram=0、temp=0/seed=42；仅 `--kv-prefix-share` 不同（off/on 各完整 server 生命周期）
- raw：`benchmark/results/kv_optimization/raw/e10_c1_paired_4b.json`

| 检查项 | off | on |
|---|---|---|
| server_ok | True | True |
| req1/req2 code | 200/200 | 200/200 |
| req1+req2 输出 hash | H1/H2 | **H1/H2（完全一致）** |
| capability 日志 | — | `E8-C1: capability rejected: memory implementation does not support...` |
| E6-C1 shared 日志 | 0 | **0** |
| kv used_cells / active / used_bytes | 1898 / 2 / 62193664 | **1898 / 2 / 62193664（一致）** |
| shared_cells | 0 | **0** |

**结论：C1 在 Qwen3.5-4B 上由正向能力判断正确拒绝，on/off 输出与资源状态完全一致 → `QWEN3.5_4B_STATUS: CORRECT_NO_OPTIMIZATION`（保持）**

## 3. 证据关闭后的状态（E10 起点）

```text
PROJECT_STATUS: PARTIAL_KV_CORRECTNESS_NO_OPTIMIZATION（保持，未定案）
C1_ATTENTION_ONLY_STATUS: PROVISIONAL_C1_ATTENTION_ONLY_PASS（E10.1 完成后确认）
QWEN3.5_4B_STATUS: CORRECT_NO_OPTIMIZATION（paired 证据关闭）
QWEN35_Q8_KV_STATUS: CANDIDATE_SINGLE_CASE_VERIFIED（E10.3 生产化）
QWEN35_CHECKPOINT_REUSE_STATUS: NOT_IMPLEMENTED（E10.4 原型）
PROMPT_CACHE_IDENTITY_STATUS: FIXED_IN_CODE_RUNTIME_NOT_VERIFIED（E10.1 结构化单测）
```
