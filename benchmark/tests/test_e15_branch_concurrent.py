"""E15.1：分支并发 paired runner 纯函数测试（不依赖 server / GPU / 真实模型）。

覆盖（对应代码审查修复项）：
- build_branch_prompt 确定性 + 分支首 token 互异（LCP 精确终止于前缀末尾）；
- sha256_text / output_ids_of / recompute_of 提取语义；真实 /completion 响应形态
  （tokens_predicted 为 int、tokens 为 list → 只读 tokens，不误用 int 计数；
  异常 token 元素 fail-safe）；
- canary_violations 跨分支污染检测（只检查解码文本 content，不拼 token ids）；
- gate_verdict：G0 数据完整性（source/全 target/单 target 失败、recompute missing、
  branch missing/None/重复 → REJECT）、空 reps → INVALID、rep count mismatch /
  branch missing / content hash mismatch → REJECT、单 rep no share → 该 rep G6 N/A
  整体仍 PASS、G3 不达 → HOLD、共享未发生 → HOLD、G1/G5/G6/G7/G8/G9 失败 → REJECT；
- G6 可判别协议：probe id_slot!=0（不得占用 source slot 0）+ probe 后 shared_cells>0
  + recheck 重验 target cache_n>0（非恒真门禁）；
- G5 也检查 source/protect probe 可观测文本；
- runner 生命周期：ready=False / replicate 异常均 stop server、部分结果落盘、fatal_error；
- CLI 参数校验（warmup<0、reps<1、parallel<branches+2、ctx 非正/过小 → 拒绝）。
"""
from __future__ import annotations

import os

import pytest

from runner.e15_branch_concurrent import (
    BRANCHES,
    CANARY_B1,
    CANARY_B2,
    CANARY_B3,
    E15BranchConcurrentRunner,
    PREFIX_CORE,
    build_branch_prompt,
    canary_violations,
    gate_verdict,
    main,
    output_ids_of,
    recompute_of,
    sha256_text,
)

SEMANTICS = "multi-sequence cell association (metadata-level, not COW)"


# ---- prompt 构造 ----


def test_build_branch_prompt_deterministic():
    p1 = build_branch_prompt(PREFIX_CORE, "b1")
    p2 = build_branch_prompt(PREFIX_CORE, "b1")
    assert p1 == p2
    assert p1.startswith(PREFIX_CORE)
    assert p1 == PREFIX_CORE + BRANCHES["b1"]


def test_branch_first_tokens_differ():
    # LCP 精确终止于前缀末尾的前提：分支首 token 互不相同
    heads = []
    for key in ("b0", "b1", "b2", "b3"):
        tail = BRANCHES[key].lstrip()
        heads.append(tail.split()[0])
    assert len(set(heads)) == 4, heads


def test_canary_embedded_in_each_branch():
    for key in ("b0", "b1", "b2", "b3"):
        assert BRANCHES[key].count("CANARY-") >= 1


# ---- 提取语义 ----


def test_sha256_text_stable():
    assert sha256_text("hello") == sha256_text("hello")
    assert len(sha256_text("x")) == 64


def test_output_ids_of_extraction():
    assert output_ids_of({"tokens": [1, 2, 3]}) == [1, 2, 3]
    assert output_ids_of({"tokens": []}) == []
    assert output_ids_of({}) == []
    assert output_ids_of({"tokens": "nope"}) == []


def test_output_ids_of_real_response_shape():
    # 真实 /completion 响应形态：tokens_predicted 是 int（n_decoded 计数），
    # tokens 是输出 token ids list；两者同时存在时必须只读 tokens。
    body = {"tokens": [1, 2, 3], "tokens_predicted": 3}
    assert output_ids_of(body) == [1, 2, 3]
    # tokens_predicted 单独出现是 int，不得被当作 ids 数组读取（不误用）
    assert output_ids_of({"tokens_predicted": 8}) == []
    assert output_ids_of({"tokens_predicted": 8, "tokens": []}) == []


def test_recompute_of_extraction():
    assert recompute_of({"prompt_n": 100, "cache_n": 50}) == 100
    assert recompute_of({"prompt_n": 0}) == 0
    assert recompute_of(None) is None
    assert recompute_of({}) is None
    assert recompute_of({"prompt_n": "x"}) is None


# ---- canary 污染检测（只检查 content，不拼 token ids）----


def _target(branch, toks, content=""):
    return {"branch": branch, "tokens": toks, "content": content}


def test_canary_no_violation():
    rows = [
        _target("b1", [10, 20], "east output"),
        _target("b2", [30, 40], "west output"),
        _target("b3", [50, 60], "south output"),
    ]
    assert canary_violations(rows) == []


def test_canary_violation_detected():
    rows = [_target("b1", [10], f"leaked {CANARY_B2} here")]
    v = canary_violations(rows)
    assert len(v) == 1
    assert "b1" in v[0] and "b2" in v[0]


def test_canary_own_branch_not_violation():
    # 输出复述本分支 canary 不算跨分支污染
    rows = [_target("b2", [10], CANARY_B2)]
    assert canary_violations(rows) == []


def test_canary_only_checks_content_not_token_ids():
    # canary 是字符串标记，token ids 是整数：拼 ids 只会稀释检测。
    # 即使 token ids 里"看起来"有 canary 相关数字，也只按 content 判定。
    rows = [_target("b1", [77, 67], "clean east text")]
    assert canary_violations(rows) == []
    rows2 = [_target("b1", [10], CANARY_B3)]
    v = canary_violations(rows2)
    assert len(v) == 1 and "b3" in v[0]


# ---- gate_verdict ----


def _kv(shared=0, physical=False):
    return {
        "used_cells": 100, "shared_cells": shared,
        "active_sequences": 5, "capacity_cells": 512,
        "physical_sharing": physical, "shared_cells_semantics": SEMANTICS,
    }


def _source_rec(recompute=200):
    return {"http_status": 200, "error": None, "prompt_n": recompute, "cache_n": 0,
            "recompute": recompute, "tokens": [100, 101], "content": "src-out",
            "content_sha256": sha256_text("src-out")}


def _target_rec(branch, toks, recompute, content=None):
    content = f"out-{branch}" if content is None else content
    return {"branch": branch, "http_status": 200, "error": None,
            "tokens": list(toks), "recompute": recompute,
            "content": content, "content_sha256": sha256_text(content)}


def _rep(shared, targets, clean_ok=True, probe_shared=None, log_delta=0, source=None,
         probe_id_slot=4, recheck_http=200, recheck_cache_n=454, recheck_error=None):
    probe_shared = shared if probe_shared is None else probe_shared
    return {
        "source": source if source is not None else _source_rec(),
        "clean_pre": {"used_cells": 0, "active_sequences": 0},
        "clean_post": {"used_cells": 0, "active_sequences": 0},
        "clean_ok": clean_ok,
        "targets": targets,
        "metrics_post": _kv(shared=shared),
        "protect_probe": {"http_status": 200, "error": None, "id_slot": probe_id_slot,
                          "metrics_after": _kv(shared=probe_shared)},
        "recheck_after_probe": {"http_status": recheck_http, "error": recheck_error,
                                "id_slot": 4, "cache_n": recheck_cache_n, "recompute": 46,
                                "metrics_after": _kv(shared=probe_shared)},
        "log_shared_delta": log_delta,
    }


def _all_pass_reps(shared, recompute_off=200, recompute_on=50, log_delta=0):
    """构造 off/on 各 5 个 formal replicate（recompute 下降 75%）。"""
    off = []
    on = []
    for i in range(5):
        off.append(_rep(0, [
            _target_rec("b1", [1, 2, 3], recompute_off),
            _target_rec("b2", [4, 5, 6], recompute_off),
            _target_rec("b3", [7, 8, 9], recompute_off),
        ], log_delta=0))
        on.append(_rep(shared, [
            _target_rec("b1", [1, 2, 3], recompute_on),
            _target_rec("b2", [4, 5, 6], recompute_on),
            _target_rec("b3", [7, 8, 9], recompute_on),
        ], log_delta=log_delta))
    return off, on


def test_gate_verdict_all_pass():
    off, on = _all_pass_reps(shared=42, log_delta=3)
    meta = {"shutdown_clean": True, "shutdown_detail": {"off": {}, "on": {}}}
    res = gate_verdict(off, on, meta)
    assert res["verdict"] == "PASS", res
    for name, g in res["gates"].items():
        assert g["pass"], f"{name}: {g['detail']}"


def test_gate_verdict_recompute_under_25pct_hold():
    # on recompute 只降 10% → HOLD_NO_MEASURABLE_GAIN
    off, on = _all_pass_reps(shared=42, recompute_off=200, recompute_on=180)
    res = gate_verdict(off, on, {"shutdown_clean": True})
    assert res["verdict"] == "HOLD_NO_MEASURABLE_GAIN"
    assert res["gates"]["G3_recompute_reduction_ge_25pct"]["pass"] is False


def test_gate_verdict_output_mismatch_reject():
    off, on = _all_pass_reps(shared=42)
    on[0]["targets"][0]["tokens"] = [99, 99, 99]
    res = gate_verdict(off, on, {"shutdown_clean": True})
    assert res["verdict"] == "REJECT_CORRECTNESS_OR_ISOLATION"
    assert res["gates"]["G1_output_identical_on_off"]["pass"] is False


def test_gate_verdict_content_hash_mismatch_reject():
    # token ids 相同但解码文本不同（content_sha256 不一致）→ G1 冗余门禁捕获
    off, on = _all_pass_reps(shared=42)
    on[0]["targets"][0]["content"] = "different output text"
    on[0]["targets"][0]["content_sha256"] = sha256_text("different output text")
    res = gate_verdict(off, on, {"shutdown_clean": True})
    assert res["verdict"] == "REJECT_CORRECTNESS_OR_ISOLATION"
    assert res["gates"]["G1_output_identical_on_off"]["pass"] is False


def test_gate_verdict_no_sharing_hold():
    # 共享完全未发生：on shared=0 且日志无 shared 事件 → HOLD（不伪造 workload）
    off, on = _all_pass_reps(shared=0, recompute_off=200, recompute_on=200, log_delta=0)
    res = gate_verdict(off, on, {"shutdown_clean": True})
    assert res["verdict"] == "HOLD_NO_MEASURABLE_GAIN"
    assert "not modified to fake sharing" in " ".join(res["notes"]).lower()


def test_gate_verdict_metrics_contract_reject():
    off, on = _all_pass_reps(shared=42)
    # 篡改一个采样的 physical_sharing → REJECT
    on[2]["metrics_post"]["physical_sharing"] = True
    res = gate_verdict(off, on, {"shutdown_clean": True})
    assert res["verdict"] == "REJECT_CORRECTNESS_OR_ISOLATION"
    assert res["gates"]["G9_metrics_contract"]["pass"] is False


def test_gate_verdict_shutdown_not_clean_reject():
    off, on = _all_pass_reps(shared=42)
    res = gate_verdict(off, on, {"shutdown_clean": False})
    assert res["verdict"] == "REJECT_CORRECTNESS_OR_ISOLATION"
    assert res["gates"]["G8_shutdown_clean"]["pass"] is False


def test_gate_verdict_clean_fail_reject():
    off, on = _all_pass_reps(shared=42)
    off[1]["clean_ok"] = False
    res = gate_verdict(off, on, {"shutdown_clean": True})
    assert res["verdict"] == "REJECT_CORRECTNESS_OR_ISOLATION"
    assert res["gates"]["G7_clean_baseline_per_rep"]["pass"] is False


def test_gate_verdict_probe_overwrite_reject():
    # protect probe 后 shared_cells 归 0 → source 被覆盖 → REJECT
    off, on = _all_pass_reps(shared=42)
    on[3]["protect_probe"]["metrics_after"]["shared_cells"] = 0
    res = gate_verdict(off, on, {"shutdown_clean": True})
    assert res["verdict"] == "REJECT_CORRECTNESS_OR_ISOLATION"
    assert res["gates"]["G6_source_partial_prefix_protected"]["pass"] is False


def test_gate_verdict_canary_violation_reject():
    off, on = _all_pass_reps(shared=42)
    # 同步更新 sha，使 REJECT 仅由 G5 触发（隔离污染本身即正确性失败）
    on[0]["targets"][1]["content"] = f"oops {CANARY_B1}"
    on[0]["targets"][1]["content_sha256"] = sha256_text(on[0]["targets"][1]["content"])
    res = gate_verdict(off, on, {"shutdown_clean": True})
    assert res["verdict"] == "REJECT_CORRECTNESS_OR_ISOLATION"
    assert res["gates"]["G5_no_cross_branch_canary"]["pass"] is False


def test_gate_verdict_rep_count_mismatch():
    off, on = _all_pass_reps(shared=42)
    on = on[:4]
    res = gate_verdict(off, on, {"shutdown_clean": True})
    assert res["verdict"] == "REJECT_CORRECTNESS_OR_ISOLATION"
    assert res["gates"]["G1_output_identical_on_off"]["pass"] is False


# ---- G0 数据完整性 ----


def test_gate_verdict_source_failed_reject():
    off, on = _all_pass_reps(shared=42)
    on[0]["source"]["http_status"] = 500
    on[0]["source"]["error"] = "source request failed"
    res = gate_verdict(off, on, {"shutdown_clean": True})
    assert res["verdict"] == "REJECT_CORRECTNESS_OR_ISOLATION"
    assert res["gates"]["G0_data_integrity"]["pass"] is False


def test_gate_verdict_all_targets_failed_reject():
    # 全 target 失败（某 rep 所有分支并发都失败）→ G0 直接 REJECT，不能 HOLD
    off, on = _all_pass_reps(shared=42)
    for t in on[2]["targets"]:
        t["http_status"] = 500
        t["error"] = "server error"
    res = gate_verdict(off, on, {"shutdown_clean": True})
    assert res["verdict"] == "REJECT_CORRECTNESS_OR_ISOLATION"
    assert res["gates"]["G0_data_integrity"]["pass"] is False


def test_gate_verdict_single_target_failed_reject():
    off, on = _all_pass_reps(shared=42)
    on[0]["targets"][1]["http_status"] = 500
    on[0]["targets"][1]["error"] = "server error"
    res = gate_verdict(off, on, {"shutdown_clean": True})
    assert res["verdict"] == "REJECT_CORRECTNESS_OR_ISOLATION"
    assert res["gates"]["G0_data_integrity"]["pass"] is False


def test_gate_verdict_recompute_missing_reject():
    # recompute（prompt_n）缺失 → G0 失败 → REJECT（不能当作无可测收益 HOLD）
    off, on = _all_pass_reps(shared=42)
    on[1]["targets"][0]["recompute"] = None
    res = gate_verdict(off, on, {"shutdown_clean": True})
    assert res["verdict"] == "REJECT_CORRECTNESS_OR_ISOLATION"
    assert res["gates"]["G0_data_integrity"]["pass"] is False


def test_gate_verdict_empty_reps_invalid():
    # off/on 空数据或 rep count<1 → INVALID（不进入门禁，不误判 PASS/HOLD）
    res = gate_verdict([], [], {"shutdown_clean": True})
    assert res["verdict"] == "INVALID"
    res2 = gate_verdict([], _all_pass_reps(shared=42)[1], {"shutdown_clean": True})
    assert res2["verdict"] == "INVALID"
    res3 = gate_verdict(_all_pass_reps(shared=42)[0], [], {"shutdown_clean": True})
    assert res3["verdict"] == "INVALID"


# ---- G1 branch key 集完整性 ----


def test_gate_verdict_branch_missing_reject():
    # 所有 on rep 都缺 b3 → 全局 branch key 集不完整 → REJECT
    off, on = _all_pass_reps(shared=42)
    for rep in on:
        rep["targets"] = [t for t in rep["targets"] if t["branch"] != "b3"]
    res = gate_verdict(off, on, {"shutdown_clean": True})
    assert res["verdict"] == "REJECT_CORRECTNESS_OR_ISOLATION"
    assert res["gates"]["G1_output_identical_on_off"]["pass"] is False


def test_gate_verdict_single_rep_branch_missing_reject():
    # 仅一个 on rep 缺 b2（其余 rep 完整）→ 配对 branch 集不同 → REJECT
    off, on = _all_pass_reps(shared=42)
    on[2]["targets"] = [t for t in on[2]["targets"] if t["branch"] != "b2"]
    res = gate_verdict(off, on, {"shutdown_clean": True})
    assert res["verdict"] == "REJECT_CORRECTNESS_OR_ISOLATION"
    assert res["gates"]["G1_output_identical_on_off"]["pass"] is False


# ---- G6 按 rep 独立判断 ----


def test_gate_verdict_single_rep_no_share_g6_na():
    # 仅一个 on rep 未发生共享 → 该 rep 的 G6 独立判定为 N/A（不判 FAIL），
    # 其余 rep 有共享且 protect probe 后仍 >0 → 整体仍 PASS
    off, on = _all_pass_reps(shared=42)
    on[2] = _rep(0, [
        _target_rec("b1", [1, 2, 3], 50),
        _target_rec("b2", [4, 5, 6], 50),
        _target_rec("b3", [7, 8, 9], 50),
    ], log_delta=0)
    res = gate_verdict(off, on, {"shutdown_clean": True})
    assert res["verdict"] == "PASS", res
    assert res["gates"]["G6_source_partial_prefix_protected"]["pass"] is True


def test_gate_verdict_probe_http_fail_reject():
    # protect probe 本身 HTTP 失败（无 spare slot / 服务异常）→ 该 rep G6 FAIL → REJECT
    off, on = _all_pass_reps(shared=42)
    on[1]["protect_probe"]["http_status"] = 503
    on[1]["protect_probe"]["error"] = "no free slot"
    res = gate_verdict(off, on, {"shutdown_clean": True})
    assert res["verdict"] == "REJECT_CORRECTNESS_OR_ISOLATION"
    assert res["gates"]["G6_source_partial_prefix_protected"]["pass"] is False


# ---- CLI 参数校验 ----


def test_cli_rejects_warmup_negative():
    with pytest.raises(SystemExit):
        main(["--server-bin", "x", "--model", "y", "--warmup", "-1"])


def test_cli_rejects_reps_zero():
    with pytest.raises(SystemExit):
        main(["--server-bin", "x", "--model", "y", "--reps", "0"])


def test_cli_rejects_parallel_too_small():
    # branches=3 需 parallel >= 3+2=5；4 → 拒绝（protect probe 无 spare slot）
    with pytest.raises(SystemExit):
        main(["--server-bin", "x", "--model", "y", "--branches", "3", "--parallel", "4"])


def test_cli_rejects_branches_unsupported():
    with pytest.raises(SystemExit):
        main(["--server-bin", "x", "--model", "y", "--branches", "4"])


def test_cli_rejects_ctx_nonpositive():
    with pytest.raises(SystemExit):
        main(["--server-bin", "x", "--model", "y", "--ctx-size", "0"])
    with pytest.raises(SystemExit):
        main(["--server-bin", "x", "--model", "y", "--ctx-size", "-1"])


def test_cli_rejects_ctx_too_small():
    # off 模式 3 分支全量 KV 峰值估算远超 64 → 开箱必败，拒绝
    with pytest.raises(SystemExit):
        main(["--server-bin", "x", "--model", "y", "--ctx-size", "64"])


# ---- output_ids_of fail-safe ----


def test_output_ids_of_failsafe_elements():
    # 异常 token 元素（None/字符串/嵌套）跳过，不崩溃
    assert output_ids_of({"tokens": [1, "x", None, 3, {"a": 1}]}) == [1, 3]
    assert output_ids_of({"tokens": ["2", "4"]}) == [2, 4]
    assert output_ids_of({"tokens": [None]}) == []


# ---- G0：branch missing / None / 重复 ----


def test_gate_verdict_target_branch_missing_reject():
    off, on = _all_pass_reps(shared=42)
    del on[0]["targets"][0]["branch"]
    res = gate_verdict(off, on, {"shutdown_clean": True})
    assert res["verdict"] == "REJECT_CORRECTNESS_OR_ISOLATION"
    assert res["gates"]["G0_data_integrity"]["pass"] is False


def test_gate_verdict_target_branch_none_reject():
    off, on = _all_pass_reps(shared=42)
    on[1]["targets"][2]["branch"] = None
    res = gate_verdict(off, on, {"shutdown_clean": True})
    assert res["verdict"] == "REJECT_CORRECTNESS_OR_ISOLATION"
    assert res["gates"]["G0_data_integrity"]["pass"] is False


def test_gate_verdict_duplicate_branch_reject():
    # 同一 rep 两个 target 同 branch：dict 覆盖会掩盖 G1 比较 → G0 直接 REJECT
    off, on = _all_pass_reps(shared=42)
    on[2]["targets"].append(_target_rec("b1", [1, 2, 3], 50))
    res = gate_verdict(off, on, {"shutdown_clean": True})
    assert res["verdict"] == "REJECT_CORRECTNESS_OR_ISOLATION"
    assert res["gates"]["G0_data_integrity"]["pass"] is False


# ---- G6 可判别协议（非恒真门禁） ----


def test_gate_verdict_probe_on_source_slot_reject():
    # probe 自由路由落到 source slot 0 → 共享源被覆盖 → G6 FAIL → REJECT
    off, on = _all_pass_reps(shared=42)
    on[1]["protect_probe"]["id_slot"] = 0
    res = gate_verdict(off, on, {"shutdown_clean": True})
    assert res["verdict"] == "REJECT_CORRECTNESS_OR_ISOLATION"
    assert res["gates"]["G6_source_partial_prefix_protected"]["pass"] is False


def test_gate_verdict_recheck_no_cache_reject():
    # probe 后重验 target cache_n=0（source 前缀不再可共享）→ G6 FAIL → REJECT
    # 证明 G6 不是恒真门禁：shared_cells>0 不足以掩盖 recheck 失败
    off, on = _all_pass_reps(shared=42)
    on[3]["recheck_after_probe"]["cache_n"] = 0
    res = gate_verdict(off, on, {"shutdown_clean": True})
    assert res["verdict"] == "REJECT_CORRECTNESS_OR_ISOLATION"
    assert res["gates"]["G6_source_partial_prefix_protected"]["pass"] is False


def test_gate_verdict_recheck_http_fail_reject():
    off, on = _all_pass_reps(shared=42)
    on[0]["recheck_after_probe"]["http_status"] = 503
    on[0]["recheck_after_probe"]["error"] = "no free slot"
    res = gate_verdict(off, on, {"shutdown_clean": True})
    assert res["verdict"] == "REJECT_CORRECTNESS_OR_ISOLATION"
    assert res["gates"]["G6_source_partial_prefix_protected"]["pass"] is False


# ---- G5：source / protect probe 可观测文本 ----


def test_gate_verdict_source_canary_reject():
    off, on = _all_pass_reps(shared=42)
    on[1]["source"]["content"] = f"leaked {CANARY_B2}"
    on[1]["source"]["content_sha256"] = sha256_text(on[1]["source"]["content"])
    res = gate_verdict(off, on, {"shutdown_clean": True})
    assert res["verdict"] == "REJECT_CORRECTNESS_OR_ISOLATION"
    assert res["gates"]["G5_no_cross_branch_canary"]["pass"] is False


def test_gate_verdict_probe_canary_reject():
    off, on = _all_pass_reps(shared=42)
    on[2]["protect_probe"]["content"] = f"probe leaked {CANARY_B1}"
    res = gate_verdict(off, on, {"shutdown_clean": True})
    assert res["verdict"] == "REJECT_CORRECTNESS_OR_ISOLATION"
    assert res["gates"]["G5_no_cross_branch_canary"]["pass"] is False


# ---- runner 生命周期：ready=False / replicate 异常均清理不泄漏 ----


class _FakeProc:
    pid = 424242
    returncode = 0


def _make_runner(tmp_path):
    return E15BranchConcurrentRunner(
        server_bin="fake", model="fake", port=8091, ctx_size=2048, parallel=5,
        cache_ram=0, min_lcp=64, n_branches=3, warmup=2, reps=5,
        out_path=str(tmp_path / "out.json"), tmp_dir=str(tmp_path))


def test_run_server_not_ready_stops_server(tmp_path, monkeypatch):
    r = _make_runner(tmp_path)
    stopped = []

    def fake_start(mode):
        r._proc = _FakeProc()
        return {"mode": mode, "ready": False, "cmd": [], "pid": 424242}

    def fake_stop():
        stopped.append(r._proc.pid)
        r._proc = None
        return {"clean": True, "detail": "fake"}

    monkeypatch.setattr(r, "start_server", fake_start)
    monkeypatch.setattr(r, "stop_server", fake_stop)
    res = r.run()
    assert stopped == [424242]  # ready=False 也 stop server，不泄漏进程
    assert r._proc is None
    assert res["meta"].get("fatal_error") is not None
    assert res["verdict"] == "INVALID"
    assert os.path.exists(str(tmp_path / "out.json"))  # 部分原始结果落盘


def test_run_replicate_exception_stops_server_and_persists(tmp_path, monkeypatch):
    r = _make_runner(tmp_path)
    stopped = []

    def fake_start(mode):
        r._proc = _FakeProc()
        return {"mode": mode, "ready": True, "cmd": [], "pid": 424242}

    def fake_stop():
        stopped.append(r._proc.pid)
        r._proc = None
        return {"clean": True, "detail": "fake"}

    def boom(mode, i, formal):
        raise RuntimeError("replicate boom")

    monkeypatch.setattr(r, "start_server", fake_start)
    monkeypatch.setattr(r, "stop_server", fake_stop)
    monkeypatch.setattr(r, "_replicate", boom)
    res = r.run()
    assert stopped == [424242]  # replicate 异常后仍 stop server
    assert r._proc is None
    fe = res["meta"].get("fatal_error")
    assert fe is not None and fe.get("type") == "RuntimeError"
    assert res["verdict"] == "INVALID"
    assert os.path.exists(str(tmp_path / "out.json"))  # 异常时部分结果仍落盘
