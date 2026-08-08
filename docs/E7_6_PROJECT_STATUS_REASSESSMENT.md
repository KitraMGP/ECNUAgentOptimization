# E7.6：项目状态复核报告（Status Reassessment）

- 复核时间：2026-08-08
- 复核范围：E6.0-E6.6 全部交付 + E7.0-E7.5 复核产物

## 1. 历史声明 vs 复核证据

| 历史声明（E6_FINAL） | 复核证据 | 判定 |
|---|---|---|
| PASS_KV_CACHE_OPTIMIZATION | 主模型 Qwen3.5-4B（hybrid）上 C1 自动禁用（实证：共享日志 0）；收益仅 TinyLlama（attention-only）| **不成立（provisional → 降级）** |
| C1 = "C1_RADIX_PREFIX_SHARING / RadixAttention 式" | 实现为线性 idle-slot LCP 扫描 + seq_cp 元数据共享，无 radix tree | **名称不准确 → 修正为 Cross-slot exact-prefix KV metadata sharing** |
| C2 = "SnapKV 式 prompt KV 压缩 oracle" | 脚本仅容量估算（无 score/压缩/decode/质量）| **范围不实 → 降级为 KV_BUDGET_FEASIBILITY_ESTIMATOR** |
| 文献 venue 已核 | 12 篇正式收录全部确认；8 篇 preprint 保持 UNVERIFIED；SnapKV 正文 preprint 备注 | **基本准确，已补充说明** |
| C1 隔离/identity 正确 | E7.2 修复 3 个高危缺陷（lora identity / SWA purge / recurrent 漏检）| **修复后正确（审计 20 问 + 10 测试）** |

## 2. 代码修正（E7.2，llama.cpp `f5837427`）

1. 共享条件新增 `!llama_model_is_recurrent`（覆盖 Mamba/RWKV 纯 recurrent 漏检）
2. 共享条件新增 `llama_model_n_swa == 0`（覆盖 SWA/iswa purge 破坏）
3. source 扫描新增 `are_lora_equal(slot.lora, other.lora)`（adapter identity：ptr + scale）

## 3. 测试修正（E7.1，llama.cpp `e7e0855`）

- 新增 `test_kv_prefix_share_audit.py` 10 例（identity/生命周期/canary）
- llama.cpp 41/41 + E1 5/5 + 根 pytest 177 全部通过

## 4. 最终状态判定（指令十五）

| 状态 | 是否满足 | 依据 |
|---|---|---|
| PASS_KV_CACHE_OPTIMIZATION | 否 | 主模型无收益；未把 attention-only 明确定义为项目目标范围 |
| **PARTIAL_KV_CORRECTNESS_NO_OPTIMIZATION** | **是** | C1 真实实现存在 + correctness/生命周期通过 + attention-only 有收益（TinyLlama 67-94%）+ 主模型无收益 |
| PARTIAL_KV_RESEARCH_NO_IMPLEMENTATION | 否 | C1 代码存在 |
| HOLD_KV_OPTIMIZATION_INCOMPLETE | 否 | 无外部阻塞（GPU/网络/模型全可用）|
| REJECT_KV_CACHE_OPTIMIZATION | 否 | 无错误复用/污染/泄漏/伪造 |

## 5. 子状态汇总

```text
PROJECT_STATUS: PARTIAL_KV_CORRECTNESS_NO_OPTIMIZATION
C1_ATTENTION_ONLY_STATUS: C1_CONDITIONAL_PASS_RECOMPUTE_ONLY
  （门槛 1-8 满足：真实 KV 路径/无损/源保护/identity 隔离/生命周期/
   12 周期 endurance/recompute ≥25%/非共享 0% 退化；
   门槛 9-11 wall-time prefill/decode/p95 在消费级 GPU + TinyLlama
   ms 级噪声下无可靠证据 —— 不虚标完整 PASS）
QWEN3.5_4B_STATUS: CORRECT_NO_OPTIMIZATION
C2_STATUS: KV_BUDGET_FEASIBILITY_ESTIMATOR
UPSTREAM_COMPARISON_STATUS: UPSTREAM_COMPARISON_COMPLETE
```
