# E9.4：Qwen3.5-4B 内存组成和瓶颈测量（Memory Breakdown）

- 复核时间：2026-08-08
- 环境：RTX 4060 Laptop 8GB；Qwen3.5-4B-Q4_K_M.gguf（2707514144 B = 2.7GB）；CUDA build；`--kv-unified --cache-ram 0`
- 实际指标（非结构公式）：/metrics/kv + nvidia-smi + timings
- raw：`raw/e9_mem_breakdown.json`、`raw/e9_mem_length_scan.json`

## 1. 各组件 bytes（实测）

| 组件 | 值 | 测量方法 |
|---|---|---|
| 模型权重文件 | 2,707,514,144 B（2.7GB）| filesize |
| 模型加载后 GPU（p1/c1024）| 2882 MB | nvidia-smi |
| attention KV capacity（ctx1024/2048/4096）| 33.5 / 67.1 / 134.2 MB | /metrics/kv capacity_bytes（每 cell 32768 B × ctx cells）|
| attention KV 实际使用 | **32768 B/token（线性）** | 长度扫描：186 tok→6.3MB、842→27.8MB、1662→54.7MB |
| recurrent state | **固定（不随 prompt 增长）**；不在 /metrics/kv 范围 | 长度扫描无 recurrent 增量 + 结构确认（E8.7）|
| graph/compute buffer（parallel 增量）| p1→p4（c4096）GPU 2982→3132 MB（+150MB）| nvidia-smi |
| KV type 支持 | **F16 默认；q8_0 实测可用**（`--cache-type-k/v q8_0`：per-cell 32768→**17408 B（-46.9%）**，输出 hash 与 F16 完全一致 39a0f0fc...）；q4_0 可加载 | 实测（E9.4 补测：E9.4 初版误报 -ctk 无效为参数名笔误 `--ctk`→`-ctk`，已修正）|

## 2. 容量边界（每 slot）

| ctx | parallel | 每 slot tokens | KV capacity | 实测 |
|---|---|---|---|---|
| 2048 | 1 | 2048 | 67.1MB | 1662 tokens OK；2274 tokens → **400**（单请求 ctx 上限）|
| 2048 | 4 | 512（池平分）| 67.1MB 共享 | E4 已测（E2 已知：--parallel 平分总 ctx）|
| 1024/4096 | 1 | 1024/4096 | 33.5/134.2MB | OK |

## 3. Throughput（Q4_K_M，消费级 GPU）

| prompt_n | prefill tokens/s | decode tokens/s |
|---|---|---|
| 186 | 1742 | 77.1 |
| 842 | 2687 | 77.5 |
| 1662 | 2871 | 76.5 |

- decode ~76-77 tokens/s（瓶颈：4B Q4 消费级 GPU 单流）；prefill 1740-2870 tps（小 batch 下 prefill 吞吐随长度增长）

## 4. 瓶颈结论

1. **decode 吞吐是主瓶颈**（~77 tps），非 KV 容量（KV 64MB 相对模型 2.9GB 小）
2. **attention KV = 32KB/token**：长 prompt/多 session 时 KV 增长快（1662 tokens = 54.7MB）；这是 C1 类优化的价值区间（共享前缀避免重复 KV）
3. **recurrent state 固定**：不随 prompt 增长 → 对长 prompt 的 KV 压力无贡献（但也意味着 C1 无法压缩它——E8.7 结论一致）
4. **`-ctk/-ctv` 在 hybrid 上不可用** → H1 候选（KV type/precision 优化）在主模型上**无现有开关可测**（需要新实现或确认 hybrid 的量化路径）
5. 单请求 ctx 上限 = slot ctx（2048/p1）；超限 400（不是 OOM——池未满时也拒绝单请求超 slot ctx）

## 5. 对候选选择的影响

- H1（attention KV type 优化）：**实测可行**——`--cache-type-k/v q8_0` 在 4B 上 per-cell 32768→17408 B（-46.9%），输出 hash 与 F16 完全一致（39a0f0fc...）→ 同容量 KV 翻倍潜力；**但为上游既有开关**（非新代码，E9.6 作 comparator 不冒充创新）
- H2（prefix checkpoint restore）：recurrent 固定 + attention 32KB/token → checkpoint 大小 ≈ attention 前缀 bytes（大）；恢复收益 = 避免重复 prefill（decode 瓶颈下 prefill 占时比例低）
- H3（recurrent 精度）：recurrent 固定小 → 收益有限
- H4（clone/fork）：copy 成本 = attention 前缀 bytes（数据复制）vs 重复 prefill（计算）——prefill 280ms/678tokens vs copy 54MB 拷贝（VRAM 内 ~ms 级）→ copy 可能更优（E9.7 prototype 实测）
