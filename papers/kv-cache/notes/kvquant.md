# KVQuant: Towards 10 Million Context Length LLM Inference with KV Cache Quantization

- **arXiv**: 2401.18079 | **Venue**: NeurIPS 2024 | 代码 https://github.com/SqueezeAILab/KVQuant
- **作者**: Coleman Hooper, Sehoon Kim, Hiva Mohammadzadeh, Michael W. Mahoney, Yakun Sophia Shao, Kurt Keutzer, Amir Gholami（UC Berkeley/ICSI/LBNL）
- **阅读状态**: FULL_TEXT_READ（精读）
- **PDF SHA256**: 7ca1001fee6be5013d40ebf00e81df3bb677c90f1feb5c78b413cba64589ecfc

## 核心思想（5 组件）
1. Per-Channel Key Quantization（匹配 outlier channel）
2. Pre-RoPE Key Quantization（RoPE 混合 channel 使 post-RoPE outlier 不稳 → 在 RoPE 前量化、反量化后 on-the-fly RoPE）
3. Non-Uniform Quantization（nuqX）：敏感度加权 k-means（Fisher 信息 Fii），16 校准样本离线求 per-layer datatype
4. Per-Vector Dense-and-Sparse：移除 ~1% 数值 outlier 存稀疏矩阵（Key 用 CSC、Value 用 CSR）
5. Attention Sink-Aware：首 token 保留 fp16

## 论文关键数据
- 相对 fp16 退化：4bit <0.02、3bit <0.1、2bit <0.5 PPL（Wikitext-2）；内存压缩 3.7×/4.8×/6.9×
- nuq2 8-GPU 支持 10M context（611.5GB）
- RULER 32K：KVQuant-3bit-1% 53.65 vs KIVI-2 39.78 vs fp16 56.40
- 消融：per-channel V 灾难（PPL 223）；post-RoPE 7.05 vs pre-RoPE 6.23；attention sink nuq2 8.47→7.23
- 校准开销：LLaMA-65B Fisher 2.8 min、逐层校准 2-5 min

## 限制
- 超长上下文需模型本身长上下文训练（正交）；延迟基准只覆盖生成阶段不覆盖 prefill；稀疏矩阵更新分配低效

## 训练/CUDA
不需要训练；需要离线校准流程 + 自定义 CUDA kernel（4-bit LUT 稠密 matvec + 平衡 CSR/CSC 稀疏 matvec + on-the-fly RoPE + 在线 topk）

## llama.cpp 映射
- 无 2bit 非对称类型、无 nuqX per-layer LUT、无 dense+sparse 机制、无 pre-RoPE K 缓存、无 attention sink fp16
- `IQ4_NL`（4bit 非均匀 LUT）可作近似但网格固定、非 per-layer 校准
- 现有量化 V 必须开 FA；K head 维需整除 blck_size
- **候选判断**: 精确复现 REJECT（需新类型/kernel/校准管线）；降级近似部分可行
