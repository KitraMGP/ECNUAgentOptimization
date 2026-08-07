# DuoAttention: Efficient Long-Context LLM Inference with Retrieval and Streaming Heads

- **arXiv**: 2410.10819 | **Venue**: ICLR 2025（proceedings 确认）
- **作者**: Guangxuan Xiao, Jiaming Tang, Jingwei Zuo, Junxian Guo, Shang Yang, Haotian Tang, Yao Fu, Song Han（MIT/Tsinghua/SJTU/Edinburgh/NVIDIA）
- **阅读状态**: FULL_TEXT_READ
- **PDF SHA256**: 0c121d19e5092019

## 核心思想
注意力头分两类：Retrieval Heads（必须全量 attention，需要完整 KV cache）与 Streaming Heads（主要关注 attention sinks + recent tokens，只需常数长度 KV）。retrieval head = "被限制到 only sink+recent 时显著改变模型输出的 head"。识别方法：给每个 KV head 分配可训练门值 α，用合成 passkey 数据做蒸馏训练（权重冻结），部署按阈值二值化。仅对 retrieval heads 全量 KV、streaming heads 常数 KV。

## 关键数据结构与算法
- 门控混合 attention：attn = α·full_attn + (1−α)·streaming_attn（训练）
- 部署二值化：α > τ → full；否则 streaming；τ 由 sparsity quantile 决定
- 部署前 head 重排（retrieval/streaming 聚成连续簇避免 scatter/gather）
- 每层两个 KV cache（retrieval 全量 + streaming 常数）；chunked pre-filling（FA2）

## 论文关键数据
- 内存节省：MHA 最高 2.55×、GQA 1.67×；解码加速 MHA 2.18×、GQA 1.50×
- Retrieval head 比例：Llama-2-7B（MHA）25%、Llama-3-8B/70B（GQA）50%
- LongBench 21 任务（Llama-3-8B，50% budget）平均：Duo 40.21 > Full 40.08
- 组合量化：DuoAttention + QServe → 单 A100 3.30M tokens，6.4× 容量
- 识别：128 sink + 256 recent；2,000 步门值优化，8×A100 数小时

## 限制
- 每个新模型需重跑门值识别训练（数小时/8 GPU）；GQA 下可压缩比例更低（50%）；streaming heads 丢弃中间 token → 需通过 streaming head 访问远程中间信息的任务失败（NarrativeQA 等）

## 训练/CUDA
需要合成数据门值训练（权重冻结，非全训练）；无新 kernel 需求（FA2 分块 pre-fill 即可）

## llama.cpp 映射
- 与 llama.cpp 连续 cell KV 布局天然契合（分两段 KV：retrieval 全量 + streaming 环形缓冲）；无新 kernel
- 需要：head 分类（离线门值训练）+ 每 head 选择全量/流式路径 + streaming 环形缓冲
- **候选判断**: 可实现（P2 有损方向，MHA 25% 收益大于 GQA 50%）；门值识别需模型专用工具链
