#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
E15.1：分支并发 paired runner —— 验证现有 C1 `--kv-prefix-share` / `seq_cp`
在真实分支 fan-out（共同前缀预热 + 多分支 target 并发）中的共享行为与不变量。

协议（用户 E15 指令）：
- 模型：TinyLlama stories260K（attention-only，非 hybrid/recurrent/SWA）；
- 共同前缀先在 source slot 预热并 idle（KV 保留）；
- 随后 N 个（2 或 3）不同分支 target 在独立 slots 并发发出
  （ThreadPoolExecutor + barrier，同时 in-flight）；
- paired 对照：--kv-prefix-share off / on 两模式，server 参数除共享开关外完全一致；
- warmup >= 2、formal reps >= 5；固定 seed=42 / temperature=0，
  相同 prompt / n_predict / ctx / parallel / cache-ram；
- 每 replicate 清空 slots 并断言 /metrics/kv used_cells==0 && active_sequences==0；
- 每请求记录：HTTP 状态/错误、prompt_n / cache_n、输出 token ids + 内容 sha256、
  latency；replicate 级记录 /metrics/kv（used/shared/active/capacity/physical_sharing/
  semantics）、server 日志 "E6-C1: shared" 事件数、清空后回基线断言。

门禁（gate_verdict，纯函数，可单测）：
- G0 数据完整性：每 formal rep source/每个 target HTTP=200、无 error、
  tokens 非空、content 非空、recompute 非 None；任一失败直接 REJECT；
- G1 on/off 输出逐 target 一致（token ids 与 content_sha256 双比较，
  branch key 集必须相同；空数据/rep count<1 → INVALID）；
- G2 on shared_cells > 0（至少一个 formal replicate）；
- G3 on recompute（= prompt_n）相对 off 逐 target 中位数下降 >= 25%；
- G4 off shared_cells == 0（全部 formal replicate）；
- G5 并发 target 无跨分支 canary/state 污染（仅检查解码文本 content）；
- G6 按每个 on rep 独立判断：该 rep 发生共享时 protect probe 后
  shared_cells 仍 > 0；该 rep 无共享 → N/A（缺 spare slot 由 CLI 前置避免）；
- G7 每 replicate 清空后回基线（used_cells==0 && active_sequences==0）；
- G8 shutdown clean；
- G9 physical_sharing 始终 false 且 shared_cells_semantics 正确。

判定：全部 PASS → PASS；仅 G3 不达 → HOLD_NO_MEASURABLE_GAIN；
共享完全未发生（G2 fail 且日志无 shared 事件）→ HOLD（记录原始证据）；
任一正确性/隔离门禁失败（G0/G1/G4-G9）→ REJECT_CORRECTNESS_OR_ISOLATION。

用法（benchmark/ 目录下）：
  python runner/e15_branch_concurrent.py \
    --server-bin ../llama.cpp/build/bin/llama-server \
    --model ../llama.cpp/tmp/models--ggml-org--test-model-stories260K/snapshots/<sha>/stories260K-f32.gguf \
    --port 8091 --ctx-size 512 --parallel 5 --cache-ram 0 --min-lcp 64 \
    --warmup 2 --reps 5 --branches 3

输出：results/e15_branch_<时间戳>.json（原始证据 + 门禁判定 + verdict）。
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import signal
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Sequence, Tuple

# 固定实验参数（协议硬编码，不随 CLI 漂移）
SEED = 42
TEMPERATURE = 0.0
N_PREDICT = 8
DEFAULT_MIN_LCP = 64

# ---- 分支 prompt 构造（确定性；分支首 token 互不相同 → LCP 精确终止于前缀末尾）----

PREFIX_CORE = (
    "Once upon a time in a deep enchanted forest, a young squirrel named Pip "
    "discovered a hidden hollow inside an ancient oak tree. Inside the hollow, "
    "there was a small wooden box with a brass key and a map drawn on soft "
    "leather. The map showed three paths leading away from the forest: one to "
    "the north, one to the east, and one to the west. Pip decided to gather "
    "supplies before setting out on a journey. The squirrel packed a pouch of "
    "acorns, a flask of clear water, a warm blanket, and a small lantern that "
    "glowed with a gentle golden light. Along the way, Pip met other animals "
    "who offered advice and warnings about the dangers that waited beyond the "
    "trees. The wind carried strange whispers, and the stars above seemed to "
    "move in patterns that no one could explain. Every step forward felt like "
    "a step into a story that had not yet been told."
)

CANARY_SRC = "CANARY-SOURCE-77C1"
CANARY_B1 = "CANARY-BRANCH-1-9F3A"
CANARY_B2 = "CANARY-BRANCH-2-5D2B"
CANARY_B3 = "CANARY-BRANCH-3-A8E4"

# 分支专属 canary 标记（纯字符串，用于跨分支污染检测）
CANARIES = {"b0": CANARY_SRC, "b1": CANARY_B1, "b2": CANARY_B2, "b3": CANARY_B3}

# 分支后缀：首 token 互不相同；canary 作为分支专属状态标记（输出不得含他分支 canary）。
# 短后缀：off 模式 4 并发全量 KV 总 cell 占用 < unified KV 池（--ctx-size 2048）。
BRANCHES = {
    "b0": " Then the squirrel hurried north. " + CANARY_SRC,
    "b1": " Meanwhile the young animal turned east. " + CANARY_B1,
    "b2": " After that the curious creature wandered west. " + CANARY_B2,
    "b3": " Suddenly the brave explorer marched south. " + CANARY_B3,
}

PROTECT_PROBE_TEXT = (
    "One two three four five six seven eight nine ten. "
    "Eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen."
)


def build_branch_prompt(prefix: str, branch_key: str) -> str:
    """确定性构造 前缀 + 分支后缀。"""
    return prefix + BRANCHES[branch_key]


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def est_tokens(text: str) -> int:
    """粗略 token 估算（字符数 / 2.5，保守偏大）。

    用于 CLI 容量前置校验：off 模式全量 KV 峰值 = Σ(source+targets prompt_n + predicted)
    必须 < ctx-size，否则开箱必败（unified KV 池溢出 → Context exceeded）。
    实测 stories260K 前缀 454 tokens / ~1700 chars ≈ 3.7 chars/token；2.5 为保守上界。
    """
    return max(1, int(len(text) / 2.5))


def median(xs: Sequence[float]) -> Optional[float]:
    xs = [float(x) for x in xs]
    if not xs:
        return None
    return float(statistics.median(xs))


def output_ids_of(body: Dict[str, Any]) -> List[int]:
    """从 /completion 响应提取输出 token ids。

    只读取 `tokens`（list，输出 token ids；需请求时带 `return_tokens: true`）。
    绝不读取 `tokens_predicted` —— 该字段在真实 /completion 响应中是 int
    （n_decoded 计数），不是 ids 数组；误用会导致把单个整数当 ids 列表。
    异常 token 元素（非 int/None/字符串）fail-safe 跳过，不使整体崩溃。
    """
    toks = body.get("tokens")
    if not isinstance(toks, list):
        return []
    out: List[int] = []
    for t in toks:
        try:
            out.append(int(t))
        except (TypeError, ValueError):
            continue  # fail-safe：异常元素跳过
    return out


def recompute_of(timings: Optional[Dict[str, Any]]) -> Optional[int]:
    """recompute = 本次实际处理的 prompt tokens = timings.prompt_n（cache_n 为命中部分）。"""
    if not isinstance(timings, dict):
        return None
    pn = timings.get("prompt_n")
    return int(pn) if isinstance(pn, (int, float)) and not isinstance(pn, bool) else None


def canary_violations(target_rows: Sequence[Dict[str, Any]]) -> List[str]:
    """检查并发 target 输出是否含其他分支 canary（跨分支状态污染）。

    只检查解码文本 content：canary 是分支专属字符串标记，token ids 是整数，
    拼接 token ids 只会稀释检测、产生误导（token ids 中不可能出现该字符串）。

    target_rows：{"branch": "b1", "tokens": [...], "content": str, ...}
    返回违规描述列表（空 = 无污染）。
    """
    violations: List[str] = []
    for row in target_rows:
        br = row.get("branch")
        content = row.get("content") or ""
        for key, canary in CANARIES.items():
            if key == br:
                continue
            if canary in content:
                violations.append(f"branch {br} output contains {key} canary {canary!r}")
    return violations


# ---- HTTP（urllib，线程安全：每次请求独立连接）----


def http_request(url: str, payload: Optional[Dict[str, Any]] = None,
                 timeout: float = 120.0) -> Tuple[Optional[int], Dict[str, Any]]:
    data = None
    headers: Dict[str, str] = {}
    method = "GET"
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
        method = "POST"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            try:
                body = json.loads(raw)
            except Exception:
                body = {"raw": raw}
            return resp.status, body
    except Exception as e:  # noqa: BLE001
        return None, {"error": f"{type(e).__name__}: {e}"}


def _http_get_json(url: str, timeout: float = 10.0) -> Tuple[Optional[int], Dict[str, Any]]:
    return http_request(url, payload=None, timeout=timeout)


# ---- 门禁判定（纯函数，可单测）----


def _target_ids_by_branch(rep: Dict[str, Any]) -> Dict[str, List[int]]:
    out: Dict[str, List[int]] = {}
    for t in rep.get("targets", []):
        out[t.get("branch")] = list(t.get("tokens") or [])
    return out


def _target_sha_by_branch(rep: Dict[str, Any]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for t in rep.get("targets", []):
        out[t.get("branch")] = t.get("content_sha256") or ""
    return out


def _recompute_by_branch(rep: Dict[str, Any]) -> Dict[str, Optional[int]]:
    out: Dict[str, Optional[int]] = {}
    for t in rep.get("targets", []):
        out[t.get("branch")] = t.get("recompute")
    return out


def _shared_cells_of(rep: Dict[str, Any]) -> int:
    m = rep.get("metrics_post") or {}
    return int(m.get("shared_cells") or 0)


def _integrity_failures_for_req(rec: Dict[str, Any], what: str) -> List[str]:
    """G0 单请求完整性检查：HTTP=200、无 error、tokens 非空、content 非空、recompute 非 None。"""
    if not isinstance(rec, dict):
        return [f"{what} record missing"]
    fails: List[str] = []
    if rec.get("http_status") != 200:
        fails.append(f"{what} HTTP {rec.get('http_status')} err={rec.get('error')}")
    if rec.get("error"):
        fails.append(f"{what} error={rec.get('error')}")
    if not rec.get("tokens"):
        fails.append(f"{what} tokens empty")
    if not rec.get("content"):
        fails.append(f"{what} content empty")
    if rec.get("recompute") is None:
        fails.append(f"{what} recompute missing")
    return fails


def _data_integrity_failures(rep: Dict[str, Any]) -> List[str]:
    """G0：某 formal replicate 的 source + 全部 targets 数据完整性违规列表（空 = 完整）。

    branch missing/None 或重复（同一 rep 两个 target 同 branch）直接判违规——
    dict 覆盖会掩盖 G1 比较，必须 REJECT 而非静默覆盖。
    """
    fails: List[str] = []
    src = rep.get("source")
    if not isinstance(src, dict):
        fails.append("source record missing")
    else:
        fails += _integrity_failures_for_req(src, "source")
    targets = rep.get("targets")
    if not targets:
        fails.append("no targets (branch set incomplete)")
    else:
        seen: set = set()
        for j, t in enumerate(targets):
            br = t.get("branch") if isinstance(t, dict) else None
            if not br:
                fails.append(f"target{j}: branch missing/None")
            elif br in seen:
                fails.append(f"target{j}: duplicate branch {br!r}")
            else:
                seen.add(br)
            fails += _integrity_failures_for_req(t, f"target{j}({br})")
    return fails


def gate_verdict(off_reps: Sequence[Dict[str, Any]],
                 on_reps: Sequence[Dict[str, Any]],
                 meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """门禁判定（E15 用户指令）：返回逐项 PASS/FAIL + verdict。

    off_reps/on_reps：formal（非 warmup）replicate 记录列表。
    """
    meta = meta or {}
    gates: Dict[str, Any] = {}
    notes: List[str] = []

    # ---- 前置：空数据 / rep count<1 → INVALID（直接拒绝，不进入门禁判定）----
    if not off_reps or not on_reps:
        detail = {"off_reps": len(off_reps), "on_reps": len(on_reps),
                  "reason": "formal replicate data missing (empty off/on); cannot judge"}
        return {"gates": {"G_preflight_data_present": {"pass": False, "detail": detail}},
                "verdict": "INVALID",
                "notes": ["no formal replicate data to evaluate gates"]}

    # G0: 数据完整性（每 formal rep source/每个 target：HTTP=200、无 error、
    #     tokens 非空、content 非空、recompute 非 None）；任一失败 → REJECT（不能 HOLD）。
    g0_fail: List[str] = []
    for mode, reps in (("off", off_reps), ("on", on_reps)):
        for i, rep in enumerate(reps):
            g0_fail += [f"{mode} rep{i}: {f}" for f in _data_integrity_failures(rep)]
    gates["G0_data_integrity"] = {
        "pass": not g0_fail,
        "detail": g0_fail or "all reps: source+targets complete (HTTP 200, no error, tokens/content non-empty, recompute present)"}

    # G1: on/off 输出逐 target 一致 —— 同时比较 token ids 与 content_sha256
    #     （冗余一致性门禁）；branch key 集必须相同（缺失分支 = 无法证明输出一致）。
    #     畸形 branch（None）由 G0 REJECT；此处用 str key 排序仅保证不崩溃。
    g1_fail = []
    all_off_keys = sorted(set().union(*(set(_target_ids_by_branch(r)) for r in off_reps)),
                          key=lambda k: str(k))
    all_on_keys = sorted(set().union(*(set(_target_ids_by_branch(r)) for r in on_reps)),
                         key=lambda k: str(k))
    if all_off_keys != all_on_keys:
        g1_fail.append(f"branch key sets differ: off={all_off_keys} on={all_on_keys}")
    for i, (off_rep, on_rep) in enumerate(zip(off_reps, on_reps)):
        off_ids = _target_ids_by_branch(off_rep)
        on_ids = _target_ids_by_branch(on_rep)
        if set(off_ids) != set(on_ids):
            g1_fail.append(f"rep{i}: branch key sets differ off={sorted(off_ids, key=lambda k: str(k))} "
                           f"on={sorted(on_ids, key=lambda k: str(k))}")
            continue
        off_sha = _target_sha_by_branch(off_rep)
        on_sha = _target_sha_by_branch(on_rep)
        for br in sorted(off_ids, key=lambda k: str(k)):
            if off_ids[br] != on_ids[br]:
                g1_fail.append(f"rep{i} branch {br}: off token ids={off_ids[br]} on={on_ids[br]}")
            if off_sha.get(br) != on_sha.get(br):
                g1_fail.append(f"rep{i} branch {br}: content_sha256 mismatch "
                               f"off={off_sha.get(br)} on={on_sha.get(br)}")
    if len(off_reps) != len(on_reps):
        g1_fail.append(f"replicate count mismatch: off={len(off_reps)} on={len(on_reps)}")
    gates["G1_output_identical_on_off"] = {
        "pass": not g1_fail, "detail": g1_fail or f"{len(off_reps)} paired reps identical "
        f"(token ids + content_sha256)"}

    # G2: on shared_cells > 0
    on_shared = [_shared_cells_of(r) for r in on_reps]
    gates["G2_on_shared_cells_positive"] = {
        "pass": any(s > 0 for s in on_shared),
        "detail": {"shared_cells_per_rep": on_shared}}

    # G3: on recompute 相对 off 下降 >= 25%（逐 target 中位数）
    g3_detail: Dict[str, Any] = {}
    g3_pass = True
    branches = set()
    for r in off_reps:
        branches.update(_recompute_by_branch(r))
    for r in on_reps:
        branches.update(_recompute_by_branch(r))
    for br in sorted(branches, key=lambda k: str(k)):
        off_rc = [r.get(br) for r in (_recompute_by_branch(x) for x in off_reps)]
        on_rc = [r.get(br) for r in (_recompute_by_branch(x) for x in on_reps)]
        off_m = median([x for x in off_rc if x is not None])
        on_m = median([x for x in on_rc if x is not None])
        if off_m is None or on_m is None or off_m <= 0:
            g3_detail[br] = {"off_median": off_m, "on_median": on_m, "reduction": None}
            g3_pass = False
            continue
        reduction = 1.0 - on_m / off_m
        g3_detail[br] = {"off_median": off_m, "on_median": on_m,
                         "reduction": round(reduction, 4)}
        if reduction < 0.25:
            g3_pass = False
    gates["G3_recompute_reduction_ge_25pct"] = {
        "pass": g3_pass, "detail": g3_detail}

    # G4: off shared_cells == 0
    off_shared = [_shared_cells_of(r) for r in off_reps]
    gates["G4_off_shared_cells_zero"] = {
        "pass": all(s == 0 for s in off_shared),
        "detail": {"shared_cells_per_rep": off_shared}}

    # G5: canary 无跨分支污染（targets + source + protect probe 的可观测文本）
    g5_fail = []
    for mode, reps in (("off", off_reps), ("on", on_reps)):
        for i, rep in enumerate(reps):
            vio = canary_violations(rep.get("targets", []))
            if vio:
                g5_fail.append(f"{mode} rep{i}: {vio}")
            # source（b0）：不得含 b1/b2/b3 canary（自身 b0 canary 允许）
            src = rep.get("source") or {}
            src_content = src.get("content") or ""
            for key, canary in CANARIES.items():
                if key == "b0":
                    continue
                if canary in src_content:
                    g5_fail.append(f"{mode} rep{i}: source contains {key} canary {canary!r}")
            # protect probe：自由路由非前缀请求，不属于任何分支，
            # 其输出不得含任何分支 canary（状态污染的额外观测面）
            probe = rep.get("protect_probe") or {}
            probe_content = probe.get("content") or ""
            for key, canary in CANARIES.items():
                if canary in probe_content:
                    g5_fail.append(f"{mode} rep{i}: protect probe contains {key} canary {canary!r}")
    gates["G5_no_cross_branch_canary"] = {
        "pass": not g5_fail,
        "detail": g5_fail or "no cross-branch canary in target/source/probe text"}

    # G6: source 非完整前缀覆盖保护 —— 可判别协议，按每个 on rep 独立判断：
    #   a) protect probe（自由路由）HTTP=200；
    #   b) probe 响应实际 id_slot != 0（/completion 响应含真实 id_slot；
    #      直接验证 server 侧共享源分配 skip 保护，非完整前缀不得占用 source slot 0）；
    #   c) probe 后 /metrics/kv shared_cells 仍 > 0（状态证据，不单独作判据）；
    #   d) 重验请求：probe 后重新请求 target（自由路由），cache_n > 0
    #      证明 source 前缀仍可被共享（功能级判别，禁止只断言 shared_cells>0）。
    # 该 rep 未发生共享 → N/A。protect probe 缺 spare slot 由 CLI 前置校验避免。
    g6_fail = []
    g6_na = 0
    for i, rep in enumerate(on_reps):
        if _shared_cells_of(rep) <= 0:
            g6_na += 1
            continue  # 该 rep 未发生共享 → N/A
        probe = rep.get("protect_probe") or {}
        if probe.get("http_status") != 200:
            g6_fail.append(f"on rep{i}: protect probe HTTP {probe.get('http_status')} "
                           f"err={probe.get('error')}")
            continue
        probe_slot = probe.get("id_slot")
        if probe_slot == 0:
            g6_fail.append(f"on rep{i}: protect probe landed on source slot 0 "
                           f"(shared source overwritten?) id_slot={probe_slot}")
        after = probe.get("metrics_after") or {}
        if not (after.get("shared_cells") or 0) > 0:
            g6_fail.append(f"on rep{i}: shared_cells==0 after protect probe "
                           f"(source prefix overwritten?) {after}")
        recheck = rep.get("recheck_after_probe") or {}
        if recheck.get("http_status") != 200:
            g6_fail.append(f"on rep{i}: recheck target HTTP {recheck.get('http_status')} "
                           f"err={recheck.get('error')}")
        elif not (recheck.get("cache_n") or 0) > 0:
            g6_fail.append(f"on rep{i}: recheck target cache_n={recheck.get('cache_n')} "
                           f"(source prefix no longer shareable after probe)")
    g6_detail = (g6_fail or
                 f"every on rep with sharing: probe id_slot!=0, shared_cells>0 after probe, "
                 f"recheck cache_n>0 (source prefix still shareable) "
                 f"({len(on_reps) - g6_na}/{len(on_reps)} reps protected, "
                 f"{g6_na} reps N/A without sharing)")
    gates["G6_source_partial_prefix_protected"] = {
        "pass": not g6_fail, "detail": g6_detail}

    # G7: 每 replicate 清空后回基线（off/on 全部 formal reps）
    g7_fail = []
    for mode, reps in (("off", off_reps), ("on", on_reps)):
        for i, rep in enumerate(reps):
            if not rep.get("clean_ok"):
                g7_fail.append(f"{mode} rep{i}: clean_ok=False "
                               f"(clean_pre={rep.get('clean_pre')} clean_post={rep.get('clean_post')})")
    gates["G7_clean_baseline_per_rep"] = {
        "pass": not g7_fail, "detail": g7_fail or "all reps: used_cells==0 && active_sequences==0 after erase"}

    # G8: shutdown clean（runner 汇总；fail 时由 runner 填充 detail）
    gates["G8_shutdown_clean"] = {"pass": bool(meta.get("shutdown_clean")),
                                  "detail": meta.get("shutdown_detail", "not recorded")}

    # G9: physical_sharing 始终 false 且 shared_cells_semantics 正确（每采样）
    g9_fail = []
    for mode, reps in (("off", off_reps), ("on", on_reps)):
        for i, rep in enumerate(reps):
            for tag, m in (("post", rep.get("metrics_post") or {}),
                           ("after_probe", (rep.get("protect_probe") or {}).get("metrics_after") or {})):
                if m.get("physical_sharing") is not False:
                    g9_fail.append(f"{mode} rep{i} {tag}: physical_sharing={m.get('physical_sharing')}")
                sem = m.get("shared_cells_semantics")
                if sem != "multi-sequence cell association (metadata-level, not COW)":
                    g9_fail.append(f"{mode} rep{i} {tag}: semantics={sem!r}")
    gates["G9_metrics_contract"] = {
        "pass": not g9_fail,
        "detail": g9_fail or "physical_sharing==false && shared_cells_semantics correct on every sample"}

    # ---- verdict ----
    correctness_gates = ["G0_data_integrity",
                         "G1_output_identical_on_off",
                         "G4_off_shared_cells_zero", "G5_no_cross_branch_canary",
                         "G6_source_partial_prefix_protected", "G7_clean_baseline_per_rep",
                         "G8_shutdown_clean", "G9_metrics_contract"]
    correctness_fail = [g for g in correctness_gates if not gates[g]["pass"]]
    g3_pass = gates["G3_recompute_reduction_ge_25pct"]["pass"]

    # 特殊情形：并发下共享完全未发生（G2 fail 且无 shared 日志事件）→ HOLD，
    # 保留原始证据，不改 workload 伪造共享。
    on_log_deltas = [r.get("log_shared_delta", 0) for r in on_reps]
    sharing_absent = (not gates["G2_on_shared_cells_positive"]["pass"]
                      and all(d == 0 for d in on_log_deltas))
    sharing_contradiction = (not gates["G2_on_shared_cells_positive"]["pass"]
                             and any(d > 0 for d in on_log_deltas))

    if correctness_fail or sharing_contradiction:
        verdict = "REJECT_CORRECTNESS_OR_ISOLATION"
    elif sharing_absent:
        verdict = "HOLD_NO_MEASURABLE_GAIN"
        notes.append("concurrent targets did not share source prefix "
                     "(no 'E6-C1: shared' events, shared_cells==0); raw evidence kept; "
                     "workload NOT modified to fake sharing")
    elif not g3_pass:
        verdict = "HOLD_NO_MEASURABLE_GAIN"
    else:
        verdict = "PASS"

    return {"gates": gates, "verdict": verdict, "notes": notes}


# ---- server 生命周期 + replicate 协议 ----


class E15BranchConcurrentRunner:
    def __init__(self, server_bin: str, model: str, port: int, ctx_size: int,
                 parallel: int, cache_ram: int, min_lcp: int, n_branches: int,
                 warmup: int, reps: int, out_path: str, tmp_dir: str,
                 n_predict: int = N_PREDICT, seed: int = SEED,
                 temperature: float = TEMPERATURE) -> None:
        self.server_bin = server_bin
        self.model = model
        self.port = port
        self.ctx_size = ctx_size
        self.parallel = parallel
        self.cache_ram = cache_ram
        self.min_lcp = min_lcp
        self.n_branches = n_branches
        self.warmup = warmup
        self.reps = reps
        self.out_path = out_path
        self.tmp_dir = tmp_dir
        self.n_predict = n_predict
        self.seed = seed
        self.temperature = temperature
        self.base_url = f"http://127.0.0.1:{port}"
        self.target_branches = [f"b{i}" for i in range(1, n_branches + 1)]
        self._proc: Optional[subprocess.Popen] = None
        self._log_path = ""

    # ---- server 生命周期 ----
    def _server_cmd(self, mode: str) -> List[str]:
        cmd = [self.server_bin, "-m", self.model,
               "--host", "127.0.0.1", "--port", str(self.port),
               "-ngl", "0",
               "--ctx-size", str(self.ctx_size),
               "--kv-unified",
               "--parallel", str(self.parallel),
               "--cache-ram", str(self.cache_ram),
               "--slot-save-path", self.tmp_dir,
               "--no-webui"]
        if mode == "on":
            cmd += ["--kv-prefix-share", "--kv-prefix-share-min-lcp", str(self.min_lcp)]
        return cmd

    def _wait_health(self, timeout: float = 120.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._proc is not None and self._proc.poll() is not None:
                return False
            status, _ = _http_get_json(f"{self.base_url}/health", timeout=5.0)
            if status == 200:
                return True
            time.sleep(0.5)
        return False

    def start_server(self, mode: str) -> Dict[str, Any]:
        cmd = self._server_cmd(mode)
        self._log_path = os.path.join(self.tmp_dir, f"server_{mode}.log")
        with open(self._log_path, "w", encoding="utf-8") as lf:
            self._proc = subprocess.Popen(cmd, stdout=lf, stderr=subprocess.STDOUT)
        ready = self._wait_health()
        return {"mode": mode, "cmd": cmd, "ready": ready, "pid": self._proc.pid}

    def stop_server(self) -> Dict[str, Any]:
        proc = self._proc
        if proc is None:
            return {"clean": True, "detail": "no process"}
        clean = True
        detail = ""
        try:
            proc.send_signal(signal.SIGTERM)
            proc.wait(timeout=15.0)
            detail = f"exit_code={proc.returncode}"
            if proc.returncode not in (0, -signal.SIGTERM):
                clean = False
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10.0)
            clean = False
            detail = "SIGTERM timeout, killed"
        self._proc = None
        return {"clean": clean, "detail": detail}

    # ---- 请求 ----
    def _completion(self, prompt: str, id_slot: int) -> Dict[str, Any]:
        payload = {"prompt": prompt, "n_predict": self.n_predict,
                   "temperature": self.temperature, "seed": self.seed,
                   "cache_prompt": True, "id_slot": id_slot,
                   "return_tokens": True}  # 请求输出 token ids（默认 false 时 tokens 为空）
        t0 = time.perf_counter()
        status, body = http_request(f"{self.base_url}/completion", payload)
        ms = (time.perf_counter() - t0) * 1000
        timings = body.get("timings") if isinstance(body, dict) else None
        content = body.get("content", "") if isinstance(body, dict) else ""
        rec = {
            "http_status": status,
            "error": None if status == 200 else (body.get("error") if isinstance(body, dict) else str(body)),
            "id_slot": body.get("id_slot") if isinstance(body, dict) else None,  # 响应实际分配的 slot
            "prompt_n": timings.get("prompt_n") if isinstance(timings, dict) else None,
            "cache_n": timings.get("cache_n") if isinstance(timings, dict) else None,
            "predicted_n": timings.get("predicted_n") if isinstance(timings, dict) else None,
            "recompute": recompute_of(timings),
            "tokens": output_ids_of(body),
            "content": content,
            "content_sha256": sha256_text(content),
            "latency_ms": round(ms, 1),
            "stop_reason": body.get("stop_reason") if isinstance(body, dict) else None,
        }
        return rec

    def _kv_state(self) -> Dict[str, Any]:
        status, body = _http_get_json(f"{self.base_url}/metrics/kv", timeout=5.0)
        if status != 200:
            return {"error": body.get("error", f"HTTP {status}")}
        return body

    def _erase_all(self) -> int:
        ok = 0
        for sid in range(self.parallel):
            status, _ = http_request(
                f"{self.base_url}/slots/{sid}?action=erase", payload={}, timeout=10.0)
            if status == 200:
                ok += 1
        return ok

    def _log_shared_count(self) -> int:
        if not self._log_path or not os.path.exists(self._log_path):
            return 0
        with open(self._log_path, encoding="utf-8", errors="replace") as f:
            return f.read().count("E6-C1: shared")

    def _assert_clean(self, tag: str) -> Tuple[bool, Dict[str, Any]]:
        st = self._kv_state()
        if "error" in st:
            return False, {"error": st["error"]}
        ok = st.get("used_cells") == 0 and st.get("active_sequences") == 0
        return ok, st

    # ---- replicate（一个 off/on 模式的单次试验）----
    def _replicate(self, mode: str, rep_idx: int, formal: bool) -> Dict[str, Any]:
        rep: Dict[str, Any] = {"mode": mode, "rep": rep_idx, "formal": formal}

        # 1) 清空 slots + 断言基线（协议：每 replicate 清空并断言 used_cells/active=0）
        clean_pre_ok, clean_pre = self._assert_clean("pre")
        if not clean_pre_ok:
            # 尝试修复一次：erase 后重查
            self._erase_all()
            clean_pre_ok, clean_pre = self._assert_clean("pre_retry")
        rep["clean_pre"] = clean_pre
        rep["clean_ok"] = clean_pre_ok

        log_before = self._log_shared_count()

        # 2) source 预热（slot 0，idle 保留 KV 供分支共享）
        source_prompt = build_branch_prompt(PREFIX_CORE, "b0")
        src = self._completion(source_prompt, id_slot=0)
        src["branch"] = "b0"
        rep["source"] = src
        if src["http_status"] != 200:
            rep["error"] = f"source request failed: {src['error']}"
            # source 失败也执行 post-clean 并如实记录（不得跳过清理）
            self._erase_all()
            clean_post_ok, clean_post = self._assert_clean("post")
            rep["clean_post"] = clean_post
            rep["clean_ok"] = clean_pre_ok and clean_post_ok
            return rep

        # 3) 并发分支 target（各自独立 slot，barrier 同时发出）
        payloads = [{"branch": br, "id_slot": i + 1,
                     "prompt": build_branch_prompt(PREFIX_CORE, br)}
                    for i, br in enumerate(self.target_branches)]
        barrier = threading.Barrier(len(payloads))
        results: List[Optional[Dict[str, Any]]] = [None] * len(payloads)

        def worker(idx: int, p: Dict[str, Any]) -> None:
            barrier.wait()
            rec = self._completion(p["prompt"], p["id_slot"])
            rec["branch"] = p["branch"]
            rec["id_slot"] = p["id_slot"]
            results[idx] = rec

        with ThreadPoolExecutor(max_workers=len(payloads)) as ex:
            futs = [ex.submit(worker, i, p) for i, p in enumerate(payloads)]
            for f in futs:
                f.result()
        rep["targets"] = [r for r in results if r is not None]

        # 4) 并发后 /metrics/kv 采样
        rep["metrics_post"] = self._kv_state()

        # 5) source 非完整前缀覆盖保护验证（自由路由的非前缀请求；
        #    1951-1958 保护应使其不占用共享源 slot；响应 id_slot 应为非 0）
        probe = self._completion(PROTECT_PROBE_TEXT, id_slot=-1)
        probe["branch"] = "probe"
        probe["metrics_after"] = self._kv_state()
        rep["protect_probe"] = probe

        # 5b) 可判别重验（仅 on 模式）：probe 后重新请求 target（自由路由），
        #     其 cache_n > 0 证明 source 前缀仍可被共享（功能级判别，非仅状态计数）。
        #     off 模式无共享语义，跳过以保持 off 模式 KV 容量（4 并发全量近满池）。
        if mode == "on":
            recheck = self._completion(build_branch_prompt(PREFIX_CORE, "b1"), id_slot=-1)
            recheck["branch"] = "recheck_b1"
            recheck["metrics_after"] = self._kv_state()
            rep["recheck_after_probe"] = recheck

        # 6) server 日志 shared 事件数（delta）
        rep["log_shared_delta"] = self._log_shared_count() - log_before

        # 7) 清空 + 回基线断言
        self._erase_all()
        clean_post_ok, clean_post = self._assert_clean("post")
        rep["clean_post"] = clean_post
        rep["clean_ok"] = rep.get("clean_ok", True) and clean_post_ok and clean_pre_ok
        return rep

    # ---- 全流程 ----
    def run(self) -> Dict[str, Any]:
        modes: Dict[str, Any] = {}
        shutdown_results: Dict[str, Any] = {}
        fatal_error: Optional[Dict[str, Any]] = None
        current_mode: Optional[str] = None
        try:
            for mode in ("off", "on"):
                current_mode = mode
                start = self.start_server(mode)
                modes[mode] = {"start": start}
                if not start["ready"]:
                    fatal_error = {"stage": f"start_server({mode})", "detail": start}
                    # server 已 Popen 但未就绪：立即停止，不泄漏进程
                    stop = self.stop_server()
                    shutdown_results[mode] = stop
                    modes[mode]["stop"] = stop
                    break
                reps: List[Dict[str, Any]] = []
                try:
                    for i in range(self.warmup + self.reps):
                        formal = i >= self.warmup
                        rep = self._replicate(mode, i, formal)
                        reps.append(rep)
                    modes[mode]["replicates"] = reps
                finally:
                    # replicate 异常/正常均停止 server（不泄漏进程）
                    stop = self.stop_server()
                    shutdown_results[mode] = stop
                    modes[mode]["stop"] = stop
                time.sleep(1.0)
        except Exception as e:  # noqa: BLE001
            fatal_error = {"stage": f"run({current_mode})",
                           "type": type(e).__name__, "message": str(e)}
            # 异常路径兜底：若 server 仍存活则强制停止（try/finally 未覆盖的场景）
            if self._proc is not None and current_mode is not None:
                stop = self.stop_server()
                if current_mode not in shutdown_results:
                    shutdown_results[current_mode] = stop
                    modes.setdefault(current_mode, {})["stop"] = stop

        off_formal = [r for r in modes.get("off", {}).get("replicates", []) if r.get("formal")]
        on_formal = [r for r in modes.get("on", {}).get("replicates", []) if r.get("formal")]
        meta = self._meta()
        meta["shutdown_clean"] = bool(shutdown_results.get("on", {}).get("clean")) and \
            bool(shutdown_results.get("off", {}).get("clean"))
        meta["shutdown_detail"] = {"off": shutdown_results.get("off"),
                                   "on": shutdown_results.get("on")}
        if fatal_error is not None:
            meta["fatal_error"] = fatal_error
            gates: Dict[str, Any] = {"G_preflight_data_present":
                                     {"pass": False, "detail": fatal_error}}
            verdict = "INVALID"
            notes: List[str] = [f"runner aborted at {fatal_error.get('stage')}: "
                                f"{fatal_error.get('message', '')}"]
        else:
            gate_res = gate_verdict(off_formal, on_formal, meta=meta)
            gates = gate_res["gates"]
            verdict = gate_res["verdict"]
            notes = gate_res["notes"]

        result = {
            "meta": meta,
            "modes": modes,
            "gates": gates,
            "verdict": verdict,
            "notes": notes,
        }
        # 部分原始结果也落盘（fatal 时含已完成的 replicates / shutdown 证据）
        os.makedirs(os.path.dirname(self.out_path) or ".", exist_ok=True)
        with open(self.out_path, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, ensure_ascii=False)
        return result

    def _meta(self) -> Dict[str, Any]:
        ts = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
        return {
            "stage": "E15.1",
            "runner": "e15_branch_concurrent",
            "model": self.model,
            "server_bin": self.server_bin,
            "port": self.port,
            "ctx_size": self.ctx_size,
            "parallel": self.parallel,
            "cache_ram": self.cache_ram,
            "min_lcp": self.min_lcp,
            "n_branches": self.n_branches,
            "target_branches": self.target_branches,
            "warmup": self.warmup,
            "reps": self.reps,
            "n_predict": self.n_predict,
            "seed": self.seed,
            "temperature": self.temperature,
            "protocol": "paired off/on; common prefix warmed on source slot then idle; "
                        "branch targets fired concurrently (ThreadPoolExecutor+barrier); "
                        "each replicate: erase all slots + assert used_cells==0/active==0",
            "timestamp": ts,
        }


# ---- CLI ----


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="E15.1 branch concurrent paired runner")
    ap.add_argument("--server-bin", required=True, help="llama-server 二进制路径")
    ap.add_argument("--model", required=True, help="GGUF 模型路径（TinyLlama stories260K f32）")
    ap.add_argument("--port", type=int, default=8091)
    ap.add_argument("--ctx-size", type=int, default=2048)
    ap.add_argument("--parallel", type=int, default=5, help="server --parallel（source+targets+1 spare）")
    ap.add_argument("--cache-ram", type=int, default=0, help="--cache-ram MiB（0 = 禁用 RAM prompt cache）")
    ap.add_argument("--min-lcp", type=int, default=DEFAULT_MIN_LCP, help="--kv-prefix-share-min-lcp")
    ap.add_argument("--branches", type=int, default=3, choices=[2, 3], help="分支 target 数（2 或 3）")
    ap.add_argument("--warmup", type=int, default=2)
    ap.add_argument("--reps", type=int, default=5)
    ap.add_argument("--out", default="", help="结果 JSON 路径（默认 results/e15_branch_<ts>.json）")
    ap.add_argument("--tmp-dir", default="", help="server 日志/slot-save 目录（默认 tempfile）")
    args = ap.parse_args(argv)

    # ---- CLI 前置校验（不满足直接拒绝，不启动 server）----
    if args.warmup < 0:
        ap.error(f"--warmup must be >= 0 (got {args.warmup})")
    if args.reps < 1:
        ap.error(f"--reps must be >= 1 (got {args.reps})")
    # --branches choices=[2,3] 已由 argparse 校验（branches supported）
    min_parallel = args.branches + 2  # source slot + target slots + protect probe spare slot
    if args.parallel < min_parallel:
        ap.error(f"--parallel {args.parallel} < branches+2 = {min_parallel} "
                 f"(need 1 source + {args.branches} targets + 1 spare slot for protect probe)")
    if args.ctx_size <= 0:
        ap.error(f"--ctx-size must be positive (got {args.ctx_size})")
    # 容量前置：off 模式 4 并发全量 KV 峰值（source + targets 全量 + predicted）须 < ctx，
    # 否则开箱必败（unified KV 池溢出）。估算保守偏大（est_tokens 用 2.5 chars/token）。
    src_est = est_tokens(build_branch_prompt(PREFIX_CORE, "b0"))
    tgt_est = est_tokens(build_branch_prompt(PREFIX_CORE, "b1"))
    est_off_peak = src_est + args.branches * (tgt_est + N_PREDICT)
    if args.ctx_size <= est_off_peak:
        ap.error(f"--ctx-size {args.ctx_size} <= estimated off-mode full-KV peak ~{est_off_peak} "
                 f"(source ~{src_est} + {args.branches}×target ~{tgt_est}+{N_PREDICT}); "
                 f"off mode would overflow the unified KV pool (Context exceeded)")

    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    out = args.out or os.path.join("results", f"e15_branch_{ts}.json")
    tmp_dir = args.tmp_dir or tempfile.mkdtemp(prefix="e15_branch_")

    runner = E15BranchConcurrentRunner(
        server_bin=args.server_bin, model=args.model, port=args.port,
        ctx_size=args.ctx_size, parallel=args.parallel, cache_ram=args.cache_ram,
        min_lcp=args.min_lcp, n_branches=args.branches,
        warmup=args.warmup, reps=args.reps, out_path=out, tmp_dir=tmp_dir)

    print(f"[E15.1] server={args.server_bin}\n"
          f"[E15.1] model={args.model}\n"
          f"[E15.1] ctx={args.ctx_size} parallel={args.parallel} cache_ram={args.cache_ram} "
          f"min_lcp={args.min_lcp} branches={args.branches}\n"
          f"[E15.1] warmup={args.warmup} reps={args.reps} seed=42 temp=0 n_predict={runner.n_predict}\n"
          f"[E15.1] running off/on paired protocol ...")
    result = runner.run()
    if result.get("meta", {}).get("fatal_error"):
        print(f"[E15.1] FATAL: {result['meta']['fatal_error']}")
        return 2

    print(f"[E15.1] verdict = {result['verdict']}")
    for name, g in result["gates"].items():
        print(f"  {name}: {'PASS' if g['pass'] else 'FAIL'}  {g['detail']}")
    for note in result["notes"]:
        print(f"  NOTE: {note}")
    print(f"[E15.1] results -> {out}")
    return 0 if result["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
