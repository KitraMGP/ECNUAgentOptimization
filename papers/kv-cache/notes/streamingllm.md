# StreamingLLM: Efficient Streaming Language Models with Attention Sinks

- **arXiv**: 2309.17453 | **Venue**: ICLR 2024
- **作者**: Guangxuan Xiao, Yuandong Tian, Beidi Chen, Song Han, Mike Lewis（MIT/Meta AI/CMU/NVIDIA）
- **阅读状态**: FULL_TEXT_READ
- **PDF SHA256**: e8a7de67c7c57642

## 核心思想
attention sink 现象：softmax 要求注意力分布和为 1，模型把多余注意力倾倒到初始 token（即使无语义）。纯 window attention 驱逐初始 token 后 PPL 剧烈飙升。StreamingLLM = 保留 4 个初始 token 作 attention sink + 最近 token 的 rolling KV cache，无需微调即可流式建模无限长度（最多 4M+ tokens）。

## 关键数据结构与算法
- Rolling KV Cache：(1) attention sinks（4 个初始 token）+ (2) 滚动窗口（最近 L 个 token）
- cache 内位置重编号：RoPE 用 cache 内偏移而非原始位置 → **缓存未旋转的 key**，每步按 cache 内位置施加旋转

## 论文关键数据
- 默认 4 个 sink token（1-2 个不足，4 个足够，8 个边际）
- Llama-2-13B PG-19：Window 5158 vs StreamingLLM 5.40（≈Dense）
- 效率：vs sliding window with recomputation 最高 22.2× 每 token 加速
- LongBench 4+3496 配置劣于截断基线（不适合长文档 QA/摘要）

## 限制
- 不扩展上下文窗口、无长期记忆；只对 cache 内信息有效；StreamEval 距离超出 cache 准确率跌至 0

## 训练/CUDA
零微调零训练；标准 GPU kernel；可选 sink-token 预训练

## llama.cpp 映射
- 原理 = 保留头部固定 cell + 环形滚动最近 L 个 cell + cache 内 RoPE 重定位
- llama.cpp RoPE 在 attention 内即时计算 → 可按 cache 内偏移施加，天然契合"缓存未旋转 key"
- 需把开头若干 cell 标记不可驱逐、滚动区环形覆盖
- **候选判断**: 可实现（P2 有损方向，中等难度）
