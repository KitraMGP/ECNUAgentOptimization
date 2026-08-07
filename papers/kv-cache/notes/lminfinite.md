# LM-Infinite: Simple On-the-Fly Length Generalization for Large Language Models

- **arXiv**: 2308.16137 | **Venue**: arXiv preprint（VENUE_UNVERIFIED）
- **作者**: 子代理读取内容（LM-Infinite 论文）
- **阅读状态**: FULL_TEXT_READ
- **PDF SHA256**: 77ca6902d34c3ab3

## 核心思想
Λ-shaped attention mask：只允许 attention 到初始段（n_starting）与最近段（n_used − L_pre-train），中间区域屏蔽。对 RoPE 加 distance ceiling（起始段 key 位置钳制到固定值）。完全零训练零微调；无自定义 kernel（仅 mask + 距离上限）。

## 关键数据
- NLL@16K ArXiv：n_starting=0 → 6.43（劣化）；≥1 → ~1.02-1.03
- 生成 10K tokens：相近计算量下比截断基线高约 5 BLEU
- 消融：只加 mask 或只加 ceiling 均导致 NLL 爆炸

## 限制
- 仅适用相对位置编码 transformer；中间 top-k 对生成任务不必要但部分任务需要；未在 1G tokens 测试

## llama.cpp 映射
- Λ-shaped mask 可直接在 llama.cpp 注意力中实现（拆两段或 mask 中间）；distance ceiling 对 RoPE 只需把起始段 key 位置钳制
- **候选判断**: 可实现（P2 有损方向，中等难度）；与 StreamingLLM 兼容性强（同为"保留开头+滑动最近"家族）
