"""M0 fan-out 结果 schema —— 常量 / builders / validator / atomic writer。

唯一权威（docs/M0_BRANCH_MEMORY_BASELINE_DESIGN.md §5.1/§6）：
- 顶层键集恒为 ``{meta, modes, gates, verdict, notes}``；
- canonical meta 固定 22 键（``CANONICAL_META_KEYS``，代码单一来源）；
- modes 唯一权威路径 ``{off,on}.server_groups[].replicates[]``；
- SCHEMA_INVALID 专有值由 ``SCHEMA_INVALID_FIXED`` 参数化（禁止递归包装）；
- 退出码：64（usage）/ 65（schema invalid）/ 70（内部 envelope bug）/ 74（IO 写失败）。

本模块无 stub/TODO；validator 全部为纯函数（可单测，不依赖 GPU/server）。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import tempfile
import time
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .fanout_prompts import (
    BUCKET_TARGETS,
    BUCKETS,
    BUDGET_THRESHOLD,
    CACHE_PROFILES,
    CONTROLS,
    FANOUTS,
    MATRIX_PLAN,
    expected_unit_ids,
    legal_matrix_plan,
)

# ---- 矩阵常量（v47 单一来源，validator/runner/测试均引用） ----
FORMAL_REPS = 5
WARMUP_REPS = 2
EXPECTED_UNIT_IDS: List[str] = expected_unit_ids()  # 24 项（MATRIX_PLAN 重建）

# ---- 退出码（v35 统一 64/65/70/74） ----
EXIT_USAGE = 64                 # 未知/非法 CLI 参数
EXIT_SCHEMA_INVALID = 65        # SCHEMA_INVALID envelope+sidecar 已落盘
EXIT_SOFTWARE = 70              # 内部 envelope/sidecar 自身校验失败（EX_SOFTWARE）
EXIT_IO_WRITE_FAILED = 74       # 结果文件 I/O 写失败（EX_IOERR）

# ---- 结构化错误码（v29：12 个；v39：parity 四元组；v40：三/四元组分离；v49：+2 erase 码 = 14） ----
ERROR_CODES = (
    "connection_error", "timeout", "http_4xx", "http_5xx",
    "malformed_response", "server_crash", "health_failed",
    "endpoint_unavailable", "parity_mismatch", "budget_rejected",
    "validation_incomplete", "schema_invalid",
    "erase_partial", "after_erase_failed",  # v49：erase 阶段失败（任务 2/5）
)
# sidecar validator 独立 6 枚举（v30）
SIDECAR_ERROR_CODES = (
    "missing_key", "extra_key", "type_mismatch",
    "enum_mismatch", "invalid_value", "invariant_violation",
)
# rep/group 错误 stage（v16/v42）
ERROR_STAGES = ("calibration", "validation", "formal")
# 顶层 verdict 五值 + gate status 三值
VERDICTS = (
    "PASS", "HOLD_NOT_VALIDATED", "HOLD_UNSTABLE_MEASUREMENT",
    "REJECT_CORRECTNESS_OR_ISOLATION",
    "INVALID_RESULTS_SCHEMA_OR_INFRASTRUCTURE",
)
GATE_STATUSES = ("PASS", "FAIL", "NOT_APPLICABLE")
REP_STATUSES = ("OK", "INVALID_DECISION", "ERROR")
GROUP_STATUSES = ("COMPLETED", "ERROR")
PHASES = ("preflight", "formal")
# 稳定规则名（§5.1）
RULES = (
    "SCHEMA_INVALID", "PREFLIGHT_INFRA", "PARITY_MISMATCH",
    "ALL_BUDGET_REJECTED", "FORMAL_INCOMPLETE", "REP_ERROR",
    "G7_SCHEMA_FAIL", "PARTIAL_REJECTION", "DECISION_FALLBACK",
    "G1_FAIL", "CORRECTNESS_FAIL", "STABILITY_FAIL", "ALL_PASS",
)
GATE_KEYS = ("G-M0-1", "G-M0-2", "G-M0-3a", "G-M0-3b", "G-M0-4", "G-M0-5", "G-M0-6", "G-M0-7")
REQUIRED_GATES = ("G-M0-1", "G-M0-2", "G-M0-3a", "G-M0-4", "G-M0-5", "G-M0-6", "G-M0-7")

# parity 校准桶全集（v36 权威有序列表）
PARITY_BUCKETS = (
    "short-P", "short-B", "medium-P", "medium-B", "long-P", "long-B",
)

# ---- canonical meta 22 键（v42 移除 parallel/fanout/prefix_len/branch_len） ----
CANONICAL_META_KEYS: Tuple[str, ...] = (
    "workload", "design_ref", "model_id", "binary_version", "ctx_size",
    "cache_profiles", "protocol", "source_phase", "phase",
    "preflight_status", "preflight_reason", "parity_ok",
    "parity_progress", "parity_compensation", "token_count_method",
    "decision_validation", "decision_validated", "decision_fallback",
    "preflight_rejections", "planned_units", "executed_units",
    "matrix_complete",
)
assert len(CANONICAL_META_KEYS) == 22, "canonical meta 必须恰好 22 键"

# SCHEMA_INVALID 专有固定值（v33 权威，禁止递归包装）
SCHEMA_INVALID_FIXED: Dict[str, Any] = {
    "workload": "fanout",
    "design_ref": "M0_BRANCH_MEMORY_BASELINE_DESIGN.md",
    "phase": "preflight",
    "preflight_status": "FAILED",
    "preflight_reason": "schema_invalid",
    "parity_ok": None,
    "parity_progress": {"completed": [], "errors": []},
    "parity_compensation": None,
    "token_count_method": None,
    "decision_validation": None,
    "decision_validated": False,
    "decision_fallback": False,
    "preflight_rejections": [],
    "planned_units": 24,
    "executed_units": 0,
    "matrix_complete": False,
    "cache_profiles": [{"ctk": "q8_0", "ctv": "q8_0"}, {"ctk": "f16", "ctv": "f16"}],
    "protocol": "OAI /v1/chat/completions, temp=0, seed=42, no-think",
}

TOKEN_COUNT_METHOD = "apply-template+tokenize"

# ---- unit/group id 解析 ----
_UNIT_RE = re.compile(r"^(off|on):(q8_0|f16)-(q8_0|f16):f([248]):(short|medium|long)$")
_GROUP_RE = re.compile(r"^(off|on):(q8_0|f16)-(q8_0|f16):f([248])$")


def parse_unit_id(unit_id: str) -> Optional[Dict[str, str]]:
    m = _UNIT_RE.match(unit_id)
    if not m:
        return None
    return {"control": m.group(1), "ctk": m.group(2), "ctv": m.group(3),
            "fanout": m.group(4), "bucket": m.group(5)}


def parse_group_id(group_id: str) -> Optional[Dict[str, str]]:
    m = _GROUP_RE.match(group_id)
    if not m:
        return None
    return {"control": m.group(1), "ctk": m.group(2), "ctv": m.group(3),
            "fanout": m.group(4)}


def group_id_of(control: str, ctk: str, ctv: str, fanout: int) -> str:
    return f"{control}:{ctk}-{ctv}:f{fanout}"


def unit_id_of(control: str, ctk: str, ctv: str, fanout: int, bucket: str) -> str:
    return f"{control}:{ctk}-{ctv}:f{fanout}:{bucket}"


def planned_units_of() -> int:
    """planned_units 派生校验（v23）：len(理论合法组合 6) × 2 profile × 2 control = 24。"""
    return len(legal_matrix_plan_total()) * len(CACHE_PROFILES) * len(CONTROLS)


def legal_matrix_plan_total() -> List[Tuple[int, str]]:
    return [(f, b) for f, buckets in legal_matrix_plan().items() for b in buckets]


# =====================================================================
# builders
# =====================================================================

def now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_parity_progress(
    completed: Iterable[str],
    errors: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """parity 校准进度对象（v36/v39/v40）。

    - ``completed`` 必须为 PARITY_BUCKETS 的（真前缀）子序列；顺序错位 → 由
      validator 判 SCHEMA_INVALID；
    - ``errors`` 元素 schema = ``{code, stage?, bucket?, template?}``（四元组），
      parity_mismatch 条目必须 stage=calibration+bucket+template。
    """
    return {"completed": list(completed), "errors": list(errors or [])}


def build_decision_session(
    requests: int,
    valid: int,
    invalid: int,
    error_count: int,
    invalid_length: int = 0,
    invalid_no_action: int = 0,
    finish_reasons: Optional[Dict[str, int]] = None,
    output_hashes: Optional[List[str]] = None,
    error_summary: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """决策验证 session（§5.1 v20 两来源不变量，由 validator 强制）。"""
    requests, valid, invalid = int(requests), int(valid), int(invalid)
    error_count = int(error_count)
    # 默认值自洽（§5.1 不变量）：finish_reasons 覆盖 OK/INVALID_DECISION 请求、
    # output_hashes 与 valid+invalid 等长——调用方显式传值时不生效。
    if finish_reasons is None:
        finish_reasons = {"stop": max(0, valid + invalid)}
    if output_hashes is None:
        output_hashes = [f"<sha256:{i}>" for i in range(valid + invalid)]
    return {
        "requests": requests,
        "valid": valid,
        "invalid": invalid,
        "error_count": error_count,
        "invalid_length": int(invalid_length),
        "invalid_no_action": int(invalid_no_action),
        "finish_reasons": dict(finish_reasons),
        "output_hashes": list(output_hashes),
        "error_summary": list(error_summary or []),
    }


def build_decision_validation(
    sessions: List[Dict[str, Any]],
    partial: bool,
) -> Dict[str, Any]:
    """decision_validation 对象：sessions + 决策级聚合 total_valid_rate。

    partial=True 表示未完整（<2 sessions 或某 session requests<10）——
    preflight 三种形态（null/partial/完整）均合法，formal 必须完整。
    """
    total_requests = sum(s["requests"] for s in sessions)
    total_valid = sum(s["valid"] for s in sessions)
    rate = (total_valid / total_requests) if total_requests > 0 else None
    return {"sessions": sessions, "total_valid_rate": rate, "partial": bool(partial)}


def build_baseline(
    server_version: str, gpu_used_mb: float, rss_mb: float,
    rs_buffer_mb: float, kv_buffer_mb: float,
    metrics_kv_snapshot: Dict[str, Any],
) -> Dict[str, Any]:
    """group.baseline 内联六字段（v42，不依赖易丢日志路径）。"""
    return {
        "server_version": server_version,
        "gpu_used_mb": gpu_used_mb,
        "rss_mb": rss_mb,
        "rs_buffer_mb": rs_buffer_mb,
        "kv_buffer_mb": kv_buffer_mb,
        "metrics_kv_snapshot": metrics_kv_snapshot,
    }


def build_rep(
    unit_id: str, rep_index: int, fanout: int, bucket: str,
    ctk: str, ctv: str, prefix_len: int, branch_len: int,
    server_group_id: str, status: str, decision_fallback: bool,
    metrics: Dict[str, Any],
    error_type: Optional[str] = None,
    error_stage: Optional[str] = None,
    error_bucket: Optional[str] = None,
) -> Dict[str, Any]:
    """单次 formal rep（v44：逐 rep 对象；unit 身份字段每 rep 内联）。"""
    rep: Dict[str, Any] = {
        "unit_id": unit_id,
        "rep_index": int(rep_index),
        "fanout": int(fanout),
        "bucket": bucket,
        "ctk": ctk,
        "ctv": ctv,
        "prefix_len": int(prefix_len),
        "branch_len": int(branch_len),
        "server_group_id": server_group_id,
        "status": status,
        "decision_fallback": bool(decision_fallback),
        "metrics": metrics,
    }
    if error_type is not None:
        rep["error_type"] = error_type
    if error_stage is not None:
        rep["error_stage"] = error_stage
    if error_bucket is not None:
        rep["error_bucket"] = error_bucket
    return rep


def build_group(
    server_group_id: str, status: str, start: str, stop: str,
    warmup_count: int = WARMUP_REPS,
    baseline: Optional[Dict[str, Any]] = None,
    replicates: Optional[List[Dict[str, Any]]] = None,
    error_type: Optional[str] = None,
    error_stage: Optional[str] = None,
) -> Dict[str, Any]:
    """server group 对象（v42：start/stop 必填；COMPLETED→baseline 完整、ERROR→error 必填）。"""
    group: Dict[str, Any] = {
        "server_group_id": server_group_id,
        "status": status,
        "start": start,
        "stop": stop,
        "warmup_count": int(warmup_count),
        "baseline": baseline,
        "replicates": list(replicates or []),
    }
    if error_type is not None:
        group["error_type"] = error_type
    if error_stage is not None:
        group["error_stage"] = error_stage
    return group


def build_modes(groups_off: List[Dict[str, Any]], groups_on: List[Dict[str, Any]]) -> Dict[str, Any]:
    """modes：仅含已启动 groups（未启动 control 不出现 key？—— 按 §5.1 两 control 均出现，见 runner）。"""
    return {"off": {"server_groups": groups_off}, "on": {"server_groups": groups_on}}


def build_preflight_envelope(
    context: Dict[str, Any],
    *,
    reason: str,
    source_phase: str,
    parity_ok: Optional[bool],
    parity_progress: Dict[str, Any],
    token_count_method: Optional[str],
    decision_validation: Optional[Dict[str, Any]],
    preflight_rejections: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """preflight envelope builder（v37 名称统一：schema_invalid / validation_incomplete 共用）。

    调用方传入 parity 状态，builder **不按 reason 暗中硬编码**；SCHEMA_INVALID
    固定例外由调用方（传 SCHEMA_INVALID_FIXED 对应值）+ validator 检查。
    """
    meta: Dict[str, Any] = dict(context["meta"])  # 受信运行上下文
    meta.update({
        "source_phase": source_phase,
        "phase": "preflight",
        "preflight_status": "FAILED",
        "preflight_reason": reason,
        "parity_ok": parity_ok,
        "parity_progress": parity_progress,
        "parity_compensation": None,
        "token_count_method": token_count_method,
        "decision_validation": decision_validation,
        "decision_validated": False,
        "decision_fallback": False,
        "preflight_rejections": list(preflight_rejections or []),
        "planned_units": planned_units_of(),
        "executed_units": 0,
        "matrix_complete": False,
    })
    return {"meta": meta, "modes": {}, "gates": {},
            "verdict": preflight_verdict(reason), "notes": []}


def preflight_verdict(reason: str) -> str:
    if reason == "parity_mismatch":
        return "HOLD_NOT_VALIDATED"          # PARITY_MISMATCH
    if reason == "budget_rejected":
        return "HOLD_NOT_VALIDATED"          # ALL_BUDGET_REJECTED
    if reason == "schema_invalid":
        return "INVALID_RESULTS_SCHEMA_OR_INFRASTRUCTURE"
    return "INVALID_RESULTS_SCHEMA_OR_INFRASTRUCTURE"  # PREFLIGHT_INFRA


# =====================================================================
# aggregate verdict（§5.1 v12 伪代码）
# =====================================================================

def aggregate_verdict(
    doc: Dict[str, Any],
    any_fallback: bool,
    any_rep_error: bool,
    any_preflight_rejection: bool,
) -> str:
    g = doc["gates"]

    def _fail(gate: str) -> bool:
        # 缺键先置 FAIL（§5.1：formal 缺 gate 键 → 完整性检查先置该 gate FAIL）
        return g.get(gate, {"status": "FAIL"}).get("status") == "FAIL"

    if doc["meta"]["phase"] == "preflight":
        return doc["verdict"]
    if not doc["meta"].get("matrix_complete", False):
        return "INVALID_RESULTS_SCHEMA_OR_INFRASTRUCTURE"  # FORMAL_INCOMPLETE
    if any_rep_error:
        return "INVALID_RESULTS_SCHEMA_OR_INFRASTRUCTURE"  # REP_ERROR
    if _fail("G-M0-7"):
        return "INVALID_RESULTS_SCHEMA_OR_INFRASTRUCTURE"  # G7_SCHEMA_FAIL
    if any_preflight_rejection:
        return "HOLD_NOT_VALIDATED"                        # PARTIAL_REJECTION
    if any_fallback:
        return "HOLD_NOT_VALIDATED"                        # DECISION_FALLBACK
    if _fail("G-M0-1"):
        return "HOLD_NOT_VALIDATED"                        # G1_FAIL
    if _fail("G-M0-2") or _fail("G-M0-5") or _fail("G-M0-3a"):
        return "REJECT_CORRECTNESS_OR_ISOLATION"           # CORRECTNESS_FAIL
    if _fail("G-M0-6") or _fail("G-M0-4"):
        return "HOLD_UNSTABLE_MEASUREMENT"                 # STABILITY_FAIL
    return "PASS"                                          # ALL_PASS


def rule_of(doc: Dict[str, Any], any_fallback: bool, any_rep_error: bool,
            any_preflight_rejection: bool) -> str:
    """聚合命中规则名（诊断用；与 aggregate_verdict 严格一致）。"""
    v = aggregate_verdict(doc, any_fallback, any_rep_error, any_preflight_rejection)
    if doc["meta"]["phase"] == "preflight":
        r = doc["meta"].get("preflight_reason")
        if r == "parity_mismatch":
            return "PARITY_MISMATCH"
        if r == "budget_rejected":
            return "ALL_BUDGET_REJECTED"
        if r == "schema_invalid":
            return "SCHEMA_INVALID"
        return "PREFLIGHT_INFRA"
    if not doc["meta"].get("matrix_complete", False):
        return "FORMAL_INCOMPLETE"
    if any_rep_error:
        return "REP_ERROR"
    if doc["gates"].get("G-M0-7", {"status": "FAIL"}).get("status") == "FAIL":
        return "G7_SCHEMA_FAIL"
    if any_preflight_rejection:
        return "PARTIAL_REJECTION"
    if any_fallback:
        return "DECISION_FALLBACK"
    if doc["gates"].get("G-M0-1", {"status": "FAIL"}).get("status") == "FAIL":
        return "G1_FAIL"
    if (doc["gates"].get("G-M0-2", {"status": "FAIL"}).get("status") == "FAIL"
            or doc["gates"].get("G-M0-5", {"status": "FAIL"}).get("status") == "FAIL"
            or doc["gates"].get("G-M0-3a", {"status": "FAIL"}).get("status") == "FAIL"):
        return "CORRECTNESS_FAIL"
    if (doc["gates"].get("G-M0-6", {"status": "FAIL"}).get("status") == "FAIL"
            or doc["gates"].get("G-M0-4", {"status": "FAIL"}).get("status") == "FAIL"):
        return "STABILITY_FAIL"
    return "ALL_PASS"


# =====================================================================
# validator（纯函数：返回 (ok, errors)）
# =====================================================================

_ERR = Tuple[str, str, Any, Any]  # (code, path, expected, actual)


def _type_name(v: Any) -> str:
    if v is None:
        return "null"
    return type(v).__name__


def to_json_pointer(path: str) -> str:
    """点号/数组下标路径 → RFC6901 JSON Pointer（v49 任务 6 修复：sidecar 合同）。

    "modes.off.server_groups[0].error_type" → "$/modes/off/server_groups/0/error_type"；
    已是 "$"/"$/" 前缀的路径原样返回；空 → "$"。
    """
    if not path or path == "$" or path.startswith("$/"):
        return path or "$"
    p = re.sub(r"\[(\d+)\]", r"/\1", path.replace(".", "/"))
    return "$" + p


def _push(errs: List[_ERR], code: str, path: str, expected: Any, actual: Any) -> None:
    errs.append((code, to_json_pointer(path), expected, _type_name(actual)))


def validate_parity_progress(pp: Any) -> List[_ERR]:
    """parity_progress validator（v36/v39/v40 四元组）。"""
    errs: List[_ERR] = []
    if not isinstance(pp, dict):
        return [( "type_mismatch", "meta.parity_progress", "object", _type_name(pp))]
    completed = pp.get("completed")
    if not isinstance(completed, list):
        _push(errs, "type_mismatch", "meta.parity_progress.completed", "list", completed)
        return errs
    # 有序权威子序列（真前缀语义）：必须等于 PARITY_BUCKETS 的前 k 项（k=0..6）
    k = len(completed)
    if k > len(PARITY_BUCKETS) or completed != list(PARITY_BUCKETS[:k]):
        _push(errs, "invalid_value", "meta.parity_progress.completed",
              f"有序子序列 of {list(PARITY_BUCKETS)}", completed)
    errs_obj = pp.get("errors")
    if not isinstance(errs_obj, list):
        _push(errs, "type_mismatch", "meta.parity_progress.errors", "list", errs_obj)
        return errs
    seen: set = set()
    for i, e in enumerate(errs_obj):
        if not isinstance(e, dict):
            _push(errs, "type_mismatch", f"meta.parity_progress.errors[{i}]", "object", e)
            continue
        code = e.get("code")
        if code not in ERROR_CODES:
            _push(errs, "enum_mismatch", f"meta.parity_progress.errors[{i}].code",
                  "ERROR_CODES", code)
        if "stage" in e and e["stage"] not in ERROR_STAGES:
            _push(errs, "enum_mismatch", f"meta.parity_progress.errors[{i}].stage",
                  "ERROR_STAGES", e.get("stage"))
        if "bucket" in e and e["bucket"] not in BUCKETS:
            _push(errs, "enum_mismatch", f"meta.parity_progress.errors[{i}].bucket",
                  "BUCKETS", e.get("bucket"))
        if "template" in e and e["template"] not in ("P", "B"):
            _push(errs, "enum_mismatch", f"meta.parity_progress.errors[{i}].template",
                  "{P,B}", e.get("template"))
        if code == "parity_mismatch":
            # 四元组必填（v39）
            if e.get("stage") != "calibration":
                _push(errs, "invalid_value", f"meta.parity_progress.errors[{i}]",
                      "stage=calibration for parity_mismatch", e.get("stage"))
            if e.get("bucket") not in BUCKETS:
                _push(errs, "invalid_value", f"meta.parity_progress.errors[{i}]",
                      "bucket∈BUCKETS for parity_mismatch", e.get("bucket"))
            if e.get("template") not in ("P", "B"):
                _push(errs, "invalid_value", f"meta.parity_progress.errors[{i}]",
                      "template∈{P,B} for parity_mismatch", e.get("template"))
        # 四元组唯一性（v40）
        key = (e.get("code"), e.get("stage"), e.get("bucket"), e.get("template"))
        if key in seen:
            _push(errs, "invariant_violation",
                  f"meta.parity_progress.errors[{i}]", "唯一性四元组", key)
        seen.add(key)
    return errs


def validate_decision_session(sess: Any, path: str = "decision_validation.sessions[i]") -> List[_ERR]:
    """session 不变量（v16/v18/v20）：requests==valid+invalid+error_count、
    invalid==invalid_length+invalid_no_action、
    invalid_length==finish_reasons.get("length",0)、
    sum(finish_reasons)==requests-error_count、len(output_hashes)==valid+invalid。"""
    errs: List[_ERR] = []
    if not isinstance(sess, dict):
        return [("type_mismatch", path, "object", _type_name(sess))]
    for k in ("requests", "valid", "invalid", "error_count",
              "invalid_length", "invalid_no_action"):
        v = sess.get(k)
        if not isinstance(v, int) or isinstance(v, bool) or v < 0:
            _push(errs, "type_mismatch", f"{path}.{k}", "非负整数", v)
    fr = sess.get("finish_reasons")
    if not isinstance(fr, dict):
        _push(errs, "type_mismatch", f"{path}.finish_reasons", "dict", fr)
        fr = {}
    for k, v in fr.items():
        if not isinstance(v, int) or isinstance(v, bool) or v < 0:
            _push(errs, "type_mismatch", f"{path}.finish_reasons.{k}", "非负整数", v)
    oh = sess.get("output_hashes")
    if not isinstance(oh, list) or not all(isinstance(h, str) for h in oh):
        _push(errs, "type_mismatch", f"{path}.output_hashes", "list[str]", oh)
        oh = []
    es = sess.get("error_summary")
    if not isinstance(es, list):
        _push(errs, "type_mismatch", f"{path}.error_summary", "list", es)
        es = []
    # 计数等式（字段缺失/类型错时按 0 继续，但类型错已报）
    g = lambda k: sess.get(k) if isinstance(sess.get(k), int) and not isinstance(sess.get(k), bool) else 0
    requests, valid, invalid, err_c = g("requests"), g("valid"), g("invalid"), g("error_count")
    invalid_length, invalid_no_action = g("invalid_length"), g("invalid_no_action")
    if requests != valid + invalid + err_c:
        _push(errs, "invariant_violation", path,
              "requests==valid+invalid+error_count", (requests, valid, invalid, err_c))
    if invalid != invalid_length + invalid_no_action:
        _push(errs, "invariant_violation", f"{path}.invalid",
              "invalid==invalid_length+invalid_no_action",
              (invalid, invalid_length, invalid_no_action))
    if invalid_length != fr.get("length", 0):
        _push(errs, "invariant_violation", f"{path}.invalid_length",
              "invalid_length==finish_reasons.length", invalid_length)
    if sum(fr.values()) != requests - err_c:
        _push(errs, "invariant_violation", f"{path}.finish_reasons",
              "sum==requests-error_count", (sum(fr.values()), requests - err_c))
    if len(oh) != valid + invalid:
        _push(errs, "invariant_violation", f"{path}.output_hashes",
              "len==valid+invalid", (len(oh), valid + invalid))
    # error_summary 三元组唯一性（v40）+ sum(count)==error_count
    seen: set = set()
    total_count = 0
    for i, e in enumerate(es):
        if not isinstance(e, dict):
            _push(errs, "type_mismatch", f"{path}.error_summary[{i}]", "object", e)
            continue
        code = e.get("code")
        if code not in ERROR_CODES:
            _push(errs, "enum_mismatch", f"{path}.error_summary[{i}].code",
                  "ERROR_CODES", code)
        key = (e.get("code"), e.get("stage"), e.get("bucket"))
        if key in seen:
            _push(errs, "invariant_violation", f"{path}.error_summary[{i}]",
                  "唯一性三元组", key)
        seen.add(key)
        cnt = e.get("count")
        if not isinstance(cnt, int) or isinstance(cnt, bool) or cnt < 0:
            _push(errs, "type_mismatch", f"{path}.error_summary[{i}].count",
                  "非负整数", cnt)
            cnt = 0
        total_count += cnt
    if total_count != err_c:
        _push(errs, "invariant_violation", f"{path}.error_summary",
              "sum(count)==error_count", (total_count, err_c))
    return errs


def validate_decision_validation(dv: Any, phase: str) -> List[_ERR]:
    errs: List[_ERR] = []
    if dv is None:
        if phase == "formal":
            errs.append(("missing_key", "meta.decision_validation",
                         "formal 必须含完整会话证据", None))
        return errs  # preflight null 形态合法
    if not isinstance(dv, dict):
        return [("type_mismatch", "meta.decision_validation", "object|null", _type_name(dv))]
    sessions = dv.get("sessions")
    if not isinstance(sessions, list):
        return [("type_mismatch", "meta.decision_validation.sessions", "list", sessions)]
    partial = dv.get("partial")
    if not isinstance(partial, bool):
        _push(errs, "type_mismatch", "meta.decision_validation.partial", "bool", partial)
    for i, s in enumerate(sessions):
        errs += validate_decision_session(s, f"meta.decision_validation.sessions[{i}]")
    rate = dv.get("total_valid_rate")
    total_req = sum(s.get("requests", 0) for s in sessions if isinstance(s, dict))
    if total_req == 0:
        if rate is not None:
            _push(errs, "invalid_value", "meta.decision_validation.total_valid_rate",
                  "null（总 requests=0）", rate)
    else:
        expected_rate = sum(s.get("valid", 0) for s in sessions if isinstance(s, dict)) / total_req
        if rate is None or abs(float(rate) - expected_rate) > 1e-9:
            _push(errs, "invalid_value", "meta.decision_validation.total_valid_rate",
                  f"sum(valid)/sum(requests)={expected_rate}", rate)
    if phase == "formal":
        if partial:
            _push(errs, "invalid_value", "meta.decision_validation.partial",
                  "formal 必须完整（partial=false）", partial)
        if len(sessions) != 2:
            _push(errs, "invariant_violation", "meta.decision_validation.sessions",
                  "formal 必须 2 sessions", len(sessions))
        for i, s in enumerate(sessions):
            if isinstance(s, dict) and s.get("requests", 0) < 10:
                _push(errs, "invariant_violation",
                      f"meta.decision_validation.sessions[{i}].requests",
                      ">=10", s.get("requests"))
    return errs


def validate_preflight_rejections(rej: Any, phase: str, matrix_complete: bool) -> List[_ERR]:
    errs: List[_ERR] = []
    if not isinstance(rej, list):
        return [("type_mismatch", "meta.preflight_rejections", "list", _type_name(rej))]
    ids: List[str] = []
    for i, r in enumerate(rej):
        if not isinstance(r, dict):
            _push(errs, "type_mismatch", f"meta.preflight_rejections[{i}]", "object", r)
            continue
        uid = r.get("unit_id")
        if parse_unit_id(uid) is None:
            _push(errs, "enum_mismatch", f"meta.preflight_rejections[{i}].unit_id",
                  "unit_id 格式", uid)
        else:
            ids.append(uid)
        b = r.get("budget")
        if not isinstance(b, int) or isinstance(b, bool) or b < 0:
            _push(errs, "type_mismatch", f"meta.preflight_rejections[{i}].budget",
                  "非负整数", b)
        th = r.get("threshold")
        if th != BUDGET_THRESHOLD:
            _push(errs, "invalid_value", f"meta.preflight_rejections[{i}].threshold",
                  BUDGET_THRESHOLD, th)
        if r.get("reason") != "budget_rejected":
            _push(errs, "enum_mismatch", f"meta.preflight_rejections[{i}].reason",
                  '"budget_rejected"', r.get("reason"))
    if len(ids) != len(set(ids)):
        _push(errs, "invariant_violation", "meta.preflight_rejections",
              "unit_id 唯一", len(ids) - len(set(ids)))
    for uid in ids:
        if uid not in EXPECTED_UNIT_IDS:
            _push(errs, "invariant_violation", "meta.preflight_rejections",
                  f"unit_id∈EXPECTED_UNIT_IDS", uid)
    return errs


def validate_modes_group_rep(modes: Any, matrix_complete: bool,
                             rejections: List[str]) -> List[_ERR]:
    """modes/group/rep validator（v42/v44/v45/v47：唯一权威路径 server_groups[].replicates[]）。"""
    errs: List[_ERR] = []
    if not isinstance(modes, dict):
        return [("type_mismatch", "modes", "dict", _type_name(modes))]
    all_reps: List[Dict[str, Any]] = []
    seen_keys: set = set()
    unit_rep_indexes: Dict[str, List[int]] = {}
    for control in ("off", "on"):
        if control not in modes:
            # 未启动 control 允许缺失（v47：modes 只含已启动 groups）
            continue
        entry = modes[control]
        if not isinstance(entry, dict) or "server_groups" not in entry:
            _push(errs, "missing_key", f"modes.{control}", "server_groups", entry)
            continue
        groups = entry["server_groups"]
        if not isinstance(groups, list):
            _push(errs, "type_mismatch", f"modes.{control}.server_groups", "list", groups)
            continue
        for gi, grp in enumerate(groups):
            gpath = f"modes.{control}.server_groups[{gi}]"
            if not isinstance(grp, dict):
                _push(errs, "type_mismatch", gpath, "object", grp)
                continue
            gid = grp.get("server_group_id")
            parsed_g = parse_group_id(gid)
            if parsed_g is None or parsed_g["control"] != control:
                _push(errs, "enum_mismatch", f"{gpath}.server_group_id",
                      f"group 格式且 control={control}", gid)
            status = grp.get("status")
            if status not in GROUP_STATUSES:
                _push(errs, "enum_mismatch", f"{gpath}.status", GROUP_STATUSES, status)
            for k in ("start", "stop"):
                v = grp.get(k)
                if not isinstance(v, str) or not v:
                    _push(errs, "type_mismatch", f"{gpath}.{k}", "RFC3339 字符串", v)
            wc = grp.get("warmup_count")
            if wc != WARMUP_REPS:
                _push(errs, "invalid_value", f"{gpath}.warmup_count",
                      WARMUP_REPS, wc)
            baseline = grp.get("baseline")
            if status == "COMPLETED":
                if not isinstance(baseline, dict):
                    _push(errs, "missing_key", f"{gpath}.baseline",
                          "COMPLETED 必须完整 baseline", baseline)
                for bk in ("server_version", "gpu_used_mb", "rss_mb",
                           "rs_buffer_mb", "kv_buffer_mb", "metrics_kv_snapshot"):
                    if bk not in (baseline or {}):
                        _push(errs, "missing_key", f"{gpath}.baseline.{bk}",
                              "baseline 六字段", None)
                if "error_type" in grp:
                    _push(errs, "invalid_value", f"{gpath}.error_type",
                          "COMPLETED 禁止 error 字段", grp.get("error_type"))
            else:  # ERROR
                if grp.get("error_type") not in ERROR_CODES:
                    _push(errs, "missing_key", f"{gpath}.error_type",
                          "ERROR 必填 error_type（12 码子集）", grp.get("error_type"))
                if grp.get("error_stage") != "formal":
                    _push(errs, "invalid_value", f"{gpath}.error_stage",
                          '"formal"', grp.get("error_stage"))
            reps = grp.get("replicates")
            if not isinstance(reps, list):
                _push(errs, "type_mismatch", f"{gpath}.replicates", "list", reps)
                reps = []
            for ri, rep in enumerate(reps):
                rpath = f"{gpath}.replicates[{ri}]"
                if not isinstance(rep, dict):
                    _push(errs, "type_mismatch", rpath, "object", rep)
                    continue
                uid = rep.get("unit_id")
                parsed = parse_unit_id(uid)
                if parsed is None:
                    _push(errs, "enum_mismatch", f"{rpath}.unit_id", "unit_id 格式", uid)
                else:
                    # unit↔group 引用关系（v42）：unit 的 control/profile/fanout 必须
                    # 与所在 group 一致（由 server_group_id 精确相等校验覆盖）
                    gid2 = group_id_of(parsed["control"], parsed["ctk"], parsed["ctv"],
                                       int(parsed["fanout"]))
                    if gid2 != gid:
                        _push(errs, "invariant_violation", f"{rpath}.server_group_id",
                              "unit↔group 引用一致", (uid, gid))
                if parsed is not None and uid in EXPECTED_UNIT_IDS:
                    all_reps.append(rep)
                    key = (uid, rep.get("rep_index"))
                    if key in seen_keys:
                        _push(errs, "invariant_violation", rpath,
                              "(unit_id, rep_index) 全局唯一", key)
                    seen_keys.add(key)
                    unit_rep_indexes.setdefault(uid, []).append(rep.get("rep_index"))
                status_r = rep.get("status")
                if status_r not in REP_STATUSES:
                    _push(errs, "enum_mismatch", f"{rpath}.status",
                          REP_STATUSES, status_r)
                fb = rep.get("decision_fallback")
                if not isinstance(fb, bool):
                    _push(errs, "type_mismatch", f"{rpath}.decision_fallback",
                          "bool", fb)
                else:
                    if status_r == "INVALID_DECISION" and not fb:
                        _push(errs, "invariant_violation", f"{rpath}",
                              "INVALID_DECISION ⇔ decision_fallback=true", fb)
                    if status_r != "INVALID_DECISION" and fb:
                        _push(errs, "invariant_violation", f"{rpath}",
                              "仅 INVALID_DECISION 可 decision_fallback=true", fb)
                if status_r == "ERROR":
                    if rep.get("error_type") not in ERROR_CODES:
                        _push(errs, "missing_key", f"{rpath}.error_type",
                              "ERROR 必填 error_type", rep.get("error_type"))
                    if "error_stage" in rep and rep["error_stage"] not in ERROR_STAGES:
                        _push(errs, "enum_mismatch", f"{rpath}.error_stage",
                              ERROR_STAGES, rep.get("error_stage"))
                rp = rep.get("rep_index")
                if not isinstance(rp, int) or isinstance(rp, bool) or not (0 <= rp < FORMAL_REPS):
                    _push(errs, "invalid_value", f"{rpath}.rep_index",
                          f"0..{FORMAL_REPS-1}", rp)
                for idk in ("fanout", "bucket", "ctk", "ctv"):
                    pass  # 由 unit_id 重建校验
                if parsed is not None:
                    if int(parsed["fanout"]) != rep.get("fanout") or \
                       parsed["bucket"] != rep.get("bucket") or \
                       parsed["ctk"] != rep.get("ctk") or \
                       parsed["ctv"] != rep.get("ctv"):
                        _push(errs, "invariant_violation", f"{rpath}",
                              "身份字段与 unit_id 重建一致", rep)
                for pk in ("prefix_len", "branch_len"):
                    v = rep.get(pk)
                    if not isinstance(v, int) or isinstance(v, bool) or v < 0:
                        _push(errs, "type_mismatch", f"{rpath}.{pk}", "非负整数", v)
    # Critical 6：校准一致性——同 (fanout, bucket) 的所有 rep 的
    # prefix_len/branch_len 必须相同（继承同一桶校准实测值，设计 v42
    # 「同桶 4 unit 一致」；validator 不访问校准映射，但强制跨 rep 不变式）
    cal_by_fb: Dict[Tuple[int, str], set] = {}
    for rp in all_reps:
        fb = (rp.get("fanout"), rp.get("bucket"))
        if not isinstance(fb[0], int) or not isinstance(fb[1], str):
            continue
        s = cal_by_fb.setdefault(fb, set())
        if isinstance(rp.get("prefix_len"), int):
            s.add(("prefix", rp["prefix_len"]))
        if isinstance(rp.get("branch_len"), int):
            s.add(("branch", rp["branch_len"]))
    for fb, vals in cal_by_fb.items():
        for kind in ("prefix", "branch"):
            kset = {v for k, v in vals if k == kind}
            if len(kset) > 1:
                _push(errs, "invariant_violation", "modes",
                      f"同 (fanout,bucket){fb} 的 {kind}_len 必须一致（桶校准实测值）",
                      sorted(kset))
    # 集合规则（v47 单一权威；集合比较，不用排序列表——EXPECTED_UNIT_IDS 为
    # bucket 语义顺序，sorted() 字典序不等价）
    expected_set = set(EXPECTED_UNIT_IDS) - set(rejections)
    observed_set = {r["unit_id"] for r in all_reps}
    complete_set = {u for u in observed_set
                    if set(unit_rep_indexes.get(u, [])) == set(range(FORMAL_REPS))}
    if not observed_set.issubset(expected_set):
        _push(errs, "invariant_violation", "modes",
              "observed ⊆ EXPECTED_UNIT_IDS − rejections", observed_set)
    if not complete_set.issubset(observed_set):
        _push(errs, "invariant_violation", "modes",
              "complete ⊆ observed", complete_set)
    if matrix_complete:
        if observed_set != expected_set or complete_set != expected_set:
            _push(errs, "invariant_violation", "modes",
                  "matrix_complete=true → observed==complete==EXPECTED−rejections",
                  (observed_set, complete_set, expected_set))
    return errs


def validate_meta(meta: Any) -> List[_ERR]:
    errs: List[_ERR] = []
    if not isinstance(meta, dict):
        return [("type_mismatch", "meta", "dict", _type_name(meta))]
    if set(meta.keys()) != set(CANONICAL_META_KEYS):
        _push(errs, "invariant_violation", "meta",
              f"键集精确等于 22 键", sorted(meta.keys()))
        return errs
    phase = meta.get("phase")
    if phase not in PHASES:
        _push(errs, "enum_mismatch", "meta.phase", PHASES, phase)
    src = meta.get("source_phase")
    if src not in PHASES:
        _push(errs, "enum_mismatch", "meta.source_phase", PHASES, src)
    # source_phase 规则（v32/v34）：普通结果 source_phase==phase；SCHEMA_INVALID 例外
    if meta.get("preflight_reason") != "schema_invalid" and src != phase:
        _push(errs, "invariant_violation", "meta.source_phase",
              "普通结果 source_phase==phase", (src, phase))
    if meta.get("preflight_reason") == "schema_invalid":
        if src not in PHASES:
            _push(errs, "enum_mismatch", "meta.source_phase", PHASES, src)
    if meta.get("preflight_status") not in ("FAILED", "N/A"):
        _push(errs, "enum_mismatch", "meta.preflight_status",
              '"FAILED"|"N/A"', meta.get("preflight_status"))
    # parity_ok 总规则（v35）+ completed 交叉不变量（v36）
    errs += validate_parity_progress(meta.get("parity_progress"))
    completed = meta.get("parity_progress", {}).get("completed", []) \
        if isinstance(meta.get("parity_progress"), dict) else []
    p_ok = meta.get("parity_ok")
    if p_ok not in (True, False, None):
        _push(errs, "enum_mismatch", "meta.parity_ok", "true|false|null", p_ok)
    else:
        if meta.get("preflight_reason") == "schema_invalid":
            # SCHEMA_INVALID 固定例外（v35）：progress 恒空
            if meta.get("parity_progress") != {"completed": [], "errors": []}:
                _push(errs, "invalid_value", "meta.parity_progress",
                      "SCHEMA_INVALID 恒 {completed:[], errors:[]}",
                      meta.get("parity_progress"))
        if meta.get("preflight_reason") == "schema_invalid":
            # SCHEMA_INVALID 固定例外
            if p_ok is not None:
                _push(errs, "invalid_value", "meta.parity_ok", "null（SCHEMA_INVALID 例外）", p_ok)
        elif p_ok in (True, False):
            if completed != list(PARITY_BUCKETS):
                _push(errs, "invariant_violation", "meta.parity_progress.completed",
                      "parity_ok∈{true,false} → 严格全集 6 项", completed)
        else:  # null
            if len(completed) >= len(PARITY_BUCKETS):
                _push(errs, "invariant_violation", "meta.parity_progress.completed",
                      "parity_ok=null → 真前缀（长度<6）", completed)
    tcm = meta.get("token_count_method")
    if tcm not in (TOKEN_COUNT_METHOD, None):
        _push(errs, "enum_mismatch", "meta.token_count_method",
              f'"{TOKEN_COUNT_METHOD}"|null', tcm)
    if meta.get("preflight_reason") == "schema_invalid" and tcm is not None:
        _push(errs, "invalid_value", "meta.token_count_method",
              "null（SCHEMA_INVALID 例外）", tcm)
    if meta.get("parity_compensation") is not None:
        _push(errs, "invalid_value", "meta.parity_compensation",
              "恒 null（当前策略）", meta.get("parity_compensation"))
    if not isinstance(meta.get("decision_validated"), bool):
        _push(errs, "type_mismatch", "meta.decision_validated", "bool",
              meta.get("decision_validated"))
    if not isinstance(meta.get("decision_fallback"), bool):
        _push(errs, "type_mismatch", "meta.decision_fallback", "bool",
              meta.get("decision_fallback"))
    pu = meta.get("planned_units")
    if pu != planned_units_of():
        _push(errs, "invalid_value", "meta.planned_units",
              f"矩阵派生 {planned_units_of()}", pu)
    eu = meta.get("executed_units")
    if not isinstance(eu, int) or isinstance(eu, bool) or eu < 0:
        _push(errs, "type_mismatch", "meta.executed_units", "非负整数", eu)
    mc = meta.get("matrix_complete")
    if not isinstance(mc, bool):
        _push(errs, "type_mismatch", "meta.matrix_complete", "bool", mc)
    errs += validate_decision_validation(meta.get("decision_validation"), phase)
    return errs


def validate_result_envelope(doc: Any) -> Tuple[bool, List[Dict[str, Any]]]:
    """顶层 result validator（§5.1 v9/v31/v34）。

    返回 (ok, errors)；errors 为 sidecar 风格结构化对象列表
    （{path, code, expected_type, actual_type}）。
    """
    if not isinstance(doc, dict):
        return False, [{"path": "$", "code": "type_mismatch",
                        "expected_type": "object", "actual_type": _type_name(doc)}]
    top = {"meta", "modes", "gates", "verdict", "notes"}
    if set(doc.keys()) != top:
        return False, [{"path": "$", "code": "invariant_violation",
                        "expected_type": "顶层五键 {meta,modes,gates,verdict,notes}",
                        "actual_type": sorted(doc.keys())}]
    errs: List[Any] = validate_meta(doc.get("meta"))
    # phase 合法组合（v31/v34）
    meta = doc.get("meta", {})
    phase = meta.get("phase")
    modes, gates = doc.get("modes"), doc.get("gates")
    if phase == "preflight":
        if modes != {} or gates != {}:
            errs.append({"path": "$", "code": "invariant_violation",
                         "expected_type": "preflight ⇔ modes={} ∧ gates={}",
                         "actual_type": (modes, gates)})
    elif phase == "formal":
        if not isinstance(modes, dict) or not modes:
            errs.append({"path": "modes", "code": "type_mismatch",
                         "expected_type": "formal ⇔ modes 非空", "actual_type": modes})
        if not isinstance(gates, dict) or not gates:
            errs.append({"path": "gates", "code": "type_mismatch",
                         "expected_type": "formal ⇔ gates 非空", "actual_type": gates})
    else:
        errs.append({"path": "meta.phase", "code": "enum_mismatch",
                     "expected_type": PHASES, "actual_type": phase})
    # gates 值域
    if isinstance(gates, dict):
        for k, v in gates.items():
            if k not in GATE_KEYS:
                errs.append({"path": f"gates.{k}", "code": "extra_key",
                             "expected_type": GATE_KEYS, "actual_type": k})
            if not isinstance(v, dict) or v.get("status") not in GATE_STATUSES:
                errs.append({"path": f"gates.{k}.status", "code": "enum_mismatch",
                             "expected_type": GATE_STATUSES,
                             "actual_type": v.get("status") if isinstance(v, dict) else v})
    # verdict 值域
    verdict = doc.get("verdict")
    if verdict not in VERDICTS:
        errs.append({"path": "verdict", "code": "enum_mismatch",
                     "expected_type": VERDICTS, "actual_type": verdict})
    if not isinstance(doc.get("notes"), list):
        errs.append({"path": "notes", "code": "type_mismatch",
                     "expected_type": "list", "actual_type": _type_name(doc.get("notes"))})
    # 拒绝项 + modes/group/rep（formal 才评估组；preflight modes={}）
    rej = meta.get("preflight_rejections", [])
    errs += validate_preflight_rejections(rej, phase, meta.get("matrix_complete", False))
    rej_ids = [r.get("unit_id") for r in rej if isinstance(r, dict)]
    if phase == "formal" and isinstance(modes, dict):
        errs += validate_modes_group_rep(modes, meta.get("matrix_complete", False), rej_ids)
    # 统一为 sidecar 风格 dict（tuple → dict 转换，与 validate_schema_sidecar 对齐）
    out: List[Dict[str, Any]] = []
    for e in errs:
        if isinstance(e, tuple) and len(e) == 4:
            out.append({"path": e[1], "code": e[0],
                        "expected_type": e[2], "actual_type": e[3]})
        elif isinstance(e, dict):
            out.append(e)
        else:
            out.append({"path": "$", "code": "invariant_violation",
                        "expected_type": "sidecar 风格错误", "actual_type": repr(e)})
    return (len(out) == 0), out


def validate_schema_sidecar(sidecar: Any) -> Tuple[bool, List[Dict[str, Any]]]:
    """SCHEMA_INVALID 诊断 sidecar validator（v30：6 枚举 + RFC6901 JSON Pointer + 类型白名单）。"""
    TYPES = ("object", "array", "string", "integer", "number", "boolean", "null", "missing")
    if not isinstance(sidecar, dict):
        return False, [{"path": "$", "code": "type_mismatch",
                        "expected_type": "object", "actual_type": _type_name(sidecar)}]
    errs: List[Dict[str, Any]] = []
    arr = sidecar.get("errors")
    if not isinstance(arr, list):
        return False, [{"path": "errors", "code": "type_mismatch",
                        "expected_type": "list", "actual_type": _type_name(arr)}]
    for i, e in enumerate(arr):
        path = f"errors[{i}]"
        if not isinstance(e, dict):
            errs.append({"path": path, "code": "type_mismatch",
                         "expected_type": "object", "actual_type": _type_name(e)})
            continue
        jp = e.get("path")
        if not isinstance(jp, str) or not jp.startswith("$"):
            errs.append({"path": f"{path}.path", "code": "invalid_value",
                         "expected_type": "RFC6901 JSON Pointer 或 $", "actual_type": jp})
        code = e.get("code")
        if code not in SIDECAR_ERROR_CODES:
            errs.append({"path": f"{path}.code", "code": "enum_mismatch",
                         "expected_type": SIDECAR_ERROR_CODES, "actual_type": code})
        for tk in ("expected_type", "actual_type"):
            v = e.get(tk)
            if v not in TYPES:
                errs.append({"path": f"{path}.{tk}", "code": "enum_mismatch",
                             "expected_type": "类型白名单", "actual_type": v})
    return (len(errs) == 0), errs


# =====================================================================
# atomic writer（v30/v31：临时文件 + fsync + rename 顺序）
# =====================================================================

def atomic_write_json(path: str, obj: Any) -> None:
    """临时文件 + fsync + 同目录 os.replace + fsync 目录。

    v48（任务 9）：tmp 名改为并发安全唯一名 `.m0_fanout_<name>.<kind>.<pid>.<rand>.tmp`
    （PID + secrets 随机后缀）——并发写同路径（多进程同 results 目录）不会互相覆盖；
    同目录 os.replace 保证原子性（跨设备 rename 可能非原子，同目录安全）。
    """
    d = os.path.dirname(os.path.abspath(path))
    name = os.path.basename(path)
    tmp = os.path.join(d, f".{name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    fd = os.open(d, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_result_pair(main_path: str, main_obj: Any,
                      sidecar_path: Optional[str] = None,
                      sidecar_obj: Optional[Any] = None) -> None:
    """原子写顺序（v30）：sidecar 先 rename、主 envelope 后；部分失败 → IO_WRITE_FAILED。

    失败时抛出 OSError 由调用方映射到 EXIT_IO_WRITE_FAILED（74）。
    """
    if sidecar_path is not None:
        atomic_write_json(sidecar_path, sidecar_obj)
    atomic_write_json(main_path, main_obj)


def cleanup_stale_tmp(results_dir: str, max_age_seconds: float = 3600.0) -> List[str]:
    """清理陈旧 `.m0_fanout_*.tmp` 残留（v48，任务 9）。

    仅删除 mtime 超过 max_age_seconds 的匹配文件——**不删除活跃并发写**
    （刚创建/正在写的 tmp 的 mtime 是新的；main 显式传阈值，默认 3600s）。
    返回清理的文件名列表。
    """
    removed: List[str] = []
    if not os.path.isdir(results_dir):
        return removed
    now = time.time()
    for fn in os.listdir(results_dir):
        if not (fn.startswith(".m0_fanout_") and fn.endswith(".tmp")):
            continue
        fp = os.path.join(results_dir, fn)
        try:
            if now - os.path.getmtime(fp) > max_age_seconds:
                os.remove(fp)
                removed.append(fn)
        except OSError:
            pass
    return removed


def content_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
