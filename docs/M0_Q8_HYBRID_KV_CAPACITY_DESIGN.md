# M0-Q8：q8_0/混合 KV 容量研究设计（Hybrid KV Capacity Design）

> 状态：**DESIGN CONTRACT + IMPLEMENTED PROBE**——本文保留原始设计合同；实现与本轮探针结果见 `docs/M0_Q8_HYBRID_KV_CAPACITY_RESULT_20260821.md`。正式多轮 32K GPU paired matrix 仍未完成。
> 日期：2026-08-09 ｜ 前置：M0 正式收口（根仓库 `3ae6bb6`，benchmark PASS；4B hybrid 共享优化
> NOT_APPLICABLE/NO_GAIN，见 `docs/M0_FINAL_RESULT.md`）｜ llama.cpp HEAD `4a699aaad`（8570，零改动）。
> 路线定位：**独立路线**，不依赖 M0 的共享机制（`--kv-prefix-share` 在 4B hybrid 上被 capability gate
> 拒绝），而是对**上游既有参数组合 `--cache-type-k/v`** 在 hybrid 模型上的容量/质量/性能边界做
> 系统性量化——**不宣称任何自研算法**。

> **Implementation follow-up (2026-08-21):** The design-only baseline below remains the
> historical experiment contract. The follow-up implementation adds component metrics,
> explicit recurrent type/capacity configuration, and the exact-token `needle` workload.
> Qwen3.5-4B q8/F32 and Q4/F32 startup/smoke probes passed; F16/BF16 recurrent state is
> explicitly rejected by the current CUDA graph (F32-only), and reduced recurrent row
> capacity is explicitly rejected because seq_id currently addresses physical rows.

---

## 0. 路线定位与前置状态

| 项 | 值 |
|---|---|
| 前置路线 M0 | 已收口：`verdict=PASS`（run6/run7，120/120 reps OK）；两层结论 A（benchmark 基础设施）PASS / B（on 机制优化收益）NOT_APPLICABLE/NO_GAIN |
| 本路线问题 | q8_0/混合 KV 容量在 Qwen3.5-4B hybrid 上的**真实收益边界**：只降 attention KV，还是影响全部 hybrid state？ |
| 核心事实（源码级，§1/§2） | `--cache-type-k/v` **只作用于 attention 部分**；recurrent 默认/当前可运行类型为 F32、与 ctx 无关、随 parallel 线性 |
| 预期结论形态 | 「局部容量收益、非全 hybrid state 优化」（§5.1）——若混合组合被拒绝/无独立观测则 HOLD/NO_GO（§5.2） |
| 本阶段交付 | 设计合同 + 实现探针结果文档 + AGENTS.md 同步（正式 32K matrix 未完成） |

---

## 1. Qwen3.5-4B hybrid 内存分层（源码级）

### 1.1 模型架构分层：32 层 = 8 attention + 24 recurrent

- 架构 `LLM_ARCH_QWEN35`（`llama_arch_is_hybrid` 含 QWEN35/QWEN35MOE，`src/llama-arch.cpp:958-977`）。
- 层结构（`src/models/qwen35.cpp:21-33`）：无 `LLM_KV_ATTENTION_RECURRENT_LAYERS` 时 fallback
  `is_recr_impl[i] = (i+1) % full_attn_interval != 0`（interval=4）→ **32 层中 8 层 full attention
  （i=3,7,11,…,31）、24 层 recurrent（gated delta net / linear attention）**。
- 4B 档判定：`n_layer==32 && n_embd==2560`（`qwen35.cpp:31-34`）；GGUF `models/qwen3-5-4B-Q4_K_M.gguf`
  （2707514144 B，SHA256 前缀 `de8e96cd0d0c3584`）。
- memory 类型 `llama_memory_hybrid`（`src/llama-model.cpp:2256-2303`）：内部
  `mem_attn`（`llama_kv_cache`）+ `mem_recr`（`llama_memory_recurrent`）双成员（`src/llama-memory-hybrid.h:19-94`）。

### 1.2 attention KV：q8_0/f16 bytes、layers/cells

- 分配（`src/llama-kv-cache.cpp:231-232`）：每 attention 层
  `k = ggml_new_tensor_3d(ctx, type_k, n_embd_k_gqa, kv_size, n_stream)`（V 同理）——
  形状 `[n_embd_k_gqa, kv_size, n_stream]`；`kv_size = n_ctx_seq`（unified 时 = n_ctx，
  `src/llama-context.cpp:288-291`）。
- **per-cell bytes（8 attention 层合计）**：`n_kv_heads × head_dim × 2(K+V) × 每元素字节`。
  - q8_0（每 32 值 + 2B scale → 34/32 系数）：`8×8×128×2×34/32 = 17408 B/cell`；
  - f16：`8×8×128×2×2 = 32768 B/cell`。
  - ctx 4096 → **q8_0 = 68 MiB / f16 = 128 MiB**（unified 池，`capacity_cells = 4096`，与 parallel 无关）——
    与 M0 探针实测精确闭合（§3.1）。
- **unified 语义**：`n_stream = kv_unified ? 1 : n_seq_max`（`src/llama-kv-cache.cpp:82`）→
  unified 下 KV cells 总数 = kv_size（**不分摊到各 slot**），capacity 由 ctx 决定、与 parallel 无关。
  非 unified 下 `n_ctx_seq = n_ctx / n_seq_max`（平分）。**本路线沿用 `--kv-unified`**（与 M0/E13.1 一致）。

### 1.3 recurrent state：RS buffer、F32 固定、parallel 线性

- 分配（`src/llama-memory-recurrent.cpp:99-101`）：每 recurrent 层
  `r = ggml_new_tensor_2d(ctx, type_r, n_embd_r, n_rows)`、`s = ggml_new_tensor_2d(ctx, type_s, n_embd_s, n_rows)`，
  `n_rows = mem_size × (1 + n_rs_seq)`；`mem_size = max(1, n_seq_max)`——**每 slot 一格，与 n_ctx 无关**。
- 维度（`src/llama-hparams.cpp:183-230`，KDA 分支，4B 闭合值）：
  - `n_embd_r = 3×(ssm_d_conv−1)×n_head×n_embd_head_kda = 3×2×32×128 = 24576`；
  - `n_embd_s = n_embd_head_kda² × n_head = 128×128×32 = 524288`。
- **每 slot RS = 24 层 × (24576+524288) × 4 B = 52,690,944 B = 50.25 MiB**——与探针实测
  （p4=201.00、p10=502.50 MiB）精确闭合，**随 parallel 线性增长**（`n_seq_max = params.n_parallel`，
  `common/common.cpp:1639`）。
- 日志观测点：`RS buffer size = X MiB`（`llama-memory-recurrent.cpp:115`）+ `R/S (f32)` 分解行
  （`:123-126`）；`layers` 打印为**总层数**（实际分配仅 recurrent 层，filter skip `:76-82`）。

### 1.4 /metrics/kv 观测边界

- 原始设计时 **`get_kv_stats` 只委托 attention 部分**；本轮已扩展为 additive 分组件快照：attention
  保留既有字段，hybrid/recurrent 增加 recurrent R/S bytes、rows、`n_seq_max`、`n_rs_slots`；
  `seq_cell_stats` 仍只统计 attention cell。
- `llama_kv_cache::get_kv_stats`（`src/llama-kv-cache.cpp:734-800`）：`capacity_bytes = total_size()`（**预分配
  固定容量**，不含权重）；`used_bytes = used_cells × kv_bytes / capacity_cells`（仅无 SWA 时有效）；
  `capacity_cells/used_cells/shared_cells/active_sequences`；`physical_sharing = false` 恒 false（无 COW）。
- HTTP 端：`GET /metrics/kv`（`tools/server/server-context.cpp:5328-5364`，推理线程执行、与 KV 变更串行化；
  组装点 `:3106-3135`）。
- **recurrent metrics 边界**：本轮新增只读分组件 capacity snapshot；可拆分预分配 R/S bytes
  与 row 配置，但不提供 recurrent token/state 使用率。hybrid 的 `memory_breakdown` context 列
  仍为 attn+recr 合并。

### 1.5 为什么 q8_0 不改变 recurrent state（源码链路）

```
common/arg.cpp:2369-2391   -ctk/-ctv 解析 → params.cache_type_k/v
common/common.cpp:1667-1668 → cparams.type_k/v
llama-context.cpp:383-393   llama_memory_params = { type_k, type_v, type_r, type_s }
llama-model.cpp:2286-2295   llama_memory_hybrid 构造：
                              attn_type_k/v ← params.type_k/v（受 -ctk/-ctv 影响）
                              recurrent_type_k/v ← GGML_TYPE_F32（硬编码）
```

→ **`--cache-type-k/v` 与 recurrent R/S 完全无关**；RS 恒 F32（每值 4 B）。因此任何 cache-type
组合都只改变 attention KV 的 68/128 MiB 档，**RS 50.25 MiB/slot 不变**——这是本路线结论的源码根基。

---

## 2. `--cache-type-k/v` 真实生效范围与组合允许性（源码核对）

### 2.1 CLI 解析与允许类型

- 参数：`-ctk/--cache-type-k`、`-ctv/--cache-type-v`（`common/arg.cpp:2369-2391`；
  官方文档 `tools/server/README.md:71-72`、`docs/multi-gpu.md:44-45`）。
- 允许值（`kv_cache_types`，`common/arg.cpp:302-313`，单一权威表）：
  `f32, f16, bf16, q8_0, q4_0, q4_1, iq4_nl, q5_0, q5_1`；默认 `f16`。
- 本路线只使用 **`q8_0` 与 `f16`**（其余为次级观察，不进入主矩阵）。

### 2.2 生效范围：只作用于 attention 部分

- 源码链路见 §1.5：`llama_memory_hybrid` 构造时 `attn_type_k/v ← params.type_k/v`、
- `recurrent_type_r/s` 现在来自显式 context 参数，默认 F32；当前 CUDA recurrent graph 对 F16/BF16
  在 context 创建阶段拒绝（不隐式回退）。
- **对 hybrid 模型，"KV cache type" 语义 = attention KV cache type**；recurrent state 另有
  `--cache-type-r/--cache-type-s` CLI 开关，但当前可运行 profile 仍限 F32
  （E9.4 结论 4「`-ctk/-ctv` 在 hybrid 上不可用」为历史笔误——实为单横线短参数名 `-ctk` 误写为
  `--ctk` 导致解析失败，长参数 `--cache-type-k/v` 实测可用且已修正（E9.4 第 18 行补测）；本设计
  以 `--cache-type-k/v` 为准）。

### 2.3 混合 K/V 组合允许性

| 检查 | 源码 | Qwen3.5-4B 结论 |
|---|---|---|
| K≠V 限制 | `src/llama-context.cpp:3560-3563`：仅 `is_mla() || LLM_ARCH_DEEPSEEK4` 拒绝 K≠V | **允许混合**（qwen35 非 MLA，`src/llama-hparams.cpp:244-248`） |
| quantized V 需 flash-attn | `src/llama-context.cpp:3565-3569`：V 量化时 FA 非 DISABLED（AUTO 自动启用） | q8_0 V 自动开 FA（M0/E10 已实证可行） |
| quantized K/V block 整除 | `src/llama-context.cpp:3576-3596`：`n_embd_head_k/v % ggml_blck_size == 0` | q8_0 block=32、head_dim=128 → 整除 ✓ |

- 结论：**K=q8_0 + V=f16（或反向）等混合组合在源码层面无拒绝路径**（预期可启动），但**本路线不把
  混合组合作为主矩阵**（主矩阵为 q8_0/q8_0 vs f16/f16 对称档）；混合组合仅作为**探针观察**验证
  「容量 = K 档 + V 档 可加性」，且**无独立观测 → HOLD/NO_GO，不推断**（§5.2）。
- **上游既有参数组合声明**：`--cache-type-k/v q8_0` 是 llama.cpp 上游参数（E10.3/E13.1 已标注
  `validated deployment profile`，非新代码）；本路线**只做量化验收与收益边界刻画，不宣称自研算法**。

### 2.4 memory breakdown 输出

- 唯一输出点：退出时 `common_memory_breakdown_print`（`common/fit.cpp:817-940`，调用点
  `server.cpp:531`）：`total = model + (context = KV + RS 合并) + compute`。
- **运行期不可用**；本路线容量对账依赖：启动日志（`RS buffer size` / `R/S (f32)` /
  `llama_kv_cache` 分配行）+ `/metrics/kv`（attention 权威）+ nvidia-smi 差分（总量 sanity）。

---

## 3. 已有实证复核（引用真实运行，不重复运行）

### 3.1 M0 探针（`docs/M0_BRANCH_MEMORY_BASELINE_DESIGN.md` §1.3；2026-08-09，RTX 4060 Laptop 8GB）

| parallel | ctk/ctv | KV buffer（attention，unified ctx4096） | RS buffer（recurrent） | GPU used（nvidia-smi 绝对值） |
|---|---|---|---|---|
| 2 | q8_0 | 68.00 MiB | 100.50 MiB | 2974 MiB |
| 4 | q8_0 | 68.00 MiB | 201.00 MiB | 3074 MiB |
| 6 | q8_0 | 68.00 MiB | 301.50 MiB | 3186 MiB |
| 8 | q8_0 | 68.00 MiB | 402.00 MiB | 3290 MiB |
| 10 | q8_0 | 68.00 MiB | 502.50 MiB | 3402 MiB |
| 4 | f16 | **128.00 MiB** | 201.00 MiB | 3132 MiB |

- `/metrics/kv`（p4/q8_0/unified/ctx4096，无请求）：`capacity_cells=4096, capacity_bytes=71303168（=68 MiB）,
  used_cells=0, active_sequences=0, shared_cells=0, physical_sharing=false`。
- 权重：`CUDA0 model buffer size = 2571.63 MiB`（+ `CPU_Mapped 497.31 MiB` mmap 页面）；
  GPU 口径 `memory.total=8188`（空闲 used≈40）→ 模型+KV+RS 增量 ≈ used − 40。
- 证据文件：`benchmark/baseline/qwen35-4b_gpu_m0_calibration_20260809.json`（run1，v55 格式）、
  `qwen35-4b_gpu_m0_calibration_run2/run3_20260809.json`（v56 格式，字段与 run1 核心一致）、
  `qwen35-4b_gpu_m0_calibration_summary_20260809.json`、`qwen35-4b_gpu_m0_probe_evidence_20260809.json`。

### 3.2 E9.4 / E10.3 / E13.1 验收（q8_0 无损与容量）

- E9.4（`docs/E9_4_QWEN35_MEMORY_BREAKDOWN.md`）：per-cell 32768→17408 B（**-46.9%**）；F16 默认；
  recurrent state 固定不随 prompt 增长。
- E10.3（`docs/E10_3_QWEN35_Q8_KV_VALIDATION.md`）：**20 reps × 6 workload 输出与 F16 完全一致（0 差异）**；
  KV capacity -47%（ctx2048/p2：67,108,864 → 35,651,584 B）；GPU 总显存 -30 MB；decode +1.5~3.3%；
  状态 `QWEN35_Q8_KV_STATUS: PASS_DEPLOYABLE`。raw：`benchmark/results/kv_optimization/raw/e10_q8_validation.json`（E9/E10 文档中简写 `raw/...` 的真实路径）。
- E13.1（`docs/E13_1_QWEN35_Q8_DEPLOYMENT_PROFILE.md`）：validated profile 固化
  （`--cache-type-k/v q8_0` + `--kv-unified` + ctx4096/p4）；GPU peak 约 -58 MB（3032→2974，5 次采样稳定）；
  decode 最大精确退化 **2.10%**；config `benchmark/configs/qwen35_4b_q8_validated.yaml`（+ E14.1 生产版
  `qwen35_4b_q8_production.yaml`）。

### 3.3 M0 run6/run7 正式矩阵（`docs/M0_FINAL_RESULT.md`，两轮一致）

- 环境：同探针；server b8570-4a699aaad；ctx4096；parallel=fanout+2（4/6/10）；24 units × 5 reps；
  cache profiles q8_0/q8_0 与 f16/f16 各 6 groups。
- 组级 baseline `gpu_used_mb`：**3026–3414（mean 3202）**；rep `peak_gpu_mb` max **3424 MiB**、
  `peak_rss_mb` max 2767/2866 MiB；rep `kv peak used_cells` max 3192（attention 池 4096 cells 内）。
- 证据：`benchmark/baseline/qwen35-4b_gpu_m0_formal_run{6,7}_20260809.json` + `_evidence_` + `_logevidence_`
  + `qwen35-4b_gpu_m0_formal_run6v7_paired_20260809.json`。

### 3.4 三态区分（本路线观测语义，沿用 M0 口径）

| 态 | 含义 | 观测途径 | 可被 cache-type 改变？ |
|---|---|---|---|
| **capacity（固定预分配）** | KV buffer 总量，由 ctx × per-cell 决定 | `/metrics/kv capacity_bytes`、启动日志 KV 分配行 | **是**（q8_0 68 vs f16 128 MiB @ctx4096） |
| **used cells（运行期占用）** | attention cells 中实际被序列占用的数量 | `/metrics/kv used_cells`（erase 后可归零验证） | 否（容量不变，占用与负载相关） |
| **recurrent state** | RS buffer 总量 + 运行期状态 | **总量：启动日志 + `/metrics/kv` recurrent snapshot；运行期使用率：无接口** | **否**（当前可运行 profile 恒 F32，50.25 MiB/slot） |

- GPU/RSS 总量差分只能看到**三者之和**（+权重+compute）；本路线的容量归因必须按 §3.1 公式
  对账，**禁止把 GPU 总量差直接称为"KV 容量收益"**。
- **recurrent 运行期观测边界**：本轮已新增只读 capacity snapshot；当前字段描述预分配 R/S
  容量与 row 配置，不等同于 recurrent 运行期 token/state 使用率。仍不得用 GPU 总量差分伪造
  recurrent 使用率。

---

## 4. 下一实验设计与门禁（正式 32K matrix 尚未执行）

### 4.1 实验目标与判定问题

1. **容量边界**：q8_0/q8_0 vs f16/f16 在 hybrid 上的 attention KV 容量差是否为公式值
   （128−68 = **60 MiB @ctx4096**，-46.9%）？RS 是否完全不随 cache-type 变化（50.25 MiB/slot 恒等）？
2. **质量边界（拆分判定）**：① 固定短请求（E10.3 workload）q8_0 与 f16 token 级 hash 是否 100%
   一致（G-Q8-1a 硬条件，预期 E10.3 已实证无损）；② agent fanout 负载（决策+分支+工具轮、
   生成更长）若出现 token 级差异，量化退化率是否 ≤2.10%（G-Q8-1b 有损描述）——
   **G-Q8-1b 不反向把 G-Q8-1a FAIL 变 PASS**。
3. **组合允许性（探针）**：混合 K/V（如 K=q8_0+V=f16）能否启动、容量是否 = 两档可加
   （§2.3 源码预期允许，需启动级实测确认）。

### 4.2 固定环境契约（同 M0/E13.1）

```bash
llama.cpp/build-cuda/bin/llama-server -m models/qwen3-5-4B-Q4_K_M.gguf \
  -ngl 99 --ctx-size 4096 --parallel N --kv-unified \
  --cache-type-k {q8_0|f16} --cache-type-v {q8_0|f16} \
  --flash-attn auto --no-speculative \
  --temp 0 --seed 42 --metrics -lv 5 \
  --log-file <run_tmp>/<tag>/server.log --slot-save-path <run_tmp>/<tag>/slots
```

- **每生命周期专属目录**：`<run_tmp>/<tag>/` 为本 server 生命周期唯一子目录（log 与 slots 均
  在其中，多 server 不共享）；`--slot-save-path` 是 `POST /slots erase` 的前置要求
  （`server-context.cpp:5448`，M0 同款），与 §4.4 erase/after_erase 合同一致。
- **`--flash-attn auto`**：统一 AUTO、**不显式关闭**（quantized V 需要 FA，
  `llama-context.cpp:3565-3569`；AUTO 在 q8_0 V 时自动启用）。
- **`--no-speculative`**：不启用 speculative decode（避免 draft 模型 KV type 干扰观测）。
- **`n_rs_seq=0`**：不启用 rollback snapshot（默认值，显式声明；G-Q8-4 公式前置条件，§4.5）。

| 固定项 | 值 | 约束来源 |
|---|---|---|
| binary | build-cuda，**同一次构建、同一 commit**（先重建到 4a699aaad 保证版本号一致） | M0 §9.5 |
| model | `models/qwen3-5-4B-Q4_K_M.gguf`（SHA256 前缀 de8e96cd） | E13.1 |
| ctx / ngl | 4096 / 99 | M0、E13.1 同档 |
| kv-unified | `--kv-unified` | 容量语义 = 全 ctx 共享池（§1.2） |
| generation | temp=0 / seed=42 | E13.1 验证范围 |
| parallel | **p4、p10**（必要时补 p8） | M0 探针已有 p2/p4/p6/p8/p10 空载档 |
| 环境 | 同一台 RTX 4060 Laptop 8GB，串行执行、每 server 生命周期独立 | M0 run6/7 先例 |
| flash-attn | `--flash-attn auto`（统一 AUTO，不显式关闭） | q8_0 V 需 FA（llama-context.cpp:3565-3569） |
| speculative / n_rs_seq | `--no-speculative`；`n_rs_seq=0`（不启用 rollback snapshot） | 保持观测纯净；G-Q8-4 公式前置条件 |

### 4.3 矩阵设计

| 维度 | 档位 | 说明 |
|---|---|---|
| cache profile | **q8_0/q8_0**、**f16/f16**（主）；混合 K/V（探针，§4.1-3） | 对称主矩阵 + 混合探针 |
| parallel | **4、10**（必要时 8） | p10 覆盖高并发容量线性；p4 与 E13.1 对齐 |
| 负载 | **①空载**（启动即采样，零推理）；**②固定短请求**（同输入 N 次，如 short_qa 6 reps）；**③agent fanout**（复用 M0 fanout 负载：决策 b1 + 分支并发 + 工具轮） | 三态观测（§3.4） |
| 重复 | 每 (profile × parallel × 负载) **≥2 次独立 run**（复现性，同 M0 run6/run7） | G-Q8-6 跨轮判定 |

单元规模（DESIGN 估算，实现时可调整）：2 profile × 2 parallel × 3 负载 × 2 轮 = **24 个 server
生命周期**（不含混合探针 2 个），单轮耗时估计 ≤ M0 单轮（~11 min 含 15 生命周期）量级。

### 4.4 观测采集合同

| 通道 | 采集内容 | 用途 |
|---|---|---|
| 启动日志（-lv 5） | `RS buffer size`、`R/S (f32)`、`llama_kv_cache` 分配行、`memory breakdown`（退出） | 容量归因（G-Q8-3/G-Q8-4） |
| `/metrics/kv`（KVProbe） | 空载 baseline 快照 + 每请求前/后 + erase 后 after_erase 采样 | attention capacity/used/回收 |
| nvidia-smi（sampler 周期采样） | GPU `memory.used` 峰值（多 PID） | 总量 sanity（G-Q8-5） |
| 进程 RSS（sampler） | peak_rss_mb | 宿主内存观测 |
| timings（driver） | latency_ms / ttft_ms（prompt_ms 代理） | 延迟对比（G-Q8-2） |
| 输出 hash | 同输入 token 级 sha256（temp=0/seed=42） | parity（G-Q8-1a） |

- 全部采样与 M0 同一 runner 基础设施语义（`M0FanoutRunner` 生命周期 / KVProbe / sampler / gate 骨架）
  或等价独立 runner；**本阶段不实现**，仅定义合同。
- **不采集**：无运行期 recurrent 接口（§3.4 三态边界）——不得伪造/推断 recurrent 运行期观测。

### 4.5 门禁与阈值（Q8 系列，参照 M0 gates 命名）

| 门禁 | 定义 | 阈值 |
|---|---|---|
| G-Q8-1a parity（硬条件） | 同输入（同 seed/temp）q8_0 与 f16 输出 sha256 一致 | **100%**（正式 reps；至少覆盖固定短请求 workload）；任一不一致 → **G-Q8-1a FAIL**，记录差异 token |
| G-Q8-1b 退化率（有损描述） | q8_0 vs f16 输出差异的量化退化率（token 级差异比例/decode 退化） | ≤2.10%（E13.1 边界）；**仅描述有损程度，不反向把 G-Q8-1a FAIL 变 PASS**——1a FAIL 即「该负载无损 parity 不成立」 |
| G-Q8-2 延迟 | q8_0 vs f16 严格 paired（同 profile 内 off/on 等价物——同负载同 parallel 跨 profile 配对） | latency/ttft 中位数偏差 **≤5%**（参考 E10.3 ≤3%、E13.1 2.10%；给并发余量） |
| G-Q8-3 KV 容量 | `/metrics/kv capacity_bytes` 与公式对账：q8 68 MiB / f16 128 MiB @ctx4096，per-cell 17408/32768 B | 误差 **≤5%**（M0 G-M0-4 同口径） |
| G-Q8-4 RS 不变 | 启动日志 `RS buffer size` 与公式对账，且 **q8_0 与 f16 档完全相等**。**前置条件（写死）**：`n_rs_seq=0`（`n_rows = mem_size = n_seq_max = parallel`）、recurrent 层数 24、R/S 恒 F32 → 公式 `RS = 24 × (24576+524288) × 4 × parallel = 50.25 × parallel MiB`；任何前置条件被违反（如 n_rs_seq>0 或层数变化）→ 公式不适用，NOT_APPLICABLE | 误差 ≤5%；跨 profile 相等是硬条件（否则 = 源码反例，升级调查） |
| G-Q8-5 GPU 总量 sanity | nvidia-smi 峰值差分 = 权重 + KV(档差 60 MiB) + RS(parallel 线性) + compute，容量差方向正确 | ±10%（M0 同口径）；**只看方向与量级，不作容量收益的独立证明** |
| G-Q8-6 复现性 | 同配置两次独立 run：capacity/RS 日志一致；latency/ttft 中位数偏差 ≤10%；parity 一致 | M0 G-M0-6 同定义 |

### 4.6 失败归因（沿用 M0 v50-v68 归因体系，不裸异常）

| 失败形态 | 归因 | 结果路径 |
|---|---|---|
| server 启动失败（bin 缺失/参数非法/混合组合被拒） | preflight 级：`PREFLIGHT_INFRA`；混合组合被拒 → 记录拒绝日志 | 合法落盘 + 该组合标注 **REJECTED**（→ §5.2 HOLD/NO_GO 触发条件之一） |
| 运行中崩溃 | `ServerCrash` → 该组/单元 ERROR + `FORMAL_INCOMPLETE`（保留已完成单元） | 合法落盘（exit 0），不伪造观测 |
| 请求 transient 失败 | 重试 wrapper（≤3 次，退避 0.25/0.5）后仍失败 → rep ERROR + 归因分类 | M0 v66-v68 语义复用 |
| erase/观测失败 | `EraseFailure`（erase_failed/after_erase_missing 等） | 组 ERROR + 诊断，不静默 PASS |
| 内部 bug（AssertionError/KeyError/TypeError 等） | `INTERNAL_RUNNER_ERROR` | exit 70，不吞 |
| parity 不一致 | **G-Q8-1a FAIL**（不因 G-Q8-1b ≤2.10% 反向恢复 PASS） | 记录差异证据；判定依据 §5.1 结论路径 |

### 4.7 重复运行与退出/归档合同

- **重复运行**：每 (profile × parallel × 负载) ≥2 次独立 run（不同 out/tmp 标识），顺序执行、
  前一轮完全 cleanup（server 无泄漏、GPU 回落基线）后才启动下一轮（M0 run6→run7 先例）。
- **退出码**：复用 M0 合同——0（合法落盘，含 INVALID/INCOMPLETE 判定）/ 64（usage）/ 65（schema）/
  70（内部 bug）/ 74（IO 失败）；结果结构复用 canonical 22 键 meta 表（`m0_schema.py`），
  **本路线新增字段不得破坏既有 validator**（设计期确认：cache profile 字段已存在于 22 键内）。
- **归档**：正式结果 → `benchmark/baseline/`（可读命名 `qwen35-4b_gpu_q8kv_cap_<run>_<ts>.json`
  系列 + evidence + logevidence key-lines）；`results/` 只留临时输出（gitignore）；-lv5 完整日志
  留专属 tmp 子目录供排障（含每生命周期的 `--slot-save-path` 目录，属同一 `<run_tmp>/<tag>/`
  专属子目录），**baseline 只归档 key lines**。
- **结论文档**：运行后产出 `docs/M0_Q8_HYBRID_KV_CAPACITY_RESULT.md`（本设计文档的对照物），
  引用本设计 §4.5 门禁逐项判定。

---

## 5. 成功/无效结论路径

### 5.1 预期结论形态（以实测为准，不预设）

- **成功路径（局部容量收益）**：G-Q8-1a PASS（短请求 100%）+ G-Q8-1b ≤2.10%（若有差异，
  有损描述）+ G-Q8-2/3/4/5/6 全 PASS + 源码链路（§1.5）确认 →
  结论为 **「q8_0 只降低 attention KV 容量（68 vs 128 MiB @ctx4096，-46.9%），RS 固定
  50.25 MiB/slot → 局部容量收益、非全 hybrid state 优化」**。收益边界量化：
  `KV 容量差 / (权重 + KV + RS×parallel + compute)` 占比（@ctx4096 容量差 = 60 MiB）。
  **口径区分**：60 MiB 是 KV **capacity 差**（预分配公式值）；E13.1 实测 -58 MB 是
  nvidia-smi **总 GPU 占用差**（含权重不变 + KV 差 + 缓冲对齐），两者同量级但**不同口径**，
  不可直接等同（-58 MB < 60 MiB 可能因 ctx 未用满/缓冲对齐，属正常）。
- **parity 退化情形（有损分级，不反向恢复）**：**G-Q8-1a 是硬条件**——固定短请求
  （E10.3 workload）上 q8_0 与 f16 必须 token 级 100% 一致（E10.3 已实证 20×6 无损）；
  agent fanout 负载（更长生成/多轮工具）允许出现 token 级差异，此时 **G-Q8-1a FAIL
  （该负载无损 parity 不成立）**，由 **G-Q8-1b 量化退化率 ≤2.10%** 作有损描述——
  最终结论分级：「短请求无损 + agent 负载有损（退化率 X%）→ 容量收益成立、质量有损」，
  引用 E13.1 边界（仅 ≤16 tokens 验证）与 E10.3 无损范围的差异；
  **G-Q8-1b 通过不改变 G-Q8-1a FAIL 的结论；不把有损称为无损**（E15.3 先例语义）。

### 5.2 HOLD / NO_GO 条件（不推断）

| 条件 | 判定 | 处置 |
|---|---|---|
| 混合 K/V 组合**被拒绝**（源码预期允许，若实测拒绝则记录拒绝路径） | HOLD（组合探针无效） | 主矩阵（对称 q8_0/f16）不受影响；混合组合结论 **不推断** |
| 混合组合容量**无独立观测**（如日志无 KV 分配行可分档） | HOLD | 不宣称"混合容量 = K 档 + V 档"可加性 |
| **recurrent 观测缺口**导致 RS 不变无法用日志对账（日志缺失/格式变化） | HOLD/NO_GO | G-Q8-4 无观测 → NOT_APPLICABLE（M0 G-M0-3b 先例），**不静默 PASS** |
| 主矩阵两档 capacity 差与公式（60 MiB）不符且无法归因 | NO_GO（容量结论） | 记录证据，升级调查源码 |
| 运行环境不可复现（GPU 不可用/驱动问题/显存采样失败） | NO_GO（本阶段） | 暂停，等待用户处理（pynvml DriverNotLoaded 先例） |

### 5.3 禁止事项

- **不得**把 benchmark/门禁 PASS 称为「hybrid 内存优化 PASS」（M0 结论 B 教训——门禁 PASS 只证明
  观测与归因正确，收益判定必须落到容量/质量实测）。
- **不得**把 `--cache-type-k/v`（上游参数组合）称为自研算法或新实现（§2.4 声明）。
- **不得**伪造/推断 recurrent 运行期观测（无接口即不可观测，§3.4）。
- **不得**把 GPU 总量差分（含权重/compute）直接当作 KV 容量收益（必须公式对账，§3.4/§4.5 G-Q8-5）。

---

## 6. 风险与未决问题

| # | 风险/未决 | 缓解/处置 |
|---|---|---|
| 1 | Laptop GPU 抖动（E3 先例） | warmup + 中位数 + 严格 paired + 跨轮复现（G-Q8-6） |
| 2 | p10 高并发下 latency 噪音（M0 f8 例外先例） | n=10 档标注小样本噪音，不单独下结论 |
| 3 | recurrent 运行期观测缺口（M0 §9.1 未决保留） | 本路线用「启动日志 + 公式」对账；扩展 `llama_kv_stats` 列为**范围外**后续候选 |
| 4 | q8_0 质量边界（E13.1 仅 ≤16 tokens/≤2.10%） | G-Q8-1a（短请求 100%）为硬条件 + G-Q8-1b（退化率 ≤2.10%）作有损描述；超出边界如实降级，不反向恢复 |
| 5 | 混合组合实测行为未知（源码允许 ≠ 运行期保证） | 探针级启动验证；被拒/无观测 → HOLD（§5.2） |
| 6 | 8GB 卡 ctx4096 上限（8192 未验证） | 保持 4096；8192 列 NOT_VALIDATED（E13.1 同口径） |

---

## 7. 本阶段交付与文件清单

| 项 | 状态 |
|---|---|
| `docs/M0_Q8_HYBRID_KV_CAPACITY_DESIGN.md`（本文档） | ✅ 设计合同，已由结果文档补充实现状态 |
| `docs/M0_Q8_HYBRID_KV_CAPACITY_RESULT_20260821.md` | ✅ 本轮实现、指标、探针证据 |
| 实现代码 / 测试代码 | ✅ 已创建并通过构建/回归 |
| 正式 32K GPU paired matrix | **未完成**（仍按 §4 门禁执行） |
| git | 根仓库单 commit；**不 push**；llama.cpp 零改动（双仓库 clean） |

---

## 8. 参考文献（真实文件/commit/源码行号）

### 提交与归档
- M0 收口：根仓库 `3ae6bb6`（docs: M0 双轮结果复审口径修订）；`0cf715a`（run6/run7 verdict=PASS）
- llama.cpp HEAD：`4a699aaad`（8570；E15.1 测试 + E13.2 capability guard；**零源码改动**）
- M0 设计：`docs/M0_BRANCH_MEMORY_BASELINE_DESIGN.md`（§1.2/§1.3/§9/§12）
- M0 结果：`docs/M0_FINAL_RESULT.md`
- 归档：`benchmark/baseline/qwen35-4b_gpu_m0_{calibration,probe_evidence,formal_run6/7*,paired}*.json`
- E 系列：`docs/E9_4_QWEN35_MEMORY_BREAKDOWN.md`、`docs/E10_3_QWEN35_Q8_KV_VALIDATION.md`、
  `docs/E13_1_QWEN35_Q8_DEPLOYMENT_PROFILE.md`、`docs/E14_*`、config
  `benchmark/configs/qwen35_4b_q8_validated.yaml` / `qwen35_4b_q8_production.yaml`

### llama.cpp 源码（只读核对，commit 4a699aaad）
- `src/llama-model.cpp:2256-2303`（hybrid memory 构造；recurrent_type_r/s 显式传入）
- `src/llama-context.cpp:260-305`（n_ctx_seq/unified）、`383-393`（llama_memory_params 含 type_r/s）、
  `3560-3596`（K≠V 限制 / FA / block 检查）、`3235-3258`（memory_breakdown context 合并）、`4206-4208`
- `src/llama-kv-cache.cpp:71-82`（n_stream）、`140-143`（v_cells）、`231-232`（K/V tensor）、
  `734-800`（get_kv_stats）、`1910-1913`（total_size）
- `src/llama-memory-recurrent.cpp:99-126`（RS tensor 分配/日志）、`709-728`（size_r/s_bytes）
- `src/llama-memory-hybrid.cpp:182-205`（memory_breakdown 合并 / 分组件 get_kv_stats）
- `src/llama-hparams.cpp:183-230`（n_embd_r/s）、`244-248`（is_mla）
- `src/models/qwen35.cpp:21-33`（recurrent 层 fallback interval=4）
- `common/arg.cpp:302-313`（kv_cache_types）、`2369-2391`（-ctk/-ctv）、
  `common/common.cpp:1639`（n_seq_max=parallel）、`1667-1668`
- `common/fit.cpp:817-940`（common_memory_breakdown_print；`server.cpp:531` 调用）
- `tools/server/server-context.cpp:3106-3135`（kv_stats 组装）、`5328-5364`（/metrics/kv handler）
- 官方文档：`tools/server/README.md:71-72`、`docs/multi-gpu.md:44-45`（-ctk/-ctv 参数表）
