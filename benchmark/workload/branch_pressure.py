"""branch_pressure —— 正式分支压力场景（E2.5 决策门禁 workload）。

设计目标（独立于 multi_turn/long_life/branch，不修改它们）：
- 两个并行分支（X/Y）匹配 parallel=2；
- 长共享前缀（system + 任务 + 工具历史，占 ctx 40%-70%）；
- 每个分支含独立工具观察、state 值与目标；
- 固定交替序列：X1 -> Y1 -> X2 -> Y2 -> X3 -> Y3 -> X-revisit -> Y-revisit
  （8 个请求，至少 4 次可计量回访：X2/Y2/X3/Y3 前缀复用 + X-rev/Y-rev 回访）；
- 模型输出固定 JSON {"branch","state","answer"}，evaluator 校验
  JSON 可解析 / branch 正确 / state 正确 / answer 规则 / 无跨分支 state
  / 同分支回访一致 / 跨分支有区分度。

正式实验使用 server-per-replicate 协议（scripts/e2_scan_e25.py），
本模块的 PROMPTS 构建与 EVALUATOR 与其共享，保证 workload hash 一致。
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from framework.config import BenchmarkConfig
from framework.driver import Driver
from framework.workload import Workload, WorkloadSpec, register

# ---------------------------------------------------------------------------
# 固定 prompt 内容（冻结后不得修改 —— manifest 记录 sha256）
# ---------------------------------------------------------------------------

SYSTEM = (
    "You are an autonomous environmental analysis agent operating in a remote "
    "monitoring network. You receive tool observations from distributed sensors "
    "and must report your analysis in strict JSON format. Never mention the "
    "other branch's observations. Only output the JSON object, nothing else."
)

TASK = (
    "Your mission is to analyze ecosystem health in the Corvus Delta region. "
    "The monitoring network has deployed sensors across five zones: riparian "
    "wetlands, upland forests, tidal marshes, gravel riverbeds, and delta "
    "channels. Each sensor reports water quality, soil composition, vegetation "
    "index, and wildlife activity at fixed intervals. You must track the "
    "accumulating evidence for your assigned branch, maintain a running state "
    "value, and produce a final assessment that summarizes the observed trends "
    "and recommends a course of action for the next monitoring cycle."
)

TOOL_HISTORY = (
    "Tool call log (shared baseline):\n"
    "  [t=00] sensor_status: all 24 sensors online, battery 88-96%.\n"
    "  [t=01] zone_scan(riparian): vegetation_index 0.62, turbidity 4.1 NTU, "
    "flow 12.4 m3/s.\n"
    "  [t=02] zone_scan(upland): canopy_cover 0.71, soil_moisture 0.34, "
    "wildlife_count 14.\n"
    "  [t=03] zone_scan(tidal_marsh): salinity 18.2 ppt, marsh_height 0.41 m, "
    "bird_count 37.\n"
    "  [t=04] zone_scan(gravel_bed): substrate_score 0.58, spawning_activity 0.22, "
    "invertebrate_density 143/m2.\n"
    "  [t=05] zone_scan(delta_channel): depth 6.8 m, sediment_load 212 mg/L, "
    "fish_count 9.\n"
    "  [t=06] trend_analysis(all): riparian stable, upland improving, tidal_marsh "
    "declining, gravel_bed stable, delta_channel improving.\n"
    "  [t=07] weather_feed: rainfall 3.2 mm/h, wind 14 km/h, temperature 19.4 C.\n"
    "  [t=08] anomaly_check(all): no critical alarms; 2 minor outliers in tidal "
    "marsh salinity readings.\n"
    "  [t=09] data_quality: 22/24 streams complete; 2 streams partial with gaps "
    "between t=04 and t=05.\n"
)

X_BRANCH_OBS = [
    "Branch X observation 1: downstream gauges report rising nitrate "
    "concentration (0.9 -> 2.1 mg/L) over the last three cycles, consistent "
    "with upstream runoff entering the riparian wetland. State value updated "
    "to X_STATE_17.",
    "Branch X observation 2: fish diversity index in the delta channel fell "
    "from 0.44 to 0.31; paired with the nitrate trend this suggests nutrient "
    "loading pressure. State value remains X_STATE_17.",
    "Branch X observation 3: wetland invertebrate density dropped 12% while "
    "flow rose 8%; the evidence points to early-stage eutrophication in the "
    "riparian zone. State value remains X_STATE_17.",
]

Y_BRANCH_OBS = [
    "Branch Y observation 1: tidal marsh sediment core shows compaction and "
    "elevation loss of 4 cm; salinity rose 1.8 ppt. State value updated to "
    "Y_STATE_42.",
    "Branch Y observation 2: marsh bird nesting pairs decreased from 37 to 29 "
    "and marsh height stabilized at 0.39 m; marsh retreat is accelerating. "
    "State value remains Y_STATE_42.",
    "Branch Y observation 3: channel dredging records indicate sediment "
    "removal that may starve the marsh of new substrate; erosion risk at the "
    "marsh edge is high. State value remains Y_STATE_42.",
]

QUESTION = (
    'Output a single JSON object with exactly these three fields: '
    '{"branch": "<X or Y>", "state": "<current state value>", '
    '"answer": "<one-sentence assessment>"}. '
    'No markdown, no explanation, no extra text.'
)

PROTOCOL = (
    "STANDING OPERATING PROCEDURE FOR THE CORVUS DELTA MONITORING NETWORK\n"
    "============================================================\n"
    "1. SCOPE. This procedure governs all automated environmental analysis "
    "performed by agents attached to the Corvus Delta monitoring network. It "
    "applies to every zone scan, trend analysis, anomaly report, and "
    "recommendation issued during continuous operation. The network covers "
    "five permanently instrumented zones: riparian wetlands, upland forests, "
    "tidal marshes, gravel riverbeds, and delta channels. Each zone is "
    "sampled on a fixed cadence defined by the network scheduler, and all "
    "sampling results are appended to the shared tool log before any agent "
    "assessment is produced.\n"
    "2. DATA ACQUISITION. Sensors transmit readings every fifteen minutes "
    "over the mesh telemetry backbone. Each transmission includes a zone "
    "identifier, a timestamp in UTC, a battery level, and a payload of up to "
    "sixteen measurement channels. The aggregation layer validates each "
    "packet against a schema and rejects packets that fail checksum "
    "verification. Aggregated readings are stored in the rolling ring buffer "
    "with a retention window of ninety days. Any gap longer than two "
    "consecutive intervals triggers a data quality flag that must be noted in "
    "every downstream analysis until the gap is backfilled or the sensor is "
    "recalibrated.\n"
    "3. MEASUREMENT CHANNELS. The riparian zone reports vegetation index, "
    "turbidity, nitrate concentration, and volumetric flow. The upland zone "
    "reports canopy cover, soil moisture at three depths, and wildlife count. "
    "The tidal marsh reports salinity, marsh surface height, and nesting "
    "bird count. The gravel riverbed reports substrate compaction score, "
    "spawning activity index, and benthic invertebrate density. The delta "
    "channel reports water depth, suspended sediment load, and migratory "
    "fish count. All channels are calibrated quarterly against reference "
    "standards traceable to the national laboratory network, and calibration "
    "offsets are published in the metadata stream.\n"
    "4. STATE MAINTENANCE. Each branch of analysis maintains an explicit "
    "state value that summarizes the accumulated evidence for that branch. "
    "The state value is updated only when new observations cross a "
    "predefined significance threshold. A state value, once assigned, "
    "identifies the branch in all subsequent outputs and must be reported "
    "verbatim in every JSON response. Agents must never report a state value "
    "that belongs to a different branch, and must never merge evidence "
    "across branches when reporting a single-branch assessment.\n"
    "5. TREND ANALYSIS. Trend analysis compares the current window against "
    "the trailing baseline window of equal length. A channel is classified "
    "as improving, stable, or declining based on the sign and magnitude of "
    "the windowed slope relative to the noise floor estimated from the "
    "calibration history. Classification results are written to the shared "
    "tool log with the exact zone name and the numerical slope. Agents "
    "incorporate these classifications into their branch narratives but must "
    "distinguish measured values from inferred conclusions in the final "
    "assessment.\n"
    "6. ANOMALY REPORTING. An anomaly is declared when a reading deviates "
    "from the smoothed expectation by more than three standard deviations. "
    "Minor anomalies are logged with a severity tag of one; critical "
    "anomalies are logged with a severity tag of two and require an "
    "immediate agent response. Each anomaly report must include the zone, "
    "the channel, the measured value, the expected range, and the probable "
    "cause category. Agents evaluate the evidence chain leading to each "
    "anomaly and fold the conclusion into their branch state only when the "
    "evidence is corroborated by at least two independent channels.\n"
    "7. REPORTING FORMAT. Every agent response must be a single JSON object "
    "with exactly three fields: branch, state, and answer. The branch field "
    "carries the branch identifier. The state field carries the current "
    "state value for that branch. The answer field carries a one-sentence "
    "assessment that references at least one measured quantity from the "
    "shared tool log. Responses containing any text outside the JSON object, "
    "any markdown formatting, or any state value from another branch are "
    "rejected by the validation gateway and must be regenerated.\n"
    "8. BRANCH ISOLATION. Each branch is an independent analysis thread "
    "with its own evidence set and its own state value. Branches share the "
    "system prompt, the task description, and the standing tool log, but "
    "never share branch-specific observations. When a branch is revisited "
    "after an intervening branch was processed, the agent must reconstruct "
    "its branch evidence from the shared prefix and its own branch "
    "observations; it must not borrow quantities, states, or conclusions "
    "from the intervening branch. The revisit must reproduce the same state "
    "value and a consistent answer.\n"
    "9. QUALITY ASSURANCE. The validation gateway checks every response "
    "against the reporting format, the state registry, and the branch "
    "isolation rule. Responses that fail any check are flagged and "
    "retried at most once with an explicit reminder of the reporting "
    "format. Persistent failures escalate to the operator dashboard with "
    "the offending branch identifier and the raw response preserved for "
    "post-mortem analysis. The gateway also records the routing decision "
    "for every request so that cache behavior can be audited against the "
    "branch revisit schedule.\n"
    "10. OPERATIONAL CONSTRAINTS. The network operates under a fixed "
    "battery budget; agents should prefer cached prefix reuse to minimize "
    "recomputation. Requests must fit within the configured context window "
    "with headroom for the JSON response. The scheduler assigns branch "
    "requests to slots in arrival order, and the routing policy decides "
    "whether a request revisits a previously populated slot or takes an "
    "empty slot. Agents must be robust to either outcome and must always "
    "produce the required JSON regardless of the routing decision.\n"
    "11. CHANGE CONTROL. This procedure is versioned and frozen for the "
    "duration of the evaluation campaign. Any change to the zone layout, "
    "the measurement channels, the state registry, or the reporting format "
    "requires a new procedure version and a fresh campaign. Operators must "
    "record the procedure version hash in the campaign manifest so that "
    "results remain comparable across runs. The branch prompts, the state "
    "values, and the question templates are part of this frozen artifact "
    "and must not be altered mid-campaign.\n"
    "12. CONTACT AND ESCALATION. Questions about sensor readings, "
    "calibration offsets, or data quality flags are routed to the network "
    "operations desk. Questions about branch evidence or state values are "
    "routed to the analysis lead. Critical anomalies with severity tag two "
    "escalate to the duty officer within one reporting cycle. All escalations "
    "are logged in the shared tool log with the relevant zone, channel, "
    "branch, and timestamp so that the full evidence chain remains "
    "reconstructible for audit.\n"
)

DATA_DICTIONARY = (
    "DATA DICTIONARY FOR THE CORVUS DELTA MONITORING NETWORK\n"
    "============================================================\n"
    "Every channel below is reported in SI units with a fixed precision. Calibration offsets from the quarterly reference check are applied before values enter the shared tool log. Historical seasonal means are recomputed monthly from the ring buffer and are authoritative for anomaly thresholding.\n"
    "\n"
    "ZONE 1: RIPARIAN\n"
    "  - channel: vegetation_index 0.55-0.75 unitless\n"
    "  - channel: turbidity 1.5-6.0 NTU\n"
    "  - channel: nitrate 0.2-2.5 mg/L\n"
    "  - channel: flow 8.0-18.0 m3/s\n"
    "  reference season mean window: 60 days; anomaly threshold: 3 sigma relative to the 30-day smoothed expectation; calibration validity: 90 days from last offset publication.\n"
    "\n"
    "ZONE 2: UPLAND\n"
    "  - channel: canopy_cover 0.55-0.85 unitless\n"
    "  - channel: soil_moisture 0.20-0.45 unitless\n"
    "  - channel: wildlife_count 8-22 count\n"
    "  reference season mean window: 60 days; anomaly threshold: 3 sigma relative to the 30-day smoothed expectation; calibration validity: 90 days from last offset publication.\n"
    "\n"
    "ZONE 3: TIDAL_MARSH\n"
    "  - channel: salinity 12.0-24.0 ppt\n"
    "  - channel: marsh_height 0.30-0.55 m\n"
    "  - channel: bird_count 20-50 count\n"
    "  reference season mean window: 60 days; anomaly threshold: 3 sigma relative to the 30-day smoothed expectation; calibration validity: 90 days from last offset publication.\n"
    "\n"
    "ZONE 4: GRAVEL_BED\n"
    "  - channel: substrate_score 0.40-0.75 unitless\n"
    "  - channel: spawning_activity 0.10-0.45 unitless\n"
    "  - channel: invertebrate_density 90-220 /m2\n"
    "  reference season mean window: 60 days; anomaly threshold: 3 sigma relative to the 30-day smoothed expectation; calibration validity: 90 days from last offset publication.\n"
    "\n"
    "ZONE 5: DELTA_CHANNEL\n"
    "  - channel: depth 5.0-9.0 m\n"
    "  - channel: sediment_load 150-320 mg/L\n"
    "  - channel: fish_count 5-16 count\n"
    "  reference season mean window: 60 days; anomaly threshold: 3 sigma relative to the 30-day smoothed expectation; calibration validity: 90 days from last offset publication.\n"
    "\n"
    "CROSS-ZONE RULES\n"
    "----------------\n"
    "R1. Nitrate load estimates combine riparian nitrate with delta flow and must be\n"
    "    computed before any branch assessment is finalized.\n"
    "R2. Marsh retreat rate is the negative slope of marsh_height over a rolling\n"
    "    45-day window, reported in cm per week.\n"
    "R3. Invertebrate density is corrected for substrate compaction using the\n"
    "    substrate_score before trend classification.\n"
    "R4. Fish migration pressure is inferred when fish_count drops more than 25%\n"
    "    below the seasonal mean for two consecutive windows.\n"
    "R5. Any channel reading outside its documented range is suspect and must be\n"
    "    cross-checked against the adjacent zone before being accepted into branch\n"
    "    evidence.\n"
    "R6. State values are assigned by the analysis lead and registered in the state\n"
    "    registry; unregistered state values are rejected by the validation gateway.\n"
    "R7. The shared tool log is append-only; branch observations are logged in\n"
    "    per-branch sections that the gateway isolates from cross-branch reads.\n"
    "R8. Timing: zone scans rotate on a 90-minute cadence; trend analysis runs at\n"
    "    the end of every 6-hour block; anomaly reports are generated immediately\n"
    "    when the 3-sigma rule triggers.\n"
    "R9. Precision: all floating values are rounded to two decimals in the tool log\n"
    "    and to one decimal in the final answer narrative.\n"
    "R10. Naming: zone names, channel names, and state values are case-sensitive\n"
    "    and must match the registry exactly in every JSON response.\n"
    "R11. Gap handling: a data gap longer than two intervals marks the affected\n"
    "    channels as provisional; provisional channels are excluded from trend\n"
    "    classification until backfilled.\n"
    "R12. Battery policy: the aggregation layer downgrades sampling frequency for\n"
    "    low-battery sensors; downgraded channels carry a quality flag and are\n"
    "    annotated with an asterisk in the tool log.\n"
    "R13. Units: salinity is reported in practical salinity units (psu), turbidity\n"
    "    in nephelometric turbidity units (NTU), flow in cubic meters per second,\n"
    "    depth in meters, sediment load in milligrams per liter, and densities per\n"
    "    square meter unless stated otherwise.\n"
    "R14. Report timeliness: assessments must be produced within one reporting\n"
    "    cycle of the triggering observation; stale assessments are flagged with\n"
    "    the gap duration in minutes.\n"
    "R15. Branch evidence is cumulative: each branch observation builds on the\n"
    "    prior observations of the same branch only; the shared prefix supplies\n"
    "    baseline context that is identical across branches by construction.\n"
    "R16. The JSON answer must reference at least one measured quantity from the\n"
    "    shared tool log; answers that reference only branch observations without\n"
    "    a measured baseline value are rejected.\n"
    "R17. When a branch is revisited, the agent must re-derive the branch state\n"
    "    from the frozen branch observations and must reproduce the registered\n"
    "    state value exactly; drift from the registered state value is a\n"
    "    contamination indicator.\n"
    "R18. The validation gateway records routing decisions, slot assignments, and\n"
    "    cache reuse counts for every request in the routing events log so that\n"
    "    performance claims can be audited against the frozen campaign manifest.\n"
    "R19. All prompts, observations, state values, and question templates are part\n"
    "    of the frozen campaign artifact; editing them invalidates the campaign.\n"
    "R20. The context budget for each request is fixed by the campaign manifest;\n"
    "    agents must fit the JSON response within the generation budget without\n"
    "    truncating the state field.\n"
    "\n"
)

X_REVISIT_QUESTION = (
    'Repeat your assessment for branch X. Output a single JSON object with '
    'exactly these three fields: {"branch": "X", "state": "<X state value>", '
    '"answer": "<one-sentence assessment>"}. No extra text.'
)

Y_REVISIT_QUESTION = (
    'Repeat your assessment for branch Y. Output a single JSON object with '
    'exactly these three fields: {"branch": "Y", "state": "<Y state value>", '
    '"answer": "<one-sentence assessment>"}. No extra text.'
)

# 8-request 固定序列：(turn_id, branch_id, expected_state, 分支观察数)
SEQUENCE = [
    ("X1", "X", "X_STATE_17", 1),
    ("Y1", "Y", "Y_STATE_42", 1),
    ("X2", "X", "X_STATE_17", 2),
    ("Y2", "Y", "Y_STATE_42", 2),
    ("X3", "X", "X_STATE_17", 3),
    ("Y3", "Y", "Y_STATE_42", 3),
    ("X_revisit", "X", "X_STATE_17", 1),
    ("Y_revisit", "Y", "Y_STATE_42", 1),
]

BRANCH_OBS = {"X": X_BRANCH_OBS, "Y": Y_BRANCH_OBS}
REVISIT_Q = {"X": X_REVISIT_QUESTION, "Y": Y_REVISIT_QUESTION}
FOREIGN_STATE = {"X": "Y_STATE_42", "Y": "X_STATE_17"}


def build_prompts() -> List[Dict[str, Any]]:
    """构建 8 个请求的完整 prompts（共享前缀 + 分支后缀）。

    返回 [{turn_id, branch_id, expected_state, messages}]，
    messages 为 OpenAI 格式 history（system + 任务 + 工具历史 + 分支观察 + 问题）。
    """
    shared = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": TASK},
        {"role": "assistant", "content": TOOL_HISTORY},
        {"role": "user", "content": PROTOCOL},
        {"role": "assistant", "content": DATA_DICTIONARY},
    ]
    prompts: List[Dict[str, Any]] = []
    for turn_id, branch, state, n_obs in SEQUENCE:
        obs = BRANCH_OBS[branch][:n_obs]
        history = list(shared)
        for i, o in enumerate(obs):
            role = "assistant" if i % 2 == 0 else "assistant"
            history.append({"role": role, "content": o})
        q = REVISIT_Q[branch] if turn_id.endswith("revisit") else QUESTION
        history.append({"role": "user", "content": q})
        prompts.append({
            "turn_id": turn_id,
            "branch_id": branch,
            "expected_state": state,
            "messages": history,
        })
    return prompts


def parse_json_response(text: str) -> Optional[Dict[str, Any]]:
    """容忍 markdown 代码块/前后杂文本的 JSON 解析。"""
    if not text:
        return None
    s = text.strip()
    m = re.search(r"\{.*\}", s, re.DOTALL)
    if m:
        s = m.group(0)
    try:
        obj = json.loads(s)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


def evaluate_response(text: str, expected_branch: str, expected_state: str) -> Dict[str, Any]:
    """JSON evaluator：返回逐项明细。"""
    obj = parse_json_response(text)
    res = {
        "json_parsable": obj is not None,
        "branch": None, "state": None, "answer": None,
        "branch_correct": False, "state_correct": False,
        "answer_ok": False, "no_foreign_state": False,
    }
    if obj is None:
        return res
    res["branch"] = obj.get("branch")
    res["state"] = obj.get("state")
    res["answer"] = obj.get("answer")
    res["branch_correct"] = obj.get("branch") == expected_branch
    res["state_correct"] = obj.get("state") == expected_state
    ans = obj.get("answer")
    res["answer_ok"] = isinstance(ans, str) and len(ans.strip()) >= 10
    res["no_foreign_state"] = FOREIGN_STATE[expected_branch] not in text
    return res


def evaluator_pass(e: Dict[str, Any]) -> bool:
    return all(e[k] for k in ("json_parsable", "branch_correct", "state_correct",
                              "answer_ok", "no_foreign_state"))


@register
class BranchPressureWorkload(Workload):
    name = "branch_pressure"
    version = "1.0"
    description = "正式分支压力：双分支 X/Y 交替回访 + JSON 输出 evaluator（E2.5 决策门禁）。"

    def params_from_config(self, config: BenchmarkConfig) -> Dict[str, Any]:
        return {"ctx": config.ctx_size or 8192}

    def generate(self, params: Dict[str, Any]) -> WorkloadSpec:
        return WorkloadSpec(
            name=self.name,
            params=dict(params),
            prompts=[p["turn_id"] for p in build_prompts()],
            expected={"sequence": [p["turn_id"] for p in build_prompts()],
                      "branches": ["X", "Y"]},
            meta={"version": self.version, "description": self.description,
                  "ctx": params.get("ctx")},
        )

    def run(self, driver: Driver, spec: WorkloadSpec) -> Dict[str, Any]:
        rows: List[dict] = []
        for p in build_prompts():
            r = driver.chat(p["messages"])
            rows.append({
                "turn_id": p["turn_id"], "branch_id": p["branch_id"],
                "expected_state": p["expected_state"], **r,
            })
        return {"rows": rows, "meta": {}}

    def evaluate(self, results: Dict[str, Any], spec: WorkloadSpec) -> Dict[str, Any]:
        rows = results["rows"]
        per_turn = []
        passed = 0
        for r in rows:
            ev = evaluate_response(r.get("text") or "", r["branch_id"], r["expected_state"])
            ok = evaluator_pass(ev)
            passed += 1 if ok else 0
            per_turn.append({"turn_id": r["turn_id"], "branch_id": r["branch_id"],
                             "evaluator_pass": ok, "evaluator": ev,
                             "response_hash": r.get("response_hash")})
        return {
            "task_success": passed == len(rows),
            "evaluator_pass_rate": passed / len(rows) if rows else 0.0,
            "checked_turns": len(rows),
            "per_turn": per_turn,
        }
