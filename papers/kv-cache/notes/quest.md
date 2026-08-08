# Quest: Query-Aware Sparsity for Efficient Long-Context LLM Inference

- **arXiv**: 2406.10774 | **Venue**: ICML 2024（PMLR 235）| 代码 https://github.com/mit-han-lab/Quest
- **作者**: Jiaming Tang, Yilong Zhao, Kan Zhu, Guangxuan Xiao, Baris Kasikci, Song Han（SJTU/MIT/UW/NVIDIA）
- **阅读状态**: FULL_TEXT_READ（精读）
- **PDF SHA256**: 93cda144859b4e38d595c42dbc578e4e0c8d436fa0891e3dfeb74716bacc4ce8

## 核心思想
token 关键性与当前 query 强相关 → 查询感知稀疏。page 粒度管理（PagedAttention 式，16 KV pairs/page）；每页记录 Key 各维 min/max；Stage 1 用 query 与该页 min/max Key 逐元素乘积+per-channel max 求和得页注意力上界；Stage 2 只加载 Top-K 关键页做 attention。**保留全部 KV（不淘汰）**，只选择参与注意力 → 不丢远期依赖。前两层全量（sparsity <10%）。

## 论文关键数据
- 7.03× self-attention 加速（32K/2048 budget vs FlashInfer）；2.23× 端到端（+4-bit AWQ）
- 内存公式：1/PageSize + K/PageNum（64K ctx/16 page/top 4K → 8×）
- passkey 10k：budget 64 即 99%（H2O 1%）；100k：1024 即 100%
- LongBench：1K budget 达 full cache 相当；lossless 时 KV sparsity 1/5–1/10
- 前两层必须 full cache；criticality estimation 仅 5–10 µs

## 限制
- 不做 token 淘汰（KV 内存不变，只省带宽）；短序列优势小；需自定义 CUDA kernel

## 训练/CUDA
不需要训练；需要 CUDA kernel 修改（criticality estimation + top-k + 稀疏页加载 attention，基于 FlashInfer）

## llama.cpp 映射
- **Stage 1 可用现有 ggml 算子拼装**（min/max 归约、top-k、kq_mask 修改）；现有 `build_attn_inp_kv_iswa` 已用 ggml_top_k + set_rows 做掩码稀疏（不省带宽）
- **Stage 2 无加速路径**：ggml-cuda flash attention 全量遍历 KV，无稀疏页跳转；需新增 CUDA kernel 或 graph 层紧凑化
- **候选判断**: 选择逻辑部分可行（不省内存不省带宽）；7× 级加速需 CUDA kernel → REJECT_UNSUPPORTED_HARDWARE（现阶段）
