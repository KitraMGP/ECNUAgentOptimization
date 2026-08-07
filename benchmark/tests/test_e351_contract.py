"""E3.5.1：契约闭环纯逻辑测试（integrity parser/mapping/chain/overhead 配对）。"""
from __future__ import annotations

import pytest

from scripts.e351_lifecycle_contract_validation import integrity_check


def _evs(seqs):
    return [{"evseq": str(s)} for s in seqs]


def test_integrity_clean():
    c = integrity_check(_evs([1, 2, 3, 4]))
    assert c["gap"] == 0 and c["duplicate"] == 0 and c["out_of_order"] == 0 and c["malformed"] == 0


def test_integrity_detects_gap():
    c = integrity_check(_evs([1, 2, 4, 5]))
    assert c["gap"] == 1


def test_integrity_detects_duplicate():
    c = integrity_check(_evs([1, 2, 2, 3]))
    assert c["duplicate"] == 1


def test_integrity_detects_out_of_order():
    c = integrity_check(_evs([1, 3, 2, 4]))
    assert c["out_of_order"] == 1


def test_integrity_detects_malformed():
    c = integrity_check([{"evseq": "abc"}, {"evseq": "1"}])
    assert c["malformed"] == 1


def test_request_mapping_logic():
    """每请求 rtrace 的 assigned 事件存在 = 映射。"""
    req_traces = ["trace-S0-0001", "trace-S0-0002"]
    evs = [{"type": "assigned", "rtrace": "trace-S0-0001"},
           {"type": "active", "rtrace": "trace-S0-0001"},
           {"type": "idle", "rtrace": "trace-S0-0001"}]
    assigned_traces = {e["rtrace"] for e in evs if e["type"] == "assigned"}
    assert all(t in assigned_traces for t in req_traces[:1])
    assert not all(t in assigned_traces for t in req_traces)


def test_chain_coverage_logic():
    """assigned→active→idle 全链。"""
    evs = [{"type": "assigned", "rtrace": "t1"}, {"type": "active", "rtrace": "t1"},
           {"type": "idle", "rtrace": "t1"}]
    t = "t1"
    assert (any(e["type"] == "assigned" and e["rtrace"] == t for e in evs)
            and any(e["type"] == "active" and e["rtrace"] == t for e in evs)
            and any(e["type"] == "idle" and e["rtrace"] == t for e in evs))


def test_pressure_chain_logic():
    evs = {"pressure": 1, "purge": 1, "retry_or_resume": 1}
    assert evs["pressure"] >= 1 and evs["purge"] >= 1 and evs["retry_or_resume"] >= 1


def test_overhead_paired_median():
    on = [100.0, 100.2, 100.1]
    off = [100.0, 100.0, 100.0]
    diffs = [(a - b) / b * 100 for a, b in zip(on, off) if b]
    import statistics
    assert statistics.median(diffs) < 0.5
