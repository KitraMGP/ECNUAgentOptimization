# ScissorHands: Exploiting the Persistence of Importance Hypothesis for LLM KV Cache Compression at Test Time

- **arXiv**: 2305.17118 | **Venue**: NeurIPS 2023（正文 preprint；自引 NeurIPS 36 2024）
- **作者**: Zichang Liu, Aditya Desai, Fangshuo Liao, Weitao Wang, Victor Xie, Zhaozhuo Xu, Anastasios Kyrillidis, Anshumali Shrivastava（Rice）
- **阅读状态**: FULL_TEXT_READ
- **PDF SHA256**: 2b75201ee3a7f636

## 核心思想
重复注意力模式（Repetitive Attention Pattern）：不同位置 token 的高注意力集中到几乎相同一组 token。持久重要性假说：只有 pivotal token（此前产生显著影响的 token）才可能对未来产生显著影响；persistence ratio 多数层 >95%。测试时将 KV cache 维持在固定预算 B，无需微调。

## 关键数据结构与算法
- Algorithm 2：历史窗口 w 内累加低分 token 计数器 I（阈值 1/t 平均分）；最近 r 个 token 永远保留；丢弃被累计低分次数最多的 m 个 token
- 预算分配：层内各 head 均分；层间按持久性分布分配（后期层给更多）
- 理论：注意力分数幂律假设下压缩输出与原始输出期望差上界

## 论文关键数据
- KV cache 内存最高减少 5× 且无质量损失
- 默认 w=400、r=10、m=0.5B
- PPL 保持到原始 KV 的 50%（OPT-6B/13B）、75%（OPT-66B）
- 下游任务 accuracy 一般在 15%–30% KV 时仍保持；兼容 4-bit 量化（Hellaswag 2× 压缩无复合误差）

## 限制
- 无法访问训练过程无法确定模式来源；随机初始化模型不成立该假说；压缩步骤引入额外注意力计算

## 训练/CUDA
无需训练（测试时算法）；不需要新 CUDA kernel（attention score 累计 + argsort）

## llama.cpp 映射
- 核心操作"固定预算周期性驱逐 token"与 llama.cpp 连续 cell KV 布局兼容：只需为每 sequence 维护 importance 计数数组
- 驱逐后两方式：物理 memmove 到连续区（省内存）或保留占位 + 存活掩码（改 flash_attn 扫描）
- **候选判断**: 可实现（P2 有损方向，中等复杂度）；注意 mask 方式省算力不省内存
