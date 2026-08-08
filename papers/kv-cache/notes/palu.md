# Palu: Compressing KV-Cache with Low-Rank Projection

- **arXiv**: 2407.21118 | **Venue**: arXiv preprint（VENUE_UNVERIFIED）
- **作者**: Chi-Chih Chang, Wei-Cheng Lin, Chien-Yu Lin, Chong-Yan Chen, Yu-Fang Hu, Pei-Shuo Wang, Ning-Chi Huang, Luis Ceze, Kai-Chiang Wu
- **阅读状态**: FULL_TEXT_READ
- **PDF SHA256**: 676754dec64711c3

## 核心思想
hidden dimension 压缩：用低秩投影 A/B 把 K/V 压缩成低维 latent H 存储，attention 时在线重建。离线 SVD 分解（J-LRD 最优：join decomposition 不改变输出）+ Hadamard 旋转改善量化 + rank search（自动 rank 每层/每 head）。

## 论文关键数据
- Palu-30% 3-bit：Llama-2-7B Wiki2 5.33 PPL、8.4GB（vs fp16 64.0GB，86.87% 压缩）；2-bit 5.76、5.6GB（91.25%）
- 2-bit 下 Palu-30% 比 KVQuant 低 1.19 PPL 且内存再多省 30%
- 低秩 50% + 4-bit = 91.25% 总压缩（11.4×）
- 速度（64K，RTX 4090）：RoPE attention 1.89×、+4-bit 2.91×；端到端 1.71×/2.59×
- J-LRD（group 32）精度最好；M-LRD 差（50% zero-shot −7.26%）

## 限制
- RoPE 模型 key 必须在线重建（额外成本）；短序列无加速；J-LRD 权重膨胀 +40%；50% 压缩长上下文难保持精度

## 训练/CUDA
不需要训练（离线 SVD 分解）；需要自定义 kernel（RoPE 在线重建 Triton + 量化融合 CUDA）

## llama.cpp 映射
- 模型层：需新增 A/B 低秩权重（ggml 格式新张量）；non-RoPE 模型可离线融合几乎零运行时改动
- KV 层：每 cell 存低维 latent H 而非原始 K/V（可定义新 per-cell 类型）
- attention 层：RoPE 需"H→重建 K→RoPE→score"在线流水，flash-attn 无法直接复用
- **候选判断**: 可实现但工作量大（架构级改动 + 新算子 + 多后端），不如 DuoAttention 顺滑；与现有 KV 量化正交可叠加
