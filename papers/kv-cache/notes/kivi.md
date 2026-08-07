# KIVI: A Tuning-Free Asymmetric 2bit Quantization for KV Cache

- **arXiv**: 2402.02750 | **Venue**: ICML 2024（PMLR 235，正文页脚确认）
- **作者**: Zirui Liu, Jiayi Yuan, Hongye Jin, Shaochen Zhong, Zhaozhuo Xu, Vladimir Braverman, Beidi Chen, Xia Hu（Rice/Texas A&M/Stevens/CMU）
- **阅读状态**: FULL_TEXT_READ（精读）
- **PDF SHA256**: df31ef32d71bfb280c533c5db8220cadf5ef42076bf45d82ba4c8da8e50ea5f4

## 核心思想
K cache 应 per-channel 量化（outlier channel）；V cache 应 per-token 量化（attention 稀疏 → 输出只依赖少数 token）。非对称 affine 量化：Q(X) = ⌊(X−z)/s⌉，B=2（KIVI-2）或 4（KIVI-4）。

## 关键数据结构与算法
- 流式矛盾：per-channel K 跨 token 无法流式 → K cache 拆 grouped（量化）+ residual（fp16）两部分，residual 满 R 整组量化并入
- V 侧队列：新 value 全精度 push，满 R 后 pop 最旧 per-token 量化并入
- 全精度 sliding window（最近 R 个 token 的 K/V 全精度）
- tiled 混合精度 MatMul + CUDA 融合 kernel

## 论文关键数据
- 默认 G=32（group size）、R=128（residual length）
- Llama/Mistral 上 2bit 精度损失 ≤2%；**Falcon-7B（MQA）必须 4bit**
- LongBench avg：Llama2-7B 16bit 44.52 / KIVI-2 44.27
- 峰值内存降低 2.6×；支持最高 4× batch size；吞吐提升 2.35×–3.47×
- 消融：G=128 显著下降（17.29 vs 20.77）；R 需足够大保 GSM8K（fake 2bit 无窗口 5.76 → KIVI 12.74）

## 限制
- MQA 模型 2bit 失败；GSM8K 等硬生成任务无全精度窗口则严重掉点；MMLU 等单步任务不适合评估

## 训练/CUDA
不需要训练；需要自定义 CUDA kernel（fused dequantize+matmul）和 Triton kernel

## llama.cpp 映射
- 现有 `-ctk/-ctv` 支持 K/V 独立 ggml_type（F32/F16/BF16/Q8_0/Q4_0/Q4_1/IQ4_NL/Q5_0/Q5_1），无 2bit 类型
- 现有量化粒度 = per-token 行内 block（沿 head 维 QK=32）→ 与 per-channel K（scale 沿 token 方向）方向正交
- 无 recent window fp16 混合精度；无 attention sink fp16
- `Q4_1`/`Q5_1`（block 级 d+m）可作非对称 4bit 近似
- **候选判断**: 精确复现 REJECT（缺 2bit 类型/per-channel 布局/混合精度窗口）；降级近似（-ctk q4_0 -ctv q4_1 等）部分可行
