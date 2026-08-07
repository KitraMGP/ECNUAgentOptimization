# InfiniGen: Efficient Generative Inference of Large Language Models with Dynamic KV Cache Management

- **arXiv**: 2406.19707 | **Venue**: arXiv preprint（VENUE_UNVERIFIED；同组 Tender 发 ISCA 2024）
- **作者**: Wonbeom Lee, Jungi Lee, Junghwan Seo, Jaewoong Sim（Seoul National University）
- **阅读状态**: FULL_TEXT_READ
- **PDF SHA256**: 267d689a1ded953f

## 核心思想
面向 offloading 型推理（KV cache 在 CPU 内存）。相邻层 Transformer block 输入高度相似（余弦相似度 OPT 0.95–0.97、Llama-2 0.89–0.91）→ 用 Layer i−1 的 attention 输入 + Layer i 部分权重做"最小排练"，推测 Layer i 注意力模式，只从 CPU 预取关键 token KV。

## 关键数据结构与算法
- 离线 SVD skewing：对每层 query 矩阵 SVD，W_Q、W_K 乘正交矩阵 A（数学等价非近似），使少数列主导注意力分数，预测只需 30% 列
- Prefill：取 |Q̃|+|K̃| 列绝对值和 top-k 选列（k=30%），生成 partial query weight 与 partial key cache 存 GPU
- Decoding：partial_query × partial_key_cache 推测 score；选 score > (max−α) 的 token 预取 K/V
- CPU KV 池：counter-based victim selection；两阶段流水（CPU 预取与 GPU 计算重叠）

## 论文关键数据
- 相比现有 KV cache 管理方法最高 3.00× 加速、准确率最多提升 32.6 个百分点
- 延迟（OPT-13B）：1.63×–32.93× 加速
- 扩展性：随序列长度增长至 5.28×（OPT-13B）
- partial query weight 仅占模型参数 2.5%、partial key cache 占 15% 总 KV
- 32K 长上下文 PPL 接近 full-cache；1M 分析显示永久淘汰法会丢失关键上下文

## 限制
- 只适用 offloading（CPU 内存）场景，全 GPU 无收益；需离线 SVD 权重变换；预测依赖相邻层输入相似性

## 训练/CUDA
无需训练；需离线权重修改（SVD skewing）；无需专用 attention kernel

## llama.cpp 映射
- llama.cpp 支持部分层/KV offload 到 CPU（backend 调度自动搬运），但无 CPU KV 池+动态淘汰管理
- attention 图按整段连续 KV view 构建，无按 token 子集索引读取路径
- **候选判断**: 长期候选（需新算子 + offload 改造）；SVD 权重变换可独立成离线工具
