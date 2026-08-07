# H2O: Heavy-Hitter Oracle

- **arXiv**: 2306.14048 | **Venue**: NeurIPS 2023（正文标注）| 代码 https://github.com/FMInference/H2O
- **作者**: Zhenyu Zhang, Ying Sheng, Tianyi Zhou, Tianlong Chen, Lianmin Zheng, Ruisi Cai, Zhao Song, Yuandong Tian, Christopher Ré, Clark Barrett, Zhangyang Wang, Beidi Chen
- **阅读状态**: FULL_TEXT_READ（精读）
- **PDF SHA256**: f2d699db2c1fe34b53677067ba6f3b903a6f87dbd04e4f6ad5b9fa324cdb36df

## 核心思想
注意力矩阵 >95% 稀疏（仅 ~5% KV 足够解码）；累计注意力分数服从 power-law → 存在 Heavy-Hitters（H²）；贪心局部统计（每步只用之前 token 的注意力分数之和）即可。H²O = 动态保留"最近 token + H² token"（budget 对半分），每步最多淘汰 1 个 KV。

## 关键数据结构与算法
- Fscore(T) = Σ o_i（累计注意力）；用长度-n 累计数组把 O(n²) 降 O(n)
- Budget 分配：总预算 k（默认 20%），前 K 槽位 H²、后 K 槽位 recent（环形队列）
- Algorithm 1：cache 满后每步淘汰累计注意力分最低的 1 个 token，每层每 head 独立执行
- 理论：动态子模最大化贪心近优 (1−1/e)(1−α)opt − β

## 论文关键数据
- 精度无损失内存缩减最多 5×（5-10× 多数任务）；20% budget 与 full KV 相当
- T4 吞吐 vs FlexGen/DeepSpeed/Accelerate 提升最多 3×/29×/29×
- 消融：仅 H² 或仅 Local 都失败（退化 2.85%–22.75%）
- 组合 4-bit 量化无复合误差（Table 6）

## 限制
- 模型参数仍主要占用；0/1-shot 任务需 30–40% budget
- Local 策略在部分模型上 60% budget 即崩溃

## 训练/CUDA
无需训练、无需离线校准；不需要专用 CUDA kernel（FlexGen 白盒实现）

## llama.cpp 映射
- 内存布局：llama.cpp 是 cell-based（每 token 一格）；写入是索引式（`ggml_set_rows` 按 cell 索引）→ 覆盖式淘汰写入路径天然支持
- 读取约束：`get_k/get_v` 返回连续区间 view，flash attention 接收连续 view → 覆盖式淘汰（不产生空洞）下 view 仍连续
- mask 基于 pos/seq 而非物理索引 → 空洞/乱序 cell 会被正确 mask
- **冲突点**: per-head 独立淘汰与 llama.cpp token 共享 cell 模型冲突（需重构 cell 粒度）；需要每层每 head 注意力分数输出（FA 下不物化）
- **候选判断**: 论文原样 per-head 独立淘汰 REJECT_INCOMPATIBLE_ARCHITECTURE；token 级共享分数简化版有条件可行
