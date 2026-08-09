# M0：真实智能体多路径决策 workload + 分支内存基线（技术调研与设计）

> 状态：**调研完成 + 详细设计 + 实现（v41–v66）；formal 矩阵 run5 已跑但 v66 审计降级 INVALID_INTERMEDIATE（transient retry 未接入）——v66 wrapper 实现后未重跑正式矩阵（重跑待用户指令）**。
> 日期：2026-08-09 ｜ 对应路线：M0（阶段 0 通过后第一条后续路线）｜ 实施建议：**GO**（见 §8）。
**v67（v66 复审收口——dv ServerCrash 显式捕获 + 停止后续 session，2026-08-09）**：（1）**`_run_decision_session` 显式捕获 ServerCrash**（wrapper poll 已确认进程退出）：当前崩溃请求计入 `requests/error_count/error_summary code=server_crash`，**立即 break 不再执行剩余请求**（server 已死，剩余必败——原实现被 `except Exception` 吞掉后继续发 9 次必败请求且归因 connection_error）；**`run_decision_validation` 依 `requests<target` 停止后续 session**（仅 ServerCrash break 会提前返回；请求级 error 不中断循环）——崩溃后不再启动新 server，partial session 已 append 保留证据 → 上层 `partial dv` → 合法 PREFLIGHT_INFRA 落盘（rc=0、validator 通过、formal 未启动）；新增 `test_m0_v67_review.py` 3 例（首 session 崩溃 e2e：dv0 崩溃 → 仅 1 个 partial session（requests=1、error_summary 含 server_crash）、dv1 未启动、formal 未启动；第二 session 崩溃 e2e：dv1 崩溃 → session 0 完整（requests=10、error_count=0）+ session 1 partial（含 server_crash）、无后续；反 e2e：无崩溃 → dv complete）；（2）**`_transient_class` 文本 fallback 与 `classify_error` 统一 connection 模式**——"connection"/"refused"/"connect" 均归 connection_error 可重试（原 "connection reset" 漏掉 "Connection error." 文本；openai 真实类型分支优先不变；未知内部异常消息不含 connection/timeout/refused → 仍不重试）；（3）**mock_server 清理**：删除无引用死代码 `_ERROR_ONCE`（v49 一次性注入已被 v66 `set_fail` 取代）；`_FAIL` 注入**显式限定 `/v1/chat/completions` 路径**（/apply-template、/tokenize、/metrics/kv、/slots 等不受影响）；docs v66 测试数 13→21；（4）**并发测试计数 Lock/事件化**：branch ThreadPool 测试的计数+注入用 `threading.Lock` 保护（`inject.done` 保证恰好一次注入），并补"retry 发生"断言（`_FAIL.remaining==0` = 注入被 wrapper 重试吸收；tool round / ERROR rep 测试同补）；全量 pytest **749 → 752**（v67 新增 3 例，`cd benchmark && uv run pytest -q`）；未跑完整 GPU 矩阵、llama.cpp 零改动。
**v66（transient retry wrapper 正式实现——§3.6/§3.8 v12 设计定稿落地，2026-08-09）**：（1）**单一 `M0FanoutRunner._chat` wrapper** 接入全部 5 处 chat 调用：calibration parity（`_calibrate_template`）、decision validation（`_run_decision_session`）、formal decision、ThreadPool branch（`_branch`）、tool round（均 `_run_unit` 内）——**此前 runner 所有 chat 直调 driver（SDK max_retries=0），设计要求的 transient retry 从未接入实际调用**（v66 审计发现，见 (5)）；（2）**可重试分类（`_transient_class`，按异常类型 + status_code 精确分类）**：APIConnectionError → connection_error、APITimeoutError → timeout（openai 2.x 中 APITimeoutError 是 APIConnectionError 子类，先判 timeout）、APIStatusError status>=500 → http_5xx；**不重试**：HTTP 4xx（APIStatusError 400-499，含 driver 内部 400 context 兜底耗尽后的 400）、malformed 成功响应（APIResponseValidationError）、内部 bug（AssertionError/KeyError/TypeError/ValueError）；**最多 3 次逻辑调用（首次 + 重试 2），确定性退避 0.25s/0.5s**（`TRANSIENT_MAX_ATTEMPTS=3`/`TRANSIENT_BACKOFF`）；（3）**每次异常后立即 poll server（三路归因 §3.6）**：已退出 → 立即 `ServerCrash`（不再重试——ThreadPool branch 的 barrier 不因重试 sleep 死锁）；健康 → sleep 退避重试，耗尽抛最后一次异常（外层 `classify_error` 归因 connection_error/timeout/http_5xx）；（4）**`classify_error` 增强**按 openai 异常类型 + status_code 精确分类（优先于文本启发式；APIResponseValidationError → malformed_response 补录）；SDK `sdk_max_retries` 保持 0（`_connect` 不变，理论最大 wire = 3 外层 × driver 400 内层 ≤2 = 6，§3.8 上限不变）；（5）**run5 审计降级 INVALID_INTERMEDIATE**：设计 §3.6/§3.8 的 transient retry 未接入（Driver sdk_max_retries=0 且 runner 直调 driver），run5 36/120 connection_error **不能称重试耗尽**；**撤销**「已重试 3 次 / 非代码 bug / 稳定基础设施现象 / 有效 off-on 性能结论」（off/on latency -4.1%/ttft -3.4% 为噪音级且无效）；与 retry 无关的结构性结论保持（shared_cells 恒 0、G-M0-5 PASS、4B hybrid C1 被拒、KV/RS 容量）；证据文件 `benchmark/baseline/qwen35-4b_gpu_m0_formal_run5_evidence_20260809.json` 增 `audit_status=INVALID_INTERMEDIATE`；（6）**baseline key-lines 证据入 baseline**：`benchmark/baseline/qwen35-4b_gpu_m0_formal_run5_logevidence_20260809.json`（server_logs_key_lines 15 tag），evidence `log_evidence_ref` 指向 baseline 内文件（不再只引用 gitignore results 下文件）；（7）**rep 级 GPU/RSS 峰值实现**：`metrics.peak_gpu_mb/peak_rss_mb` = decision/branch/tool **成功 row** 的 rss_mb/gpu_mb 取 max（None 过滤；ERROR rep 保留已有成功样本，不再 0.0 占位——修复 §5.2 与实现不一致的已知限制）；（8）**mock_server 增 `set_fail(n, status)` 连续 chat 失败注入**（只作用于 chat.completions 路径，/apply-template、/tokenize、/metrics/kv、/slots 等不受影响——校准 parity 的 apply-template/tokenize 前置不会被误注入）；（9）**新增 `test_m0_v66_retry.py` 21 例**（wrapper 单元 5：首失败后成功/连续 3 次→ERROR/HTTP400 一次不重试/HTTP500 三次/重试期间 server 退出→ServerCrash；分类单元 8：APITimeoutError/APIConnectionError/5xx/4xx/malformed/内部 bug/未知内部异常不重试/urllib HTTPError；阶段覆盖 5：calibration parity、dv、formal decision、branch barrier 无死锁、tool round；peak 采样 3：成功 row 峰值/取 max/ERROR 保留样本）+ 适配 3 处旧测试（v49 warmup 一次性 500 改连续 3 次、v50 `_ToolFailDriver` 连续 3 次 500、v59 `_FakeDrv` fail_at 起连续 3 次——原"单次 500/连接异常即 ERROR"语义被 wrapper 重试吸收，须 retry 耗尽才落 ERROR）；（10）**未跑完整 GPU 矩阵**（wrapper 生效后 run5 结论不成立，重跑待用户指令）；llama.cpp 零改动。
**v60（v59 终审建议的 formal 中断归因，2026-08-09）**：（1）**`_stop_server` 检查 `adapter.stop()` 返回 `(ok, detail)`**——`ok=False` 视为 stop failure（进程可能残留，如 SIGTERM 超时转 kill），记录 `_stop_errors` 与 detail、返回 False；抛异常路径保留（同记 `_stop_errors` 不传播）；非 tuple 返回（旧 mock 契约 None）视为干净；dv/formal 结果可诊断；补**真实返回 False 而非抛异常**测试（dv 路径 session break、formal 路径 FormalIncomplete）；（2）**formal `_capture_baseline` / `_connect` 基础设施异常必须转 FormalIncomplete**（OSError / openai / ServerError，含 TimeoutError）——**首 formal group（尚无 completed group）按首 group preflight/PREFLIGHT_INFRA 规则**（FirstGroupStartFailed → 合法 preflight 落盘、保留完整 decision_validation）；**第 2+ group 保留已完成 groups、失败 group ERROR（endpoint_unavailable）、FormalIncomplete 合法落盘**（phase=formal、rule=FORMAL_INCOMPLETE、exit 0）——**绝不 exit70 丢结果**；**内部 AssertionError/KeyError/TypeError 仍 70**（不在此捕获）；补首 group 与第 2+ group OSError/openai 异常 e2e（含 KeyError 仍 70 反例）；（3）**cleanup_stale_dirs 并发安全**——`--cleanup-tmp-age` 合理最小值**钳制 `<300s → 300s`（`CLEANUP_AGE_MIN=300.0`）**并在文档明确**禁止共享父目录并发清理**；**每 run marker（`ACTIVE.marker`，JSON `{pid, create_ts, keep}`）**：runner 创建专属 tmp 子目录后写入；cleanup **只删无活跃 marker 且过期目录**——keep=true 永不删、marker pid 存活（`os.kill(pid,0)`）不删、pid 已死按 age 删；**keep-tmp 生命周期明确**（无 `--keep-tmp` 参数：默认 pid 退出 + age 超龄后由 cleanup 删除；用户可改 marker keep=true 或改名目录避免意外清理）；marker 为控制元数据、`_dir_newest_mtime` 排除（否则带 marker 的陈旧目录永不超龄）；补活跃不删 / 死 pid 删 / keep 保留 / marker 不计入测试；（4）**`_dir_newest_mtime` 含递归子目录 mtime**（子目录新建/增删子项是活跃信号）；清理 dv 不可达注释（`_finalize` 0 group 防御性注释更新为 v60 归因语义）；**删除无调用点冗余 stop try/except 死代码 `_cleanup_all`**（v58 测试改用 `_stop_server`）；（5）全量 pytest 690 → **706（v60 实测 `cd benchmark && uv run pytest -q` → 706 passed，约 365s）**（v60 新增 `test_m0_v60_review.py` 16 例：stop 返回值 4、formal 基础设施归因 4、cleanup marker 7、_cleanup_all 删除 1）；（6）**确认下一步不再改代码，直接运行完整 24-unit 4B formal 矩阵**（待用户指令执行）。
**v59（v58 终审 H1 + formal 前 Medium/Low，2026-08-09）**：（1）**calibration runner 日志路径统一 `r.tmp_dir`**（M0FanoutRunner 专属子目录）——`rs_observations`/`probe_p10` 的 `_grep_log` 改读 `r.tmp_dir/server_{calib,dv*,p10}.log`（v58 子目录化后旧路径读外层父目录恒空，真实运行丢 RS/capability 观测）；补集成测试（LogWritingFakeAdapter 真实写日志文件：`rs_observations`/`probe_p10`/`server_logs_summary` 非空且 tag/pid/log_path 正确、父目录无 server 日志）；（2）**formal run() dv 阶段基础设施/请求异常 → 合法 preflight 落盘**——`run_decision_validation` 捕获 session 级 `_INFRA_EXCEPTIONS`（OSError/openai/ServerError，含 TimeoutError）停止验证阶段（不传播），`run()` not-complete 路径构造 `decision_validation=sch.build_decision_validation(self.decision_sessions, partial=True)` 保留已完成 session（原为 null 丢证据）；**旧 `{complete, sessions}` 结构仅存在于历史 v55 run1 证据文件（`baseline/qwen35-4b_gpu_m0_calibration_20260809.json`），当前无代码读取/比较该结构，如需对比按 partial 派生语义手工对应**；AssertionError/KeyError 等内部 bug 仍传播 → run_safe 70（不吞）；v58 旧断言 TimeoutError 传播的测试改写为捕获语义；补第 2 control 中途 openai 连接异常两测试（请求级逐条 error_count=1 + session 级整体抛）均断言 partial dv 落盘；（3）**calibration report `date` 统一 `sch.now_utc()`**（RFC3339 UTC Z，替换 %z 本地时间，与 started_at 同格式）；（4）**`_stop_server` finally 异常不掩盖 primary**——stop 异常捕获记 `self._stop_errors`（诊断）不传播；仅 try 块无 primary 时 stop 失败按基础设施（dv → break 不完整、formal group → FormalIncomplete）；补 4 测试（primary 保留/stop 失败 break/干净无记录/formal group）；（5）**日志筛选 pattern 收敛单一常量**（`LOG_KEY_LINE_KEYS`/`LOG_RS_BUFFER`/`LOG_CAPABILITY`/`LOG_KV_BUFFER` 模块级），`_read_log_refresh` 与 `server_logs_summary` 共用，防新增 pattern 漏同步；（6）**formal tmp 策略明确 + `cleanup_stale_dirs`**——专属子目录含 -lv5 完整日志成功后按设计保留在 results 供排障（不视为泄漏）、baseline 只归档 key lines；新增 `sch.cleanup_stale_dirs`（只删 `m0_fanout_`/`m0_cal_` 前缀且**递归最新 mtime**超龄的一级子目录——目录 mtime 只随子项增删更新、不随日志内容写入更新，活跃运行中的 server 持续写日志可能目录 mtime 很旧，必须取目录树内最新文件 mtime 判定；不删活跃/父目录/非目录文件），main 入口复用 `--cleanup-tmp-age` 调用；（7）全量 pytest **673 → 690**（v59 新增 `test_m0_v59_review.py` 17 例：日志路径 3、dv partial 3、stop 4、pattern 2、cleanup 5——含活跃目录递归最新 mtime 判定 3）；（8）**确认下一步可直接运行完整 24-unit 4B formal 矩阵**（H1 收口完成，待用户指令执行）。
**v58（v57 审查 H1 + 正式矩阵前收口 7 项，2026-08-09）**：（1）**run_decision_validation 同步 self.decision_sessions**——函数开始清空；每完成一个 session 立即 append（异常中断时已完成 session 保留，供上层构造 partial 证据）；测试调用真实方法（仅替换 `_start_server`/`_run_decision_session`/`_stop_server` 子方法），第 2 个 control 中途抛异常断言保留 session 0，**禁止 monkeypatch 整方法手写属性**（原 `test_dv_failure_preserves_partial_sessions` 违反，v58 重写）；（2）**calibration report decision_validation 统一 `sch.build_decision_validation` 标准结构**（`{sessions, total_valid_rate, partial}`；complete 由 partial 派生、不造第二 schema——成功路径与异常路径均改；**errors 结构化 `{code, stage, count, detail}`（v57+ 格式，与旧字符串 error 不兼容）**）；（3）**`校准长度缺失` 改独立 `CalibrationLengthMissing` 异常**——不入 `_INFRA_EXCEPTIONS`（warmup 不吞、健康不忽略）、formal/warmup 均不可恢复 → run_safe 归 70（先吞后报消除）；`ServerError` 仅表示可恢复基础设施；补测试（不在 _INFRA_EXCEPTIONS / 真实 _run_unit 抛出 / run_safe 70）；（4）**`_read_log_refresh` 流式筛选 key lines**（RS/KV buffer、capability、启动等 11 个关键 pattern，不整文件 f.read 回传）读失败回退快照；**started_at 统一 RFC3339 UTC**（`sch.now_utc()` Z 后缀，替换本地偏移 %z）；（5）**`--tmp-dir` 安全**——视为父目录创建本 run 唯一子目录（`m0_cal_{run_id}` / `m0_fanout_{ts}_{pid}`），**绝不递归删除用户既有共享目录**、cleanup 只删自建子目录；help/文档说明；补既有目录内容不被删测试（calibration/fanout 各一 + keep-tmp 子目录保留）；（6）测试注释结构修正 + 全量 pytest **659 → 673**（v58 新增 `test_m0_v58_review.py` 14 例：同步/清空/异常保留 3、标准结构 1、CalibrationLengthMissing 3、日志流式/fallback/started_at 4、tmp 安全 3）；（7）**暂不跑 formal**——H1 修复后待复审，确认后可立即跑完整 24-unit 4B formal 矩阵。

**v57（v56 审查 Medium 7 项修复实现记录，2026-08-09）**：（1）decision session validator 向后兼容 v55——两个 representative 字段同时缺失按旧格式接受（缺字段不报 type_mismatch、不强制 valid>0 语义）；任一存在则两者必须同时存在（键存在性检查，非值空判定）且 hash 可重算（保留 v56 同空/同非空值语义）；字段存在时 valid>0 必须代表输出非空（valid=0 可同空）——`validate_decision_session` 新增 has_rep/has_sha + valid>0 非空断言，旧 run1 校验通过、valid>0 空反例拒绝；（2）`M0FanoutRunner._start_server` 成功时记录 `_server_meta[tag]={port, pid(真实子进程 PID), started_at, log_path}`；`_server_logs_summary` 只用该映射（删除 sorted index + port_base+idx 推断 pid、删除摘要生成时刻 started_at），stop 后经 `_read_log_refresh` 重读盘刷新 key lines（读失败回退启动期快照）；（3）calibration tmp 清理策略：runner 异常/报告写失败默认保留 tmp 排障，仅报告成功写盘后且非 --keep-tmp 才删除（main() 删除无条件 finally rmtree）；（4）`cleanup.leftover_pids` 只检查本 runner 采样 PID 集合（peak.pids_seen + `_pid_alive_filter`，os.kill 0 探测），删除全系统 pgrep——共享其他 llama-server 不算残留，method 字段声明；（5）calibration 各阶段异常时报告保留 `parity_progress` / `calibrated_lengths` / `preflight_rejections` 已完成部分与结构化 error（{code, stage, count, detail}），dv 阶段保留 partial sessions；（6）`server_logs_summary.started_at` 用真实启动时刻、`probe_p10` 显式 stage 键（pid/phase 已有）、`gpu_rss_samples.comparability` 明确 peak_rss 跨 v55/v56 采样口径不可比（v55 单次静态 vs v56+ 周期峰值）；（7）`_INFRA_EXCEPTIONS` 纳入 ServerError（类定义前移避免 NameError）——warmup 基础设施 catch 覆盖 ServerError（健康忽略继续/死转崩溃），AssertionError/KeyError/TypeError 仍不捕获 → run_safe INTERNAL_RUNNER_ERROR + EXIT_SOFTWARE 70；test_m0_calibration_runner.py 11→28、test_m0_runner_fixes.py 49→52、test_m0_schema.py 89 例数据适配 v57 语义；**659 pytest 全绿（`cd benchmark && uv run pytest -q`）**；仍未跑 24-unit formal 矩阵、llama.cpp 零改动。
**v48（a408031 审查 10 项 Critical 修复实现记录，2026-08-09）**：（1）KVProbe 数据路径——baseline 的 `capacity_bytes` 在 `snapshot['data']` 内（原实现 `kv_snap.get('capacity_bytes')` 恒 0），`metrics_kv_snapshot` 保存权威 data；每 formal rep 独立 `begin_run/end_run` 边界 + erase 后 `after_erase` 采样；`run_aggregate` 增 active_sequences 聚合；rep `metrics.kv` 包装为 gate/validator 消费的一致结构 `{first,last,peak}`；G-M0-3a/G-M0-5 缺观测（None/缺键）→ **FAIL 不静默 PASS**；G-M0-2 缺分支观测 → FAIL；（2）Driver.chat 顶层 `finish_reason = choices[0].finish_reason`（runner 只从顶层判定，不依赖非标准 timings）；mock 可配置 `finish_reason="length"` → 决策 INVALID_DECISION + `invalid_length>0`；（3）`_connect` 显式传实际 host/port（原默认 8080 导致 PID 定位错配）；sampler 兜底改 net_connections 按端口过滤（不再返回无关 llama-server）；（4）启动 OSError（bin 缺失 FileNotFoundError / 日志不可写）→ ServerError → 按阶段落 PREFLIGHT_INFRA；`run_safe` 兜底 EXIT_SOFTWARE 70 无 traceback/exit 1；（5）CLI 入口 `cleanup_stale_tmp`（atomic tmp 命名 `.m0_fanout_<name>.<kind>.tmp` 与 cleanup 匹配）；（6）prefix_len/branch_len 用 `(bucket,fanout)` 校准实测值（apply-template+tokenize，预算复核同源），禁止 BUCKET_TARGETS 近似值落结果；validator 新增同 (fanout,bucket) 长度一致性不变式；（7）warmup 崩溃不再被吞——立即 group ERROR + FORMAL_INCOMPLETE；（8）mock_server 删除误导性实例 `count` 死属性（计数唯一在 httpd.count）；（9）G-M0-5 日志匹配真实 llama.cpp 行前缀 `E8-C1: capability rejected: hybrid`（server-context.cpp:1505，`--kv-prefix-share` + hybrid 模型时 SRV_WRN；FakeAdapter 不再专造短行）；（10）G-M0-4 拆 **derived 自检**（rs_buffer_mb 与公式一致，防内部 bug）**+ RS buffer 独立观测**（真实日志 `RS buffer size = X MiB`，llama-memory-recurrent.cpp:115；无观测 → NOT_APPLICABLE 明确标注、超差 >5% → FAIL）——不再「恒 PASS 却宣称实验验证」。新增 `benchmark/tests/test_m0_runner_fixes.py`（21 测试）；全仓 pytest **558 passed**；未跑正式 4B 矩阵、llama.cpp 零改动。**仍需真实 4B 验证的风险**：RS buffer 日志口径（per-slot vs 总量）与公式对账需实跑确认；G-M0-3a after_erase 依赖真实 /metrics/kv 归零时序；G-M0-5 依赖 hybrid 模型 + --kv-prefix-share 启动日志。
**v51（复审阻断修复，2026-08-09）**：（1）**ERROR_CODES 14→16**——补录 `erase_failed`（clean_all_slots 异常防御分支）与 `after_erase_missing`（after_erase 观测缺失）两个 v50 代码已抛、枚举未收录的码；rep/group validator 接受（validate_modes_group_rep 正反用例：两新码通过、非枚举拒绝）；**完整 run() e2e**（禁止只测 _run_unit）：真实端点失败（/metrics/kv 500）触发 after_erase_missing、clean_all_slots 异常（server 健康）触发 erase_failed——各自 rc=0、phase=formal、FORMAL_INCOMPLETE（matrix_complete=false）、ERROR group/rep error_type 正确、最终 envelope validator 通过；（2）**warmup clean_all_slots 返回 (0,0) 主动 poll**——server 死 → ServerCrash/group ERROR；健康且确实无 slot → 允许继续（记可诊断 note）；真实端点不可达（mock /slots 畸形响应 → list_slots 空 → (0,0)）healthy 正例 + poll dead 反例；（3）**校准 catch-all 不吞内部 bug**——`run()` 校准 try 只捕获 ServerError/ServerCrash/EraseFailure + 明确网络/OS 基础设施异常（`_INFRA_EXCEPTIONS`：HTTPError/URLError/OSError/TimeoutError/ConnectionError）→ PREFLIGHT_INFRA；AssertionError/KeyError/TypeError 等内部 bug 不捕获 → run_safe 归 INTERNAL_RUNNER_ERROR + EXIT_SOFTWARE(70)，正反测试（AssertionError → 70 不落盘；HTTP 500 → preflight 合法落盘 rc=0）；（4）**warmup 工具轮异常与决策异常一致**——server 死 → ServerCrash；健康 → 记 note（`warmup 工具轮请求异常（server 健康，继续）`）不静默 return（formal rep 仍标 ERROR 语义不变）；健康/已退出正反 e2e；（5）**preflight envelope builder 支持 notes 参数**——`build_preflight_envelope(..., notes=None)` 透传 doc.notes，`_fail_preflight` 传 `self.notes` 保留 runner 已有 notes；validator 仍 22 键 canonical meta 不变；note 落盘测试（builder 透传 + runner 保留）；（6）mock_server 增 `slots_malformed` 开关（/slots 畸形响应生产路径）。新增 13 测试（`test_m0_v51_review.py`）；全仓 pytest **613 passed**；未跑正式 4B 矩阵、llama.cpp 零改动。
**v50（真实失败归因收口，2026-08-09）**：（1）**after_erase 观测缺失走真实可达分支**——`/metrics/kv` 端点失败（真实 `_fetch` 返回 None + `last_error`，非 mock 抛异常）→ `snapshot('after_erase')` 返回 None → 显式 `EraseFailure('after_erase_missing')` → group ERROR + FORMAL_INCOMPLETE；补 `_fetch` 失败/返回 None 生产路径测试；（2）**校准阶段 ServerCrash/清理异常 → PREFLIGHT_INFRA**——`run()` 校准 try 宽捕获（ServerError/ServerCrash/EraseFailure/其他）→ `_fail_preflight('validation_incomplete', ...)` 合法落盘（**保留已收集 `preflight_rejections`**），不得 run_safe exit70；补校准清理崩溃测试（parity_ok=true 语义验证：6 桶完成后清理失败）；（3）**formal 工具轮 HTTP 异常**——server 健康 → 标当前 rep ERROR（`error_type=classify_error(tool_error)`，修复原 `next(...)` StopIteration 裸逃逸）+ 继续统一严格 erase/after_erase；server 已退出 → ServerCrash/group ERROR；补工具轮 500 健康/已退出正反 e2e；（4）**error_type 区分**——`erase_failed`（clean_all_slots 异常，防御）/`erase_partial`/`after_erase_failed`/`after_erase_missing`；**删除 warmup loop 不可达 EraseFailure 分支**（warmup 无严格路径，注释说明）；（5）**warmup best-effort erase 异常记录可诊断 note**（`doc.notes`；clean_all_slots 不抛异常、真实可达异常 = partial erase）不静默吞；测试 used_cells 先非 0 再证明 erase 归零 + partial note；（6）**mock_server 删双 return 死代码**；（7）**to_json_pointer 完整 RFC6901 转义**（`~`→`~0`、`/`→`~1`，数组下标先占位再按点号分字段、段内转义、还原分隔符），9 参数化单测（$ 幂等/空串/数组下标/转义）；（8）**失败路径复核**：任何请求/erase/观测失败只落设计 preflight/formal 结果或 schema error（内部不变量 bug 除外 run_safe 70）。新增 15 测试（`test_m0_v50_failures.py` 6 + `TestToJsonPointer` 9）；全仓 pytest **600 passed**；未跑正式 4B 矩阵、llama.cpp 零改动。\n**v49（3 Medium + 相关 Low 修复实现记录，2026-08-09）**：（1）**G-M0-2 基于成功分支观测语义**——全矩阵无任何非 ERROR 分支观测（如 5 reps 全 ERROR）→ `NOT_APPLICABLE`（绝不静默 PASS；ERROR rep 跳过、至少 1 个非 ERROR 分支观测才判定 PASS/FAIL）；补全 ERROR / ERROR+OK 混合无泄漏 PASS / 混合含泄漏 FAIL / e2e PASS 正反测试；（2）**erase 异常路径**——server 健康但 `clean_all_slots` 部分成功或 `after_erase` 采样异常 → `EraseFailure(error_type=erase_partial|after_erase_failed)` → rep ERROR + group ERROR（保留可诊断 error_type）+ `FORMAL_INCOMPLETE`（优先后者以免污染后续 rep），不再裸异常 exit70 无合法结果；server 已退出 → `ServerCrash` 归因不变；**严格 partial 检查仅对 formal rep**（warmup 的 erase 由 finally best-effort 承担）；（3）**periodic 迟到样本不得覆盖 after_erase**——`run_aggregate` 的 last 优先 tag=after_erase 样本（stop join 超时后 daemon 线程仍在写也防御）；stop 后线程仍最终退出、不跨 run 污染；（4）**warmup 决策异常即使 server 健康也执行 best-effort erase**——异常后 poll：崩溃 → group ERROR、健康 → 可继续；校准后清理同样容错；（5）**clean_all_slots 返回 (attempt, ok)**——partial erase（attempt>0 且 ok<attempt）立即按 (2) 归 FORMAL_INCOMPLETE 并带诊断，不等 gate 间接发现；（6）ERROR_CODES 12→14（+`erase_partial`/`after_erase_failed`）；sidecar path 转 RFC6901 JSON Pointer（`sch.to_json_pointer`，修 v30 合同与实现不一致）；mock_server 增 `_ERROR_ONCE` 一次性 500。新增 11 测试（`test_m0_runner_fixes.py` TestG2BranchObsSemanticsV49/TestEraseFailureV49/TestWarmupEraseV49/TestPeriodicLateAfterEraseV49）；全仓 pytest **585 passed**；未跑正式 4B 矩阵、llama.cpp 零改动。
> 修订：2026-08-09 v2（矩阵/预算/层数/架构/接口合同/回收观测）；v3（精确 token 计数、矩阵手算修正、driver 合同、门禁语义）；v4（driver `_retry` 位置参数兼容、thinking 字段生效实证、G-M0-1 与矩阵完全分离、RS 日志行号、CLI 临时目录）；v5（G-M0-1 口径统一、可提交证据、token parity 门禁、verdict 规则）；v6（INVALID 谓词 OR 语义、verdict/gates 值域映射、add_special 归因、parity 校准协议）；v7（gate/verdict 分层、parity 前置、schema 完整化、聚合定稿）；v8（preflight/schema 闭环）；v9（状态机/schema 终审）；v10（验证/聚合路径收口）；v11（跨章节同步定稿）；v12（运行边界合同）；v13（实现阻断收口）；v14（validator/测试合同终审）；v15（落盘/测试盲区终审）；v16（结构化错误/schema/测试残留终审）；v17（最后 Medium/Low 文档残留）；v18（终审阻断与规范残留）；v19（示例不变量终审）；v20（决策统计不变量终审）；v21（测试/schema 残留终审）；v22（GO 后终审剩余 Medium/Low）；v23（验证崩溃边界与测试缺口）；v24（规则澄清收口）；v25（最后歧义收口）；v26（验证启动失败残留与清理测试）；v27（验证阶段最后歧义）；v28（evidence failure 路径与测试合同）；v29（SCHEMA_INVALID 落盘合同）；**v43（replicate 粒度与 group ERROR/FORMAL_INCOMPLETE schema 收口——modes.server_groups[].replicates[] 每项 = 一次正式 rep 运行（warmup 不落 replicates、group warmup_count=2 必填整数；每项含 unit 身份 + rep_index(0..reps-1) + rep 级 status/decision_fallback/metrics；每 unit 固定 formal reps=5）、unit_id 允许 5 rep 重复（validator 按 (unit_id,rep_index) 全局唯一；已执行 unit 的 rep_index 集合必须 {0..4}、ERROR/中断 unit 可为前缀并使 matrix_complete=false；24 完整性按 replicates 中 unit_id 去重集合验证；删除「重复 unit_id 即非法」旧断言）、删除所有扁平 modes.{off,on}.replicates[] 残留（唯一权威路径 server_groups[].replicates[]；§5.2/G-M0-7/测试同步）、group schema 完整化（start/stop 始终必填 RFC3339 尝试开始/清理结束；COMPLETED → baseline 完整且 error 字段禁止；ERROR → baseline 可 null（启动后取得的部分 baseline 可保留合法结构）+error_type 必填+error_stage=formal；warmup_count 必填整数 2；validator/测试㉗ 覆盖）、FORMAL_INCOMPLETE 子集规则（modes 只含已启动 groups——失败 group 以 ERROR 出现、未启动不出现；matrix_complete=true → unit 集合==EXPECTED-rejections 且每 unit 5 reps；false → unit 集合为其子集、len 去重 unit==executed_units——仅完整完成 unit 计 executed、失败 unit 部分 rep 不计）、group phase 旧规则清理（仅首 formal group 启动失败→preflight；第 2+ 启动失败/运行崩溃→formal incomplete；删除 §3.5/§3.7 并排 v22 无条件规则、格式分行）、测试㉖ 权威补回（首 formal group 启动失败→preflight/PREFLIGHT_INFRA/planned 24/executed 0/matrix false/modes.gates 空/完整 decision_validation；㉗ 覆盖第 2+ 启动失败/运行崩溃/group ERROR/subset unit 与 reps；§6 权威编号①–㉗）、AGENTS 清理（27 键/prefix_bucket/branch_bucket/27→26 旧残留 → 22 键 + per-unit 单 bucket）、§6 合并 SCHEMA_INVALID_FIXED 重复、§5.1 preflight_rejections 示例改 {unit_id,budget,threshold,reason}、m0_schema 职责补 modes/group/rep validator（rep_index/5reps/warmup_count/incomplete 子集））；**v43 注：v45 已取代 v43 的 executed 口径（complete 由 rep_index 完整性判定、executed=len(complete)，v46 确认）**；v44（FORMAL_INCOMPLETE rep 子集不变量（observed_unit_ids/complete_unit_ids：observed ⊆ EXPECTED−rejections、executed_units==len(complete)、complete ⊆ observed、matrix_complete=true 时三者相等、partial rep 计 observed 不计 executed）、删除 v22 无条件 formal 启动失败段（首 group 启动失败 → preflight、第 2+ 启动失败/运行崩溃 → FORMAL_INCOMPLETE）、MATRIX_PLAN 常量（FORMAL_REPS=5/WARMUP_REPS=2、CLI 删 --reps/--warmup、显式传 → EXIT_USAGE 64）、replicates 逐 rep 权威字段（unit_id/rep_index/fanout/bucket/ctk/ctv/prefix_len:int/branch_len:int/server_group_id/status/decision_fallback/error_type?/error_stage?/error_bucket? + §5.2 metrics；不存在独立 unit 对象、unit 身份字段每 rep 内联）、unit 身份字段权威改名去歧义、测试编号 ①–㉗ 顺序排列（㉗ 移段末）、§6 合并 validator 段为 modes/group/rep 唯一权威、测试㉗ 补首/中途 group phase + FORMAL_INCOMPLETE 集合断言、AGENTS 同步 v44）**；v45（complete unit 口径与旧约束清理——complete_unit_ids 仅由 rep_index 集合完整 {0..FORMAL_REPS−1} 判定、不要求 status!=ERROR（ERROR rep 但 5 次尝试均有记录仍计 complete/executed；REP_ERROR 与 matrix_complete=true 可共存；仅 rep_index 中断前缀不全不计 complete）；删除 §5.1 phase 约束/测试㉗/§6 中 len(去重 unit_id)==executed_units 旧等式，统一 observed/complete/executed 集合语义（observed ⊆ EXPECTED_UNIT_IDS−rejections、executed_units==len(complete_unit_ids)、complete ⊆ observed、matrix_complete=true 时 observed==complete==EXPECTED−rejections、false 时允许任意子集、不限于前缀）；EXPECTED_UNIT_IDS 常量 = MATRIX_PLAN 重建 24 unit_id（validator/测试㉗ 统一引用）；测试计划句首句尾统一 ①–㉗；CLI 未知参数清单与测试㉕ 补 --warmup/--reps 共 8 项；清理 §3.5/§3.7 堆叠 v41/v42/v43 标注与"任务 N"标签、仅保留权威规则文字；全文"部分前缀"改"子集"（仅 rep_index 中断可称前缀；G6 门禁名保留）** **v47（v46 复审最后一致性项——AGENTS v43 历史标注（24 unit_id 去重集合唯一完整/扣 rejections 后 executed 匹配 → 已被 v45/v46 取代：executed=len(complete_unit_ids)、complete 仅由 rep_index 完整性判定）、测试㉗ 合并 v44/v45 重复 FORMAL_INCOMPLETE 集合段（仅保留段末单一权威断言）、测试㉗ 补 len(EXPECTED_UNIT_IDS)==24 且与 MATRIX_PLAN 映射重建集合精确相等（断言集相等而非仅长度）、§6 合并两处 EXPECTED_UNIT_IDS 定义为单一段（MATRIX_PLAN 合法组合映射 + 2 profile + off/on controls + ID 格式 + 24 数量 + Markdown 粗体修复）、对照显式建立 A=modes.off、B=modes.on 并统一后续术语优先 off/on、测试㉗ 补崩溃边界正反例（第 5 rep 已开始且记录 ERROR → 0..4 完整、unit 计 complete；尚未开始 → 仅 0..3、不计 complete））**；**v47（终审一致性收口：无 Critical/High）——测试㉗ 补回 modes 独立断言（只含已启动 groups/失败 status=ERROR/未启动不出现）+ 崩溃第 5 个 rep 正例（group 崩溃 → matrix_complete=false，与请求级 REP_ERROR 严格区分）；测试㉗ 前部重复 observed/executed 规则删除、仅留段末 FORMAL_INCOMPLETE 集合断言指针；§6 EXPECTED_UNIT_IDS 行 Markdown 括号/加粗配对修复；§4.2/§5.1 对照 {A,B} 残留改 controls {off,on}（A/B 仅映射表）；§5.1 validator 段重复 v45 集合表述清理为单一当前规则；「允许任意子集（非"前缀"）」统一为「允许任意子集、不限于前缀」**，见各节。
> 约束：本阶段只调研与设计，不实现代码、不运行长 GPU benchmark；不改 E6–E15 历史 raw/结论。
> 探针（§1.3/§7）：仅"启动 server → 读取内存分配日志与 /metrics/kv → 退出"，未做任何推理请求。

---

## 1. 现状调查（证据）

### 1.1 benchmark 侧（根仓库 benchmark/）

**Workload 抽象**（`benchmark/framework/workload.py`）：
- `WorkloadSpec`（`name/params/prompts/expected/meta` + `fingerprint()`，`:25-42`）；
- `Workload` 抽象基类：`generate(params) -> WorkloadSpec`、`run(driver, spec) -> {"rows":[...], "meta":{...}}`、`evaluate(results, spec)`（`:44-80`）；
- 注册表 `_REGISTRY` + `@register`（实例化注册，重名 ValueError），`get_workload/all_workloads`（`:83-103`）。

**四场景**（`benchmark/workload/__init__.py:5` 导入 branch/long_life/multi_turn/tool_call）：
- `multi_turn.py`：固定 8 条中文 prompt 池循环 `rounds` 次累积 history（`:17-61`）；
- `tool_call.py`：文本 ACTION 协议 + MOCK_TOOLS 大段 JSON 回填；E15.2 `ToolPayloadRun` 集成（externalized put→projection→resolve 拦截；off 默认旧行为）（`:33-68, 258-301`）；
- `branch.py`（见 §2 详细分析）：固定 A/B 分支问题 + 固定"继续完善方案X"循环（`:15-65`）；
- `long_life.py`：多轮对话+工具+应用层截断，`est>budget` 丢早期消息，主指标 `state_retention_rate`（`:48-157`）。
- `branch_pressure.py`（E2.5）：双分支 X/Y、长共享前缀、固定交替 8 请求 `SEQUENCE`、JSON evaluator（`:319-405`）；**不在 `workload/__init__.py` 注册列表**，仅显式 `import workload.branch_pressure` 时才触发 `@register`（`tests/test_branch_pressure.py` 中 `get_workload("branch_pressure")` 前必须先 import 该模块）。

**runner**（`benchmark/runner/e15_branch_concurrent.py`，E15.1）：
- CLI：`--server-bin --model --port(8091) --ctx-size(2048) --parallel(5) --cache-ram(0) --min-lcp(64) --branches(2|3) --warmup(2) --reps(5) --out --tmp-dir`，前置校验 `parallel>=branches+2`（E15 现状；M0 用 `parallel:=fanout+2` 恒等派生，v38）、off 模式 KV 峰值<ctx（`:838-873`）；固定 `SEED=42/TEMPERATURE=0.0/N_PREDICT=8`（`:66-70`）；
- prompt：`PREFIX_CORE`（英文童话）+ `BRANCHES[b0..b3]` 固定后缀 + 专属 canary，**纯字符串拼接**，分支首 token 互异是硬编码设计（`:74-114`，测试 `test_branch_first_tokens_differ`）；
- 并发：`threading.Barrier` + `ThreadPoolExecutor`，target 各自 `id_slot=i+1`（`:686-703`）；source 预热 slot 0 后 idle（`:672-675`）；
- 门禁 `gate_verdict` G0–G9（纯函数，`:292-515`）：G0 数据完整 / G1 on/off 输出一致 / G2 shared_cells>0 / G3 recompute 降≥25% / G4 off 无共享 / G5 canary 无污染 / G6 部分前缀保护 / G7 清空回基线 / G8 shutdown clean / G9 physical_sharing==false 契约；
- server 生命周期：`start_server` Popen + `/health` 轮询、`stop_server` SIGTERM（`:548-599`）；
- 请求走**原生 `/completion`**：`{prompt, n_predict, temperature, seed, cache_prompt, id_slot, return_tokens}`（`:602-608`）；
- 落盘 JSON：`{"meta", "modes":{"off"/"on":{"server_groups":[{"start","stop","status","baseline","warmup_count","replicates","error_type?","error_stage?"}]}}, "gates", "verdict", "notes"}`（v43：唯一权威路径 `modes.{off,on}.server_groups[].replicates[]`，扁平 replicates 已删除）。

**driver**（`benchmark/framework/driver.py`）：OpenAI SDK 封装走 `/v1/chat/completions`（`:58`）；`chat()` 请求前 `preprocessor.process(messages)`（默认 off）、`kv_probe` 前后快照（`:75-103`）；返回行 `text/prompt_tokens/completion_tokens/total_tokens/cached_tokens/latency_ms/rss_mb/gpu_mb/timings`（`:89-99`）；`_extract_timings` 从 `timings` 属性 / `model_extra` / `__pydantic_extra__` 提取（`:160-180`）；**`_extra_body` 固定 `chat_template_kwargs.enable_thinking=False`（`:135-142`，no-think 保证可比）**；**400 超 ctx 重试**：`exceed_context_size_error` 且 `_retry<max_retry(3)` 时丢弃最早约 1/3 非 system 消息，若候选丢失全部真实 user query 则找回最早一条（Qwen3.5 multi_step_tool 模板要求，`:107-131`；`_is_real_user_query` 定义 `:147-158`）。**原生 `/completion` 未在 driver 封装**（e15_branch_concurrent 直接可用）。

**sampler / KVProbe**：`framework/sampler.py` —— `find_server_pid`（ss -ltnp + psutil 兜底）、`find_server_rss_mb`（psutil rss）、`find_server_gpu_mb`（pynvml 按 pid 匹配 usedGpuMemory，容器内降级 None）（`:21-75`）；`framework/kv_probe.py` —— `KVProbe` GET `/metrics/kv`（自动剥 /v1，`:25-29`）、`snapshot(tag)`、`kv_state/list_slots/erase_slot/clean_all_slots`（`:95-144`）、后台周期采样（`:147-158`）、`run_aggregate`（first/last/peak_used_cells，`:177-196`）。

**结果 schema**（`benchmark/runner/runner.py`）：`{"config": config.to_dict(), "summary": {...}, "scenarios": {...}}`（`:249-254`）落盘 `results/bench_<ts>.json`（`:257-264`）；`scenarios[name]` repeat=1 为 rows 数组、repeat>1 为 `{"runs", "protocol"}`（`:143-147`）；`--kv-probe` 追加 `kv_observations`。`metrics/metrics.py summarize`：p50/p95/mean/std/throughput/decode_tps/peak_rss/gpu/cache_hit_rate/recompute_tokens（`:93-107`）。

**config**（`framework/config.py:28-64`）：`ctx_size(2048) parallel(0=探测) repeat(1) warmup(0) seed(42) temperature(0.0) rounds(20) tool_steps(6) branch_rounds(5) long_rounds(40) kv_probe_enabled(False) replicate_mode("auto") kv_clean("erase") extra({})` 等；`configs/qwen35_4b_q8_production.yaml`（E14.1：ctx 4096、parallel 4、q8_0、kv_unified、-ngl 99、slot_save_path，q4_0 禁止）。

### 1.2 llama.cpp 侧（HEAD 4a699aaad）

**`/metrics/kv`**：路由 `tools/server/server.cpp:236`；handler 投递 `SERVER_TASK_TYPE_METRICS` 到推理线程（`server-context.cpp:5328-5364`）；组装点 `server-context.cpp:3104-3135`（`llama_memory_get_kv_stats` → 字段 `schema_version/capacity_bytes/used_bytes/used_bytes_valid/capacity_cells/used_cells/active_sequences/shared_cells/physical_sharing/shared_cells_semantics`）；`llama_kv_stats` 结构 `include/llama.h:727-751`；标准实现 `src/llama-kv-cache.cpp:734-800`（`capacity_bytes=total_size()`、cells 跨 stream 累加、`shared_cells`=引用数>1 的 cell、`physical_sharing=false` 恒 false）。

**hybrid 结构与统计**（`src/llama-memory-hybrid.h:19-94`）：`llama_memory_hybrid` 内部 `mem_attn`（llama_kv_cache）+ `mem_recr`（llama_memory_recurrent）双成员；**`get_kv_stats` 只委托 attention 部分**（`llama-memory-hybrid.cpp:190-194`，注释 "recurrent state is not part of the KV cache statistics"）；`seq_cell_stats` 同样只委托 attention（`:196-199`）；`memory_breakdown` 把 attn+recr 字节合并返回（`:182-188`，无法拆分）。

**recurrent 内存**（`src/llama-memory-recurrent.cpp:99-126`）：经 `filter` 仅对 **recurrent 层**分配 `cache_r_l{i}`（`[n_embd_r, n_rows]`）/`cache_s_l{i}`（`[n_embd_s, n_rows]`），`n_rows = mem_size × (1+n_rs_seq)`，**类型固定 F32**；`mem_size = max(1, n_seq_max)`（每 slot 一格，**与 n_ctx 无关**）；日志 `RS buffer size` 与 `size = ... (cells, layers, seqs rs_seq), R/S (f32)` 的 `layers` 打印为**总层数**（`n_layer=hparams.n_layer()`，`:29`），实际分配仅 recurrent 层（filter skip，`:76-82`）；`size_r/s_bytes()` 累加非空 tensor（`:709-716`）。

**capability gate（C1 前缀共享）**：启动期日志 `server-context.cpp:1497-1515`（memory null / 不支持共享 / **hybrid** / recurrent / SWA 依次 rejected，仅日志不 throw）；运行时生效门 `server-context.cpp:3851-3856`（`kv_prefix_share && kv_unified && supports_cross_slot_prefix_sharing && !hybrid && !recurrent && n_swa==0`）；默认实现 `src/llama-memory.h:122-124` 返回 false，**唯一 override true 为** `llama_kv_cache::supports_cross_slot_prefix_sharing`（`src/llama-kv-cache.h:156`）；hybrid 不 override → **Qwen3.5-4B 无条件被拒**。

**`/completion` 与 OAI**：`handle_completions_impl` 定义 `server-context.cpp:4890`；`post_completions`（/completion、/completions）委托 `:5621-5625`，OAI 端点（/v1/completions、/chat/completions、/v1/chat/completions）同一 handler（`server.cpp:242-246` 路由；`server-context.cpp:5633-5762`）；timings 填充 `server-context.cpp:568-576`（`prompt_n/cache_n/predicted_n/prompt_ms/predicted_ms` 等）；`t_prompt_processing` 定义 `:342` → `timings.prompt_ms`（`:571`，**TTFT 代理**，见 §5.1）。**并发模型**：`server_queue::start_loop`（`server-queue.cpp:125-215`）主线程阻塞循环 = 队首 task → `update_slots()` 单次遍历所有 slots 完成 pre_decode/decode/sample（`server-context.cpp:3398+`）→ 即 cont-batching 是**单线程调度**，HTTP 线程独立（`server.cpp:504/519`）；并行只存在于 ggml 内部线程与多 slot 批处理。

**memory_breakdown / position**：唯一输出点 `server.cpp:531` 退出时 `common_memory_breakdown_print`（`common/fit.cpp:817-940`），由 model 权重 + memory（KV/记忆）+ compute buffer 三部分构成；**hybrid 下 `context` 列 = attn+recr 合并，无法拆分**。运行期唯一 recurrent 细分 = 启动日志：`RS buffer size` 行（`llama-memory-recurrent.cpp:115`）+ `R/S (f32)` 分解行（`:123-126`）。

**checkpoint / COW**：`--checkpoint-reuse`（`arg.cpp:3684-3690`）在 hybrid/recurrent 上启动强制禁用（`server-context.cpp:1479-1487`，E12 实证 restore 后仍全量 prefill）；请求级 `checkpoint_save/restore`（`server-task.h:81-83`）；slot 级 `POST /slots/:id?action=save|restore|erase`（`server.cpp:275`；`server-context.cpp:5446-5477`）需 `--slot-save-path`；**无 COW**（physical_sharing 恒 false，共享是 metadata/bitset 级）。

**Qwen3.5-4B**：`llama_arch_is_hybrid` 含 `LLM_ARCH_QWEN35/QWEN35MOE`（`src/llama-arch.cpp:958-977`）；**层结构**（`src/models/qwen35.cpp:21-27`）：`is_recr_impl[i] = (i+1) % full_attn_interval != 0`（interval=4，GGUF 无 `LLM_KV_ATTENTION_RECURRENT_LAYERS` 时 fallback）→ **32 层中 8 attention（i=3,7,...,31）、24 recurrent**；4B 档 = `n_layer==32 && n_embd==2560`（`qwen35.cpp:31-34`）；memory 类型 `llama_memory_hybrid`（`llama-model.cpp:2256-2303`）；**`--cache-type-k/v` 只作用于 attention 部分**（`llama-model.cpp:2288-2296`），recurrent 固定 F32。

**recurrent 状态维度**（`src/llama-hparams.cpp:183-231`，KDA 分支）：
- `n_embd_r() = 3×(ssm_d_conv−1)×n_head×n_embd_head_kda`；4B 实测闭合值 = `3×(3−1)×32×128 = 24576`（ssm_d_conv=3、n_head=32、head_dim=128）；
- `n_embd_s() = n_embd_head_kda² × n_head = 128×128×32 = 524288`。
- 每 slot RS = `24 层 × (24576+524288) × 4 B = 52,690,944 B = 50.25 MiB` —— 与探针实测（§1.3）精确闭合。

**attention KV per-cell**（8 attention 层）：`n_kv_heads×head_dim×2×(每元素字节)`；q8_0（每 32 值 + 2B scale → 34/32 系数）= `8×8×128×2×34/32 = 17408 B/cell`；f16 = `8×8×128×2×2 = 32768 B/cell`。ctx 4096 → q8 68 MiB / f16 128 MiB —— 与探针实测精确闭合。

### 1.3 M0 探针实测（2026-08-09，RTX 4060 Laptop 8GB / driver 610.43.03）

方法：`build-cuda/bin/llama-server`（**version 8569/afbf375c6**；4a699aaad 仅新增测试文件、未改核心代码，故**核心代码与 HEAD 等价**；正式实验前仍重建到 4a699aaad 保证版本号一致，见 §9.5）加载 `models/qwen3-5-4B-Q4_K_M.gguf`，`-ngl 99 --ctx-size 4096 --kv-unified`，轮询 `/health` 就绪后抓 nvidia-smi 与启动日志，立即 SIGTERM（**零推理请求**）。日志留于忽略目录 `llama.cpp/tmp/m0_probe2_*.log`、`m0_metrics_probe*.log`（不入库）。

| parallel | ctk/ctv | KV buffer（attention，unified） | RS buffer（recurrent） | R/S 分解 | GPU used |
|---|---|---|---|---|---|
| 2 | q8_0 | 68.00 MiB | 100.50 MiB | R 4.50 + S 96.00 | 2974 MiB |
| 4 | q8_0 | 68.00 MiB | 201.00 MiB | R 9.00 + S 192.00 | 3074 MiB |
| 6 | q8_0 | 68.00 MiB | 301.50 MiB（= 6×50.25） | — | 3186 MiB |
| 8 | q8_0 | 68.00 MiB | 402.00 MiB | R 18.00 + S 384.00 | 3290 MiB |
| 10 | q8_0 | 68.00 MiB | 502.50 MiB（= 10×50.25） | — | 3402 MiB |
| 4 | f16 | 128.00 MiB | 201.00 MiB | R 9.00 + S 192.00 | 3132 MiB |

模型权重实测：`CUDA0 model buffer size = 2571.63 MiB`（+ `CPU_Mapped 497.31 MiB`，mmap 页面；GGUF 文件 2707514144 B = 2.52 GiB）。

`/metrics/kv` 实测（p4/q8_0/unified/ctx4096，无请求）：`capacity_cells=4096, capacity_bytes=71303168（=68 MiB，与 KV buffer 精确吻合）, used_cells=0, active_sequences=0, shared_cells=0, physical_sharing=false`。

**GPU 口径说明**：`nvidia-smi memory.total = 8188 MiB`（空闲 free=7795、used=40）；探针"GPU used"为 nvidia-smi `memory.used` 绝对值（含系统约 40 MiB）；模型+KV+RS 实际增量 ≈ used − 40。

**关键量化事实**：
1. **attention KV（unified）与分支数无关**：ctx 4096 时 q8_0=68 MiB、f16=128 MiB 固定池——"KV 预分配固定"在 hybrid 上同样成立（`capacity_bytes` 由 ctx 决定，§5.2）。
2. **recurrent RS buffer 随 parallel 线性增长：50.25 MiB/slot**（= 24 层 × 548864 × 4B；R 2.25 + S 48.0）——**hybrid fan-out 唯一随活跃分支数线性增长的显存项**。
3. **GPU 峰值完全在 8GB 预算内**：parallel 10 q8_0 = 3402 MiB（8188 的 41.5%），余量约 4.8 GiB。
4. `/metrics/kv` **只反映 attention 部分**；recurrent 内存无运行期接口（观测缺口见 §5.1）。

### 1.4 可提交探针证据（H2，benchmark/baseline/ 归档）

结构化证据：**`benchmark/baseline/m0_probe_thinking_token_parity_20260809.json`**（可复核，非 ignored tmp 日志）。内容摘要（2026-08-09，4B Q4_K_M / ctx4096 / unified / q8_0，无敏感公共消息）：

**thinking 渲染三路**（同一 messages + add_generation_prompt，`/apply-template`）：
| 配置 | prompt chars | tokens | 渲染尾部 | prompt sha256 |
|---|---|---|---|---|
| 不传（default） | 147 | 44 | `<|im_start|>assistant\n<think>\n` | `6fe4c9f6…b341ec7` |
| `enable_thinking=false` | 158 | 46 | `<think>\n\n</think>\n\n`（空 think 块，no-think） | `91c6bbac…7a70a5` |
| `enable_thinking=true` | 147 | 44 | 同 default | `6fe4c9f6…b341ec7` |

→ server 默认 thinking 开启；显式 false 实际生效（渲染空 think 块）。

**token parity**（no-think 配置，同一 messages）：
| 路径 | tokens |
|---|---|
| `/apply-template` → `/tokenize`（`add_special:false`） | 46 |
| `/apply-template` → `/tokenize`（`add_special:true`） | 46（**幂等归因（v7 完整措辞）**：该 GGUF `tokenizer.ggml.add_bos_token=false`、`bos_token_id` 缺失、`add_eos_token` 字段缺失（默认 false）→ 无 BOS token 可加，add_special 无可加；**仅对本模型/配置成立，换模型必须重验**，GGUF 元数据实测见下） |
| 真实 `/v1/chat/completions`（max_tokens=1, temp=0, seed=42, no-think）`usage.prompt_tokens` | 46（finish_reason=length） |

→ **parity 成立、偏差 0**：M0 一致性门禁 = 比较 chat `usage.prompt_tokens` vs apply-template→tokenize(`add_special:false`)（§4.2 步骤 2/3）。**GGUF 元数据实测（2026-08-09）**：`tokenizer.ggml.add_bos_token=false`、`tokenizer.ggml.bos_token_id` 缺失（None）、`tokenizer.ggml.add_eos_token` 字段缺失（默认 false）、`eos_token_id=248046`、`tokenizer.ggml.model="gpt2"`——**add_special 幂等的真实原因是无 BOS token 可加（add_bos_token=false 且无 bos_token_id）**；**结论仅限本 GGUF/配置，换模型必须重验**。

---

## 2. 现有场景为何不是"真实多路径决策"

| 维度 | 现状（证据） | 与真实 Agent fan-out 的差距 |
|---|---|---|
| 分支来源 | `branch.py:15-18` 固定 `BRANCH_QUESTIONS`（A/B 两问）+ `:53-65` 固定"继续完善方案X"循环；`e15_branch_concurrent.py:99-114` 脚本拼接 `PREFIX_CORE+BRANCHES[bX]`；`branch_pressure.py` 固定 `SEQUENCE` | **模型从不决策分叉**，只是固定 prompt 上的续写；无"决策点→分支"语义 |
| 分支首 token | 人为设计互异（保证 LCP 精确终止） | 真实分支由内容决定，LCP 自然可变 |
| 并发形态 | HTTP 并发（barrier+ThreadPoolExecutor），但 server 单线程调度（`server_queue::start_loop`+`update_slots` 单循环，§1.2） | "并发"是队列化并发，非并行推理；fan-out 测量的是调度/批处理行为而非并行计算 |
| 模型域 | E15.1 用 TinyLlama stories260K attention-only（`e15_branch_concurrent.py` 目标模型） | **不可外推 4B**：4B 是 hybrid，C1 前缀共享被 capability gate 永久拒绝（`server-context.cpp:1497-1515/3851-3856`），无 shared_cells>0 可言 |
| 内存语义 | E15.1 测的是 metadata 前缀共享（shared_cells/recompute），且无 COW（physical_sharing 恒 false） | hybrid 下 fan-out 内存主成分是 **recurrent state**（§1.3 实测 50.25 MiB/slot 线性），与 attention KV 共享机制**不同量级、不同机制** |
| 4B gate 覆盖 | benchmark 侧无 4B 决策 workload；`e15_3_b1_paired.py` 是 preprocessor 无损门禁（无关） | 缺少"4B 上真实分支语义 + 内存归因"的基线 |

结论：**现有 branch 场景全部是"脚本模板分支"，不是"模型决策多路径"**；TinyLlama E15.1 的共享收益证据不能外推到 4B hybrid（capability gate + 无 COW + recurrent 主导）。M0 需要新的 workload 语义。

---

## 3. M0 workload 设计（DESIGN）

### 3.1 语义（真实多路径决策，temp=0 可稳定复现）

**决策点协议（确定性）**：公共上下文末尾要求模型输出唯一合法分支指令，枚举受限：
```
ACTION: branch(b1|b2|b3|b4|b5|b6|b7|b8)
```
- 决策点 prompt 采用枚举式约束（"只能输出上述 ACTION 之一，不得输出其他内容"），规避自由文本指令漂移；
- `temperature=0, seed=42` 固定 → 分支选择在**给定公共上下文下确定性**；用 0.8B（CPU）离线校准决策点 prompt 的合法输出率，4B（GPU）正式；
- **fallback 与判定（审查修订 v4：专用验证与正式矩阵分离）**：
  - **专用稳定性验证**（G-M0-1 数据源）：4B 决策点合法输出率 <100%（2 次独立会话 × ≥10 次，无 ACTION 或 `finish_reason=length` 均计 INVALID_DECISION）→ **G-M0-1 = FAIL → 顶层 `HOLD_NOT_VALIDATED`**（规则 `G1_FAIL`）——**不称真实决策 PASS**；
  - **正式矩阵**：单 rep 决策失败**只记录该 rep `status=INVALID_DECISION`**（rep 级字段，**不是顶层 verdict**，顶层 verdict 仅用 §5.1 五值枚举），并使用**固定路由 fallback**（脚本按固定规则选分支）**继续内存压力实验**（不中断、不重试），该 rep **不计入 G-M0-1**（G-M0-1 只用专用验证数据）；fallback 触发时在结果 schema `decision_fallback: true` 与顶层 `verdict` 显式标记（§5.1），报告不得宣称真实决策。
  - **INVALID_DECISION 谓词（v8 定稿，可直接编码，三处同一）**：`decision_invalid ⇔ (finish_reason == "length") OR (输出不含合法 ACTION)` —— **任一条件即 rep 判定 `INVALID_DECISION`**（rep 级，非顶层 verdict）；即使截断文本（length）已含合法 ACTION 也判 INVALID_DECISION（截断输出不可信）。专用验证与正式矩阵均用同一谓词。
- 分支语义稳定：分支 prompt = 公共上下文 + 决策点输出 + 分支专属任务 + 分支专属 canary；分支任务**不依赖模型继续决策**（后续轮次为固定后续推理/工具调用）。

**分支-工具-回收流**：
```
[公共上下文+决策点] → 模型输出 ACTION: branch(bX)
  → N 个分支请求并发发出（barrier；分支内容 = 公共前缀 + 各自任务 + canary）
  → 每分支 M 轮：工具结果注入（复用 tool_call 的 ACTION 协议或直接注入）→ 后续推理
  → 回收：全部分支完成后 erase（/slots/:id?action=erase，KVProbe.erase_slot）
```
- **identity 定义**：`branch`（b1..b8）是内容分支；`session` 是 workload 实例；`thread` 是并发出线程。canary 隔离：每分支专属 `CANARY-BX-<hex>` 注入分支 prompt 尾部，输出检测跨分支泄漏（复用 E15.1 G5 模式）。
- **工具注入（v5 定稿）**：**M0 不使用 OpenAI `tools` schema**（避免两条路径（apply-template vs chat）渲染差异与精确计数遗漏）；分支工具观测以**固定、无敏感、可审计的消息注入**（如 `<tool_response>` 文本块，复用 tool_call.py 协议）实现——精确计数请求因此不遗漏 tools；若未来改用 `tools` schema，则 /apply-template 与 /tokenize 两条路径必须携带**同一 tools schema**（并重新验证 token parity，§4.2 步骤 3）。

### 3.2 模块边界（**单一架构定稿**，审查修订）

**定稿：独立 M0 runner（与 E15 runner 约定一致），不注册第五场景**。理由：fanout 的决策/并发/门禁逻辑独立于现有四场景的"累积多轮对话"范式；注册场景需改 `config.py`/`runner.py`（场景发现、summary 处理、`extra` 语义），引入兼容性成本；独立 runner 与 `e15_branch_concurrent.py` 同为"专项 runner"先例，无第二套 config 约定（CLI 直接参数化，不走 BenchmarkConfig 扩展）。

- **复用**：`framework/driver.py`（OAI 封装/timings 提取）、`framework/kv_probe.py`、`framework/sampler.py`、`metrics/metrics.py summarize`、`e15_branch_concurrent.py` 的 server 生命周期（`start_server/stop_server`，`:548-599`）与 gate_verdict 纯函数模式。
- **新增（下一实现阶段，本阶段不写）**：
  - `benchmark/framework/fanout_prompts.py` —— 纯函数：决策点 prompt 构造（枚举约束）、分支扩展、canary 注入、`ACTION: branch(bX)` 解析、**token 精确计数（§4.2：/apply-template + /tokenize）**、KV 预算校验（§4 公式，fail-fast）。
  - `benchmark/runner/m0_fanout_runner.py` —— CLI + 矩阵编排 + server 生命周期 + 并发（barrier+ThreadPoolExecutor）+ gate + 落盘（唯一顶层 schema，§5.1）。
  - `benchmark/tests/test_fanout_prompts.py`、`benchmark/tests/test_m0_fanout_runner.py` —— 纯函数 + mock server e2e。
- **不改**：`config.py`、`runner.py`、`workload/__init__.py`、任何既有 workload（可回滚：删除新增文件即完全回滚，默认无行为改动）。
- **失败降级（v12 定稿）**：server 启动/health 失败（重试 3 次后）或 `/metrics/kv` 不可用等**基础设施失败 → 落盘 preflight 五键结果（`phase=preflight`、`preflight_status=FAILED`、verdict 按规则 `PREFLIGHT_INFRA` = `INVALID_RESULTS_SCHEMA_OR_INFRASTRUCTURE`）并停止**——**不使用"run 级 INVALID"措辞**（rep 级 status 仅 OK/INVALID_DECISION/ERROR，见 §5.1）；预算逐 unit（§4/§6，超限 unit 排除、**全部 24 候选经精确 tokenizer 复核被拒（v38）** → preflight `ALL_BUDGET_REJECTED` = `HOLD_NOT_VALIDATED`；preflight_rejections 永不含静态 3 组合）；决策点 fallback → **`HOLD_NOT_VALIDATED`**（规则 `DECISION_FALLBACK`/`G1_FAIL`）。

### 3.3 请求接口合同（审查修订 v3）

| 项 | 合同 |
|---|---|
| 统一接口 | **OAI `/v1/chat/completions`**（`driver.chat`）——决策点与分支请求同一接口；**thinking 合同（v5 实证，§1.4 证据）**：server 默认 **thinking 开启**（apply-template 不传时渲染 `<|im_start|>assistant\n<think>\n`），必须显式 `chat_template_kwargs.enable_thinking=false` 才渲染空 think 块（`<think>\n\n</think>`）实现 no-think——该字段经 `oaicompat_chat_params_parse` 解析**实际生效**（`server-common.cpp:1095-1102` 覆盖默认；可复核证据 `benchmark/baseline/m0_probe_thinking_token_parity_20260809.json`）；M0 沿用 `driver._extra_body`（`driver.py:135-142`）显式 false，并以 token parity 一致性门禁（§4.2 步骤 3）执行验证；原生 `/completion` 仅为 e15 对照既有路径，M0 不用 |
| 确定性 | `temperature=0, seed=42`；`--n-predict`：**决策点 16（v3：由 8 上调，保证 `ACTION: branch(bX)` 输出完整且留边界）**、分支轮 64（CLI 可配 `--decision-n-predict/--branch-n-predict`） |
| 停止判定 | OAI `finish_reason`（stop/length）；**INVALID_DECISION 谓词（v8）**：`(finish_reason=="length") OR (输出不含合法 ACTION)` → 任一即该 rep `status=INVALID_DECISION`（rep 级，非顶层 verdict；length 截断即使含 ACTION 也不可信），走固定路由 fallback（§3.1）、**不计入 G-M0-1**（G-M0-1 仅用专用 2 次独立会话 × 每次 ≥10 请求（总数 ≥20）数据，§8） |
| 可比性 | 同 seed/temp 下输出 `tokens` 数组 + `content_sha256` 逐字节比较（e15 G1 模式）；决策点与分支均在请求级记录 prompt_tokens/completion_tokens/timings |
| slot 控制 | 默认不强制 `id_slot`（server 自动调度）；若需确定性 slot 映射（决策点 slot 0、分支 1..N），经 `extra_body["id_slot"]` 透传（见下方 driver 扩展，默认缺省保持旧行为） |
| timings | `driver._extract_timings`（`:160-180`）；TTFT 代理 = `timings.prompt_ms`（`server-context.cpp:571`），见 §5.1 |

**driver 最小兼容扩展（审查修订 v4 —— `_retry` 保持位置参数）**：当前 `driver.chat(messages, _retry=0)` **不支持 temperature/seed/max_tokens 透传**（`driver.py:62`）。设计签名：

```python
def chat(
    self, messages: List[dict], _retry: int = 0,   # _retry 保持位置参数（`*` 之前），兼容现有递归调用
    *,
    temperature: Optional[float] = None,   # → create(temperature=...)，None 不传（保持旧默认）
    seed: Optional[int] = None,            # → create(seed=...)
    max_tokens: Optional[int] = None,      # → create(max_tokens=...)
    extra_body: Optional[Dict[str, Any]] = None,  # 与 _extra_body() 合并（id_slot 等），None 不合并
) -> Dict[str, Any]:
```

- **兼容性（v4 修正）**：`_retry` 位于 `*` **之前**（位置参数区），现有调用全部不受影响——外部 `driver.chat(messages)` 与内部递归 `self.chat(messages, _retry + 1)`（`driver.py:132`，位置传参）均无需改动；新增参数全部 keyword-only 且默认 None → 不传时行为与旧版逐字节一致（`tests/test_driver.py` 回归用例锁定：旧签名调用方式全绿）；
- **实施文件**：`benchmark/framework/driver.py`（仅 `chat` 签名 + `_extra_body` 合并逻辑）；`benchmark/tests/test_driver.py` 新增用例（默认 None 旧行为、透传生效、**旧调用 `chat(msgs, 2)` 位置传 `_retry` 兼容**、keyword-only 参数拒绝位置传参、extra_body 合并优先级）；
- **OpenAI 参数映射**：`temperature/seed/max_tokens` 直接映射 `chat.completions.create` 同名参数；`extra_body` 与既有 `_extra_body()` 字典合并（调用方优先）。
- **retry 构造合同（v12）**：当前 `Driver.__init__` 的 `max_retry=3`（`driver.py:36`）与 `OpenAI(base_url, api_key)`（`:58`）——OpenAI SDK **隐式默认 `max_retries=2`**（对连接错误/429/5xx 重试），须显式可控。设计：
  - `Driver.__init__` 新增可选 **`sdk_max_retries: Optional[int] = None`**（None 时保持既有 SDK 默认行为，**不改变其他 workload**）；M0 显式 `sdk_max_retries=0`（SDK 层不重试，统一由外层 transient wrapper 控制，§3.8）；
  - M0 实例化 **`Driver(max_retry=1, sdk_max_retries=0)`**：内层 context400 最多 2 次 wire（首 + 内部重试 1），外层 transient 3 次逻辑调用 → **理论最大 6 wire**（§3.8 上限不变）；
  - 实施文件：`driver.py`（`__init__` 加 `sdk_max_retries` → 传给 `OpenAI(max_retries=...)`）+ `tests/test_driver.py`（默认 None 保持 SDK 行为、显式 0 禁用、max_retry=1 实例化语义）；§3.8 retry 分层引用本构造。
  - **context400 递归透传（v13）**：`chat()` 内部递归 `self.chat(messages, _retry + 1)`（`driver.py:132`）**必须透传 `temperature`/`seed`/`max_tokens`/`extra_body`**（签名改为 `self.chat(messages, _retry + 1, temperature=..., seed=..., max_tokens=..., extra_body=...)`）；测试验证**重试前后参数完全保持**（mock 记录两次 wire 请求参数一致）；旧调用（单参数/位置 `_retry`）兼容仍保留。

### 3.4 请求序列与计时

每 replicate：决策点请求（slot 0）→（可选 erase 决策点 slot，见 §4 预算）→ N 分支并发（barrier，slot 1..N）→ 每分支 M 轮 → `erase` 全部 → 断言回基线。warmup≥1、reps≥5（与 `benchmark/README.md:145` 约定一致），中位数报告。

### 3.5 执行顺序（v12 定稿，H1/M2 收口）

```
1. server 就绪（--health 200）且 /metrics/kv、/apply-template、/tokenize 端点可用
   —— 启动/health/端点探测失败 → 基础设施失败 → 落盘 preflight INVALID_RESULTS_SCHEMA_OR_INFRASTRUCTURE（M1）
2. 每长度桶分别校准：P 模板与 B/工具观测模板 apply-template+tokenize → parity 对比（max_tokens=1 chat，豁免 INVALID_DECISION 谓词）
   —— **数值 parity mismatch 绝不单桶 fail-fast（v38）：继续完成全部 6 项（short/medium/long × P/B）；每项 mismatch 检测时立即写结构化 parity_mismatch 条目且该项计入 completed；全部完成后若任一偏差非零 → PARITY_MISMATCH/HOLD（parity_ok=false、completed 严格 6 项全集、errors 记 parity_mismatch）**
   —— **仅端点/请求基础设施失败中断 → parity_ok=null、completed 为已完成有序前缀（v38）；混合场景：此前 mismatch 条目保留、再写基础设施 error、停止后续项、parity_ok=null——证据不丢（测试覆盖）**；**混合场景顶层 `preflight_reason` 规则（v39）**：只记录导致中断的基础设施 code（`PREFLIGHT_INFRA`）；**此前 parity_mismatch 仅保留在 `parity_progress.errors`、不进 `preflight_reason`**（测试㉑ 正反覆盖）
   —— 校准期间请求异常/端点消失 → 基础设施失败 → preflight INVALID_RESULTS_SCHEMA_OR_INFRASTRUCTURE（M1）
3. 精确 budget 校准（全部候选 unit 逐一 tokenize 实测）
   —— 单 unit 超预算 → 记入 meta.preflight_rejections[] 并排除该 unit；其余合法 unit 继续（M3-8）
   —— **全部 24 候选经精确 tokenizer 复核被拒（v38）→ 落盘 phase=preflight HOLD_NOT_VALIDATED（静态 3 组合预筛排除、不参与）**
4. erase 全部 slot → /metrics/kv used_cells==0 && active_sequences==0（attention 回基线）
5. G-M0-1 专用稳定性验证：2 次**独立 server 启动** × 每次 ≥10 请求（总数 ≥20）
   —— 证据落盘 meta.decision_validation（M2）；**<100% → G-M0-1=FAIL，但不停止**（v11 语义保留，v12 补失败路径）：
      formal 仍运行且**仍请求模型**，只有满足 INVALID_DECISION 谓词的 rep 才走 fixed fallback（`decision_fallback=true`），
      **合法 rep 保持 `status=OK`、`decision_fallback=false`**——不强制全部 rep fallback；
      G-M0-1 gate 已 FAIL → 顶层为 `HOLD_NOT_VALIDATED` 或更高优先级的 INVALID 类（规则 `G1_FAIL`/`DECISION_FALLBACK`，若并存 `REP_ERROR`/`FORMAL_INCOMPLETE` 则取 INVALID 类）
   —— **验证阶段失败路径（v12）**：
      a) 验证中普通请求 ERROR/invalid：**如实记入 meta.decision_validation**（含结构化 error_summary），G-M0-1=FAIL；
         **验证请求 in-flight 时 server 崩溃（v23 边界定稿）**：该请求**计入 `requests` 与 `error_count`**，`error_summary` 加 `{code: "server_crash", stage: "validation", count: 1}`；
         **边界（v28 统一）**：**两次验证 session 均达到目标 requests≥10 且每个请求都有 OK/INVALID_DECISION/ERROR 记录（包括最后 in-flight 崩溃但完整）→ 证据完整、继续 formal，不要求 server health 正常**（崩溃本身不影响证据完整性）；**5b 三触发（v28 权威定义）**：① 启动失败、② 崩溃且 session 不完整（requests<10）、③ **decision_validation 序列化失败**——**均立即停止验证（第一/第二 session 不完整都不启动 formal）**；崩溃只有 session 不完整时走 5b（完整则按 5a）；启动失败/序列化失败独立触发 5b；**`decision_validation`**：**任一验证 session 启动失败即停止验证阶段**（§3.5 步骤 5b 策略）：无任何 session/请求证据 → `null`；已有第一 session 或部分当前 session → `partial`（**首 session requests=0 时无证据 → null，≥1 且 <10 → partial**）；
         **闭合示例（v21）**：`requests=10, valid=8, invalid=1, error_count=1`（10=8+1+1）；`invalid_length=1, invalid_no_action=0`（1=1+0）；`finish_reasons={stop: 8, length: 1}`（sum=9=10−1；length=1=invalid_length）；`error_summary=[{code: "server_crash", stage: "validation", count: 1}]`（完整结构，v22；sum=1=error_count）；`output_hashes` 长度 = 9 = valid+invalid（8+1）；**保证全部等式/不变量成立**；测试覆盖；
         若**两次验证 server 均能完成**（health 正常、会话完整）→ **继续 formal**；
      b) 验证 server **启动失败 / 崩溃且 session 不完整 / decision_validation 序列化失败** → **phase=preflight 基础设施失败**（规则 `PREFLIGHT_INFRA` = `INVALID_RESULTS_SCHEMA_OR_INFRASTRUCTURE`）；**三触发无歧义（v29）**：崩溃大类**仅 session 不完整走 5b、完整按 5a**；**启动失败 / 序列化失败独立触发 5b**（与 session 完整性无关）；
        **"证据无法落盘"路径（v28 定稿）**：
        a) **decision_validation 序列化异常**（如 `json.dumps` TypeError）→ **整体证据原子丢弃为 `null`（v28：即使已有第一 session 也写 null，不保留部分对象，避免写入不可验证结构）**，结果 writer 可用时**落盘 preflight 五键**、`preflight_reason=validation_incomplete`、**`parity_ok=true`（parity 已完整通过后发生，v35 总规则）、`token_count_method=apply-template+tokenize`（v35 总规则）、`parity_progress` 保留完整实际进度**、**正常退出 0**——**不再混入 validator 结构非法**；
        a2) **序列化成功但 schema validator 判非法** → 规则 **`SCHEMA_INVALID`**、**保留构造出的 JSON/诊断证据**（不丢弃）、顶层 `INVALID_RESULTS_SCHEMA_OR_INFRASTRUCTURE`——独立测试 mock/断言（⑬b-f）；
        b) **最终结果文件本身 I/O 写失败** → **兜底路径（v28：5b/任何结果落盘动作失败的兜底，不是新 5b 触发）**：stderr 输出固定错误码（`IO_WRITE_FAILED`）、**非零退出（常量 `EXIT_IO_WRITE_FAILED = 74`，Linux `EX_IOERR`）**、明确为**外部基础设施阻塞**、**不虚构结果文件**（测试断言 exit 74）；
        **启动失败策略（v25）**：**任一验证 session 启动失败即停止验证阶段，不再启动后续 session**；`decision_validation`：**尚无任何 session/请求证据 → `null`**；**已有第一 session 或部分当前 session → `partial`**（删除"两个均失败"依赖，单 session 启动失败即触发）；**启动失败 code 映射（v26，复用 formal 同一三 code）**：进程 spawn 失败/早退 → `server_crash`；进程存活但 health 超时 → `health_failed`；健康但必需端点缺失 → `endpoint_unavailable`；
         `decision_validation` 可为 `null` 或 partial（validator 有则验证结构，**不强制 2 sessions**）；
      c) **仅 formal 才强制完整 2 sessions × 每次 ≥10**（§5.1 phase 约束）
6. formal 矩阵（按 `cache profile × fanout × control` 分组启动，共 2×3×2 = 12 个独立 server group；每组 `parallel = fanout + 2`，运行该 fanout 下合法长度桶（fanout 2 → 三桶 short/medium/long、fanout 4 → 两桶 short/medium、fanout 8 → 一桶 short）；**每组独立启动/回基线/停止，崩溃归属到该 group 并使 `matrix_complete=false`；不得用单 server 切换 cache type/parallel**；**group 失败 phase 规则（权威）**：**仅第 1 个 formal group 启动失败 → `phase=preflight`、规则 `PREFLIGHT_INFRA`、`modes={}`、`gates={}`、`executed_units=0`、`matrix_complete=false`**（保留完整 decision_validation，测试㉖ 明确首组）；**第 2+ group 启动失败或任一 group 运行中崩溃 → `phase=formal`：保留已完成 group/unit、`matrix_complete=false`、`FORMAL_INCOMPLETE` 优先（含 unit 间隙）、停止剩余 group**（测试㉗ 明确中途组）；**group 启动失败/崩溃统一停止剩余矩阵，崩溃归属到该 group**；**仅首 group 启动失败才 `executed_units=0` / modes 可空**；24 单元 = 12 group × 桶数；每单元 warmup 2 + reps 5；**FORMAL_INCOMPLETE 集合断言见 §5.1（规则表与 validator）与测试㉗**）：
   —— **使用新的独立 server 启动**（与验证会话分离），先确认 attention 基线（used_cells==0）再开始（M2 防污染）；
   —— **formal group 失败 phase 规则（权威）**：**第 1 个 formal group 启动失败 → `phase=preflight`、规则 `PREFLIGHT_INFRA`、`modes={}`、`gates={}`、`executed_units=0`、`matrix_complete=false`**（§3.7）；**第 2+ 个 group 启动失败或运行崩溃 → formal incomplete（FORMAL_INCOMPLETE，保留已完成 group/rep）**；**启动失败 code 固定（v20）**：进程 spawn 失败/早退 → `server_crash`；进程存活但 health 超时 → `health_failed`；健康但必需端点缺失 → `endpoint_unavailable`
```

**前置失败分类（v10）**：步骤 1–4 的失败 → 落盘 preflight 五键结果并停止（verdict：基础设施 → `INVALID_RESULTS_SCHEMA_OR_INFRASTRUCTURE`；parity 数值非零/全部 24 候选经精确 tokenizer 复核被拒 → `HOLD_NOT_VALIDATED`）；步骤 5 的 G-M0-1 <100% **不是前置失败**（formal 继续，H1）。

### 3.6 transient retry 与端点归因（v12 定稿，M1/M3）

- **transient retry（M3）**：formal 与校准请求中，**连接错误 / 请求超时 / HTTP 5xx → 最多重试 2 次（总尝试 3 次），确定性退避 0.25s / 0.5s**；**HTTP 4xx 与"畸形成功响应"（200 但缺字段/JSON 畸形）不重试**——ctx 超限由预算保证（§4.2），driver 既有 400 context fallback 保持原行为，但重试/回退后仍失败即 `status=ERROR`（→ `any_rep_error`）；
- **验证请求 retry 规则（v28）**：验证请求**同样使用 transient wrapper**；失败归因**三路（当前版本）**：① **`Popen.poll() != None` 确认进程已退出 → in-flight 请求立即归因 `server_crash`，不再 transient retry**；② **进程仍存活 → 连接/超时/5xx 按 0.25s/0.5s 重试，耗尽后归因 `connection_error`/`timeout`/`http_5xx`**；③ **首次异常时进程存活、但重试期间进程退出 → 归因 `server_crash`**（不再继续重试）；测试⑩ 补三路用例；
- **重试耗尽 → rep ERROR → 规则 `REP_ERROR`（any_rep_error）**；校准请求重试耗尽 → 按 M1 归因（见下）；
- **端点归因唯一化（M1）**：
  - server 启动失败 / `/health` 失败 / 端点（/metrics/kv、/apply-template、/tokenize）探测失败、**校准期间请求异常或端点消失** → **基础设施失败 → preflight `INVALID_RESULTS_SCHEMA_OR_INFRASTRUCTURE`**；
  - **仅当端点正常响应但 token parity 数值非零** → `HOLD_NOT_VALIDATED`（preflight）；
- **测试覆盖**：5xx 后重试恢复（总尝试 ≤3）、重试耗尽 → ERROR、4xx 不重试、畸形 200 不重试、校准端点消失 → 基础设施归因。

### 3.7 server 生命周期与崩溃处理（v12 定稿）

- **生命周期**：
  1. **校准 server**：启动 1 次（步骤 1–4 全在此 server 完成），`finally` 停止；
  2. **G-M0-1 验证 server**：**两次独立启动/停止**（每次 ≥10 请求），每次 `finally` 停止；
  3. **formal 12 server groups（顺序 15 次进程 = 校准 1 + 验证 2 + formal 12 groups）**：每个 formal group 独立启动/回基线（`used_cells==0`）/运行该 group 合法桶/停止；**profile/fanout/control 均由启动参数决定，不在 server 内切换**；
     - **formal group 失败 phase 规则**：**第 1 个 formal group 启动失败 → `phase=preflight`、规则 `PREFLIGHT_INFRA`、`modes={}`、`gates={}`、`executed_units=0`、`matrix_complete=false`**（保留完整 decision_validation，测试㉖ 明确首组）；**第 2+ group 启动失败或任一 group 运行中崩溃 → `phase=formal`（保留已完成 group/unit）、`matrix_complete=false`、`FORMAL_INCOMPLETE` 优先、停止剩余 group**（测试㉗ 明确第 2+ group/运行中崩溃）；
  - **端口策略**：由 runner 分配可用端口（`socket` 绑定探测或端口池），不硬编码单一端口；启动前后做 health 检查与进程退出检查（`pgrep -x llama-server` 确认无残留，复用 e15 生命周期骨架 `e15_branch_concurrent.py:548-599`）。
  - **崩溃后回收（v29）**：**任何 server（校准 / 验证 ×2 / formal）**崩溃后，runner **必须在 `finally`/清理路径回收进程对象、子进程与端口**——**清理固定序列**：**`poll()`** → **存活则 `terminate()`** → **`wait(timeout=2)`** → **仍存活则 `kill()`** → **`wait()/reap`**；**已退出则直接 `wait()`**；**不向已退出进程 send_signal**；**捕获 `ProcessLookupError`**（进程已消失时静默）；**所有失败路径（含 IO_WRITE_FAILED 兜底）均仍执行 finally 清理 tmp-dir/server/端口（v28）**；**results 目录主文件与 sidecar 不清理（v29）**；最终检查**子进程（`pgrep -x llama-server`）与端口**无残留（端口释放后可供下次启动复用）；测试⑰ **四阶段（校准/验证×2/formal）全覆盖**，并断言**已退出不调用 terminate、terminate 超时转 kill、ProcessLookupError、端口可复用**。
- **formal server 崩溃/端点消失（v12）**：**立即停止后续矩阵**（不再启动新 unit）；**保留已完成 rep**；当前进行中 rep 写 `status=ERROR`（`error_type="server_crash"`、可选 `error_stage`/`error_bucket`；**不保存原始异常/路径/URL/prompt**）；**`meta.matrix_complete=false`**、**`executed_units = len(complete_unit_ids)`（v46：不硬断言 < planned；若崩溃发生在某 unit 第 5 个 rep 且 rep_index 0..4 均有记录（含 ERROR），该 unit 计 complete，v45 口径）**、`notes` 记录中断；落盘 formal 五键结果；顶层 → 规则 **`FORMAL_INCOMPLETE`** = `INVALID_RESULTS_SCHEMA_OR_INFRASTRUCTURE`（**绝不 PASS**；依赖完整矩阵的 gates（G-M0-6 复现、G-M0-4 归因）置 `NOT_APPLICABLE`）。
- **单 rep ERROR 继续（v12）**：**server 仍健康时**，单个 rep ERROR 后**继续剩余 matrix**（不立即停止）；ERROR rep 保留在落盘数据、不参与 G-M0-4 归因；矩阵正常跑完后 `matrix_complete=true`，顶层由规则 `REP_ERROR` → `INVALID_RESULTS_SCHEMA_OR_INFRASTRUCTURE`；**仅 server 崩溃/端点消失才立即停止并置 `FORMAL_INCOMPLETE`**。

### 3.8 retry 分层（v12 定稿）

- **外层 transient wrapper（v25 接口定稿）**：**接口明确接收 `server_process: subprocess.Popen`（或等价 `is_alive()` 回调）**；只处理**连接错误 / 请求超时 / HTTP 5xx**，**最多 3 次逻辑调用**（首次 + 重试 2 次，确定性退避 0.25s / 0.5s）；**每次异常及每次重试前都 `poll()` 进程**：**首次异常时进程存活、但重试期间进程退出 → 归因 `server_crash`**（不再继续重试）；
- **内层 driver**：每次逻辑调用内，driver 对 **context 400**（`exceed_context_size_error`）最多**内部重试 1 次**（丢弃最早消息后重试，`driver.py:107-131` 既有逻辑）；**理论最大 wire request = 3 × 2 = 6 次**；
- **不外层重试**：HTTP 4xx（除 context 400 内层处理）与"畸形 200"（200 但缺字段/JSON 畸形）→ 直接 `status=ERROR`；
- **精确预算使 context 400 仅为安全兜底**（P/B 由 tokenize 实测，§4.2）；重试耗尽 → rep ERROR → `any_rep_error`（规则 `REP_ERROR`）。

---

## 4. 实验矩阵与 KV 预算（审查修订）

### 4.1 对照

| 对照 | 配置 | 目的 |
|---|---|---|
| **A = `modes.off`（full-prefill baseline，v47 显式建立）** | 无 `--kv-prefix-share`，每分支全量 prefill（`cache_prompt` 生效但无跨 slot 共享） | 分支内存/延迟基线（used_cells 峰值 = 决策 + 各分支完整占用，见 §4.2 公式） |
| **B = `modes.on`（当前 C1 请求，v47 显式建立）** | `--kv-prefix-share --kv-prefix-share-min-lcp 64`；**4B 预期 capability rejected**（`server-context.cpp:1497-1515` 日志 + shared_cells 恒 0） | 安全对照：验证 gate 行为契约（日志出现、共享不生效），不宣称收益 |
| **C. q8_0 可复用接口** | `--cache-type-k/v q8_0`（= `qwen35_4b_q8_production.yaml` 的 KV 形态） | M0 后 q8_0 分支容量实验的同一接口；本阶段仅记录接口与容量数字（68 MiB @ctx4096，§1.3） |

**术语统一（v47）**：全文对照优先使用 `modes.off` / `modes.on`（A/B 仅在此表显式建立映射，不作为后续唯一引用）；`modes.off`=对照 A=full-prefill baseline、`modes.on`=对照 B=当前 C1 请求（--kv-prefix-share）。

**明确不做**：checkpoint/COW 实现（`--checkpoint-reuse` 在 hybrid 上已禁用，slot save/restore 为 E12 原型范畴；M0 只做内存归因基线）。

### 4.2 unified KV 严格预算公式与 fail-fast（审查修订 v3：精确 token 计数）

**容量事实**：unified KV 池 `capacity_cells = ctx_size = 4096`（§1.3 实测），所有 slot 共享 cell 池；4B 上 C1 被 gate 拒绝 → **on/off 对照实际均为 full-prefill**（每分支从零 prefill 完整上下文）。

**token 精确计数（审查修订，不再以 chars 估算为门禁；chars/2.5 仅为旧估算对照）**：
- **预算函数**：`budget(P, B, N) = P + N×(P+B)`（P=决策点/公共前缀 token 数，B=每分支后缀 token 数，N=fan-out）；约束 `budget ≤ 4096×0.85 = 3481 cells`（安全余量 15%）。
- **P/B 的取值必须来自 chat 模板渲染后的真实 prompt 精确 token 计数**（实现时）：
  1. `POST /apply-template`（`server.cpp:263`；`server-context.cpp:5775-5781`）：body `{messages, add_generation_prompt: true, chat_template_kwargs: {enable_thinking: false}}`，经 `oaicompat_chat_params_parse`（与 /v1/chat/completions 同一解析路径，**thinking=false 模板一致**；字段生效证据：`server-common.cpp:1095-1102` 解析覆盖 + 探针证据 §1.4）→ 响应 `{"prompt": "<渲染后完整 prompt>"}`；
  2. `POST /tokenize`（`server.cpp:261`；`server-context.cpp:5828+`）：body `{content: <prompt>, add_special: false}` → 响应 `{"tokens": [id, ...]}`，`P/B = len(tokens)`；
  3. **一致性门禁（v7：token parity + 校准/清理协议）**：
     - **执行时机（独立前置校准）**：正式矩阵开始前、server 就绪后，**对每个长度桶分别校准两个模板**：公共决策点 P 模板与分支 B/工具观测模板（各自 messages 构造）；比较真实 chat 请求（`max_tokens=1, temp=0, seed=42`, no-think）的 `usage.prompt_tokens` 与 `/apply-template`→`/tokenize(add_special:false)` 的 token 数；
     - **max_tokens=1 校准请求豁免决策 INVALID_DECISION 谓词（v8）**：校准请求仅用于 parity 计数，**不进入任何 rep/gate、不触发决策 INVALID_DECISION 判定**（其 `finish_reason=length` 是预期的）；
     - **允许偏差 0**（实测成立，§1.4；add_special 幂等归因 = 该 GGUF 无 BOS token，**换模型必须重验**）；
     - **PARITY_MISMATCH 判定时机（v37）**：**必须完成全部 6 个桶后统一判定**——**数值偏差不 fail-fast，继续收集剩余桶**；全部完成且任一桶偏差≠0 → 前置 `HOLD_NOT_VALIDATED`（规则 `PARITY_MISMATCH`）并停止正式矩阵（落盘 preflight，不产生矩阵结果）；**落盘口径（v37）**：`parity_ok=false`、`completed` 严格 6 项全集、`token_count_method=apply-template+tokenize`，并在 `parity_progress.errors` 记录结构化 `parity_mismatch` 条目；**仅端点/请求基础设施失败中断才 `parity_ok=null`、`completed` 为成功完成前缀（v38：`SCHEMA_INVALID` 固定例外另行注明——parity_ok 恒 null、completed 恒空，与基础设施中断无关）**；不做未经验证的自动补偿；若确需补偿，必须写入 `meta.parity_compensation`（含确定公式）并有对应单测（当前策略 `parity_compensation=null`）；
     - **校准后状态清理**：校准完成后 `erase` 全部 slot 并确认 `/metrics/kv` `used_cells==0 && active_sequences==0`（attention 回基线），**之后才采集正式基线**；
     - **mock 测试只校验请求参数一致性**（messages、thinking=false、temperature、seed、max_tokens），不声称比较渲染结果；
  - 探针实测（2026-08-09，4B，中文为主）：**旧估算对照 chars/2.5**（历史 v2 使用，仅作对照，不作为当前方法名/门禁）低估真实 token 数 **7.4%（short）→ 12.6%（medium）→ 16.3%（long）**（真实 chars/token ≈ 2.1–3.1，中文 1 字常 1–2 token）——**long 边界下 est=3400 实际可达 ~3944 > 3481 预算**；**正式诊断 fallback 使用 `chars/2.0`（更保守，v15）**，仅写入 notes 诊断字段，不作为正式 token_count_method 主值。
- **失败降级（v18 定稿，端点归因全文同步）**：`/apply-template` 或 `/tokenize` **探测失败、请求异常、端点消失或 schema 无法解析**（HTTP 非 200 / JSON 畸形 / 缺必需字段）→ **基础设施失败 → 落盘 preflight（规则 `PREFLIGHT_INFRA` = `INVALID_RESULTS_SCHEMA_OR_INFRASTRUCTURE`）并停止**；**仅当端点正常响应但 token parity 数值非零** → 落盘 preflight（规则 `PARITY_MISMATCH` = `HOLD_NOT_VALIDATED`）并停止；**预算超限同样落盘 preflight 五键结果（规则 `ALL_BUDGET_REJECTED` = `HOLD_NOT_VALIDATED`）——已取消 v8 的"不生成文件"行为**（保证证据不丢）；**`token_count_method` 总规则（v35）**：完整执行 apply-template+tokenize 时 = `"apply-template+tokenize"`（无论 parity pass/mismatch）；**未完整建立 → `null`**（进度记入 `parity_progress`）；`chars/2.0` 仅作为 notes/meta **诊断字段的估算方法**（`notes["est_method"]="chars/2.0-diagnostic"`），**不作为正式 `token_count_method` 主值**，**不允许绕过 parity 开始正式矩阵**。
- **运行前校准**：runner 在 server 就绪后、正式运行前，先对各桶 prompt 执行一次 apply-template+tokenize 得真实 P/B → 预算校验 → 合法才运行（预算表由函数计算，**单测断言合法/非法组合表，避免手工常量漂移**）。

**长度桶（token 目标值，实测校准；v3 收紧 long）**：
| 桶 | prefix P | branch B | 说明 |
|---|---|---|---|
| short | 150 | 150 | 决策点 + 短分支任务 |
| medium | 280 | 480 | 中等上下文 + 分支任务+工具注入 |
| long | 400 | 600 | 长公共上下文（v2 的 600/800 经探针证明余量不足，收紧） |

**合法矩阵（6 组合；parallel = fanout+2 动态；budget 值由公式计算，单测锁定）**：

| fanout N | parallel | short | medium | long |
|---|---|---|---|---|
| 2 | 4 | ✓ 750 | ✓ **1800** | ✓ 2400 |
| 4 | 6 | ✓ 1350 | ✓ **3320** | ✗ 4400 > 3481 |
| 8 | 10 | ✓ 2550 | ✗ **6360** | ✗ |

- 修正说明（v3）：medium×2 = 280+2×760 = **1800**、medium×8 = 280+8×760 = **6360**（v2 手算 1900/6700 有误，已由预算函数+单测取代手算）；long 桶收紧后 long×2 余量 = 3481−2400 = **1081 cells**（v2 的 81 cells 不足，因估算低估 16% 时 3400 est 实际 ~3944 超预算）。
- `parallel = N + 2`（+1 决策点 slot、+1 spare），前置校验 `parallel := fanout+2`（恒等派生，v38）；
- 矩阵维度 × ctk{q8_0, f16} × controls {off, on} → 6 组合 × 2 × 2 = **24 个运行单元**（每单元 warmup 2 + reps 5）；
- **RTX 4060 8GB 安全预算（§1.3 探针实测）**：最大 parallel 10 q8_0 = 3402 MiB（41.5%）；全部合法组合 GPU 峰值 < 3.5 GiB，余量 >4.6 GiB——**无需降级**。

---

## 5. 落盘 schema 与硬门禁

### 5.1 唯一顶层 JSON schema（审查修订：单一架构）

独立 runner 落盘 `results/m0_fanout_<ts>.json`，结构对齐 e15_branch_smoke（`e15_branch_concurrent.py:795-831`）。

**唯一权威 canonical meta 表（v34，逐键白名单；meta 内部键集按 phase 精确校验，不允许"等/可额外键"；model 与 model_id 去重——只保留 `model_id` 一个 canonical 字段，全文同步；**准确键数 = 22（v41：top meta 移除 parallel/fanout/prefix_len/branch_len 四键——per-unit 承载；ctx_size 仍 top）**）**：

| 键 | 类型 | 可 null | 普通 preflight/formal 值来源 | SCHEMA_INVALID 值 |
|---|---|---|---|---|
| `workload` | string | 否 | 固定 `"fanout"` | 同左 |
| `design_ref` | string | 否 | `"M0_BRANCH_MEMORY_BASELINE_DESIGN.md"` | 同左 |
| `model_id` | string | 否 | 配置（模型 id，唯一 canonical，删除 `model`） | 同左 |
| `binary_version` | string | 否 | 运行时（server version） | 同左 |
| `ctx_size` | integer | 否 | 配置 | 同左 |
| ~~parallel / fanout / prefix_len / branch_len~~ | — | — | **v41：从 top meta 移除——由 per-unit 权威表承载（§5.1 per-unit 字段）** | — |
| `cache_profiles` | array | 否 | **固定 `[{ctk: "q8_0", ctv: "q8_0"}, {ctk: "f16", ctv: "f16"}]`（v40：top meta 移除单值 ctk/ctv；per-unit replicate 仍带各自 ctk/ctv）** | 同左 |
| `protocol` | string | 否 | 固定 OAI 描述 | 同左 |
| `source_phase` | string | 否 | **= phase（普通结果规则；普通 PREFLIGHT_INFRA 即使在 formal server 启动时触发也 = preflight，v33）** | `preflight` 或 `formal`（记录原触发阶段，仅 schema_invalid 例外） |
| `phase` | string | 否 | preflight / formal | `preflight` |
| `preflight_status` | string | 否 | preflight: FAILED；formal: N/A | `FAILED` |
| `preflight_reason` | string 或 code 列表 | **是** | 固定归因 code | `schema_invalid` |
| `parity_ok` | bool | **是** | **parity 阶段完成状态总规则（v35）**：parity 全部桶完成且偏差=0 → `true`；全部完成但偏差≠0 → `false`；未完整完成 → `null`（不按 preflight_reason 硬编码） | **`null`（v35 例外：SCHEMA_INVALID 为 parity 总规则的固定错误报告例外——不信任原始对象、无论真实 parity 状态，恒 null）** |
| `parity_progress` | object | 否 | 实际校准进度（validation_incomplete 保留步骤 2 已完成的桶/错误）；**`completed` 为有序 list（v36），权威顺序 = `short-P, short-B, medium-P, medium-B, long-P, long-B`（6 项），顺序错位 → SCHEMA_INVALID（不使用无序集合语义）**；**`parity_progress.errors` 统一 schema = `{code, stage?, bucket?, template?}`（v39）**；**parity_mismatch 条目必须 `stage=calibration`、`bucket ∈ {short, medium, long}`、`template ∈ {P, B}`（v39）；其他错误 template 可省略**；**PARITY_MISMATCH 按 short/medium/long + P/B 精确定位（v38），测试覆盖 short-P 与 short-B 同时 mismatch 不被错误聚合**；**completed 语义（v37）**：**桶的请求/渲染/token 计数/对账流程完成即计入 completed，即使数值 mismatch 也算完成**；**基础设施失败的桶不计 completed、只入 errors 并停止后续桶——因此 parity_ok=null 时 completed 是真前缀**；**parity 交叉不变量（v36）**：普通结果 `parity_ok∈{true,false}` 时 completed **必须严格等于权威有序 6 项列表**；`parity_ok=null` 时 completed **必须是该列表的真前缀（可空、长度<6），不得全集**；SCHEMA_INVALID 固定例外 parity_ok=null + completed 空 | **恒 `{completed: [], errors: []}`（v35 例外：不信任原始对象）** |
| `parity_compensation` | object | **是** | 恒 `null`（当前策略不补偿） | `null` |
| `token_count_method` | string | **是** | **总规则（v35）**：完整执行 apply-template+tokenize 时 = `"apply-template+tokenize"`（无论 parity pass/mismatch）；未完整建立 → `null` | **`null`（v35 例外：同 parity_ok，恒 null）** |
| `decision_validation` | object | **是** | 验证证据 / null / partial | **恒 `null`（保守：不信任任何子结构）** |
| `decision_validated` | bool | 否 | **仅 decision_validation 完整（2 sessions×每次≥10）且 G-M0-1=PASS 时 true（v34）；其他所有（preflight / G1_FAIL / partial / null / schema_invalid / validation_incomplete）均 false** | `false` |
| `decision_fallback` | bool | 否 | **仅 formal 任一 rep fallback 时 true（v34）；否则 false（不允许 null）** | `false` |
| `preflight_rejections` | array | 否 | **unit 级（v42 定稿）：每条 = `{unit_id, budget, threshold, reason: "budget_rejected"}`（不复制冗余字段——validator 从 EXPECTED_UNIT_IDS/MATRIX_PLAN 重建 fanout/bucket/profile/control）；某 (fanout, bucket) 超限展开 4 个 unit_id（2 profile × 2 control）；unit_id 唯一子集；executed_units = 24 − len(rejections) 仅 formal 且 matrix_complete=true；所有 preflight 仍 executed_units=0；全部拒绝 = 24 条** | `[]` |
| `planned_units` | integer | 否 | **静态预算函数预筛后的 6 个理论合法组合 × 2 ctk × 2 control = 24（v36：long×4/medium×8/long×8 在设计/计划构建阶段被 budget 函数排除，不进入 planned_units、也不写 preflight_rejections）；矩阵合同派生，非 CLI 整数输入** | `24` |
| `executed_units` | integer | 否 | 实际执行数 | `0` |
| `matrix_complete` | bool | 否 | true / false | `false` |

**nullable 白名单（v34）**：仅 `preflight_reason` / `parity_ok` / `parity_compensation`（**必填但允许 null 且当前恒 null，v34：从不可 null 示例删除**） / `token_count_method` / `decision_validation` 可 null；**不可 null 键启动前默认值（v34/v42）**：`binary_version` 未知 → 固定 `"unknown"`；整数运行配置（**仅 ctx_size——parallel/fanout/prefix_len/branch_len 已非 top 键（v42），移到 per-unit/group**；**planned_units 由矩阵合同派生、非 CLI 输入，v36 从本列表移除**）**必须来自 CLI/矩阵合同，不得用魔法 0——配置本身缺失/非法属 CLI/config preflight 解析失败 → `EXIT_USAGE=64` + stderr `INVALID_CONFIGURATION`、不落盘实验结果（v35；不进入 SCHEMA_INVALID 包装）**；字符串配置（model_id）来自配置、**不可用空串**；**per-unit/group 字段（prefix_len/branch_len/ctk/ctv 等）在所属对象中必填 int/string、无 nullable（v42）**；不可 null 默认示例：`parity_progress` 空结构 `{completed:[],errors:[]}`、`decision_validated=false`、`decision_fallback=false`、`preflight_rejections=[]`、`executed_units=0`、`matrix_complete=false`；**不得省略任何键**。

**SCHEMA_INVALID 完整专有值清单仅此处权威定义（v33）**：phase=preflight、preflight_status=FAILED、preflight_reason=schema_invalid、parity_ok=null、parity_progress={completed:[],errors:[]}、parity_compensation=null、token_count_method=null、decision_validation=null、decision_validated=false、decision_fallback=false、preflight_rejections=[]、planned_units=24、executed_units=0、matrix_complete=false、source_phase∈{preflight,formal}、modes={}、gates={}、verdict=INVALID_RESULTS_SCHEMA_OR_INFRASTRUCTURE、notes 含 sidecar 相对路径；**其他行/合法组合表/测试⑯均引用本表，不复制第三套不完整清单**。

```json
{
  "meta": {"workload": "fanout", "design_ref": "M0_BRANCH_MEMORY_BASELINE_DESIGN.md",   # v31：canonical 固定键集（精确白名单，无第二套清单）
           "model_id": "qwen3-5-4B-Q4_K_M", "binary_version": "8570/4a699aaad",
           "ctx_size": 4096,   # v41：ctx_size 仍 top；parallel/fanout/prefix_len/branch_len 由 per-unit 承载（不在此）
           "cache_profiles": [{"ctk": "q8_0", "ctv": "q8_0"}, {"ctk": "f16", "ctv": "f16"}],   # v41：固定双 profile
           "protocol": "OAI /v1/chat/completions, temp=0, seed=42, no-think",
           "source_phase": "preflight|formal",    # v31：所有结果必含；普通结果 = phase；SCHEMA_INVALID envelope 记录原触发阶段
           "phase": "preflight|formal",           # v9：唯一 phase 字段（必含）
           "preflight_status": "FAILED|N/A",      # v10：仅两值（preflight 时 FAILED，formal 时 N/A）
           "preflight_reason": null,              # v16：固定 error code 或 code 列表（无自由文本）；phase=preflight 时必填
           "decision_validation": {"sessions": [  # v13（M2/G-M0-1 证据）：两次独立 server 启动；formal 强制 2 sessions×≥10，preflight 允许 null/partial
               {"server_start": 1, "requests": 10, "valid": 10, "invalid": 0,
                "invalid_length": 0, "invalid_no_action": 0,   # v20：invalid = invalid_length + invalid_no_action；invalid_length = finish_reasons["length"]
                "error_count": 0,
                "finish_reasons": {"stop": 10, "length": 0},
                "output_hashes": ["<sha256:1>", "<sha256:2>", "<sha256:3>", "<sha256:4>", "<sha256:5>",
                                  "<sha256:6>", "<sha256:7>", "<sha256:8>", "<sha256:9>", "<sha256:10>"],   # v21：10 个不同 sha256 占位符（len=10=valid+invalid=10+0）；ERROR 不写 hash
                "error_summary": []},
               {"server_start": 2, "requests": 10, "valid": 9, "invalid": 1,   # v22：第二 session（formal 强制 2 sessions×≥10）
                "invalid_length": 0, "invalid_no_action": 1, "error_count": 0,   # stop+无 ACTION 型（有可解析响应计入 finish_reasons stop）
                "finish_reasons": {"stop": 10, "length": 0},   # sum=10=10−0；invalid_no_action=1 计入 stop
                "output_hashes": ["<sha256:11>", "<sha256:12>", "<sha256:13>", "<sha256:14>", "<sha256:15>",
                                  "<sha256:16>", "<sha256:17>", "<sha256:18>", "<sha256:19>", "<sha256:20>"],   # len=10=valid+invalid=9+1
                "error_summary": []}],
               "total_valid_rate": 0.95},          # v22：decision_validation 级聚合 = sum(valid)/sum(requests) = (10+9)/(10+10) = 19/20 = 0.95（总 requests=0 时 null）；error_count>0 或 invalid>0 → G-M0-1=FAIL
               # partial 示例（v20，preflight 合法；全部不变量闭合）：{"sessions": [{"server_start": 1, "requests": 5, "valid": 4,
               #   "invalid": 1, "invalid_length": 1, "invalid_no_action": 0, "error_count": 0,
               #   "finish_reasons": {"stop": 4, "length": 1}, "output_hashes": ["h1","h2","h3","h4","h5"],   # v20：length 型 INVALID_DECISION（有响应可解析 → 有 hash）；len(output_hashes)=5=valid+invalid
               #   "error_summary": []}],
               #   "total_valid_rate": 0.8}   # requests=5=4+1+0（等式）；invalid=1=invalid_length(1)+invalid_no_action(0)（两来源）；finish_reasons[length]=1=invalid_length；sum(finish_reasons.values())=5=5-0；len(output_hashes)=5=valid+invalid；sum(valid)/sum(requests)=4/5
           "preflight_rejections": [],            # v43（unit 级）：[{unit_id, budget, threshold, reason: "budget_rejected"}]，空数组=无排除
           "planned_units": 24,                   # v24：全量计划单元数，与执行进度无关；派生值 = len(理论合法组合 6) × len(cache_types 2 = ctk{q8_0,f16}) × len(controls 2 = {off, on}) = 6×2×2 = 24；**理论合法组合数由 §4.2 预算函数单测锁定（v24）**，运行时精确 tokenizer rejection 只进 `preflight_rejections[]`、**不改变 planned_units**；validator 按同一矩阵合同公式计算期望值，矩阵变化时单点更新
           "executed_units": 24,                  # v14：实际执行的 unit 数；= planned_units − |preflight_rejections| 仅当 matrix_complete=true 且无崩溃中断时成立；preflight_rejections 只记录精确 tokenizer 复核后进一步被拒的 unit
           "matrix_complete": true,               # v12：formal 是否完整跑完（崩溃/提前终止 → false → FORMAL_INCOMPLETE）
           "decision_validated": true|false,
           "decision_fallback": false|true,        # 任一正式矩阵 rep fallback 时 true（聚合）
           "parity_ok": true|false|null,           # v35：parity 阶段完成状态总规则——true=全部桶完成且偏差=0；false=全部完成但偏差≠0；null=未完整完成（不按 preflight_reason 硬编码）
           "parity_progress": {"completed": ["short-P", "short-B"], "errors": []},  # v42：{completed: list[str], errors: list[{code, stage?, bucket?, template?}]}；preflight 可为空结构；PREFLIGHT_INFRA 部分完成时保存；formal 记录完整进度；不改变 verdict；**SCHEMA_INVALID 恒 {completed: [], errors: []}（v31，schema 错误只在 sidecar，不与 attribution code/stage 混用）**
           "parity_compensation": null,            # v7：当前策略不补偿，恒 null；如未来启用须为确定公式对象
           "token_count_method": "apply-template+tokenize"|null},  # v35：完整执行 apply-template+tokenize 时 = 该字符串（无论 pass/mismatch）；null 仅表示 parity 未完整建立；chars/2.0 仅 notes/诊断字段估算方法
  "modes": {"off": {"server_groups": [...]}, "on": {"server_groups": [...]}},   # v42：modes 结构重定义
  #   server_groups[] 每 control 各 6 group（cache profile × fanout = 2×3）；**group 对象权威 schema（v42 定稿）**：
  #   {server_group_id, ctk, ctv, fanout, parallel, start, stop, status, baseline, replicates[],
  #    error_type?, error_stage?}
  #   - status ∈ {COMPLETED, ERROR}；start/stop 为 RFC3339 UTC 字符串（**始终必填：尝试开始 / 清理结束时刻，v43**）
  #   - **status=COMPLETED → baseline 完整且 error_* 字段禁止；status=ERROR → baseline 可 null（启动后取得的部分 baseline 可保留合法结构）、error_type 必填、error_stage=formal（v43）**
  #   - baseline 为内联结构化对象（不依赖易丢日志路径）：{server_version, gpu_used_mb, rss_mb,
  #     rs_buffer_mb, kv_buffer_mb, metrics_kv_snapshot}（v42）
  #   - ERROR 时 error_type 必填（12 码子集）、error_stage=formal；COMPLETED 时 error 字段禁止
  #   - **warmup_count：必填整数 2（v43：warmup 不落入 replicates，只记录在 group 级）**
  #   - replicates[]：**每项 = 一次正式 rep 运行（v43）**，位于所属 group；不再用单 control start/stop + 扁平 replicates
  #   - **每 unit 固定 formal reps = 5（v43，FORMAL_REPS 常量 v44）**；每 rep 项必含完整权威字段（v44）：
  #     {unit_id, rep_index (0..FORMAL_REPS-1), fanout, bucket, ctk, ctv,
  #      prefix_len: int, branch_len: int, server_group_id,
  #      status (OK|INVALID_DECISION|ERROR), decision_fallback: bool,
  #      error_type?, error_stage?, error_bucket?, + §5.2 全部 metrics}
  #   - **replicates 是逐 rep 对象，不存在独立 unit 对象（v44）**；unit 身份字段按每 rep 内联并与 MATRIX_PLAN 重建一致
  #   - **validator（v47 单一权威，清理 v44/v45 重复集合表述）**：`(unit_id, rep_index)` 全局唯一（unit_id 允许在 5 个 rep 中重复）；每个已执行 unit 的 rep_index 集合 ⊆ `{0..FORMAL_REPS−1}`；**complete = rep_index 完整 `{0..FORMAL_REPS−1}` 的 unit（不要求 status!=ERROR）；observed ⊆ EXPECTED_UNIT_IDS − preflight_rejections；executed_units = len(complete)；matrix_complete=true → observed == complete == EXPECTED − rejections 三集合相等；matrix_complete=false → complete 允许任意子集、不限于前缀；删除 v43 的 len(去重 unit_id)==executed_units 旧等式**
  # v42：modes.{off,on}.server_groups[].replicates[] 为唯一权威路径（扁平 replicates 已删除）；per-unit 单 bucket 字段（v42）：**per-unit 另含 `server_group_id`（引用该 group 启动日志/RS/KV 基线，v40）**；**group 启动失败记录落在 group 对象（v42）：`status=ERROR`、`error_type`/`error_stage=formal`；无 rep 时也能记录**；**G-M0-4 按实际 ctk/ctv 与 group 公式归因（v40/v42：每 group 按实际 profile/parallel 对账，测试㉖ 明确首组、㉗ 明确第 2+ group/运行中崩溃）**；**validator 校验（v46，取代 v40 旧句）**：**observed ⊆ EXPECTED_UNIT_IDS − preflight_rejections；complete = rep_index 完整 {0..FORMAL_REPS−1} 的 unit（不要求 status!=ERROR）；executed = len(complete)；matrix_complete=true → observed == complete == EXPECTED − rejections 三集合相等，false → complete 允许子集**。
**unit 身份字段权威（v44：改名自「per-unit 权威表」——replicates 为逐 rep 对象，此处仅定义 unit 身份字段，避免与 per-rep 对象混淆；prefix/branch 合并单 `bucket` 字段——prefix 与 branch 始终同桶）**：`unit_id`、`fanout`（2/4/8）、**`bucket`（short/medium/long 枚举，unit_id 中 bucket 即该字段）**、`ctk`、`ctv`（q8_0/f16）、`prefix_len`（**int，继承桶校准结果，同桶 4 unit 一致，v42**）、`branch_len`（**int，同上**）、`server_group_id`、`status`、`decision_fallback`、`error_type?`、`error_stage?`、`error_bucket?`；control 由父 modes.off/on 表达不在 unit 重复。
**ID 格式固定并可重建（v42）**：`unit_id = {control}:{ctk}-{ctv}:f{fanout}:{bucket}`（如 `off:q8_0-q8_0:f4:medium`）、`server_group_id = {control}:{ctk}-{ctv}:f{fanout}`；**validator 校验格式、unit↔group 引用关系（unit 的 server_group_id 前缀 = 自身 control/ctk/ctv/fanout）、全局唯一与 24 预期集合（扣除 rejections）；bucket 超限展开 4 个 unit_id（同 profile/control/fanout）**
  "gates": {"G-M0-1": {"status": "PASS|FAIL|NOT_APPLICABLE"}, "...": {...}},
  "verdict": "PASS|HOLD_NOT_VALIDATED|HOLD_UNSTABLE_MEASUREMENT|REJECT_CORRECTNESS_OR_ISOLATION|INVALID_RESULTS_SCHEMA_OR_INFRASTRUCTURE",
  "notes": [...]
}
```

每 replicate 行字段（§5.2）：含 **`decision_fallback: bool`（v5：该 rep 是否触发固定路由 fallback）** 与 **`status`（rep 级，取值 `OK | INVALID_DECISION | ERROR`；`INVALID_DECISION` 不是顶层 verdict）**；KV 观测经 `KVProbe.run_aggregate`（first/last/peak_used_cells）。

**两个 phase 的合法 schema 变体（v8 定稿）**——唯一顶层键集始终为 `{meta, modes, gates, verdict, notes}`：

| phase | 触发 | meta 关键字段 | modes | gates | verdict | G-M0-7 |
|---|---|---|---|---|---|---|
| `preflight` | parity 失败（数值非零 / 端点异常 / schema 无法解析）或预算全部被拒或 server/health/基础设施失败**或 formal server 启动失败（验证证据完整，测试㉖）** | `phase="preflight"`, `preflight_status="FAILED"`, `preflight_reason=<结构化错误码（规则映射见 §5.1）>`, **`planned_units=24, executed_units=0`（v22：矩阵未启动，所有 preflight 文件统一）**, **`matrix_complete=false`（v21：任何 phase=preflight 文件统一 false，覆盖 PARITY_MISMATCH / ALL_BUDGET_REJECTED / PREFLIGHT_INFRA 早期与后期路径）**, **`parity_ok`/`token_count_method` 按 parity 阶段完成状态总规则（v35）**（不按 preflight_reason 硬编码）：parity 全部桶完成且偏差=0 → `parity_ok=true`；全部完成但偏差≠0 → `false`；未完整完成 → `null`；token_count_method 完整执行 apply-template+tokenize 时 = 该字符串（无论 pass/mismatch）、未完整建立 → `null`；**`parity_progress`（v40）**：仅记录已完成 P/B 桶与结构化错误码（`errors` 统一四元组 `{code, stage?, bucket?, template?}`；parity_mismatch 必须 stage=calibration+bucket+template、其他 template 可省），**不改变 verdict** | `{}`（无矩阵运行） | `{}`（不评估任何 gate） | `HOLD_NOT_VALIDATED`（`PARITY_MISMATCH`/`ALL_BUDGET_REJECTED`）或 `INVALID_RESULTS_SCHEMA_OR_INFRASTRUCTURE`（`PREFLIGHT_INFRA`） | **不评估**（仅 formal 评估） |
| `formal` | parity 通过且预算通过 | `phase="formal"`, `parity_ok=true`, `token_count_method="apply-template+tokenize"` | `{off:{...}, on:{...}}` | `G-M0-1..7`（G-M0-3b 恒 NOT_APPLICABLE） | §5.1 verdict 规则五值 | 评估（缺键 → FAIL → INVALID_RESULTS_SCHEMA_OR_INFRASTRUCTURE） |

- **schema 判定规则（v9 可编码）**：**validator 在 phase 分支前先验证顶层键集精确等于 `{meta, modes, gates, verdict, notes}`**（preflight 与 formal 均强制，缺任一键即非法）；`phase=preflight` ⇔ `modes={} ∧ gates={}`（verdict 为 `HOLD_NOT_VALIDATED` 或 `INVALID_RESULTS_SCHEMA_OR_INFRASTRUCTURE` 之一，由前置失败类型决定）；`phase=formal` ⇔ `modes 非空 ∧ gates 非空`；任何文件 `phase=preflight` 且 `gates≠{}`、或顶层键集不精确 → 非法 schema；
- **G-M0-7 评估范围（v9）**：仅 `phase=formal` 时评估；**formal 缺 `gates["G-M0-7"]` 键 → 完整性检查先置该 gate FAIL**（聚合用 `get(default)` 不 KeyError，§伪代码）；preflight 不评估任何 gate。
- **phase 字段约束（v12，validator 校验必需字段与类型）**：
  - `phase=preflight`：`decision_validation` **三种形态均合法（v20）：`null` / partial / 完整 2 sessions×≥10**——**形态例外（v29）**：**decision_validation 序列化失败无论已有 session 多少都原子写 `null`、不启动 formal**（§3.5 b-a)；正常启动/崩溃路径才按"无证据 null / 有证据 partial / 完整"规则（§3.5 b)）**；**5b 路径（v25）**：任一验证 session 启动失败即停止验证阶段、不再启动后续 session；尚无任何 session/请求证据 → `null`；已有第一 session 或部分当前 session → `partial`（validator 校验已有 session 结构：`requests/valid/invalid/error_count/finish_reasons/output_hashes/error_summary` 类型正确、**等式 `requests == valid + invalid + error_count` 对 preflight partial 与 formal 均强制（v18）**、**INVALID_DECISION 两来源（v20）**：session 增 `invalid_length` 与 `invalid_no_action`，**`invalid == invalid_length + invalid_no_action`**、**`invalid_length == finish_reasons.get("length", 0)`**（length 型 INVALID_DECISION）、`invalid_no_action` = finish_reason 非 length 但输出无合法 ACTION 的请求数（**删除 v19 的 `length==invalid` 总约束**，改为上述两来源分解）、**finish_reasons 不变量（v20）**：`sum(finish_reasons.values()) == requests - error_count`（error 请求无响应/无 finish_reason；**任何 status=ERROR 请求一律不写入 finish_reasons，即使畸形 200 携带 finish_reason**）、所有 count（valid/invalid/error_count/invalid_length/invalid_no_action/finish_reasons 值）为非负整数、**`output_hashes` 约束（v20）**：只记录有可解析响应的 OK/INVALID_DECISION 请求，**长度必须 `== valid + invalid`**、ERROR 不写 hash、**`representative_output` / `representative_output_sha256`（v56，可审计代表输出）**：每 session 记录**第一条 valid 请求的原始输出文本**（必须通过 `parse_action`）及其 UTF-8 sha256；**sha256 必须可重算**（validator 校验 64-hex 格式 + `sha256(text)==声明值` 可重算性，测试验证）；无 valid 请求时两者同空（validator 允许）；**不破坏 canonical meta 键集**（decision session 非 canonical meta 成员）、`total_valid_rate` 为 decision_validation 级聚合 `= sum(valid)/sum(requests)`（总 requests=0 时 null））但**不强制 2 sessions / 每次 ≥10**（仅 formal 强制）；**preflight 统一容量字段（v22）**：任何 phase=preflight 文件 `planned_units=24, executed_units=0`（矩阵未启动；部分 unit rejection 后进入 formal 属 formal，不在 preflight 统一字段范围）；**formal server 启动失败（验证证据完整时）→ `PREFLIGHT_INFRA` 落盘：保留完整 decision_validation、`modes={}`、`gates={}`、`planned_units=24, executed_units=0`、**`matrix_complete=false`（v20：所有 preflight 文件统一 false）**，为合法持久化（测试㉖）**；`preflight_rejections` **必须为数组（v25：5b 发生在预算步骤之后时保留实际 preflight_rejections；只有发生在预算计算之前的基础设施/parity 失败才为空 `[]`；phase validator 按执行进度接受数组并校验结构，不能丢证据）**（全部 24 候选经精确 tokenizer 复核被拒时为含全部排除项的数组）；`modes={}`、`gates={}`；**`parity_ok` 总规则（v35）**：parity 全部桶完成且偏差=0 → `true`；全部完成但偏差≠0 → `false`；**未完整完成 → `null`**（含 parity 部分完成后端点消失；`parity_progress` 仅记录已完成桶与结构化错误码，不改变 verdict）；validation_incomplete、验证/formal server_crash/health_failed/endpoint_unavailable **若 parity 已通过均 `true` + `apply-template+tokenize`**；违反 → `SCHEMA_INVALID`；
  - `phase=formal`：`decision_validation` **必须含两次独立 server 会话证据**（`sessions` 长度 = 2、每会话 `requests ≥ 10`、`valid/invalid/error_count` 为非负整数且**等式 `requests == valid + invalid + error_count` 成立（v16，preflight partial 与 formal 均强制）**、**INVALID_DECISION 两来源（v20，同 preflight）**：`invalid == invalid_length + invalid_no_action`、`invalid_length == finish_reasons.get("length", 0)`、**finish_reasons 不变量（v20）**：`sum(finish_reasons.values()) == requests - error_count`（ERROR 请求一律不写 finish_reasons，畸形 200 携带 finish_reason 也不写）、所有 count 非负整数、**`output_hashes` 长度 `== valid + invalid`（v20，ERROR 不写 hash）**、`finish_reasons`/`output_hashes`/`error_summary`（结构化错误码，无错误 []）类型正确；`total_valid_rate` = **decision_validation 级聚合 `sum(valid)/sum(requests)`**，**总 requests=0 时必须 `null`（v16）**）；`preflight_rejections` 必须为数组（可为空）；`planned_units`/`executed_units` 为正整数且 `executed_units ≤ planned_units`；**`planned_units` 派生校验（v23）**：validator 按矩阵合同计算期望值 `len(理论合法组合 6) × len(cache_types 2 = ctk{q8_0,f16}) × len(controls 2 = {off, on}) = 24`（v25 术语同步），与实际值不符 → `SCHEMA_INVALID`（矩阵变化时单一来源更新）；**`matrix_complete`（v12）必须为 bool，且 `matrix_complete=true` 时 `executed_units == planned_units − |preflight_rejections|`**；**FORMAL_INCOMPLETE 子集规则（v45）**：`matrix_complete=false` 时 **modes 只含已启动 groups**（失败 group 以 status=ERROR 出现、未启动 groups 不出现）；**observed ⊆ EXPECTED_UNIT_IDS − rejections、executed_units == len(complete_unit_ids)、complete ⊆ observed（仅 rep_index 完整的 unit 计 executed，v45；删除 v43 的 len(去重 unit_id)==executed_units 旧等式）**；`parity_ok` 必须 `true`；`modes` 非空、`gates` 非空；
  - 违反任一约束 → `SCHEMA_INVALID`。
- **rep status 状态机（v10 定稿，H3 + 逻辑约束）**：每个正式矩阵 replicate 的 `status` 取值：
  - `OK`：请求成功（HTTP 200）、响应字段完整、决策合法（`finish_reason=stop` 且含合法 ACTION）；**必须 `decision_fallback=false`**；
  - `INVALID_DECISION`：`(finish_reason=="length") OR (输出不含合法 ACTION)`（谓词三处同一）→ **走固定路由 fallback 继续**（§3.1），**`decision_fallback=true`（双向 ⇔：status=INVALID_DECISION ⇔ decision_fallback=true）**，不计入 G-M0-1，**可参与 G-M0-4 内存归因**，但排除真实决策质量声明；
  - `ERROR`：HTTP 最终非 200（按 §3.6 transient retry 后仍失败）、请求超时、响应缺字段/畸形、请求异常 → **必须 `decision_fallback=false` 且 `error_type` 为固定 error code（v16，可选 `error_stage`/`error_bucket`；不保存原始异常/路径/URL/prompt）**；**任何 formal rep ERROR → 顶层 `INVALID_RESULTS_SCHEMA_OR_INFRASTRUCTURE`**（规则 `REP_ERROR`），**ERROR rep 不参与 G-M0-4 数值归因**；
  - **server 启动/health/endpoint 失败不属于 rep status**——它们是 preflight 基础设施失败（§3.2/§3.6 M1），落盘 preflight 五键结果。
- **G-M0-7 对 formal 每 rep 校验**：`status`（三值）、`decision_fallback`（bool）、`error_type`（固定 error code，ERROR 时必填）、可选 `error_stage`/`error_bucket` 等必需键（§5.2）；缺键 → G-M0-7 = FAIL。
- **结构化错误码（v16）**：**不保存原始异常字符串/路径/URL/prompt**；`error_type`、`preflight_reason`、`error_summary`、`parity_progress.errors` 一律使用固定枚举 code（可选 `stage`/`bucket`/`template` 字段，v42：parity_progress.errors 为四元组，error_summary 保持三元组无 template）：
  - **枚举（v51，16 个）**：`connection_error`、`timeout`、`http_4xx`、`http_5xx`、`malformed_response`、`server_crash`、`health_failed`、`endpoint_unavailable`、`parity_mismatch`、`budget_rejected`、`validation_incomplete`、**`schema_invalid`（v29：SCHEMA_INVALID 落盘专用）**、**`erase_partial` / `after_erase_failed`（v49：erase 阶段失败，rep/group error_type）**、**`erase_failed` / `after_erase_missing`（v51：clean_all_slots 异常 / after_erase 观测缺失，rep/group error_type；v50 代码已抛、枚举未收录——v51 补录并 e2e 验证）**；
  - **表示形式（v16）**：`error_summary` = **无错误时统一 `[]`，有错误时 `[{code, stage?, bucket?, count}]`（v39：不引入 template 维度；count 必需正整数）**；`parity_progress.errors` = `[{code, stage?, bucket?, template?}]` 固定对象（v42：四元组；parity_mismatch 强制 stage=calibration + bucket + template，其他 template 可省）；`preflight_reason` = 单个固定 code 或 code 列表（无自由文本）；
  - **validator 只接受上述枚举与值类型**（`stage ∈ {calibration, validation, formal}`、`bucket ∈ {short, medium, long}`、`count` 为正整数）；**测试禁止任意 raw message**（任何非枚举字符串 → `SCHEMA_INVALID`）。
  - **`error_stage`/`error_bucket` 语义（v18）**：`error_stage` 标识错误发生阶段（calibration/validation/formal）；**`error_bucket` 为当前 unit 的长度桶（short/medium/long）**；**非 unit 级错误（如 server 启动/health/端点探测）可省略 `bucket`**。
  - **parity_progress.errors 唯一性四元组（v40，§5.1 主定义）：`(code, stage ?? null, bucket ?? null, template ?? null)`——相同键必须聚合为唯一项；与 error_summary 三元组分开定义**；**`error_summary` 约束（v18）**：每 session **`sum(error_summary[].count) == error_count`**；**唯一性（v18）**：省略的 `stage`/`bucket` 视为 `null`，**比较键 = `(code, stage ?? null, bucket ?? null)`**；相同键必须聚合为唯一项（不重复条目）；`count` 为正整数；违反任一 → `SCHEMA_INVALID`。
  - **SCHEMA_INVALID 合法落盘包装（v30 定稿）**：
    - **主 envelope（v32）**：**不复制第二套 meta 清单——明确调用 `build_preflight_envelope`（v37：builder 名称统一，删除 canonical meta/envelope builder 别名）**，完整包含 §5.1 canonical 表与 `CANONICAL_META_KEYS` 的 22 键（v42：**SCHEMA_INVALID 段不内联键列表、只引用权威表/常量；已删 top 键 parallel/fanout/prefix_len/branch_len/ctk/ctv 不得在此出现**），值从配置与受信运行上下文填充；**启动前未知字段按 canonical schema 指定 null 类型，不得省略**；**SCHEMA_INVALID 专有值（v39：**仅 canonical meta 权威表 SCHEMA_INVALID 列权威，本段纯引用不复制字段**；仅保留特有说明——notes 含 sidecar 相对路径、禁止递归 SCHEMA_INVALID、自检 70）**；**完整 result schema validator 必须接受该 envelope**（禁止递归 SCHEMA_INVALID）；
    - **诊断 sidecar（v31 细化）**：写 `results/m0_fanout_<ts>.schema_invalid.json`（**results 目录，非 tmp-dir**），只保存结构化 validator 错误 **`{path, code, expected_type, actual_type}`** 数组——**独立 validator 错误码枚举（与 12 归因码分开，v30）**：`missing_key` / `extra_key` / `type_mismatch` / `enum_mismatch` / `invalid_value` / `invariant_violation`；**path 必须为 RFC6901 JSON Pointer（v31）**；**`expected_type`/`actual_type` 仅允许类型名白名单（v31）**：`object` / `array` / `string` / `integer` / `number` / `boolean` / `null` / `missing`，**不保存值**；**归因 12 码规则不适用于 sidecar（v31，`schema_invalid` 仅用于 envelope 的 preflight_reason）**；不保存 prompt、URL、路径值或原始自由文本；**finally 不删除 results sidecar**；
    - **validator 拆分（v30）**：`validate_result_envelope(doc)`（完整五键 + phase + verdict + meta 必填字段）与 `validate_schema_sidecar(doc)`（错误数组 + 独立枚举）——**删除 v29"主/sidecar 均用五键最小 validator"表述**；
    - **写入顺序/部分失败（v30）**：先在内存验证 envelope+sidecar → 分别写同 results 目录**临时文件并 fsync** → **先原子 rename sidecar，再 rename 主 envelope**；任一写/rename 失败 → `IO_WRITE_FAILED`/74；可能已落盘的 sidecar **仅作诊断，无主 envelope 时消费者不得当作实验结果**；**只有主 envelope 存在且 exit 65 才是完整 schema-invalid 证据对**；
    - **覆盖任意 source_phase（v30）**：SCHEMA_INVALID 包装适用于任意触发阶段（preflight/formal），envelope 的 `source_phase` 记录来源，统一转 `phase=preflight`，exit 65；测试覆盖 formal modes 非法触发；
    - **退出码（v30）**：envelope+sidecar 均落盘成功 → **exit 65（`EXIT_SCHEMA_INVALID`）**；任一写失败 → 升级 **`IO_WRITE_FAILED` / exit 74**（不得虚构文件）；**envelope/sidecar 任一自身校验失败属于实现 bug → stderr 固定 `INTERNAL_SCHEMA_ENVELOPE_BUG`、exit 70（`EX_SOFTWARE`），不得递归包装（v30）**；
  - **SCHEMA_INVALID 写入流程（v31）**：
    1. **validate 原始结果**：`validate_result_envelope` 失败 → 收集结构化错误（sidecar 独立枚举）；
    2. **构造合法 envelope + sidecar**（canonical meta 固定键集，§5.1）；
    3. **分别自检**：`validate_result_envelope(envelope)` 与 `validate_schema_sidecar(sidecar)`——**任一自检失败 → stderr `INTERNAL_SCHEMA_ENVELOPE_BUG`、exit 70（`EX_SOFTWARE`）、不写任何 results 文件**（禁止递归包装）；
    4. **自检通过才写盘**：results 目录临时文件 `.m0_fanout_<ts>.<kind>.tmp`（**kind ∈ {main, sidecar}（v33）：SCHEMA_INVALID envelope 也是 main**）→ 文件 `fsync` → **先原子 rename sidecar、再 rename 主 envelope** → **`fsync` results 目录**；任一步失败 → `IO_WRITE_FAILED`/74；**进程异常/下次启动时清理陈旧 `.tmp`**；主 envelope notes 中 sidecar 相对路径基准 = **results 目录**；
    5. **部分落盘语义**：无主 envelope 时已落盘 sidecar 仅作诊断、**消费者忽略、不得当实验结果**；已有 sidecar 保留但非证据对；**只有主 envelope 存在且 exit 65 才是完整 schema-invalid 证据对**。
  - **atomic writer 共用合同（v32，任务 6）**：**普通 preflight/formal 主结果也复用 `m0_schema.py` atomic writer**——内存完整校验 → 同目录 `.m0_fanout_<ts>.main.tmp` + fsync → rename 主文件 → fsync 目录；**SCHEMA_INVALID 只是多 sidecar 且先 rename sidecar**；**不得存在第二套非原子写入**。
  - **phase 合法组合（v34）**：SCHEMA_INVALID 合法组合**以 canonical meta 权威表 SCHEMA_INVALID 列为唯一依据**（本行不复制清单）；validator 接受该组合，**不得递归包装 SCHEMA_INVALID**。
  - **source_phase validator（v32，任务 4）**：普通结果（`preflight_reason != schema_invalid`）**必须 `source_phase == phase`**；SCHEMA_INVALID envelope **例外允许 `phase=preflight` 且 `source_phase ∈ {preflight, formal}`**；其他组合（普通结果 source_phase≠phase、SCHEMA_INVALID source_phase 越界）→ `SCHEMA_INVALID`；测试正反覆盖。
    | 退出码 | 常量 | 条件 |
    |---|---|---|
    | `0` | — | **任何合法 preflight/formal 主结果成功落盘**（含 PASS/HOLD/REJECT 与 `INVALID_RESULTS_SCHEMA_OR_INFRASTRUCTURE`，但 SCHEMA_INVALID 专用 65 除外；⑬b-a/b/c/d 成功落盘均 exit 0） |
    | `64` | `EXIT_USAGE`（EX_USAGE） | **CLI/config preflight 解析失败（v35）：整数运行配置缺失/非法 → stderr 受控 code `INVALID_CONFIGURATION`、不落盘实验结果**；仅配置完整后构造结果对象发生 schema 非法才走 65 包装 |
    | `65` | `EXIT_SCHEMA_INVALID` | SCHEMA_INVALID 合法 envelope + sidecar 均已落盘（完整证据对） |
    | `70` | `EX_SOFTWARE` | envelope/sidecar 自身校验失败（实现 bug）：stderr `INTERNAL_SCHEMA_ENVELOPE_BUG`，不得递归包装 |
    | `74` | `EXIT_IO_WRITE_FAILED` | 任何结果写失败（Linux `EX_IOERR`），stderr 固定 code、不得虚构文件 |
    序列化失败后五键落盘成功 → exit 0；落盘失败 → 升级 74。
  - **`preflight_reason` 规则映射（v18）**：
    | 前置失败类型 | 允许的 code 集合 |
    |---|---|
    | `PARITY_MISMATCH` | 仅 `parity_mismatch` |
    | `ALL_BUDGET_REJECTED` | 仅 `budget_rejected` |
    | `PREFLIGHT_INFRA` | 基础设施类：`connection_error` / `timeout` / `http_4xx` / `http_5xx` / `malformed_response` / `server_crash` / `health_failed` / `endpoint_unavailable` / `validation_incomplete`（按实际阶段；**parity_ok 按总规则 v35：parity 已通过后的任何基础设施失败（validation_incomplete/server_crash/health_failed/endpoint_unavailable）→ true + apply-template+tokenize；parity 未完整建立 → null**） |
    | `SCHEMA_INVALID` | 仅 `schema_invalid`（v30：validator 对此组合合法） |
    支持 code 列表，但**每项必须属于对应集合**；错配（如 PARITY_MISMATCH 带 `budget_rejected`）→ `SCHEMA_INVALID`（测试覆盖）。
- **测试要求（v12 更新，规则名称引用）**：① preflight 变体单测（`PREFLIGHT_INFRA`/`PARITY_MISMATCH`/`ALL_BUDGET_REJECTED` 三路径 → 五键精确匹配，verdict 分别为 INVALID_RESULTS_SCHEMA_OR_INFRASTRUCTURE / HOLD_NOT_VALIDATED / HOLD_NOT_VALIDATED）；② formal 变体单测（全 gate status 三值枚举、verdict 五值枚举、`gates["G-M0-7"].status` 仅在 formal 存在）；③ 非法 schema 检测单测（顶层键集多/缺键、preflight 携带 gates、**phase 字段约束违反（v15）**：**preflight 的 decision_validation 三种形态（null / partial / 完整 2 sessions×≥10）均合法**（只要结构合法——仅表示 formal modes 尚未产生，不因验证证据完整而非法）；**formal 缺完整 2 sessions×≥10 证据非法**；formal 缺两会话证据、executed>planned、matrix_complete=true 但 executed≠planned−|rejections|、session 等式 requests≠valid+invalid+error_count）→ `SCHEMA_INVALID`；补正反单测）；④ **verdict 持久化证据**：preflight 文件即持久化输出（含 `preflight_status=FAILED`/`preflight_reason`/`parity_ok`）；⑤ rep 状态机单测（OK/INVALID_DECISION/ERROR 判定与逻辑约束：INVALID_DECISION⇔fallback、OK/ERROR 必须 fallback=false、**ERROR 必填 `error_type`（固定 error code，v18）且 `error_stage`/`error_bucket` 可选，不保存任何自由文本**）；⑥ **并存优先级单测**（`REP_ERROR` + `G1_FAIL` + fallback 并存 → 顶层仍 INVALID_RESULTS_SCHEMA_OR_INFRASTRUCTURE；`G7_SCHEMA_FAIL` 高于 `DECISION_FALLBACK`；`FORMAL_INCOMPLETE` 高于一切 HOLD 类）；⑦ **aggregate_verdict 签名与 validator 前置单测**（`aggregate_verdict(doc, any_fallback, any_rep_error, any_preflight_rejection)`，schema 非法 → `SCHEMA_INVALID` 直接返回，不进入聚合；preflight 分支返回 `doc["verdict"]`）；⑧ **preflight_rejections/planned/executed 单测**（单 unit 被拒排除、`executed_units=planned−|rejections|`、其余继续、`PARTIAL_REJECTION` → HOLD_NOT_VALIDATED、全部被拒 → `ALL_BUDGET_REJECTED` preflight HOLD）；⑨ **decision_validation 落盘单测**（两次独立 server 启动、字段完整、formal 新 server 启动防污染）；⑩ **transient retry 分层单测（v27）**（外层 3 次逻辑调用处理连接/超时/5xx、内层 context 400 内部重试 1 次、理论最大 6 wire、4xx/畸形 200 不外层重试、耗尽→ERROR；**畸形 200 正反用例**：畸形 200（缺字段/JSON 畸形）即使携带 finish_reason → `status=ERROR` 且**不写入 finish_reasons**（反例：写入则 SCHEMA_INVALID）；正常 200 → 正常计数；**验证请求三路归因（当前版本 v27）**：① `Popen.poll()!=None` 进程已退出 → in-flight 请求立即归因 `server_crash`、不再 transient retry；② 进程存活 → 连接/超时/5xx 按 0.25/0.5s 重试、耗尽归因 `connection_error`/`timeout`/`http_5xx`；③ 首次异常时进程存活、但重试期间进程退出 → 归因 `server_crash`（不再继续重试））；⑪ **formal server 崩溃单测**（崩溃 → 停止后续、保留已完成 rep、当前 rep ERROR、`matrix_complete=false`、`FORMAL_INCOMPLETE`）；⑫ **FORMAL_INCOMPLETE 单测**（`matrix_complete=false` → 顶层 INVALID、**绝不 PASS**、依赖完整矩阵 gates（G-M0-4/6）置 NOT_APPLICABLE）；⑬a **验证 server 失败路径单测——session 完整（v23）**（两次验证 server 均完成或**崩溃但 session 完整（requests≥10 且每个请求有 OK/INVALID_DECISION/ERROR 记录，含最后 in-flight 崩溃）** → 证据完整、**计入 formal 2 sessions**、继续 formal；含 ERROR/invalid → G-M0-1=FAIL；**不要求 server health 正常**）；⑬b **验证 server 失败路径单测——session 不完整（v29，六子例 a-f）**（**组级说明：仅 formal 强制 2 sessions×≥10**）：
   ⑬b-a **首个验证 server 启动失败 → `decision_validation=null`，并断言第二个 server 未启动**（启动失败即停止验证阶段）；code 按三映射（server_crash/health_failed/endpoint_unavailable）；**显式断言（v35）：因 parity 已通过，普通 PREFLIGHT_INFRA 结果 `parity_ok=true`、`token_count_method=apply-template+tokenize`**；
   ⑬b-b **第一 session 完整后第二 server 启动失败 → `partial`**（第一 session 证据保留，第二 server 未产生数据）；
   ⑬b-c **当前 session 中途崩溃 requests<10 → `partial`**，**立即停止验证阶段、不启动任何后续 session（v27）**——**覆盖两形态 + 边界（v29）**：① **首 session 中途崩溃 → `partial` 且断言第二 server 未启动**；② **第一 session 完整、第二 session 中途崩溃 → `partial`（第一 session 证据保留、第二为 partial）**，**且断言 formal server 未启动（v28）**；**requests=0 边界（v29）**：崩溃前无任何请求 → 无证据 → `decision_validation=null`（非 partial）；断言崩溃请求计入 `requests` 与 `error_count`、`error_summary` 含 `{code: server_crash, stage: validation, count: 1}`、**全部计数等式成立**（requests=valid+invalid+error_count、invalid=invalid_length+invalid_no_action、sum(finish_reasons)=requests−error_count、len(output_hashes)=valid+invalid）；
   ⑬b-d **decision_validation 序列化异常**（如 `json.dumps` TypeError，v28）→ 整体证据**原子丢弃为 `null`**、**使用与 schema_invalid 相同的 `build_preflight_envelope`（v37：名称统一），差异字段（v37）**：`preflight_reason=validation_incomplete`（非 schema_invalid）、`source_phase=preflight`、**`parity_ok=true`（validation_incomplete 发生在 parity 已完整通过后的验证阶段，v35 总规则）**、`token_count_method=apply-template+tokenize`（v35 总规则）、**`parity_progress` 保留完整实际校准进度（validation_incomplete 属 PREFLIGHT_INFRA，不恒空）**、**正常退出 0**（`decision_validation` 与 schema_invalid 同为 null——成因不同（序列化原子丢弃 vs 不信任子结构）但取值相同，移出差异字段，v33）（其余 meta 必填与 schema_invalid 包装一致）；**序列化失败无论 session 完整性均不启动 formal（v30，断言从 ⑬b-c 移入）**；**组合测试（v29）**：五键落盘失败 → 升级 `IO_WRITE_FAILED` / exit 74；落盘成功 → exit 0——**不再混入 validator 结构非法**；**与 ⑬b-f 对比（v30）**：⑬b-d 序列化异常（运行时 TypeError）vs ⑬b-f validator 判非法（结构化结果），两者均不启动 formal；
   ⑬b-e **最终结果文件 I/O 写失败（v28 兜底）** → 常量 `EXIT_IO_WRITE_FAILED = 74`（Linux `EX_IOERR`）、stderr 固定错误码 `IO_WRITE_FAILED`、**断言 exit 74**、**不虚构结果文件**（外部基础设施阻塞）；
   ⑬b-f **序列化成功但 schema validator 判非法（v28/v30/v31）** → 规则 `SCHEMA_INVALID`——**保留合法 envelope + 结构化 sidecar 证据（v31），不保存原始非法 JSON**；顶层 `INVALID_RESULTS_SCHEMA_OR_INFRASTRUCTURE`；**覆盖任意 source_phase（含 formal modes 非法触发，v30）**；**退出断言（v31）**：**exit 65 时主 envelope + sidecar 均存在（finally 后保留）**；**exit 70 时无任何 results 文件**；**exit 74 时部分 sidecar 语义（无主 envelope 时消费者忽略、不得当实验结果）**；envelope/sidecar 自身校验失败 → stderr `INTERNAL_SCHEMA_ENVELOPE_BUG` + exit 70；**⑬b-f/⑰ 断言 finally 保留 results 主/sidecar（⑰ 仅正常路径主结果保留、不要求 sidecar，v31）**）；⑭ **rep ERROR 继续单测（v46：补完整矩阵断言）**（server 健康单 rep ERROR → 继续剩余 matrix；**ERROR rep 所在 unit 因 rep_index 完整仍计 complete（v45）；executed_units = 24 − len(preflight_rejections)、matrix_complete=true、顶层 REP_ERROR（v46）**；**反例：以 status!=ERROR 过滤 complete 集合 → SCHEMA_INVALID（避免 status 过滤 complete，v46）**）；⑮ **SDK 重试禁用单测**（`Driver(sdk_max_retries=0)` → `OpenAI(max_retries=0)`；默认 None 保持 SDK 行为；`Driver(max_retry=1, sdk_max_retries=0)` 的 6-wire 上限成立）；⑯ **parity_ok 三值 validator 单测（v38：parity 阶段完成状态总规则 + `CANONICAL_META_KEYS` 常量驱动）**——**明确引用 `m0_schema.py::CANONICAL_META_KEYS` 常量（v37/v40），断言 meta 键集与常量精确相等；SCHEMA_INVALID 固定值由常量（SCHEMA_INVALID_FIXED）参数化驱动（v40）、不内联全清单；canonical 表列为文档人工镜像；删除「14/27」与「从 Markdown 表派生」表述**；逐字段固定值/错配测试由参数化表驱动；——**总规则正反（v35）**：parity 全部桶完成且偏差=0 → `true`；全部完成但偏差≠0 → `false`；未完整完成 → `null`；**parity 已通过后四类基础设施失败（validation_incomplete / server_crash / health_failed / endpoint_unavailable）→ 必须 `parity_ok=true` + `token_count_method=apply-template+tokenize`**；**parity 阶段中途失败（桶部分完成）→ 必须 `parity_ok=null`**；错配 → SCHEMA_INVALID；**逐字段正反覆盖**：完整 canonical 22 键（v42：**引用 `CANONICAL_META_KEYS` 常量，不内联键列表；parallel/fanout/prefix_len/branch_len/ctk/ctv 不在其中**）、`preflight_status=FAILED`、`preflight_reason=schema_invalid`、`parity_ok=null`、**`parity_progress` 恒空**、`token_count_method=null`、**`decision_validation=null`（保守，formal 来源也不复制）**、`modes={}`/`gates={}`、`verdict=INVALID_RESULTS_SCHEMA_OR_INFRASTRUCTURE`、`source_phase∈{preflight,formal}`；**每个错配反例 → SCHEMA_INVALID 且禁止递归**；source_phase 规则正反（普通结果必须 ==phase、SCHEMA_INVALID 例外）；**phase 错配反例（v35 合并）：SCHEMA_INVALID envelope `phase ≠ preflight`（如 phase=formal）→ SCHEMA_INVALID**；**「完整 canonical 键」列表补 `phase`（v35）**；**逐字段覆盖全部 canonical meta 与 SCHEMA_INVALID 固定值（v33）**：`preflight_rejections=[]`、`planned_units=派生 24`、`executed_units=0`、`matrix_complete=false`、`notes` 含 sidecar 相对路径、`decision_validated=false`、`decision_fallback=false`、`parity_compensation=null`——**每项错配反例 → SCHEMA_INVALID 且不递归**；
⑰ **端口生命周期单测（v42）**（`--port-base` 占用时向上探测、未给时动态分配、**顺序 15 进程（校准 1 + 验证 2 + formal 12 groups），每个 group 独立端口/清理/复用**、`pgrep` 无遗留；**崩溃路径断言（v26）**：**四阶段任一崩溃后** runner 走固定清理序列（poll → 存活 terminate → wait 2s → 仍存活 kill → wait/reap），断言**已退出不调用 terminate、terminate 超时转 kill、ProcessLookupError 静默、端口可立即复用**（`pgrep -x llama-server` 空、同端口 bind 成功））；⑱a **decision_validation 聚合纯函数单测（v20）**（**独立 fixture：纯聚合函数**用非对称 sessions（s1={requests:10, valid:10, invalid:0, invalid_length:0, invalid_no_action:0, error_count:0}、s2={requests:5, valid:4, invalid:1, invalid_length:1, invalid_no_action:0, error_count:0}）验证 `total_valid_rate = sum(valid)/sum(requests) = 14/15`，总 requests=0 时 null；**不得用 s2 requests=5 的 doc 作为 formal validator fixture**）；⑱b **formal validator 判定单测（v20）**（**合法 formal doc（每 session requests≥10）**验证 `invalid>0` 或 `error_count>0` → G-M0-1=FAIL；**INVALID_DECISION 两来源三型用例（v20）**：① length 型（`invalid_length>0, invalid_no_action=0`，`invalid_length==finish_reasons["length"]`）；② stop+无 ACTION 型（`invalid_no_action>0, invalid_length=0`，**具体 fixture（v21）**：如 requests=10, valid=9, invalid=1（invalid_no_action=1）, error_count=0，`finish_reasons={stop: 10, length: 0}`——**invalid_no_action 请求有可解析响应（stop）必须计入 finish_reasons 实际键 stop**（sum=10=10−0、stop=10=valid+invalid_no_action 一致），`output_hashes` 长度=10=valid+invalid）；③ 混合型（两者均 >0，`invalid==invalid_length+invalid_no_action`）；**output_hashes 长度 `== valid+invalid`**（ERROR 不写 hash）；`error_summary` 为结构化枚举对象校验（非自由文本"去敏"）；preflight partial 校验结构但不强制 2 sessions×≥10）；⑲ **Driver context400 递归透传单测**（首次与重试 wire 的 temperature/seed/max_tokens/extra_body 完全一致；旧 `chat(msgs, 2)` 位置传参兼容）；⑳ **FORMAL_INCOMPLETE 优先单测**（`matrix_complete=false && any_rep_error=true` → 顶层 FORMAL_INCOMPLETE 而非 REP_ERROR）；㉑ **parity_progress 单测（v33：validation_incomplete 断言）**——**⑬b-d/㉑ 断言 validation_incomplete 的 parity_progress 等于步骤 2 实际完成的桶/错误（正常情况下 completed 非空），与 schema_invalid 恒空 {completed:[],errors:[]} 形成对照（v33）；**㉑ 补 template 维度断言（v38）：parity_progress.errors 元素可带 template∈{P,B}、唯一性键含 template、short-P 与 short-B 同时 mismatch 不被错误聚合**；**㉑ 补 parity 混合场景（v39，从 ㉖ 移入）：数值 mismatch 检测时立即写结构化 parity_mismatch 条目且计入 completed；后续基础设施失败保留此前 mismatch 条目、再写基础设施 error、停止后续项、parity_ok=null——证据不丢；顶层 preflight_reason 只记基础设施 code（PREFLIGHT_INFRA）、parity_mismatch 不进 preflight_reason（正反覆盖）****；（类型 `{completed: list[str], errors: list[{code, stage?, bucket?, template?}]}`，**errors 元素必须为结构化枚举对象，非枚举对象（自由文本/任意字符串）→ `SCHEMA_INVALID`**；preflight 空/缺进度用空结构、PREFLIGHT_INFRA 部分完成保存、**不改变 PREFLIGHT_INFRA verdict**、validator 校验类型与枚举）；㉒ **决策 ACTION 解析/确定性/canary 单测**（`ACTION: branch(bX)` 解析边界（缺失/畸形/多行/大小写）、决策点 prompt 枚举约束下 temp=0/seed=42 确定性、canary 注入与跨分支检测）；㉓ **apply-template+tokenize mock 与端点失败单测**（正常响应 → token 数正确；HTTP 非 200/JSON 畸形/缺字段 → `PREFLIGHT_INFRA` 落盘、不降级继续）；㉔ **budget 函数组合表单测（v35：仅预算职责，fixture 与 ㉕ 不重叠）**（6 合法组合（§4.2 表）全部通过、3 非法组合（long×4、medium×8、long×8）全部拒绝；精确 tokenizer 复核后 rejection 落盘移到 runner/schema 集成测试⑧/㉖（v36：㉔ 只测 budget 纯函数 6 合法 + 3 排除））；㉕ **CLI 参数/port-base/fail-fast 单测（v42：CLI 只断言未知参数与真实 CLI 参数）**（**`--fanout`/`--prefix-len`/`--branch-len`/`--ctk`/`--ctv`/`--parallel`/`--warmup`/`--reps` **共 8 个未知参数项（v45：--warmup/--reps 亦属未知）** → `EXIT_USAGE=64` + `INVALID_CONFIGURATION`、不落盘（v42：删除旧合法 fanout/bucket/parallel 校验——矩阵枚举/结构由 MATRIX_PLAN 内部 validator 保证，测试㉗）；其余真实 CLI 参数（--server-bin/--model/--ctx-size/--decision-n-predict/--branch-n-predict/--tool-rounds/--out/--tmp-dir/--port-base（**v44：--warmup/--reps 已删，显式传入属未知参数 → EXIT_USAGE 64**））类型/必需项校验**、`--port-base` 占用向上探测、**结构非法仅限已暴露选项值域（如 --ctx-size 非正 int）→ EXIT_USAGE 64**
㉖ **首 formal group 启动失败落盘单测（v43，权威补回）**：仅首 formal group 启动失败 → `phase=preflight`、`preflight_status=FAILED`、`preflight_reason=PREFLIGHT_INFRA`、`planned_units=24`、`executed_units=0`、`matrix_complete=false`、**`modes={}`、`gates={}`**、**保留完整 decision_validation（2 sessions×≥10）**、`parity_ok=true`（parity 已通过）、合法持久化；**与第 2+ group 启动失败（formal incomplete）正反对照（v43）**。
㉗ **多 cache profile 编排单测（v45）**：per-unit 字段缺失/错误 server_group_id/profile → SCHEMA_INVALID；**`(unit_id, rep_index)` 全局唯一——unit_id 允许在 5 个 rep 中重复（v43，删除「重复 unit_id 即非法」旧断言）；每个已执行 unit 的 rep_index 集合 ⊆ {0..FORMAL_REPS−1}；**complete 仅由 rep_index 完整判定（v45：ERROR rep 但 5 次尝试均有记录仍计 complete；REP_ERROR 与 matrix_complete=true 可共存；仅 rep_index 中断前缀不全不计 complete）**；**集合规则见段末 FORMAL_INCOMPLETE 断言（v47：单一权威，前部不重复 observed/executed 规则）；**12 server group 生命周期（cache profile × fanout × control = 2×3×2，每组 parallel=fanout+2、独立启动/回基线/停止）；profile/fanout/control 重启；崩溃 group → 该 group 不完整且 matrix_complete=false**；**崩溃第 5 个 rep 正例（v47）：group 崩溃 → formal incomplete、matrix_complete=false（与请求级 REP_ERROR 但无进程崩溃、matrix_complete 可 true 严格区分）**；**第 2+ group 启动失败或任一 group 运行中崩溃 → formal + FORMAL_INCOMPLETE + 保留已完成 group/unit + 停止剩余（v42）**；**modes 只含已启动 groups；失败 group 以 `status=ERROR` 出现；未启动 groups 不出现；**任何 ERROR group（启动失败或崩溃）→ `matrix_complete=false`（v48，本断言限定）**；**group 对象逐项覆盖（v42）**：status∈{COMPLETED,ERROR}、start/stop RFC3339 UTC、baseline 内联六字段（server_version/gpu_used_mb/rss_mb/rs_buffer_mb/kv_buffer_mb/metrics_kv_snapshot）、ERROR 时 error_type 必填（12 码子集）+error_stage=formal、COMPLETED 时 error 字段禁止；**rejections 覆盖（v42）**：unit 级 {unit_id,budget,threshold,reason:budget_rejected}、某桶超限展开 4 unit_id、unit_id 唯一子集、executed=24−len(rejections) 仅 formal 且 matrix_complete=true、所有 preflight executed=0；**rep 粒度覆盖（v43）**：replicates 每项=正式 rep、warmup_count=2 必填整数、rep 级 status/decision_fallback/metrics、每 unit 固定 5 reps、rep_index∈0..4；**per-unit 单 bucket 覆盖（v42）**：bucket 枚举、prefix_len/branch_len int 且同桶 4 unit 一致、unit_id 中 bucket==该字段。**首 group vs 中途 group phase 断言（v44）**：**第 1 个 formal group 启动失败 → phase=preflight、PREFLIGHT_INFRA、executed_units=0**；**第 2+ 个 group 启动失败 → phase=formal、FORMAL_INCOMPLETE、保留已完成 groups/rep、matrix_complete=false**；两种断言与 §3.5/§3.7/phase 规则表全文一致（正反覆盖）。**FORMAL_INCOMPLETE 集合断言（v47 单一权威，合并 v44/v45 重复段）**：observed_unit_ids ⊆ EXPECTED−rejections；executed_units == len(complete_unit_ids)；complete ⊆ observed；matrix_complete=true → observed==complete==EXPECTED−rejections；**false → 允许任意子集、不限于前缀**（仅 rep_index 完整的 unit 计 executed、部分 rep 计 observed 不计 executed）；**崩溃边界正反例（v47）**：**第 5 个 rep（rep_index=4）已开始且该 rep 已记录 ERROR → rep_index 集合 {0..4} 完整、unit 计 complete（v45：ERROR rep 但 5 次尝试均有记录仍计）**；**第 5 个 rep 尚未开始（仅 0..3 有记录）→ 该 unit 不计 complete、仅计 observed、matrix_complete=false**；**len(EXPECTED_UNIT_IDS)==24（v47），且与 MATRIX_PLAN 合法组合映射（fanout2→short/medium/long、fanout4→short/medium、fanout8→short）笛卡尔积重建集合精确相等（断言集相等而非仅长度）。**

**verdict 确定规则（v7 定稿：gate 与顶层分层，确定性聚合）**：

- **`gates.*.status` 值域**：`PASS | FAIL | NOT_APPLICABLE`（required gates = G-M0-1、G-M0-2、G-M0-3a、G-M0-4、G-M0-5、G-M0-6、G-M0-7；G-M0-3b 为 smoke 观测，status 恒 `NOT_APPLICABLE` 且不计入判定）。
- **顶层 `verdict` 值域**：`PASS | HOLD_NOT_VALIDATED | HOLD_UNSTABLE_MEASUREMENT | REJECT_CORRECTNESS_OR_ISOLATION | INVALID_RESULTS_SCHEMA_OR_INFRASTRUCTURE`（单一枚举值，不使用枚举外字符串）。
- **分层原则**：`gates` 只反映**各自测量域**的 PASS/FAIL（G-M0-1 只由专用 2×≥10 验证决定，**正式矩阵 fallback 不改变 G-M0-1 status**）；fallback / parity / 预算超限等**顶层聚合条件**作为独立判定项，在映射表中显式给出 gate 状态与顶层 verdict 的组合。
- **verdict 确定规则（v12 定稿：稳定规则名称，跨章节唯一引用）**：

- **稳定规则名称表（全文唯一引用，不再用易漂移的编号）**：

| 规则名 | 条件 | gates 状态 | verdict |
|---|---|---|---|
| `SCHEMA_INVALID` | schema validator 前置失败（顶层键集不精确 / phase 非法 / 值域越界） | （validator 拒绝，不评估 gates） | `INVALID_RESULTS_SCHEMA_OR_INFRASTRUCTURE` |
| `PREFLIGHT_INFRA` | 前置：基础设施失败（server 启动/health/端点探测失败、校准期间请求异常/端点消失，M1）**或 formal server 启动失败（验证证据完整，测试㉖，v18）**→ 落盘 preflight | gates = `{}` | `INVALID_RESULTS_SCHEMA_OR_INFRASTRUCTURE` |
| `PARITY_MISMATCH` | 前置：端点正常但 token parity 数值非零（M1）→ 落盘 preflight | gates = `{}` | `HOLD_NOT_VALIDATED` |
| `ALL_BUDGET_REJECTED` | 前置：全部 24 候选经精确 tokenizer 复核被拒（§3.5 步骤 3）→ 落盘 preflight | gates = `{}` | `HOLD_NOT_VALIDATED` |
| `FORMAL_INCOMPLETE` | formal：`matrix_complete=false`（server 崩溃/提前终止，含 unit 间隙，§3.7）→ 落盘已完成 rep，**依赖完整矩阵的 gates 置 NOT_APPLICABLE**（**优先于 REP_ERROR**：崩溃期间写入的 ERROR rep 不改变归属）；**rep 子集不变量（v44）**：`observed_unit_ids` = replicates 中所有 unit_id 去重；`complete_unit_ids` = **rep_index 集合完整 {0..FORMAL_REPS−1} 的 unit（v45：仅由 rep_index 完整性判定，不要求 status!=ERROR——ERROR rep 但 5 次尝试均有记录仍计 complete/executed；REP_ERROR 与 matrix_complete=true 可共存）**；要求 `observed ⊆ EXPECTED − rejections`、`executed_units = len(complete_unit_ids)`、`complete ⊆ observed`；部分 rep unit 可在 observed 但不计 executed；`matrix_complete=true` 时 observed = complete = EXPECTED − rejections，**false 时允许任意子集、不限于前缀；仅 rep_index 中断的 unit 因前缀不全不计 complete，v45）** | 受影响 gate = NOT_APPLICABLE | `INVALID_RESULTS_SCHEMA_OR_INFRASTRUCTURE` |
| `REP_ERROR` | formal：`matrix_complete=true` 且任一 rep `status=ERROR`（`any_rep_error=true`；server 健康时单 rep ERROR 后**继续剩余 matrix**，§3.7） | 各 gate 保持原值（ERROR rep 不参与 G-M0-4 归因） | `INVALID_RESULTS_SCHEMA_OR_INFRASTRUCTURE` |
| `G7_SCHEMA_FAIL` | formal：G-M0-7 FAIL（落盘缺键/逻辑约束违反，缺键先置 FAIL） | G-M0-7 = FAIL | `INVALID_RESULTS_SCHEMA_OR_INFRASTRUCTURE` |
| `PARTIAL_REJECTION` | formal：`meta.preflight_rejections[]` 非空（≥1 unit 被拒，其余执行） | 各 gate 正常评估 | `HOLD_NOT_VALIDATED` |
| `DECISION_FALLBACK` | formal：任一 rep `decision_fallback=true`（`any_fallback=true`） | **G-M0-1 保持专用验证原值** | `HOLD_NOT_VALIDATED` |
| `G1_FAIL` | formal：专用验证合法率 <100%（G-M0-1=FAIL） | G-M0-1 = FAIL | `HOLD_NOT_VALIDATED` |
| `CORRECTNESS_FAIL` | formal：G-M0-2 隔离 / G-M0-5 对照 / G-M0-3a 回收任一 FAIL | 对应 gate = FAIL | `REJECT_CORRECTNESS_OR_ISOLATION` |
| `STABILITY_FAIL` | formal：G-M0-6 复现 / G-M0-4 归因任一 FAIL | 对应 gate = FAIL | `HOLD_UNSTABLE_MEASUREMENT` |
| `ALL_PASS` | formal：全部 required gates PASS 且无 fallback / rep ERROR / rejection | 全 PASS | `PASS` |

- **优先级（高→低，命中即终值）**：`SCHEMA_INVALID` = `PREFLIGHT_INFRA` = `FORMAL_INCOMPLETE` = `REP_ERROR` = `G7_SCHEMA_FAIL`（INVALID_RESULTS_SCHEMA_OR_INFRASTRUCTURE 类；**`FORMAL_INCOMPLETE` 在 `REP_ERROR` 之前**——matrix_complete=false 先命中）> `PARITY_MISMATCH` = `ALL_BUDGET_REJECTED` = `PARTIAL_REJECTION` = `DECISION_FALLBACK` = `G1_FAIL`（HOLD_NOT_VALIDATED 类）> `CORRECTNESS_FAIL` > `STABILITY_FAIL` > `ALL_PASS`；**INVALID 类恒高于 HOLD 类**（即使并存，如 REP_ERROR + G1_FAIL → REP_ERROR 生效）。
- **gates status 值域**：`PASS | FAIL | NOT_APPLICABLE`（G-M0-3b 恒 NOT_APPLICABLE）。
- **顶层 verdict 值域**：`PASS | HOLD_NOT_VALIDATED | HOLD_UNSTABLE_MEASUREMENT | REJECT_CORRECTNESS_OR_ISOLATION | INVALID_RESULTS_SCHEMA_OR_INFRASTRUCTURE`。

> 注：G-M0-7 只负责**落盘 schema/必需键**完整性；parity 失败已由 `PARITY_MISMATCH` 前置处理（不再挂 G-M0-7）。

**确定性聚合伪代码（v12 定稿，规则名称化）**：
```
# 前置：schema_validator(doc) 先执行（顶层键集精确五键 + phase 合法 + 值域 + 必需字段类型），
#       非法 → 返回 SCHEMA_INVALID（INVALID_RESULTS_SCHEMA_OR_INFRASTRUCTURE），不进入聚合
def aggregate_verdict(doc, any_fallback, any_rep_error, any_preflight_rejection):
    g = doc["gates"]                                   # formal gates，一律 .get(default)，不 KeyError
    if doc["meta"]["phase"] == "preflight":
        return doc["verdict"]                          # 前置失败已由落盘函数确定（PARITY_MISMATCH/ALL_BUDGET_REJECTED/PREFLIGHT_INFRA），不重算
    # ---- formal（优先级高→低）----
    if not doc["meta"].get("matrix_complete", False):
                                 return "INVALID_RESULTS_SCHEMA_OR_INFRASTRUCTURE"  # FORMAL_INCOMPLETE（优先于 REP_ERROR；绝不 PASS）
    if any_rep_error:            return "INVALID_RESULTS_SCHEMA_OR_INFRASTRUCTURE"  # REP_ERROR（matrix_complete=true 才命中）
    if g.get("G-M0-7","FAIL")=="FAIL": return "INVALID_RESULTS_SCHEMA_OR_INFRASTRUCTURE"  # G7_SCHEMA_FAIL
    if any_preflight_rejection:  return "HOLD_NOT_VALIDATED"                        # PARTIAL_REJECTION
    if any_fallback:             return "HOLD_NOT_VALIDATED"                        # DECISION_FALLBACK（G-M0-1 保持原值）
    if g.get("G-M0-1","FAIL")=="FAIL":   return "HOLD_NOT_VALIDATED"                # G1_FAIL
    if g.get("G-M0-2","FAIL")=="FAIL" or g.get("G-M0-5","FAIL")=="FAIL" or g.get("G-M0-3a","FAIL")=="FAIL":
                                 return "REJECT_CORRECTNESS_OR_ISOLATION"           # CORRECTNESS_FAIL
    if g.get("G-M0-6","FAIL")=="FAIL" or g.get("G-M0-4","FAIL")=="FAIL":
                                 return "HOLD_UNSTABLE_MEASUREMENT"                 # STABILITY_FAIL
    return "PASS"                                                                    # ALL_PASS
```
- **优先级原则**：`REP_ERROR`/`G7_SCHEMA_FAIL`（INVALID_RESULTS_SCHEMA_OR_INFRASTRUCTURE 类）**恒高于** `PARTIAL_REJECTION`/`DECISION_FALLBACK`/`G1_FAIL`（HOLD_NOT_VALIDATED 类）——并存时 INVALID 类生效（如 REP_ERROR + G1_FAIL + fallback 并存 → REP_ERROR）。
- **H3 边界**：聚合只读 `doc["meta"]["phase"]` 与顶层 `doc["verdict"]`（preflight 分支），formal 只读 `doc["gates"]`（get/default）；**绝不把 meta 子对象与顶层 doc 混淆**；schema validator 在任何聚合逻辑之前执行。
- **PASS 的唯一充分条件（ALL_PASS）**：`phase=formal` 且 `G-M0-1..7（required，G-M0-3b 除外）全部 PASS` 且 `parity_ok=true` 且 `any_fallback=false` 且 `any_rep_error=false` 且 `any_preflight_rejection=false`。
- **fallback rep 的归属**：带 `decision_fallback:true` 标记，**可继续参与内存归因 G-M0-4**（内存压力行为与决策来源无关），但**排除在真实决策质量声明之外**（不并入决策合法率、不参与 G-M0-1 统计）；**ERROR rep 不参与 G-M0-4 数值归因**（其数据不可信）。
- `meta/modes/gates/verdict` 关系：`meta` 记录全局配置与决策验证结论（`phase`/`preflight_status`/`preflight_reason`、`decision_validation`、`planned_units`/`executed_units`/`preflight_rejections[]`、`decision_validated`/`decision_fallback` 聚合、`parity_ok`/`parity_compensation`/`token_count_method`）；`modes.{off,on}.server_groups[].replicates[]` 为逐 rep 数据（含 rep 级 `status`/`decision_fallback`/`error_type`/`error_stage`/`error_bucket`）；`gates` 为门禁判定明细（每 gate 一个 `status`）；`verdict` 为上述聚合伪代码的单值结论。

### 5.2 指标字段

| 类别 | 字段 | 来源 |
|---|---|---|
| GPU/RSS 峰值 | `peak_gpu_mb` / `peak_rss_mb` | `sampler.find_server_gpu_mb/rss_mb`（行级 max，`metrics.py:104-105`）；GPU 口径 = nvidia-smi `memory.used`（total 8188、空闲 used≈40，增量 = used−40） |
| KV（attention） | `capacity_cells/used_cells/shared_cells/active_sequences/capacity_bytes/used_bytes` | `/metrics/kv`（KVProbe 快照 + `run_aggregate`） |
| token 分解 | `timings.prompt_n/cache_n/predicted_n`（`prompt_n+cache_n==prompt_tokens`） | OAI timings（`server-context.cpp:568-576`） |
| 延迟 | `latency_ms`；**TTFT 代理 = `timings.prompt_ms`**（= `t_prompt_processing`，`server-context.cpp:342/571`；llama-server 无独立 TTFT 字段） | driver 行 / timings |
| 输出 | `tokens`（数组）+ `content_sha256` + `finish_reason` | driver/runner 提取（e15 模式） |
| 分支质量 | `task_success`、canary 泄漏标志、决策点合法率、回收断言 | workload gate |
| rep 级状态（v16） | `status`（`OK\|INVALID_DECISION\|ERROR`，状态机 §5.1 + 逻辑约束：`INVALID_DECISION ⇔ decision_fallback=true`；`OK` 必须 `decision_fallback=false`；`ERROR` 必须 `decision_fallback=false` 且 `error_type` 为固定 error code（可选 `error_stage`/`error_bucket`；**不保存原始异常/路径/URL/prompt**））、`decision_fallback`（bool）、`error_type`/`error_stage`/`error_bucket` | runner 写入；**G-M0-7 对 formal 每 rep 校验这些必需键与逻辑约束** |

**recurrent/attention 细分 —— 最小可观测性缺口（如实记录，本阶段不实现 llama.cpp 改动）**：
- `/metrics/kv` 只统计 attention（`llama-memory-hybrid.cpp:190-194`）；recurrent 内存**无运行期接口**（仅启动日志 `RS buffer size` + 退出时 `common_memory_breakdown_print` 的合并 `context` 列）。
- M0 归因方案：**启动日志解析 `RS buffer size`（`llama-memory-recurrent.cpp:115`）与 `R/S (f32)`（`:123-126`）+ nvidia-smi 峰值差分**（全模型峰值 − 空载基线 = KV+RS+compute），配合 §1.3 精确公式（RS = 24×548864×4×N、KV = 8×8×128×2×elem×ctx）分离各成分。后续若需运行期 recurrent 计数，需 llama.cpp 最小扩展（`llama_kv_stats` 增加 recurrent 字段或 `/metrics/kv` 加 breakdown）——**列为未决问题 §9.1，不在 M0 承诺**。

### 5.3 指标语义区分

- **"预分配峰值不变"**：`capacity_bytes`/`capacity_cells` 由 ctx 决定（§1.3 实测 68 MiB@ctx4096），分支数不改变 capacity —— 这是预期，不是收益；
- **"used cells/容量改善"**：`used_cells` 随分支活跃/回收变化；改善指标 = 同 capacity 下承载更多活跃分支（used_cells 峰值占比）或回收后回落；
- **recurrent 是唯一随分支线性增长的显存项**（50.25 MiB/slot）→ 分支容量上限由 `(GPU 余量 − 固定 KV/权重) / 50.25` 决定，量化验收用此式与实测对账（G-M0-4）。

---

## 6. 实现文件清单与合同（下一阶段，审查修订）

| 文件 | 类型 | 内容 |
|---|---|---|
| `benchmark/framework/fanout_prompts.py` | 新增 | 纯函数：决策点 prompt（枚举约束）、分支扩展、canary 注入/检测、`ACTION: branch(bX)` 解析、**token 精确计数客户端（/apply-template + /tokenize，§4.2；端点不可用 → 报 preflight 失败，不降级继续）**、预算函数 `budget(P,B,N)` 与 fail-fast、**结果 doc 构造（§5.1 canonical meta 表）；所有写入委托 `m0_schema.py` atomic writer（v33）** |
| `benchmark/runner/m0_schema.py` | 新增 | **canonical schema 模块（v44 整理：引用 §5.1 当前 schema，不标注固定版本号）**——
  **常量**：`CANONICAL_META_KEYS`（meta 键集代码单一来源，builder/validator/测试⑯ 引用）、`SCHEMA_INVALID_FIXED`（SCHEMA_INVALID 固定值参数化）、`MATRIX_PLAN`（`FORMAL_REPS=5`/`WARMUP_REPS=2`，validator 派生 rep_index {0..4}）、**`MATRIX_PLAN` 合法组合映射（v47 单一权威）**：`fanout=2 → buckets [short, medium, long]`（3 桶）、`fanout=4 → [short, medium]`（2 桶）、`fanout=8 → [short]`（1 桶）；cache profiles 两种（q8_0/q8_0、f16/f16）、controls off/on；**`EXPECTED_UNIT_IDS`（v47 单一定义，合并 v45/v46 两处）**：= 由 MATRIX_PLAN 合法组合映射 × 2 cache profile × 2 control 笛卡尔积重建的 **24 个 unit_id 集合**（= Σ桶数(3+2+1) × 2 × 2 = 6×2×2；不再写 fanout×所有 bucket 的 36 歧义）；ID 格式 `{control}:{ctk}-{ctv}:f{fanout}:{bucket}`（fanout∈{2,4,8}、bucket 按 §5.1 枚举）；validator 与测试㉗ 统一引用、**代码单一来源、不落 Markdown 手工清单**；测试断言 **len==24 且与映射重建集合精确相等**；exit constants `EXIT_USAGE=64`/`EXIT_SCHEMA_INVALID=65`/`EX_SOFTWARE=70`/`EXIT_IO_WRITE_FAILED=74`；
  **validator**：`validate_result_envelope`（完整五键+phase+verdict+meta 固定键集+phase 合法组合表）、`validate_schema_sidecar`（错误数组+独立 6 枚举+RFC6901+类型白名单）、parity_progress.errors 四元组 validator（{code,stage?,bucket?,template?}）、error_summary 三元组 validator（{code,stage?,bucket?,count}）、**modes/group/rep validator（v44 唯一权威段）**：modes 只含 `{off,on}.server_groups[]`（唯一权威路径、扁平 replicates 已删除）；group 对象 = {server_group_id, ctk, ctv, fanout, parallel, start/stop RFC3339, status COMPLETED|ERROR, error_type?, baseline, warmup_count=2, replicates[]}；rep 对象 = {unit_id, rep_index, fanout, bucket, ctk, ctv, prefix_len:int, branch_len:int, server_group_id, status, decision_fallback, error_type?, error_stage?, error_bucket?, §5.2 metrics}；`(unit_id, rep_index)` 全局唯一、unit_id 格式 `{control}:{ctk}-{ctv}:f{fanout}:{bucket}`、unit↔group 引用关系校验；**FORMAL_INCOMPLETE 集合校验（v45）**：observed_unit_ids ⊆ EXPECTED_UNIT_IDS−rejections、executed_units==len(complete_unit_ids)、complete ⊆ observed、matrix_complete=true 时 observed==complete==EXPECTED_UNIT_IDS−rejections、false 时允许任意子集、不限于前缀；
  **builder**：`build_preflight_envelope(context, *, reason, source_phase, parity_ok, parity_progress, token_count_method, decision_validation, preflight_rejections)`（调用方传 parity 状态，builder 不按 reason 硬编码；schema_invalid/validation_incomplete 共用但传不同值）；
  **atomic writer**：`.m0_fanout_<ts>.<kind>.tmp` + fsync + rename 顺序（sidecar 先、主 envelope 后）+ fsync 目录 + 陈旧 tmp 清理；runner 调用；测试归属 `tests/test_m0_schema.py` |
| `benchmark/tests/test_m0_schema.py` | 新增 | canonical validator 正反例、sidecar 枚举/JSON Pointer/类型白名单、原子写顺序（普通主结果 + SCHEMA_INVALID 双文件）、退出码、陈旧 tmp 清理、**source_phase 规则正反、SCHEMA_INVALID 逐字段组合与错配反例（v32）** |
| `benchmark/runner/m0_fanout_runner.py` | 新增 | CLI + 矩阵编排 + server 生命周期（复用 e15 骨架）+ barrier 并发 + gate（G-M0-1..7）+ 唯一顶层 schema 落盘（§5.1） |
| `benchmark/framework/driver.py` | 修改 | **chat() keyword-only 扩展（temperature/seed/max_tokens/extra_body，§3.3）**，默认 None 保持旧行为 |
| `benchmark/tests/test_fanout_prompts.py` / `test_m0_fanout_runner.py` | 新增 | 纯函数（决策解析确定性/canary/**预算函数合法/非法组合表单测**/tokenize 客户端 mock/fail-fast/gate 判定/CLI）+ mock server e2e |
| `benchmark/tests/test_driver.py` | 修改 | 新增 chat 扩展用例（默认 None 旧行为、透传生效、keyword-only 兼容、extra_body 合并） |
| 文档 | 更新 | 本文件补实测结果 + AGENTS.md 状态更新 |

**第 2 步实现记录（2026-08-09，真实落地，非虚构）**：
- 新增 `benchmark/runner/fanout_prompts.py`（MATRIX_PLAN/BUCKET_TARGETS/budget/决策协议/canary/ACTION 解析/INVALID_DECISION 谓词，纯函数）；
- 新增 `benchmark/runner/m0_schema.py`（CANONICAL_META_KEYS=22/SCHEMA_INVALID_FIXED/EXPECTED_UNIT_IDS=24/FORMAL_REPS=5/WARMUP_REPS=2/退出码 64·65·70·74/builders/validator（含 parity 四元组、error_summary 三元组、decision session 两来源不变量、modes/group/rep、集合不变量）/atomic writer/aggregate_verdict + rule_of）；
- 新增 `benchmark/runner/m0_fanout_runner.py`（CLI 8 未知参数 → EXIT_USAGE 64；15 顺序 server 生命周期（校准 1 + 验证 2 + formal 12 groups）；首 formal group 启动失败 → preflight/PREFLIGHT_INFRA、第 2+ 启动失败/运行崩溃 → formal/FORMAL_INCOMPLETE；warmup 不落 replicates、每 unit 5 reps；barrier 并发分支；rep 状态机含 ERROR 必 decision_fallback=false；SCHEMA_INVALID envelope+sidecar 原子落盘（65/70/74）；server cleanup 固定序列）；
- 修改 `benchmark/framework/driver.py`（chat() keyword-only 扩展 + sdk_max_retries + /apply-template、/tokenize、count_tokens；`_retry` 保持位置参数，既有调用兼容）；
- 修改 `benchmark/tests/mock_server.py`（/apply-template、/tokenize 端点 + 实例级 crash_at 崩溃模拟）；
- 新增测试 124 个（test_fanout_prompts 22、test_m0_schema 80、test_m0_fanout_runner 22）；
- 实测：`cd benchmark && uv run pytest -q` → **`537 passed`**（413 + 124）；mock CLI smoke（test_main_smoke）生成并校验 `phase=formal` 合法 schema 结果；崩溃第 5 rep / 首组 / 第 2+ 组失败路径均由 mock e2e 断言；
- **未执行**：正式 4B 矩阵、真实 llama-server 长跑、GPU 采样（本步仅代码+测试+mock smoke）；llama.cpp 零改动。

**不改**：`config.py`、`runner.py`、`workload/`。

CLI 合同（v12 收紧）：`--server-bin --model [--port-base N] --ctx-size 4096 --decision-n-predict 16 --branch-n-predict 64 --tool-rounds M --out <path>（**v44：删除 --warmup/--reps——固定 MATRIX_PLAN 常量 FORMAL_REPS=5、WARMUP_REPS=2；显式传入属未知参数 → EXIT_USAGE 64**） [--tmp-dir <dir>]`（**v41：正式入口一次恒跑完整 24-unit 矩阵——删除用户单值 `--fanout/--prefix-len/--branch-len`，这些维度由固定 `MATRIX_PLAN` 定义（fanout ∈ {2,4,8}、bucket 枚举、per-unit 派生）；可保留独立 debug/smoke 模式但不得产出 formal verdict（暂不设计，v41）**）（**v39：删除 --ctk/--ctv 单值覆盖——M0 formal 矩阵固定遍历两种 cache profile：`q8_0/q8_0` 与 `f16/f16`，每 unit meta 写实际 ctk/ctv；用户不可缩减该维度，planned_units 恒 6×2×2=24**）；**`parallel` 由 runner 恒等派生 `parallel := fanout+2`（v37：删除恒真的 `parallel>=fanout+2` 输入校验——输入不暴露 parallel、无手工覆盖面）。****端口合同（v12）**：`--port-base` **可选**；未给时由 OS/端口探测分配（socket bind 探测，同 §3.7）；给了则 runner 每次启动从 base 起向上寻找可用端口；**校准 server、G-M0-1 验证 ×2、formal server 各自独立端口/进程**，每次 `finally` 停止——删除含糊的必传 `--port`。**临时目录合同（落盘/顶层键习惯对齐 E15；gates 子结构为 M0 自有）**：`--tmp-dir` 为**可选用户参数**（缺省时 runner 用 `tempfile.mkdtemp()` 创建、退出时 `shutil.rmtree` 清理——**此"创建并主动清理"是 M0 相对 E15 的增强**，E15 runner 接受 `--tmp-dir` 但清理语义未承诺）；`--slot-save-path` **不作为独立用户参数**，由 runner 内部绑定为同一临时目录（`--slot-save-path <tmp-dir>`）传给 server——保证 `POST /slots/:id?action=erase` 可用（slot erase 依赖该路径，`e15_branch_concurrent.py:548-560` 先例）且生命周期随 runner 清理。**"对齐 E15"仅指**：结果落盘位置（`results/`）与顶层键集 `{meta, modes, gates, verdict, notes}`（`e15_branch_concurrent.py:795-831`）；`gates` 子结构（§5.1 三值 status 与 G-M0-1..7 定义）为 M0 自有，不沿用 E15 的 gate 字段。**CLI/config 职责（v42 定稿）**：CLI **仅校验语法、类型、必需参数与已暴露选项的值域**；**`--fanout / --prefix-len / --branch-len / --ctk / --ctv / --parallel / --warmup / --reps` 共 8 项全部为未知参数（v45：--warmup/--reps 并入权威清单；矩阵枚举/结构只由内部 `MATRIX_PLAN` validator 保证，v42：删除旧合法 fanout/bucket/parallel 校验）——显式传入 → `EXIT_USAGE=64` + stderr `INVALID_CONFIGURATION`、不落盘实验结果**；**预算职责（v36/v37）**：**静态预算函数先排除 long×4 / medium×8 / long×8（设计/计划构建阶段，不进入 planned_units、不写 preflight_rejections，v36）**；`budget(P,B,N) > 3481` 的 **24 候选（planned_units）经真实 apply-template+tokenize 精确复核后进一步超限的 unit** 记入 `meta.preflight_rejections[]` 并排除；其余合法 unit 继续执行（formal 只运行剩余 unit，`executed_units = 24 − |rejections|`）；存在任何 rejection → 顶层 HOLD_NOT_VALIDATED（PARTIAL_REJECTION）；**全部 24 候选被拒 → 落盘 phase=preflight HOLD_NOT_VALIDATED（ALL_BUDGET_REJECTED，v37：仅指 24 候选经精确复核全部被拒，静态 3 组合不参与）**——**"不再 CLI 直接退出/不落盘"限定为预算行为（v35），预算超限不由 CLI 64 拦截**。

**测试计划（v44：以 §5.1 测试要求 ①–㉗ 为唯一权威来源，本段仅摘要，避免双轨漂移）**：纯函数 pytest（决策点解析/确定性/canary/预算函数与逐 unit 排除/rep 状态机/transient retry 分层/decision_validation 落盘/schema validator 与 phase 约束/aggregate_verdict 优先级/Driver retry 与递归透传/端口生命周期/parity_progress/formal server 失败落盘）+ mock OpenAI server e2e（现有 `tests/mock_server.py` 模式）+ 可选 TinyLlama CPU smoke（仅冒烟，不宣称 4B 结论）；**完整用例编号与断言见 §5.1 ①–㉗（v45：句首句尾统一权威范围），本段不再重复列项**。

---

## 7. 8GB 可行性（探针实证，§1.3）

- 4B Q4_K_M + ctx4096 + unified + parallel 10（矩阵最大）+ q8_0：**GPU 峰值 3402 MiB / 8188（41.5%）**，全部合法组合 < 3.5 GiB；
- 权重 CUDA0 2571.63 MiB（+ CPU_Mapped 497.31）；attention KV 固定 68（q8）/128（f16）MiB；recurrent 50.25 MiB/slot 线性；
- 分支容量理论上限：`(8188 − 2572 − 68 − 系统/compute ~400) / 50.25 ≈ 102 slot`，但 decode 吞吐/批处理预算先于显存成为限制 → 矩阵取 fanout 2/4/8 是安全的。
- **结论：矩阵无显存阻塞；无需降级配置**。

---

## 8. 实施建议与量化验收标准

**建议：GO** —— 调研充分（全部关键接口有 file:line 证据）、可行性实证（§1.3/§7 探针）、矩阵合法（§4.2 公式收敛）、纯新增可回滚、风险可控。

可量化验收标准（M0 实现阶段）：
| 门禁 | 标准 |
|---|---|
| G-M0-1 决策确定性（v24：专用验证协议，与矩阵分离） | **专用稳定性验证，与正式矩阵数据完全分离**：**2 次独立 server 会话，每次 ≥10 请求（总数 ≥20；v54：session 0 → control off、session 1 → control on，`VALIDATION_CONTROLS=("off","on")`，session 记录含 `control` 字段）**，temp=0/seed=42 下 4B 决策点合法 `ACTION: branch(bX)` 输出率 **100%**；**INVALID_DECISION 谓词（三处同一）**：`(finish_reason=="length") OR (输出不含合法 ACTION)` → 任一即该 rep `status=INVALID_DECISION`（rep 级，非顶层 verdict；length 截断即使含 ACTION 也不可信）；**合法率 <100% → G-M0-1 = FAIL → 顶层 `HOLD_NOT_VALIDATED`**；**v23-v26 边界**：验证 server 崩溃但 session 达 ≥10 且证据完整（每请求有 OK/INVALID_DECISION/ERROR 记录，含最后 in-flight 崩溃）→ **仍计入验证、继续 formal，不要求崩溃后的 health**；**任一验证 session 启动失败即停止验证阶段、不再启动后续 session（v25/v26）**；session 不完整（requests<10）/ 启动失败 / 证据无法落盘 → `PREFLIGHT_INFRA`（decision_validation：无任何证据 → null、已有第一/部分当前 session → partial）；**正式矩阵单 rep 决策失败仅记录 `status=INVALID_DECISION` 并走固定路由 fallback 继续内存压力实验，不计入 G-M0-1，不宣称真实决策 PASS**；fallback 触发 → `decision_fallback: true` + verdict 标记（§5.1） |
| G-M0-2 隔离 | canary 跨分支泄漏率 **0**（**v49：基于成功分支观测——ERROR rep 跳过；全矩阵无任何非 ERROR 分支观测 → `NOT_APPLICABLE` 绝不静默 PASS；至少 1 个非 ERROR 分支观测才判定**） |
| G-M0-3 回收（v3：不形成伪门禁） | **G-M0-3a（门禁）**：每 rep 末 `erase` 后 `/metrics/kv` `used_cells==0 && active_sequences==0` —— **仅证明 attention cells 回收**；**G-M0-3b（smoke，非门禁）**：全部 reps 完成后 nvidia-smi `used` 回落至空载基线（±50 MiB）—— **只做显存泄漏 smoke，不证明 recurrent state 回收**；recurrent 回收**保持不可观测缺口**（无运行期接口，§5.1），M0 不承诺、不验收 |
| G-M0-4 内存归因（v4：启动日志精确值门禁） | **主门禁**：**group.baseline 内联字段（v42：rs_buffer_mb / kv_buffer_mb，不依赖易丢日志路径）**对账 `RS buffer size`（`llama-memory-recurrent.cpp:115`）精确值 vs 公式 `24×548864×4×parallel` 误差 **≤5%**；attention KV 同理（`llama_kv_cache` 分配 68/128 MiB vs `8×8×128×2×elem×4096`）；**按每个 server_group 的 actual profile/parallel（v42：baseline 内联字段与 group.ctk/ctv/parallel 对账，错配 → G-M0-4 FAIL/SCHEMA_INVALID）**；按每个 server_group 的 parallel/ctk/ctv 分别对账启动日志 RS/KV 与公式（v41）**；**GPU used 差分只作总量 sanity**（阈值 ±10%，口径 = nvidia-smi `memory.used` 增量，探针观测 run-to-run 波动 <10%） |
| G-M0-5 对照有效性 | 4B 上 off vs `--kv-prefix-share`：两者 `shared_cells` 恒 0，且 on 会话日志含 `E8-C1: capability rejected: hybrid`（100%） |
| G-M0-6 复现 | 同配置两次独立 run：TTFT（`timings.prompt_ms`）/latency 中位数偏差 ≤10%，决策分支归属一致 |
| G-M0-7 数据落盘 | 每 rep 完整记录 §5.2 字段，无缺键；`results/m0_fanout_*.json` 可独立复算 summarize |

**输出（M0 实现交付）**：一份 `branch memory baseline` 报告，回答"fan-out 内存/延迟主成分"（预期：recurrent state 线性项主导显存，attention KV 固定池，重复 prefill 主导延迟；以实测为准，不预设结论）。

---

## 9. 未决问题

1. **recurrent 内存运行期观测缺口**：需 llama.cpp 最小扩展（`llama_kv_stats` 加 recurrent 字段）或接受"启动日志 + 差分"近似——M0 用后者，列为后续优化候选。
2. **4B 决策点提示工程**：需 0.8B 离线校准（CPU，可跑）→ 4B 验证；若枚举约束下 4B 仍不稳定，fallback = 固定决策映射且判定 **`HOLD_NOT_VALIDATED`**（规则 `DECISION_FALLBACK`/`G1_FAIL`）。
3. **temp=0 下分支"选择/回收"轮语义**：模型回收/选择轮的稳定性需实测（**决策/分支协议统一为 `ACTION: branch(bX)`**；回收走 `erase` 固定路径）；失败则降级为固定回收（`erase`），不阻塞内存归因目标。
4. **parallel 10 + long 桶的 decode 吞吐**：单线程调度下 latency 可能高，M0 只报告、不优化（且 long×大 fanout 已被预算公式排除）。
5. **build-cuda 二进制版本**：探针用 8569/afbf375c6（核心代码与 HEAD 等价，因 4a699aaad 仅新增测试文件）；正式 M0 实验前重建到 4a699aaad（version 8570）保证版本号一致。

## 10. 风险

- Laptop GPU 抖动（E3 门禁先例）→ warmup + 中位数 + 同硬件 off/on paired。
- 0.8B 指令遵循不稳定 → 决策点枚举约束 + 离线校准（§9.2 fallback → `HOLD_NOT_VALIDATED`）。
- 模型可用性：4B GGUF 已就位（2.7 GB，`models/qwen3-5-4B-Q4_K_M.gguf`）；0.8B 需 `download_models.sh 0.8b`（M0 实现时按需拉取）。

## 11. 实施清单（下一步，不在本阶段执行）

1. 新增 `benchmark/framework/fanout_prompts.py` + 纯函数测试（决策点解析/确定性/canary/预算公式 fail-fast）；
2. 新增 `benchmark/runner/m0_fanout_runner.py`（复用 e15 生命周期/gate 骨架）+ mock e2e 测试；
3. `cmake --build build-cuda` 重建 4B 实验二进制到 HEAD（4a699aaad）；
4. 0.8B 决策点校准 → 4B 决策确定性验证（G-M0-1，<100% → `HOLD_NOT_VALIDATED`）；
5. 跑合法矩阵（§4.2，6 组合 × 2 ctk × 2 对照 = 24 单元）→ 落盘 → 归因报告（§8 输出）→ 对照 G-M0-2..7；
6. 结果归档 `benchmark/baseline/` + 报告文档 + AGENTS.md 状态更新。

---

## 12. 阶段 0 实测记录（2026-08-09，v55）

> 真实 Qwen3.5-4B GPU 短校准（**未跑 24-unit formal 矩阵**）。证据归档：
> `benchmark/baseline/qwen35-4b_gpu_m0_calibration_20260809.json`（主结果）与
> `benchmark/baseline/qwen35-4b_gpu_m0_probe_evidence_20260809.json`（GGUF/pynvml/nvidia-smi/RS/KV 探针）。
> 复现命令（精确，v56 入库模块入口，相对路径 clone 后可用）：
> `cd benchmark && uv run python -m runner.m0_calibration_runner \
>   --server-bin ../llama.cpp/build-cuda/bin/llama-server \
>   --model ../models/qwen3-5-4B-Q4_K_M.gguf \
>   --out ../benchmark/results/m0_cal_4b_runN.json \
>   --tmp-dir ../benchmark/results/m0_cal_4b_runN_tmp --run-id m0-cal-runN`
> （复用 M0FanoutRunner 生命周期：calibration + off/on decision-validation + 可选
> parallel10 探针；不调用 CLI/run()/run_formal()；`--keep-tmp` 保留 server 日志；
> 报告自带 `server_logs_summary` 结构化摘录，不依赖 gitignore 的 results 日志。
> **历史临时脚本 `results/m0_cal_4b.py` 已由入库入口取代、不作为复现入口**）。

### 12.1 环境与构建

- **llama.cpp 重建**：`cmake --build build-cuda -j $(nproc)`（GGML_CUDA=ON，CMAKE_CUDA_ARCHITECTURES=89）；
  `llama-server --version` → **`8570 (4a699aaad)`**（与 HEAD 一致）；llama.cpp 零源码改动。
- **GPU**：NVIDIA GeForce RTX 4060 Laptop GPU，8188 MiB，driver 610.43.03；`nvidia-smi` 可用。
- **pynvml 依赖**：`sampler.find_server_gpu_mb` 曾返回 null——根因是旧 `.venv` 未安装
  `nvidia-ml-py`（pyproject.toml 已声明 `nvidia-ml-py>=13.610.43`）；`uv sync` 后 `pynvml OK, devices=1`。
  注意：**校准脚本须用 `uv run python` 运行**（系统 python3 无 pynvml）。
- 无遗留 llama-server（校准全程每阶段清理，最终 `leftover_pids=[]`）。

### 12.2 代码修复（v55，真实首跑暴露）

1. **端口错配（根因）**：`_server_cmd` 原用 `--port self._next_port`（已 +1），而 `adapter.port` 为递增前
   值——真实 server 监听 `next_port+1`、`wait_health` 探测 `port` 永远超时（mock 测试不启动真实进程未暴露）。
   修复：`_server_cmd(..., port)` 显式传参；回归测试 `test_server_cmd_port_matches_adapter`。
2. **日志级别**：`RS buffer size`（llama-memory-recurrent.cpp:115）与 `llama_kv_cache` 分配行
   默认 verbosity=3 不打印（实测确认）→ `_server_cmd` 加 `-lv 5`（G-M0-4 独立观测前置）；测试断言。
3. **decision-validation 双 control（v54）**：2 sessions = off/on 各一（`VALIDATION_CONTROLS`），
   session 记录含 `control` 字段；测试 `test_m0_v54_validation_controls.py`（5 例）。

### 12.3 校准结果（parity）

- **parity_ok = true**：6 桶（short-P/B, medium-P/B, long-P/B）全部 completed、`errors=[]`；
  三次独立运行结果完全一致（apply-template+tokenize 与 chat prompt_tokens 一致，add_special 归因正确）。
- **calibrated_lengths（真实 apply-template+tokenize，P prefix / B branch tokens）**：

  | (bucket, fanout) | prefix | branch |
  |---|---|---|
  | short / 2 | 260 | 344 |
  | medium / 2 | 420 | 504 |
  | long / 2 | 551 | 635 |
  | short / 4 | 274 | 359 |
  | medium / 4 | 434 | 519 |
  | short / 8 | 302 | 386 |

- **preflight_rejections = []**：24 候选预算（`budget(P,B,N) ≤ 3481`）全部通过，无排除。
- **binary_version**：8570（/props build_info 尽力而为记录）。

### 12.4 decision-validation（G-M0-1 前置）

- **complete = true**：off/on 两 session 各 `requests=10, valid=10, invalid=0, error_count=0`；
  `invalid_length=0, invalid_no_action=0`；`finish_reasons={stop:10}`；`error_summary=[]`。
- **确定性 100%**：每 session 内 10/10 输出哈希完全相同（`03c79f28...`），且 off/on 哈希也相同
  ——temp=0/seed=42 下 4B 决策点稳定输出合法 `ACTION: branch(bX)`，control 开关不影响决策输出。
- **结论：G-M0-1 前置通过**（合法率 100%，无 length 截断、无 error）。

### 12.5 探针（parallel10 q8_0 + --kv-prefix-share）

- **RS buffer（G-M0-4 数据源）**：`llama_memory_recurrent: CUDA0 RS buffer size`——
  parallel4 = **201.00 MiB**、parallel10 = **502.50 MiB** → **50.25 MiB/parallel**；
  **与常量 `RS_BYTES_PER_ROW = 24×548864×4 / 1048576 = 50.25 MiB/parallel` 完全一致**（无需改常量，
  注释"0.8B 实测"实为 4B 架构参数，证据已更新）。
- **attention KV**：`llama_kv_cache: size = 68.00 MiB (4096 cells, 8 layers, 10/1 seqs), K q8_0 34 + V q8_0 34`
  ——与公式一致（qwen35 hybrid：8 attention 层 + 24 recurrent 层）；`/metrics/kv` 实测
  `capacity_bytes=71303168`（68 MiB）、`capacity_cells=4096`、`shared_cells=0`、`physical_sharing=false`。
- **G-M0-3b 前置**：pynvml 峰值 **3358 MiB**（probe 存活时 3354 MiB，4B ctx4096 p10 q8_0），
  空载 40 MiB → 显存增量 3318 MiB，8 GB 卡（8188 MiB）无 OOM 风险；nvidia-smi CLI 口径可用。
- **G-M0-5**：control=on 会话日志含 `E8-C1: capability rejected: memory implementation does not
  support cross-slot prefix metadata sharing (only standard unified attention KV is audited)`
  （dv_on + probe 各 1 行）；control=off 无该行——**真实 hybrid capability-rejected 日志存在**。

### 12.6 结论与限制

- **PASS**：parity（6 桶全对）、决策确定性（off/on 各 10/10）、24 候选预算（0 拒绝）、
  RS 系数（50.25 MiB/parallel 与常量一致）、KV 容量（68 MiB）、G-M0-5 日志存在。
- **具备进入完整 24-unit formal 矩阵条件**（parity 与决策门禁前置全过；剩余 G-M0-2..7 与
  TTFT/latency 主成分需正式矩阵采集）。
- **限制**：① 本次未跑 formal groups（12 server group 生命周期与 per-unit 落盘未实测）；
  ② 决策点使用 short/fanout8 模板（与正式矩阵一致）；③ GPU 采样需 `uv run`（pynvml 在
  uv 环境）；④ `-lv 5` 日志量较大（~2.6k 行/15s）——**正式矩阵日志策略 v56 定稿**：
  **完整 -lv5 日志仅保留在 `results/`/`tmp-dir`（gitignore，不入库）；canonical/baseline
  只归档每 server 的 key lines（RS buffer size / capability rejected / KV buffer size）+
  server tag + run_id + pid + phase（`server_logs_summary` 结构，同 m0_calibration_runner 报告），
  防止 -lv5 体积失控**；**v57（审查 Medium 2/6）：`server_logs_summary` 只用
  `M0FanoutRunner._server_meta` 真实映射（`_start_server` 成功时记录 tag →
  {port, pid(真实子进程 PID), started_at, log_path}）——禁止 sorted index /
  端口偏移推断 tag→PID；started_at 为真实启动时刻（非摘要生成时刻）；日志文本在
  server 停止后重新读盘刷新（避免 health 时启动期快照不完整）；启动失败/缺 tag 的
  server 不会错配后续 PID（无 meta 即无条目）**；
  ⑤ build-cuda 重建（含 -lv 5 无影响，llama.cpp 零改动）。

---

## 12. 完整 24-unit 4B formal 矩阵结果（2026-08-09，正式归档 run5）

**执行**：`cd benchmark && uv run python -m runner.m0_fanout_runner --server-bin ../llama.cpp/build-cuda/bin/llama-server --model ../models/qwen3-5-4B-Q4_K_M.gguf --ctx-size 4096 --out results/m0_formal_4b_run5_20260809.json --tmp-dir results --port-base 8080 --ngl 99 --cleanup-tmp-age 3600`（约 10 分钟，EXIT=0）。环境：RTX 4060 Laptop 8188 MiB / driver 610.43.03 / server b8570-4a699aaad / Qwen3.5-4B Q4_K_M。

**结果**：12 server groups 全 COMPLETED（off 6 + on 6）、24 units × 5 reps = 120、(unit_id,rep_index) 全局唯一、matrix_complete=true、planned/executed=24/24、rejections=0；gates G-M0-1/2/3a/4/5/7 **PASS**、G-M0-3b/6 NOT_APPLICABLE；**verdict=INVALID_RESULTS_SCHEMA_OR_INFRASTRUCTURE（REP_ERROR：36/120 reps connection_error）**。

**矩阵实测暴露并修复 3 个实现 bug**（commit 7d777bf/5acf962/a48a06f）：
1. G-M0-4 `_parse_rs_buffer_mib` 对真实 llama.cpp 日志多行求和（201.00×2=402 → 超差）→ 取首个匹配值（v63）；
2. G-M0-5 匹配串 `"capability rejected: hybrid"` 不匹配真实 4B 行（`memory implementation does not support cross-slot prefix metadata sharing`——llama_memory_hybrid 未 override 共享 → 源码 memory 分支先命中，server-context.cpp:1502）→ 改匹配稳定前缀 `"E8-C1: capability rejected:"`（v63）；
3. rep 缺 latency_ms/ttft_ms（§5.2 定义未落）→ build_rep 增可选字段 + _run_unit 聚合本 rep 全部请求 avg（v65）。

**off/on 对比**（OK reps，run5）：latency 528 vs 507 ms（on −4.1%）、ttft 287 vs 277 ms（−3.4%）——**v66 审计撤销该对比的有效性**（transient retry 未接入，30% rep ERROR 下 OK rep 子集不具代表性），仅记录原数值不宣称结论；shared_cells 恒 0（4B hybrid C1 被拒，G-M0-5 PASS 验证日志与基线，**与 retry 无关的结构性结论保持**）；decision_fallback=false。

**connection_error 归因（v66 审计修正）**：36/120 reps（30%）连接错误——**设计 §3.6/§3.8 要求的 transient wrapper 未接入实际调用**（Driver sdk_max_retries=0 且 runner 所有 chat 直调 driver），**不能称重试耗尽**；run1/2/3/5 均 17–30% 只能说明**无 transient retry 时 4B GPU 单线程 server 高并发（parallel 8–10 + 分支/工具轮）下的原始请求失败率**；**run5 降级 INVALID_INTERMEDIATE**（中间结果，不作为正式矩阵结论；证据文件 `audit_status=INVALID_INTERMEDIATE`）；ERROR rep 不参与 G-M0-4 归因。

**归档**：`benchmark/baseline/qwen35-4b_gpu_m0_formal_run5_20260809.json`（机器可读结果）+ `qwen35-4b_gpu_m0_formal_run5_evidence_20260809.json`（命令/环境/矩阵/gates；**v66 增 `audit_status=INVALID_INTERMEDIATE`**）+ **`qwen35-4b_gpu_m0_formal_run5_logevidence_20260809.json`（v66：server_logs_key_lines 15 tag 已入 baseline，log_evidence_ref 不再指向 gitignore results）**；完整 -lv5 日志留 `results/m0_fanout_*` 专属目录（gitignore 不入库）；run1/2/3 为修复前中间结果（run1 有 G-M0-4/5 FAIL、run2/3 有 notes 噪音），不归档为正式。

**限制**：
- rep 级 `peak_gpu_mb/peak_rss_mb` 在 run5 数据中仍为 0.0 占位（v66 已实现成功 row 取 max，未重跑——run5 数据保持旧值）；
- G-M0-6 NOT_APPLICABLE：单次 run 无复现对比（connection_error 30% 下是否需要 run2 复现以判定稳定性，待评估）；
- server_logs_summary 未写入 formal doc meta（v56 归档字段在 calibration 报告；formal 的 key lines 证据以本节的 log evidence JSON 归档）。
