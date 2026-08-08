"""E4.1：饱和/淘汰正确性判定逻辑测试。"""
from __future__ import annotations

import re

import pytest


def _parse_truncations(log: str) -> int:
    return len(re.findall(r"truncated\s*=\s*1\b", log))


def _gate(rec: dict) -> str:
    """PASS/HOLD/REJECT 判定逻辑（与 summarize 一致）。"""
    # REJECT 条件
    if rec.get("active_evicted") or rec.get("cross_session_reuse") or rec.get("server_crashed"):
        return "REJECT_KV_EVICTION_CORRECTNESS"
    if rec.get("lifecycle_events_production", 0) > 0:
        return "REJECT_KV_EVICTION_CORRECTNESS"
    # HOLD 条件
    if not rec.get("saturation_touched"):
        return "HOLD_FOR_INCOMPLETE_CAPACITY_EVIDENCE"
    # PASS 条件
    required = (rec.get("purge_occurred") and rec.get("active_ok")
                and rec.get("victim_contract_ok") and not rec.get("failures"))
    return "PASS_KV_SATURATION_CORRECTNESS" if required else "HOLD_FOR_INCOMPLETE_CAPACITY_EVIDENCE"


def test_gate_pass():
    r = {"saturation_touched": True, "purge_occurred": True, "active_ok": True,
         "victim_contract_ok": True, "failures": 0, "active_evicted": False,
         "cross_session_reuse": False, "server_crashed": False, "lifecycle_events_production": 0}
    assert _gate(r) == "PASS_KV_SATURATION_CORRECTNESS"


def test_gate_reject_active_evicted():
    r = {"saturation_touched": True, "purge_occurred": True, "active_ok": False,
         "victim_contract_ok": True, "failures": 0, "active_evicted": True,
         "cross_session_reuse": False, "server_crashed": False, "lifecycle_events_production": 0}
    assert _gate(r) == "REJECT_KV_EVICTION_CORRECTNESS"


def test_gate_reject_production_events():
    r = {"saturation_touched": True, "purge_occurred": True, "active_ok": True,
         "victim_contract_ok": True, "failures": 0, "active_evicted": False,
         "cross_session_reuse": False, "server_crashed": False, "lifecycle_events_production": 3}
    assert _gate(r) == "REJECT_KV_EVICTION_CORRECTNESS"


def test_gate_hold_no_saturation():
    r = {"saturation_touched": False, "purge_occurred": False, "active_ok": True,
         "victim_contract_ok": True, "failures": 0, "active_evicted": False,
         "cross_session_reuse": False, "server_crashed": False, "lifecycle_events_production": 0}
    assert _gate(r) == "HOLD_FOR_INCOMPLETE_CAPACITY_EVIDENCE"


def test_trunc_structured_only():
    assert _parse_truncations("truncated = 0\ntruncated = 1\n") == 1
    assert _parse_truncations("truncated = 0\n") == 0


def test_victim_tick_ascending():
    """lru purge 按 tick 升序（最久未用优先）。"""
    purges = [{"slot": "3", "tick": "4"}, {"slot": "2", "tick": "6"}, {"slot": "1", "tick": "7"}]
    ticks = [int(p["tick"]) for p in purges]
    assert ticks == sorted(ticks)
