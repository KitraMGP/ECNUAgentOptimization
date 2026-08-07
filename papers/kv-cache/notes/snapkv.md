# SnapKV: LLM Knows What You are Looking for Before Generation

- **arXiv**: 2404.14469 | **Venue**: NeurIPS 2024（poster 93531 确认）| 代码 https://github.com/FasterDecoding/SnapKV
- **作者**: Yuhong Li, Yingbing Huang, Bowen Yang, Bharat Venkitesh, Acyr Locatelli, Hanchen Ye, Tianle Cai, Patrick Lewis, Deming Chen（UIUC/Cohere/Princeton）
- **阅读状态**: FULL_TEXT_READ（精读）
- **PDF SHA256**: d17ce59a5a3342c9b0560c3503b0136f794fae92910421aa07dbca32e4279bf4

## 核心思想
每个 attention head 生成时只稳定关注 prompt 特定特征；该模式可用 prompt 末尾 observation window 提前识别。SnapKV 在 prompt 阶段用 observation window 的 query 对 prefix 的 key 做 voting（按 head 聚合注意力）→ TopK 选出重要位置 → 1D pooling（clustering）保留簇 → 选中 prefix KV 与 observation window 拼接成压缩 KV。**prompt KV 一次性压缩、生成阶段零额外开销**。

## 关键数据结构与算法
- Lprompt = Lprefix + Lobs；Voting: C = Σ Wobs[:, i, :]，I = Topk(C, k)，k = ⌊p × Lprefix⌋
- Clustering: pool1d(vote, kernel_size, padding, stride=1)（max-pooling）
- Listing 1：q_len < capacity 全保留；否则投票→pool→topk→gather 选中 prefix KV + obs window

## 论文关键数据
- 16k 输入：3.6× 解码加速（恒定不随长度增长）；8.2× 内存效率（131k vs 16k OOM）
- NIAH 380k：140k 前正确检索；380× 压缩比（1024 KV + window 16 + kernel 5）
- LongBench：1024 容量即近似全精度；SnapKV(1024) 在 Mistral 上 11/16 数据集优于 H2O(4096)
- Command-R 128k 32× 压缩 -0.5%；RAG F1 -1.2%~-2.1%

## 限制
- 不能扩展模型长上下文能力；不覆盖 prompt 推理阶段（prefill 仍需全量处理）；指令差异大时 overlap 降

## 训练/CUDA
无需训练、无离线校准；无需新 CUDA kernel（topk + gather + pool1d + cat）

## llama.cpp 映射
- **天然契合**：一次性 prompt 后压缩对应 llama.cpp "prefill → decode" 两阶段；observation window = slot 缓存尾部连续 Lobs token
- 所需能力：(a) prefill 注意力分数输出通道（FA 下需新增）；(b) 压缩后 KV 紧凑/重排到连续 cell（`ggml_set_rows` 支持索引 copy）；(c) server 层压缩触发与记账
- 位置编码无阻碍（pos 存 cell，RoPE 不需重算）；kernel 连续性需紧凑后满足
- **per-head 选择冲突**：llama.cpp cell 是 token 级共享多 head → 需降级为 token 级选择（实现简单、精度差异可实验）
- **候选判断**: 可实现（P1 有损候选，比 H2O 映射难度低一档）
