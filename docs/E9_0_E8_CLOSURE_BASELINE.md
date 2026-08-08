# E9.0：E8 证据冻结基线（E8 Closure Baseline）

- 复核时间：2026-08-08
- 目的：冻结 E8 证据，独立核对报告与 raw 一致性，记录缺口

## 1. 仓库状态

| 项 | 值 |
|---|---|
| 根仓库 | `1d2d6f2`（benchmark-enhance，clean）|
| llama.cpp | `94e88846`（llama.cpp，clean）|
| E8 commits（根 9 + llama 3）| 全部存在（git cat-file -e 验证）|
| E8 raw 文件 | 5 个 perf JSON 存在（SHA256 见下）|
| 未跟踪 | `docs/20260808_E9_INSTRUCTION.md`（新指令）|

**E8 raw SHA256**：
- `e8_c1_perf_shared.json` `91aee4d2ba6e`
- `e8_c1_perf_partial.json` `e766951e0529`
- `e8_c1_perf_noshare.json` `ca8ff81605c0`
- `e8_c1_perf_capacity.json` `90ec0a1c830f`
- `e8_c1_perf_smoke_shared.json` `1b0115029c70`

## 2. build 对应 commit

- `build/bin/llama-server` mtime 2026-08-08 11:06:04；llama.cpp `94e88846` commit 时间 11:24:01
- 说明：94e88846 仅新增测试文件（不改代码）；二进制对应 `e8484452`（E8.3，tenant scope 代码）——**功能等价**。E8.5 性能实验使用该 build ✓

## 3. 报告 vs raw 一致性核对

| 核对项 | 结果 | 判定 |
|---|---|---|
| E8.4 4B "同一 build 重验" | **无严格 paired off/on raw**：E8.4 仅 on 模式验证（capability 日志 + 共享 0 + 输出 hash 与旧 off 数据对比）；off 数据来自 E7 前旧 commit（69711a2d6）| **缺口（CONTRADICTED）**：E9 需补同一 commit 下的严格 paired 4B raw |
| E8.5 `capacity_bytes=524288` | raw `e8_c1_perf_capacity.json` 实际为 **5242880**（8192 cells × 640 B/cell）；报告写 524288（×64B）为**笔误** | **CONTRADICTED**：E8_5/E8_FINAL 容量表需修正（524288→5242880）；物理分配结论不变（on/off 均 5242880）|
| shared_cells / used_cells / physical_sharing 语义 | 各报告一致：shared_cells=cell 级元数据关联数（非 COW）、physical_sharing=false、used_cells 表实际占用 | VERIFIED |
| 测试 47/47 | **实际 47 passed + 3 skipped**（LoRA 3 例网络不可达 skip）| 修正：报告措辞应为 "47 passed, 3 skipped"；LoRA 3 例**不计 passed** |
| LoRA 3 例计 passed | 否（3 skipped）| VERIFIED（未虚计）|

## 4. 其他核对

- 4B 模型 SHA256：`de8e96cd0d0c3584...`（与 E8.4 记录一致）；TinyLlama 缓存 479896ec（存在）
- LoRA/SWA/recurrent 资产：**无**（网络不可达，E8.4 已记录；E9.4 再次尝试）
- E8.5 的 30-rep 数据完整性：5 个 perf JSON 含全部 35 rep（warmup 5 + formal 30）✓

## 5. E8 遗留缺口清单（E9 关闭项）

1. **4B 严格 paired raw**（E8.4 缺口）→ E9.1/9.2 期间补（同一 build 下 off/on 各跑并保存 raw）
2. **capacity_bytes 笔误**（524288→5242880）→ E9_FINAL 修正说明（不改 E8.5 历史报告，E9 报告记录）
3. **prompt-cache LoRA identity**（E8.6 发现的上游继承缺陷）→ E9.1 修复
4. **E8 后 endurance 未重跑**（E8.5 引用 E6.5）→ E9.2 重跑（12+30 周期）
5. **C1 扫描扩展性未测** → E9.3
6. **LoRA/SWA/recurrent 运行时** → E9.4（无资产则 NOT_VERIFIED）
7. **LM-Infinite 正式发表状态**（E8.1 保持 UNVERIFIED）→ E9.7 再核验

## 6. 结论

- E8 证据主体有效（E8.5 数据完整、容量结论正确），但 2 处报告-raw 矛盾（capacity_bytes 笔误、4B paired 缺失）已在 §3 记录，E9 关闭
- 冻结状态不变：`PARTIAL_KV_CORRECTNESS_NO_OPTIMIZATION` / `C1_CONDITIONAL_PASS_RECOMPUTE_ONLY`
