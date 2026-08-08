# E9.2：C1 E9.1 修复后 endurance 与生命周期复验（Post-Hardening Endurance）

- 复核时间：2026-08-08
- 代码：llama.cpp `d6d679e7`（E9.1 prompt cache identity 修复后）同一 build
- 模型：TinyLlama stories260K；ctx=2048/parallel=2（churn 用 e6_c1_churn.py 默认配置）
- **不引用 E6.5**：12/30 周期 churn 与生命周期全部在 E9.1 后重跑

## 1. churn 复验（E9.1 后）

| 项 | 12 周期 | 30 周期 |
|---|---|---|
| cycles | 12 | 30 |
| 同逻辑状态 drift | **0** | **0** |
| used_cells 序列 | 232/247/262 循环（3 周期回环，无累积）| 同（30 周期全程循环）|
| shared_cells 序列 | 195/210/225 循环（不累积）| 同 |
| errors | 无 | 无 |
| erase 后 | used=0 / active=0 / shared=0 | used=0 / active=0 / shared=0 |
| verdict | PASS_ENDURANCE | PASS_ENDURANCE |

raw：`raw/e9_churn12.json`、`raw/e9_churn30.json`（含全部周期样本 + 最终 erase 快照）

## 2. 生命周期场景复验（on/off 各跑，raw/e9_lifecycle.json）

| 场景 | on 结果 | off 结果 | 判定 |
|---|---|---|---|
| cancel（cleanup 语义）| 请求 200 → erase 200 → 后续请求 200 | 同 | PASS |
| retry | 首次 200 + 重试 200，hash `9247d733...` 一致 | 同 | PASS |
| save/restore | save 200（filename=e9_slot0）→ restore 200 → 恢复后请求 200 | 同 | PASS |
| generation（同 slot 3 任务）| 3 任务 hash 全部 `9247d733...`（无跨代污染）| 同 | PASS |
| shutdown/restart | 重启前 used=363/shared=326/active=2 → 重启后 **全 0** | 重启前 used=689/shared=0 → 重启后全 0 | PASS（资源回收）|
| capacity pressure（2 slot 长 prompt）| used=695、shared=650（共享 -48%）、无失败 | used=1345、shared=0 | PASS |
| combos（on + cache_ram off）| 共享 326、双请求 200 | 无共享 | PASS |
| server exit code | 全部 0 | 全部 0 | PASS |

## 3. 结论

- **E9.1 修复后 endurance 全过**：drift=0、erase 回基线、无 active victim、无跨 session 污染、无 identity 错误复用（无 lora 场景）、无 crash/hang/timeout（所有请求 ≤200 + 超时未触发）
- shared_cells 在 30 周期内循环不累积（共享随 erase 正确释放）
- capacity pressure 下 on 模式 used_cells -48%（与 E8.5 一致），无 failed/rejected
- 说明：本 build 无 `/cancel` endpoint（用 cleanup 语义近似）；save/restore 需显式 `filename` 参数（上游 API 语义，已适配）

## 4. 复现

```bash
uv run python benchmark/scripts/e6_c1_churn.py --cycles 30 --output raw/e9_churn30.json
uv run python benchmark/scripts/e9_2_lifecycle.py
```
