"""m0_schema 测试（设计 §5.1 测试要求：canonical 22 键、EXPECTED 集合精确相等、
parity 交叉不变量、SCHEMA_INVALID 逐字段、source_phase 规则、决策不变量、
modes/group/rep、aggregate_verdict 优先级、原子写/退出码）。"""
from __future__ import annotations

import json
import os
import time

import pytest

from runner import m0_schema as sch
from runner.fanout_prompts import expected_unit_ids

# ---- 常量 ----

EXPECTED = set(sch.EXPECTED_UNIT_IDS)


class TestConstants:
    def test_canonical_meta_keys_22(self):
        assert len(sch.CANONICAL_META_KEYS) == 22
        assert len(set(sch.CANONICAL_META_KEYS)) == 22
        # 关键键存在
        for k in ("workload", "model_id", "phase", "parity_ok", "parity_progress",
                  "decision_validation", "planned_units", "matrix_complete",
                  "source_phase"):
            assert k in sch.CANONICAL_META_KEYS
        # v42：parallel/fanout/prefix_len/branch_len 已移除
        for k in ("parallel", "fanout", "prefix_len", "branch_len"):
            assert k not in sch.CANONICAL_META_KEYS

    def test_expected_unit_ids_24_and_exact(self):
        # 断言集合精确相等（v47），而非仅长度
        assert len(sch.EXPECTED_UNIT_IDS) == 24
        assert set(sch.EXPECTED_UNIT_IDS) == set(expected_unit_ids())

    def test_exit_codes(self):
        assert sch.EXIT_USAGE == 64
        assert sch.EXIT_SCHEMA_INVALID == 65
        assert sch.EXIT_SOFTWARE == 70
        assert sch.EXIT_IO_WRITE_FAILED == 74

    def test_matrix_constants(self):
        assert sch.FORMAL_REPS == 5
        assert sch.WARMUP_REPS == 2

    def test_planned_units_24(self):
        assert sch.planned_units_of() == 24


class TestIDs:
    def test_unit_id_roundtrip(self):
        uid = "off:q8_0-q8_0:f4:medium"
        parsed = sch.parse_unit_id(uid)
        assert parsed == {"control": "off", "ctk": "q8_0", "ctv": "q8_0",
                          "fanout": "4", "bucket": "medium"}
        assert sch.unit_id_of("off", "q8_0", "q8_0", 4, "medium") == uid

    def test_unit_id_invalid(self):
        for bad in ("", "off:q8_0:f4:medium", "on:f16-f16:f3:short",
                    "x:q8_0-q8_0:f4:medium", "off:q8_0-q8_0:f4:tiny"):
            assert sch.parse_unit_id(bad) is None

    def test_group_id(self):
        assert sch.group_id_of("on", "f16", "f16", 8) == "on:f16-f16:f8"
        assert sch.parse_group_id("on:f16-f16:f8")["fanout"] == "8"


def _meta_ok(over=None, base=None):
    m = base if base is not None else {
        "workload": "fanout",
        "design_ref": "M0_BRANCH_MEMORY_BASELINE_DESIGN.md",
        "model_id": "mock",
        "binary_version": "v-mock",
        "ctx_size": 4096,
        "cache_profiles": [{"ctk": "q8_0", "ctv": "q8_0"}, {"ctk": "f16", "ctv": "f16"}],
        "protocol": "OAI /v1/chat/completions, temp=0, seed=42, no-think",
        "source_phase": "formal",
        "phase": "formal",
        "preflight_status": "N/A",
        "preflight_reason": None,
        "parity_ok": True,
        "parity_progress": {"completed": list(sch.PARITY_BUCKETS), "errors": []},
        "parity_compensation": None,
        "token_count_method": sch.TOKEN_COUNT_METHOD,
        "decision_validation": None,
        "decision_validated": True,
        "decision_fallback": False,
        "preflight_rejections": [],
        "planned_units": 24,
        "executed_units": 24,
        "matrix_complete": True,
    }
    if over:
        m.update(over)
    return m


def _full_dv():
    s1 = sch.build_decision_session(10, 10, 0, 0)
    s2 = sch.build_decision_session(10, 10, 0, 0)
    return sch.build_decision_validation([s1, s2], partial=False)


def _formal_doc(modes=None, gates=None, verdict="PASS", with_dv=True,
               matrix_complete=False):
    m = _meta_ok()
    m["matrix_complete"] = matrix_complete
    if with_dv:
        m["decision_validation"] = _full_dv()
    doc = {"meta": m, "modes": modes if modes is not None else {
        "off": {"server_groups": []}, "on": {"server_groups": []}},
        "gates": gates if gates is not None else {g: {"status": "PASS"} for g in sch.REQUIRED_GATES},
        "verdict": verdict, "notes": []}
    return doc


class TestMetaValidator:
    def test_meta_ok(self):
        assert sch.validate_result_envelope(_formal_doc())[0]

    def test_top_keys_exact(self):
        doc = _formal_doc()
        doc["extra"] = 1
        ok, errs = sch.validate_result_envelope(doc)
        assert not ok
        assert any(e["code"] == "invariant_violation" for e in errs)

    def test_meta_keyset_exact_22(self):
        doc = _formal_doc()
        doc["meta"]["extra_key"] = 1
        ok, _ = sch.validate_result_envelope(doc)
        assert not ok
        doc2 = _formal_doc()
        del doc2["meta"]["matrix_complete"]
        ok2, _ = sch.validate_result_envelope(doc2)
        assert not ok2

    def test_verdict_enum(self):
        doc = _formal_doc(verdict="BOGUS")
        ok, errs = sch.validate_result_envelope(doc)
        assert not ok
        assert any(e["path"] == "verdict" for e in errs)

    def test_gates_status_enum(self):
        doc = _formal_doc(gates={"G-M0-1": {"status": "BOGUS"}})
        ok, errs = sch.validate_result_envelope(doc)
        assert not ok
        assert any("gates.G-M0-1" in e["path"] for e in errs)

    def test_source_phase_rule_ok(self):
        assert sch.validate_result_envelope(_formal_doc())[0]
        # 普通结果 source_phase==phase
        doc = _formal_doc()
        doc["meta"]["source_phase"] = "preflight"
        ok, errs = sch.validate_result_envelope(doc)
        assert not ok
        assert any("source_phase" in e["path"] for e in errs)

    def test_parity_ok_cross_invariant(self):
        # parity_ok=true → completed 必须严格全集
        doc = _formal_doc()
        doc["meta"]["parity_progress"]["completed"] = ["short-P"]
        ok, errs = sch.validate_result_envelope(doc)
        assert not ok
        assert any("parity_progress" in e["path"] for e in errs)
        # parity_ok=null → completed 必须真前缀（<6）
        doc = _formal_doc()
        doc["meta"]["parity_ok"] = None
        doc["meta"]["parity_progress"]["completed"] = list(sch.PARITY_BUCKETS)
        ok2, _ = sch.validate_result_envelope(doc)
        assert not ok2
        doc["meta"]["parity_progress"]["completed"] = ["short-P", "short-B"]
        ok3, _ = sch.validate_result_envelope(doc)
        assert ok3

    def test_parity_progress_order(self):
        doc = _formal_doc()
        doc["meta"]["parity_progress"]["completed"] = ["short-B", "short-P",
                                                       "medium-P", "medium-B",
                                                       "long-P", "long-B"]
        ok, errs = sch.validate_result_envelope(doc)
        assert not ok  # 顺序错位 → SCHEMA_INVALID

    def test_parity_mismatch_four_tuple(self):
        pp = {"completed": list(sch.PARITY_BUCKETS),
              "errors": [{"code": "parity_mismatch", "stage": "calibration",
                          "bucket": "short", "template": "P"}]}
        doc = _formal_doc()
        doc["meta"]["parity_ok"] = False
        doc["meta"]["parity_progress"] = pp
        ok, errs = sch.validate_result_envelope(doc)
        assert ok, errs
        # 缺 stage → 非法
        pp2 = {"completed": list(sch.PARITY_BUCKETS),
               "errors": [{"code": "parity_mismatch", "bucket": "short",
                           "template": "P"}]}
        doc["meta"]["parity_progress"] = pp2
        ok2, errs2 = sch.validate_result_envelope(doc)
        assert not ok2
        # short-P 与 short-B 同时 mismatch 不被错误聚合（独立条目合法）
        pp3 = {"completed": list(sch.PARITY_BUCKETS), "errors": [
            {"code": "parity_mismatch", "stage": "calibration",
             "bucket": "short", "template": "P"},
            {"code": "parity_mismatch", "stage": "calibration",
             "bucket": "short", "template": "B"}]}
        doc["meta"]["parity_progress"] = pp3
        ok3, _ = sch.validate_result_envelope(doc)
        assert ok3

    def test_parity_progress_errors_uniqueness(self):
        pp = {"completed": list(sch.PARITY_BUCKETS), "errors": [
            {"code": "parity_mismatch", "stage": "calibration",
             "bucket": "short", "template": "P"},
            {"code": "parity_mismatch", "stage": "calibration",
             "bucket": "short", "template": "P"}]}  # 重复 → 未聚合
        doc = _formal_doc()
        doc["meta"]["parity_progress"] = pp
        ok, errs = sch.validate_result_envelope(doc)
        assert not ok
        assert any("唯一性四元组" in str(e.get("expected_type")) for e in errs)

    def test_token_count_method(self):
        doc = _formal_doc()
        dv = _full_dv()
        doc["meta"]["decision_validation"] = dv
        doc["meta"]["token_count_method"] = None
        ok, errs = sch.validate_result_envelope(doc)
        assert ok, errs  # 允许 null
        doc["meta"]["token_count_method"] = "chars/2.5"
        ok2, _ = sch.validate_result_envelope(doc)
        assert not ok2

    def test_planned_units_derived(self):
        doc = _formal_doc()
        doc["meta"]["planned_units"] = 23
        ok, _ = sch.validate_result_envelope(doc)
        assert not ok


class TestDecisionValidation:
    def _session(self, requests=10, valid=10, invalid=0, error=0, length=0,
                 no_action=0, fr=None, hashes=None, err_summary=None):
        fr = fr if fr is not None else {"stop": valid + invalid}
        hashes = hashes if hashes is not None else ["h"] * (valid + invalid)
        return sch.build_decision_session(
            requests, valid, invalid, error, length, no_action, fr, hashes, err_summary)

    def test_session_invariants_ok(self):
        s = self._session(10, 8, 1, 1, length=1, fr={"stop": 8, "length": 1},
                          hashes=["h"] * 9,
                          err_summary=[{"code": "connection_error", "count": 1}])
        errs = sch.validate_decision_session(s)
        assert errs == []

    def test_session_requests_eq(self):
        s = self._session(10, 9, 0, 0)
        s["requests"] = 11
        errs = sch.validate_decision_session(s)
        assert any("requests==valid+invalid+error_count" in str(e) for e in errs)

    def test_session_two_sources(self):
        s = self._session(10, 8, 2, 0, length=1, no_action=1,
                          fr={"stop": 9, "length": 1})
        errs = sch.validate_decision_session(s)
        assert errs == []

    def test_invalid_length_matches_finish_reasons(self):
        s = self._session(10, 8, 2, 0, length=2, no_action=0,
                          fr={"stop": 8, "length": 1})
        errs = sch.validate_decision_session(s)
        assert any("invalid_length" in e[1] for e in errs)

    def test_finish_reasons_sum(self):
        s = self._session(10, 8, 1, 1, fr={"stop": 8, "length": 1, "extra": 1})
        errs = sch.validate_decision_session(s)
        assert any("finish_reasons" in e[1] for e in errs)

    def test_output_hashes_len(self):
        s = self._session(10, 8, 1, 1, hashes=["h"] * 8)  # 应为 9
        errs = sch.validate_decision_session(s)
        assert any("output_hashes" in e[1] for e in errs)

    def test_error_summary_sum_count(self):
        s = self._session(10, 8, 0, 2, err_summary=[
            {"code": "connection_error", "count": 1}])
        errs = sch.validate_decision_session(s)
        assert any("error_summary" in e[1] and "sum(count)==error_count" in str(e)
                   for e in errs)

    def test_error_summary_triple_uniqueness(self):
        s = self._session(10, 8, 0, 2, err_summary=[
            {"code": "connection_error", "count": 1},
            {"code": "connection_error", "count": 1}])
        errs = sch.validate_decision_session(s)
        assert any("唯一性三元组" in str(e) for e in errs)

    def test_aggregate_rate(self):
        s1 = self._session(10, 10, 0, 0)
        s2 = self._session(5, 4, 1, 0, length=1, fr={"stop": 4, "length": 1},
                           hashes=["h"] * 5)
        dv = sch.build_decision_validation([s1, s2], partial=False)
        assert dv["total_valid_rate"] == pytest.approx(14 / 15)

    def test_aggregate_rate_zero_requests_null(self):
        dv = sch.build_decision_validation([], partial=True)
        assert dv["total_valid_rate"] is None

    def test_formal_requires_complete(self):
        s1 = self._session(10, 10, 0, 0)
        dv = sch.build_decision_validation([s1], partial=True)
        doc = _formal_doc()
        doc["meta"]["decision_validation"] = dv
        ok, errs = sch.validate_result_envelope(doc)
        assert not ok
        assert any("partial" in e.get("path", "") for e in errs)

    def test_formal_requires_2x10(self):
        s1 = self._session(10, 10, 0, 0)
        s2 = self._session(5, 5, 0, 0)
        dv = sch.build_decision_validation([s1, s2], partial=False)
        doc = _formal_doc()
        doc["meta"]["decision_validation"] = dv
        ok, errs = sch.validate_result_envelope(doc)
        assert not ok
        assert any(">=10" in str(e.get("expected_type")) for e in errs)


class TestPreflightRejections:
    def _rej(self):
        return [{"unit_id": "off:q8_0-q8_0:f4:medium", "budget": 3320,
                 "threshold": sch.BUDGET_THRESHOLD, "reason": "budget_rejected"}]

    def test_rejections_ok(self):
        errs = sch.validate_preflight_rejections(self._rej(), "formal", True)
        assert errs == []

    def test_rejections_bad_reason(self):
        r = self._rej()
        r[0]["reason"] = "x"
        errs = sch.validate_preflight_rejections(r, "formal", True)
        assert any("budget_rejected" in str(e) for e in errs)

    def test_rejections_duplicate_unit(self):
        r = self._rej() * 2
        errs = sch.validate_preflight_rejections(r, "formal", True)
        assert any("unit_id 唯一" in str(e) for e in errs)

    def test_rejections_must_be_expected(self):
        r = [{"unit_id": "on:f16-f16:f8:long", "budget": 1,
              "threshold": sch.BUDGET_THRESHOLD, "reason": "budget_rejected"}]
        errs = sch.validate_preflight_rejections(r, "formal", True)
        assert any("EXPECTED_UNIT_IDS" in str(e) for e in errs)


class TestModesGroupRep:
    def _rep(self, unit_id="off:q8_0-q8_0:f2:short", rep_index=0, status="OK"):
        return sch.build_rep(unit_id, rep_index, 2, "short", "q8_0", "q8_0",
                             150, 150, "off:q8_0-q8_0:f2", status, False,
                             {"kv": {}, "branches": []})

    def _group(self, status="COMPLETED", reps=None):
        return sch.build_group(
            "off:q8_0-q8_0:f2", status, "2026-08-09T00:00:00Z",
            "2026-08-09T00:01:00Z", baseline=sch.build_baseline(
                "v", 1.0, 1.0, 100.0, 68.0, {}),
            replicates=reps or [])

    def _doc(self, groups_off, groups_on=None, matrix_complete=False):
        modes = {"off": {"server_groups": groups_off},
                 "on": {"server_groups": groups_on or []}}
        doc = _formal_doc(modes=modes, matrix_complete=matrix_complete)
        return doc

    def test_rep_index_unique_and_5_reps(self):
        reps = [self._rep(rep_index=i) for i in range(5)]
        g = self._group(reps=reps)
        doc = self._doc([g], matrix_complete=False)
        ok, errs = sch.validate_result_envelope(doc)
        assert ok, errs

    def test_rep_index_duplicate_illegal(self):
        reps = [self._rep(rep_index=0), self._rep(rep_index=0)]
        g = self._group(reps=reps)
        doc = self._doc([g])
        ok, errs = sch.validate_result_envelope(doc)
        assert not ok
        assert any("全局唯一" in str(e.get("expected_type")) for e in errs)

    def test_rep_index_out_of_range(self):
        reps = [self._rep(rep_index=5)]
        g = self._group(reps=reps)
        doc = self._doc([g])
        ok, errs = sch.validate_result_envelope(doc)
        assert not ok
        assert any("rep_index" in e.get("path", "") for e in errs)

    def test_warmup_count_must_be_2(self):
        g = self._group()
        g["warmup_count"] = 3
        doc = self._doc([g])
        ok, errs = sch.validate_result_envelope(doc)
        assert not ok
        assert any("warmup_count" in e.get("path", "") for e in errs)

    def test_error_group_requires_error_type(self):
        g = sch.build_group("off:q8_0-q8_0:f2", "ERROR", "s", "e")
        doc = self._doc([g])
        ok, errs = sch.validate_result_envelope(doc)
        assert not ok
        assert any("error_type" in e.get("path", "") for e in errs)

    def test_error_group_with_error_type_ok(self):
        g = sch.build_group("off:q8_0-q8_0:f2", "ERROR", "s", "e",
                            error_type="server_crash", error_stage="formal")
        doc = self._doc([g])
        ok, errs = sch.validate_result_envelope(doc)
        assert ok, errs

    def test_completed_group_forbids_error(self):
        g = self._group()
        g["error_type"] = "server_crash"
        doc = self._doc([g])
        ok, errs = sch.validate_result_envelope(doc)
        assert not ok

    def test_completed_group_requires_baseline(self):
        g = self._group()
        g["baseline"] = None
        doc = self._doc([g])
        ok, errs = sch.validate_result_envelope(doc)
        assert not ok

    def test_unit_group_reference_mismatch(self):
        rep = self._rep(unit_id="on:f16-f16:f4:short")
        g = self._group(reps=[rep])
        doc = self._doc([g])
        ok, errs = sch.validate_result_envelope(doc)
        assert not ok
        assert any("unit↔group" in str(e.get("expected_type")) for e in errs)

    def test_rep_identity_fields_match_unit_id(self):
        rep = self._rep(unit_id="off:q8_0-q8_0:f4:medium")
        rep["fanout"] = 2  # 与 unit_id f4 不符
        g = self._group(reps=[rep])
        doc = self._doc([g])
        ok, errs = sch.validate_result_envelope(doc)
        assert not ok

    def test_invalid_decision_fallback_bidirectional(self):
        rep = self._rep(status="INVALID_DECISION", rep_index=0)
        rep["decision_fallback"] = False
        g = self._group(reps=[rep])
        doc = self._doc([g])
        ok, errs = sch.validate_result_envelope(doc)
        assert not ok
        # OK 且 fallback=true → 非法
        rep2 = self._rep(rep_index=1)
        rep2["decision_fallback"] = True
        g2 = self._group(reps=[rep2])
        doc2 = self._doc([g2])
        ok2, _ = sch.validate_result_envelope(doc2)
        assert not ok2

    def test_error_rep_requires_error_type(self):
        rep = self._rep(rep_index=0, status="ERROR")
        g = self._group(reps=[rep])
        doc = self._doc([g])
        ok, errs = sch.validate_result_envelope(doc)
        assert not ok
        assert any("error_type" in e.get("path", "") for e in errs)

    def test_matrix_complete_requires_full_expected(self):
        # matrix_complete=true 但只有 1 个 unit → 非法
        reps = [self._rep(rep_index=i) for i in range(5)]
        g = self._group(reps=reps)
        doc = self._doc([g], matrix_complete=True)
        ok, errs = sch.validate_result_envelope(doc)
        assert not ok
        assert any("matrix_complete=true" in str(e.get("expected_type")) for e in errs)

    def test_full_matrix_complete_ok(self):
        # 构造全部 24 unit × 5 reps 的最简合法 doc（组按 profile/fanout 展开）
        groups_off, groups_on = [], []
        for control in ("off", "on"):
            groups = groups_off if control == "off" else groups_on
            for (ctk, ctv) in (("q8_0", "q8_0"), ("f16", "f16")):
                for fanout, buckets in sch.legal_matrix_plan().items():
                    gid = sch.group_id_of(control, ctk, ctv, fanout)
                    reps = []
                    for b in buckets:
                        uid = sch.unit_id_of(control, ctk, ctv, fanout, b)
                        for ri in range(5):
                            reps.append(sch.build_rep(
                                uid, ri, fanout, b, ctk, ctv, 150, 150, gid,
                                "OK", False, {"kv": {}, "branches": []}))
                    groups.append(sch.build_group(
                        gid, "COMPLETED", "s", "e",
                        baseline=sch.build_baseline("v", 1, 1, 100, 68, {}),
                        replicates=reps))
        doc = self._doc(groups_off, groups_on, matrix_complete=True)
        ok, errs = sch.validate_result_envelope(doc)
        assert ok, errs


class TestSCHEMAInvalid:
    def _envelope(self, **over):
        ctx = {"meta": {"workload": "fanout",
                        "design_ref": "M0_BRANCH_MEMORY_BASELINE_DESIGN.md",
                        "model_id": "mock", "binary_version": "v",
                        "ctx_size": 4096,
                        "cache_profiles": [{"ctk": "q8_0", "ctv": "q8_0"},
                                           {"ctk": "f16", "ctv": "f16"}],
                        "protocol": "p"}}
        doc = sch.build_preflight_envelope(
            ctx, reason="schema_invalid", source_phase="formal",
            parity_ok=None, parity_progress={"completed": [], "errors": []},
            token_count_method=None, decision_validation=None)
        for k, v in over.items():
            doc["meta"][k] = v
        return doc

    def test_schema_invalid_envelope_ok(self):
        doc = self._envelope()
        ok, errs = sch.validate_result_envelope(doc)
        assert ok, errs
        assert doc["meta"]["preflight_reason"] == "schema_invalid"
        assert doc["meta"]["parity_ok"] is None
        assert doc["meta"]["parity_progress"] == {"completed": [], "errors": []}
        assert doc["meta"]["token_count_method"] is None
        assert doc["meta"]["phase"] == "preflight"
        assert doc["verdict"] == "INVALID_RESULTS_SCHEMA_OR_INFRASTRUCTURE"

    def test_schema_invalid_fixed_no_recursion(self):
        doc = self._envelope()
        doc["meta"]["preflight_reason"] = "schema_invalid"
        # 不能再包装成 SCHEMA_INVALID（reason 保持 schema_invalid）
        assert doc["meta"]["preflight_reason"] == "schema_invalid"
        ok, _ = sch.validate_result_envelope(doc)
        assert ok

    def test_schema_invalid_parity_fixed_exception(self):
        # parity_ok 非 null → 违反 SCHEMA_INVALID 固定例外
        doc = self._envelope(parity_ok=True)
        ok, errs = sch.validate_result_envelope(doc)
        assert not ok
        assert any("SCHEMA_INVALID 例外" in str(e.get("expected_type")) for e in errs)

    def test_schema_invalid_token_count_fixed(self):
        doc = self._envelope(token_count_method="apply-template+tokenize")
        ok, errs = sch.validate_result_envelope(doc)
        assert not ok

    def test_schema_invalid_progress_empty(self):
        doc = self._envelope()
        doc["meta"]["parity_progress"] = {"completed": ["short-P"], "errors": []}
        ok, errs = sch.validate_result_envelope(doc)
        assert not ok

    def test_schema_invalid_source_phase_any(self):
        for src in ("preflight", "formal"):
            doc = self._envelope()
            doc["meta"]["source_phase"] = src
            ok, _ = sch.validate_result_envelope(doc)
            assert ok, src
        # 越界 source_phase
        doc = self._envelope(source_phase="x")
        ok, _ = sch.validate_result_envelope(doc)
        assert not ok

    def test_schema_invalid_phase_must_be_preflight(self):
        doc = self._envelope(phase="formal")
        ok, errs = sch.validate_result_envelope(doc)
        assert not ok

    def test_schema_invalid_fixed_matches_doc(self):
        # SCHEMA_INVALID_FIXED 与 builder 输出一致（v33 参数化）
        fixed = sch.SCHEMA_INVALID_FIXED
        assert fixed["parity_ok"] is None
        assert fixed["parity_progress"] == {"completed": [], "errors": []}
        assert fixed["token_count_method"] is None
        assert fixed["decision_validated"] is False
        assert fixed["executed_units"] == 0
        assert fixed["matrix_complete"] is False
        assert fixed["planned_units"] == 24


class TestSidecar:
    def test_sidecar_ok(self):
        sidecar = {"errors": [{"path": "$.meta", "code": "type_mismatch",
                               "expected_type": "object", "actual_type": "string"}]}
        ok, errs = sch.validate_schema_sidecar(sidecar)
        assert ok, errs

    def test_sidecar_code_enum(self):
        sidecar = {"errors": [{"path": "$", "code": "bad_code",
                               "expected_type": "x", "actual_type": "y"}]}
        ok, errs = sch.validate_schema_sidecar(sidecar)
        assert not ok
        assert any("enum_mismatch" in e["code"] for e in errs)

    def test_sidecar_path_json_pointer(self):
        sidecar = {"errors": [{"path": "meta", "code": "missing_key",
                               "expected_type": "x", "actual_type": "y"}]}
        ok, errs = sch.validate_schema_sidecar(sidecar)
        assert not ok

    def test_sidecar_type_whitelist(self):
        sidecar = {"errors": [{"path": "$", "code": "missing_key",
                               "expected_type": "dict", "actual_type": "list"}]}
        ok, errs = sch.validate_schema_sidecar(sidecar)
        assert not ok

    def test_sidecar_errors_missing(self):
        ok, _ = sch.validate_schema_sidecar({"x": 1})
        assert not ok


class TestAtomicWriter:
    def test_atomic_write_roundtrip(self, tmp_path):
        p = str(tmp_path / "out.json")
        sch.atomic_write_json(p, {"a": 1})
        with open(p, encoding="utf-8") as f:
            assert json.load(f) == {"a": 1}
        # 无 .tmp 残留
        assert [x for x in os.listdir(str(tmp_path)) if x.endswith(".tmp")] == []

    def test_write_pair_sidecar_first(self, tmp_path):
        main = str(tmp_path / "main.json")
        side = str(tmp_path / "side.json")
        sch.write_result_pair(main, {"m": 1}, side, {"s": 1})
        assert os.path.exists(main) and os.path.exists(side)

    def test_stale_tmp_cleanup(self, tmp_path):
        d = str(tmp_path)
        # v48（任务 9）：仅删超过 age 阈值的陈旧 tmp；活跃并发写（新 mtime）不删
        old = os.path.join(d, ".m0_fanout_x.main.tmp")
        fresh = os.path.join(d, ".m0_fanout_x.main.999.aaaa.tmp")
        with open(old, "w") as f:
            f.write("{}")
        with open(fresh, "w") as f:
            f.write("{}")
        past = time.time() - 7200
        os.utime(old, (past, past))
        open(os.path.join(d, "keep.json"), "w").close()
        removed = sch.cleanup_stale_tmp(d, max_age_seconds=3600.0)
        assert ".m0_fanout_x.main.tmp" in removed
        assert os.path.exists(fresh)   # 活跃（新）不删
        assert os.path.exists(os.path.join(d, "keep.json"))

    def test_io_failure_raises(self, tmp_path):
        with pytest.raises(OSError):
            sch.atomic_write_json(os.path.join(str(tmp_path), "no", "x.json"), {})


class TestAggregateVerdict:
    def test_preflight_returns_own(self):
        doc = _formal_doc()
        doc["meta"]["phase"] = "preflight"
        doc["verdict"] = "HOLD_NOT_VALIDATED"
        assert sch.aggregate_verdict(doc, False, False, False) == "HOLD_NOT_VALIDATED"

    def test_incomplete_wins(self):
        doc = _formal_doc()
        doc["meta"]["matrix_complete"] = False
        assert sch.aggregate_verdict(doc, True, True, True) == \
            "INVALID_RESULTS_SCHEMA_OR_INFRASTRUCTURE"

    def test_rep_error(self):
        doc = _formal_doc()
        assert sch.aggregate_verdict(doc, False, True, False) == \
            "INVALID_RESULTS_SCHEMA_OR_INFRASTRUCTURE"

    def test_g7_fail(self):
        doc = _formal_doc(gates={"G-M0-1": {"status": "FAIL"}})  # 缺 G-M0-7
        assert sch.aggregate_verdict(doc, False, False, False) == \
            "INVALID_RESULTS_SCHEMA_OR_INFRASTRUCTURE"

    def test_partial_rejection(self):
        doc = _formal_doc(matrix_complete=True)
        assert sch.aggregate_verdict(doc, False, False, True) == "HOLD_NOT_VALIDATED"

    def test_fallback(self):
        doc = _formal_doc(matrix_complete=True)
        assert sch.aggregate_verdict(doc, True, False, False) == "HOLD_NOT_VALIDATED"

    def test_g1_fail(self):
        doc = _formal_doc(gates={k: {"status": "PASS"} for k in sch.REQUIRED_GATES}, matrix_complete=True)
        doc["gates"]["G-M0-1"]["status"] = "FAIL"
        assert sch.aggregate_verdict(doc, False, False, False) == "HOLD_NOT_VALIDATED"

    def test_correctness_fail(self):
        doc = _formal_doc(gates={k: {"status": "PASS"} for k in sch.REQUIRED_GATES}, matrix_complete=True)
        doc["gates"]["G-M0-2"]["status"] = "FAIL"
        assert sch.aggregate_verdict(doc, False, False, False) == \
            "REJECT_CORRECTNESS_OR_ISOLATION"

    def test_stability_fail(self):
        doc = _formal_doc(gates={k: {"status": "PASS"} for k in sch.REQUIRED_GATES}, matrix_complete=True)
        doc["gates"]["G-M0-4"]["status"] = "FAIL"
        assert sch.aggregate_verdict(doc, False, False, False) == \
            "HOLD_UNSTABLE_MEASUREMENT"

    def test_all_pass(self):
        doc = _formal_doc(gates={k: {"status": "PASS"} for k in sch.REQUIRED_GATES}, matrix_complete=True)
        assert sch.aggregate_verdict(doc, False, False, False) == "PASS"

    def test_rule_of_consistency(self):
        doc = _formal_doc()
        doc["meta"]["matrix_complete"] = False
        assert sch.rule_of(doc, True, True, True) == "FORMAL_INCOMPLETE"
        doc2 = _formal_doc()
        doc2["meta"]["phase"] = "preflight"
        doc2["meta"]["preflight_reason"] = "parity_mismatch"
        assert sch.rule_of(doc2, False, False, False) == "PARITY_MISMATCH"


class TestBuilders:
    def test_build_preflight_envelope(self):
        ctx = {"meta": {"workload": "fanout", "model_id": "m"}}
        doc = sch.build_preflight_envelope(
            ctx, reason="parity_mismatch", source_phase="preflight",
            parity_ok=False,
            parity_progress={"completed": list(sch.PARITY_BUCKETS), "errors": []},
            token_count_method=sch.TOKEN_COUNT_METHOD, decision_validation=None)
        assert doc["meta"]["phase"] == "preflight"
        assert doc["meta"]["preflight_reason"] == "parity_mismatch"
        assert doc["verdict"] == "HOLD_NOT_VALIDATED"
        assert doc["modes"] == {} and doc["gates"] == {}
        assert doc["meta"]["planned_units"] == 24
        assert doc["meta"]["executed_units"] == 0
        assert doc["meta"]["matrix_complete"] is False

    def test_builder_not_hardcode_parity(self):
        # builder 不按 reason 硬编码 parity（v37）
        ctx = {"meta": {"workload": "fanout", "model_id": "m"}}
        doc = sch.build_preflight_envelope(
            ctx, reason="validation_incomplete", source_phase="preflight",
            parity_ok=True,
            parity_progress={"completed": list(sch.PARITY_BUCKETS), "errors": []},
            token_count_method=sch.TOKEN_COUNT_METHOD, decision_validation=None)
        assert doc["meta"]["parity_ok"] is True
        assert doc["meta"]["token_count_method"] == sch.TOKEN_COUNT_METHOD
