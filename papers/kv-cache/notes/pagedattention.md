# PagedAttention（vLLM）

- **arXiv**: 2309.06180 | **Venue**: SOSP 2023 | DOI 10.1145/3600006.3613165
- **作者**: Woosuk Kwon, Zhuohan Li, Siyuan Zhuang, Ying Sheng, Lianmin Zheng, Cody Hao Yu, Joseph E. Gonzalez, Hao Zhang, Ion Stoica（UC Berkeley/Stanford/UCSD）
- **阅读状态**: FULL_TEXT_READ（精读）
- **PDF SHA256**: 55b3b324d779a67c59dac2519445e3b07c14e6ff5c656fadb47a3d7b5997469e

## 核心思想
受 OS 虚拟内存分页启发，把请求 KV cache 划分为固定大小 KV block，存于非连续物理内存；block table 记录逻辑块→物理块映射（block=page、token=byte、request=process）。实现近零内存浪费、block 粒度共享（parallel sampling/beam search/shared prefix）、COW + 抢占（swap/recompute）。

## 关键数据结构与算法
- 逻辑/物理 KV block（默认 16 token/块）；block table（物理块号 + #filled）
- refcount（物理块引用计数）；COW（写共享块先复制）
- 三原语：fork / append / free
- prefill 按 prompt 分配块；decode 每步 1 token，最后块满才分配新块（浪费 ≤1 块）
- 抢占：FCFS，swap 到 CPU RAM 或 recompute（tokens 拼回 prompt 重 prefill）

## 论文关键数据
- vLLM 使吞吐提升 2–4×（同延迟）；现有系统仅 20.4%–38.2% KV 内存用于实际 token
- kernel 开销比 FasterTransformer 高 20–26%（仅 attention 算子）
- 共享：parallel sampling 省内存 6.1%–30.5%；beam search 37.6%–66.3%；共享前缀 5-shot 3.58×
- block size 默认 16；recompute 开销从不高于 swap 的 20%

## 限制/失败场景
- 必须重写 attention kernel（非连续 KV 读取）；非 LLM compute-bound 场景间接寻址反而降级
- block size 偏大→内部碎片+共享机会下降；swap 小块低效

## 训练/CUDA
无需训练；需要 CUDA kernel 修改（PagedAttention 读写 kernel）

## llama.cpp 映射
- **可行部分**：cell 级按需填充与跨序列共享已原生存在（`seq_cp` 零拷贝 + bitset，粒度比 block 更细）；`cpy_k/cpy_v` 的 idx 机制支持非连续写入
- **不可行部分**：读取侧 paged attention 需为全部 ggml 后端重写 kernel（与 ggml 图执行+多后端架构冲突）；运行时按需扩容与固定形状 KV buffer 不兼容；refcount/COW/swap 调度器超出单机推理库范围
- **候选判断**: 完整方案 REJECT_INCOMPATIBLE_ARCHITECTURE；部分借鉴（调度层 swap/recompute 思想）可行
