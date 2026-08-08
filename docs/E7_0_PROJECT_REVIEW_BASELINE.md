# E7.0：项目复核与环境确认基线（Project Review Baseline）

- 复核时间：2026-08-08
- 复核范围：E6.0-E6.6 全部交付（原最终状态 `PASS_KV_CACHE_OPTIMIZATION` 视为 **provisional**，本复核完成前不作为最终事实）

## 1. 环境快照

| 项 | 值 |
|---|---|
| 根仓库 branch | `benchmark-enhance` |
| 根仓库 HEAD | `071a0f2d8ed82a783e932f1dc6f88dad518efb22` |
| 根仓库工作树 | clean（仅未跟踪 `docs/20260808_REVIEW_INSTRUCTION.md`、`docs/20260807_MAJOR_INSTRUCTION.md`）|
| llama.cpp branch | `llama.cpp` |
| llama.cpp HEAD | `02541f720ec025d89c548f4de173e962cf02f833` |
| llama.cpp 工作树 | clean |
| 根 remote | `https://github.com/KitraMGP/ECNUAgentOptimization.git` |
| llama.cpp remote | `https://github.com/KitraMGP/ECNUAgentOptimization-llama.cpp.git` |
| GPU | RTX 4060 Laptop 8GB（8188 MiB），驱动 610.43.03 |
| llama-server 进程 | 无（空闲）|
| 模型 | `models/qwen3-5-4B-Q4_K_M.gguf`（存在）|
| 构建 | `llama.cpp/build`（CPU）+ `llama.cpp/build-cuda`（CUDA）均存在 |

## 2. 上次报告 commit 与实际存在情况

全部存在（`git cat-file -e` 验证）：
- 根：`c1b4a96`（E6.0）、`b321f87`+`f74589b`（E6.1）、`28b892a`（E6.2）、`8d9b74f`（E6.3）、`9b80a68`（E6.4）、`0f64e73`（E6.5）、`071a0f2`（E6.6）
- llama.cpp：`568b8733`（perf C1）、`02541f72`（test 保护）

## 3. 代码、测试、脚本、原始数据一致性

全部存在：
- 报告：`docs/E6_0..E6_5` + `docs/E6_KV_OPTIMIZATION_FINAL_REPORT.md` + `docs/KV_CACHE_LITERATURE_REVIEW.md` + `docs/KV_CACHE_CURRENT_ARCHITECTURE.md`
- 论文：`papers/kv-cache/{manifest.json, references.bib, notes/21 篇}`
- 脚本：`benchmark/scripts/e6_{runner,summarize,c1_probe,c1_bench,c1_churn,c2_oracle}.py`
- raw 数据：`benchmark/results/kv_optimization/raw/` 18 个 JSON + 日志

## 4. 上一轮 PASS 初步可信度：**低**（以下质疑点经初步核对成立）

1. **主模型无收益**：E6_2 报告自述 Qwen3.5-4B（hybrid）上 C1 自动禁用（`llama_model_is_hybrid`）→ 主模型没有获得 C1 收益；E6_FINAL 的 PASS 依据标准架构 TinyLlama
2. **C1 非完整 radix tree**：E6_2 实现为线性 idle-slot LCP 扫描 + `seq_cp` 元数据共享；无 radix tree、无节点分裂/合并、无节点级 LRU、无 cache-aware scheduling——但候选名/报告多处使用 "C1_RADIX_PREFIX_SHARING" / "RadixAttention 式"
3. **C2 范围**：E6_3 脚本 `e6_c2_oracle.py` 只做 prompt token 计数 + budget keep 估算 + 理论 attention KV bytes 计算；无 attention score、无压缩 KV 生成、无 decode、无质量测量——被命名为 "SnapKV 式 prompt KV 压缩离线 oracle"，范围不符
4. **文献 metadata 需核验**：manifest 中 KIVI 标 ICML 2024（正文页脚确认为 ICML）；H2O 标 NeurIPS 2023（与 ICML 2024 潜在矛盾）；SnapKV 标 NeurIPS 2024（正文标注 "Preprint. Under review."）；部分 venue 需重新核验
5. **cache identity / 生命周期未独立验证**：C1 共享仅比较 token LCP，未验证 adapter/RoPE/KV type/context config 等 identity；共享源保护、state restore、cancel/retry 的引用完整性无专门测试

## 5. 发现的文档-代码-数据矛盾

| # | 矛盾 |
|---|---|
| 1 | 报告称 "C1_RADIX_PREFIX_SHARING / RadixAttention 式"；实现为线性 LCP + seq_cp（无 radix 结构）|
| 2 | 最终 PASS 声称"优化收益"；主模型 4B hybrid 上 C1 自动禁用（收益仅 TinyLlama）|
| 3 | E6_3 称 "SnapKV oracle"；脚本无任何 attention score / 压缩 / decode |
| 4 | E6_2 报告提及 "hybrid 自动禁用（WRN 提示）"——需验证 WRN 是否实际存在（load_model 仅打印 enabled）|
| 5 | E6_FINAL 第 12 节自我标注"若验收要求收益在主模型上则改判 PARTIAL"——本复核正是此情形 |

## 6. 结论

- E6 的 PASS_KV_CACHE_OPTIMIZATION 为 **provisional，不成立为最终事实**
- 后续阶段：E7.1 C1 identity/生命周期审计 → E7.2 必要修正 → E7.3 上游对比 → E7.4 扩展 benchmark → E7.5 C2/文献修正 → E7.6 最终状态
- 初步预期：主模型仍为 Qwen3.5-4B 且 C1 禁用时，项目级状态**优先倾向 `PARTIAL_KV_CORRECTNESS_NO_OPTIMIZATION`**（按指令十五规则 2）
