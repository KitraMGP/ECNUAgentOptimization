# LeanKV（DiffKV: Differentiated Memory Management for Large Language Models with Parallel KV Compaction）

- **arXiv**: 2412.03131 | **Venue**: arXiv preprint（VENUE_UNVERIFIED）
- **作者**: 子代理读到的文件实际内容为 DiffKV（与 LeanKV 论文标题不符；arXiv ID 2412.03131 对应文件内为 DiffKV 论文）
- **阅读状态**: FULL_TEXT_READ
- **PDF SHA256**: 5e191c11d28a7d2e

## 核心思想
LLM serving 中 KV 内存差异化管理：不同模型/层/token 对 KV 精度敏感度不同 → 差异化精度分配（K8V4 等）+ 并行 KV compaction。统一 paging（固定大小页、多精度存储）+ 双向页表。

## 关键数据
- KV cache 压缩 2.7×–5.7×（near-lossless）；吞吐提升 1.9×–5.4×
- 平均精度退化仅 0.3%；内存 19.3%–36.7%
- K8V4 匹配 FP16；K4V1（2-bit value）近零 → 2-bit 是 value 量化下界
- Qwen2.5-7B（GQA ratio 7）对 4-bit key 高度敏感
- 需 4.5K 行 CUDA/C++（混合精度 attention kernel、并行前缀和内存管理器）

## 限制
- 高 GQA 比率架构是已知风险；阈值需离线 profiling；长 CoT 压缩误差累积传播

## 训练/CUDA
无需训练；深度依赖自定义 CUDA kernel

## llama.cpp 映射
- 可近似部分：token 重要性剪枝（与 ScissorHands 类似）
- 主要障碍：per-token 混合精度（连续 cell 布局要求同构）；无 paged 内存管理；自定义 kernel
- **候选判断**: 完整方案 REJECT（需架构级重构）；per-head 均匀量化（现有 KV 量化）+ 重要性剪枝可行中间态
