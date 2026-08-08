# vAttention

- **arXiv**: 2405.04437 | **Venue**: ASPLOS 2025（MLSys 相关）| DOI 10.1145/3669940.3707256
- **作者**: Ramya Prabhu, Ajay Nayak, Jayashree Mohan, Ramchandran Ramjee, Ashish Panwar（Microsoft Research Bengaluru/IISc）
- **阅读状态**: FULL_TEXT_READ（精读）
- **PDF SHA256**: 89017c3e7f5900cdf2b7295d647fc19bf45e674dddd3525ba86a5f203861c701

## 核心思想
PagedAttention 把 KV cache 虚拟布局从连续改为非连续，需重写 kernel + 自建内存管理器。vAttention 用 CUDA VMM API 解耦虚拟/物理分配：虚拟内存预分配大而连续（保留 kernel 兼容），物理按需（demand paging）映射——消物理碎片又不破坏虚拟连续性。配套：分配与计算重叠、机会性预分配、延迟回收。

## 关键数据结构与算法
- 2×N 虚拟 tensor（N=层数）；reqId 索引子区域
- page-group 粒度可配（64KB/128KB/256KB/2MB）
- 无 block table（虚拟连续即可索引）
- 分配与计算重叠：decode 每迭代至多 1 个新 page-group，背景线程提前映射

## 论文关键数据
- 端到端吞吐最高 1.23×；decode 吞吐最高 1.99×（vs vLLM，Yi-6B）
- prefill 吞吐 1.24×–1.36×；online 中位端到端延迟降 28%–42%
- PagedAttention 生态代价：vLLM kernel 比 FA2 慢至 2.8×；FA2 paged prefill 慢 37%

## 限制
- 仅 CUDA 后端；修改 NVIDIA 开源驱动（新增 64KB/128KB/256KB 页）；无 swap 到 CPU

## 训练/CUDA
无需训练；无需改 attention kernel（原生 FA2/FA3 直插）；改 NVIDIA 开源驱动

## llama.cpp 映射
- 不可直接移植（依赖 CUDA VMM + 驱动改动 + PyTorch 语义）
- 调度优化思想（分配重叠、延迟回收、eager 预分配）可移植到 llama-server 服务层
- **候选判断**: REJECT_UNSUPPORTED_HARDWARE（完整方案）；调度思想可借鉴
