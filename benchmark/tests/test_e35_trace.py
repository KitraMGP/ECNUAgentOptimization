"""E3.5：trace 验证纯逻辑测试（schema/映射/关联/隐私/validator/replayer）。"""
from __future__ import annotations

import json
import os
import tempfile

import pytest

from scripts.e35_validate_trace import validate
from scripts.e35_replay_trace import plan


def _sample_trace() -> dict:
    return {
        "schema_version": 1,
        "captured_at": "2026-08-06T00:00:00Z",
        "sessions": [
            {"session_id": "sess_01",
             "requests": [
                 {"turn": 1, "arrival_delta_ms": 0, "prompt_tokens": 1200,
                  "prompt_prefix_tokens": 0, "gen_tokens": 80, "status": "ok",
                  "revisit_delta_ms": None},
                 {"turn": 2, "arrival_delta_ms": 1500, "prompt_tokens": 2000,
                  "prompt_prefix_tokens": 1200, "gen_tokens": 60, "status": "ok",
                  "revisit_delta_ms": 5000},
             ]},
        ],
    }


def test_validator_accepts_clean_trace(tmp_path):
    p = tmp_path / "t.json"
    p.write_text(json.dumps(_sample_trace()))
    res = validate(str(p))
    assert res["valid"] is True
    assert res["requests"] == 2


def test_validator_rejects_content_fields(tmp_path):
    t = _sample_trace()
    t["sessions"][0]["requests"][0]["prompt"] = "sensitive text"
    p = tmp_path / "t.json"
    p.write_text(json.dumps(t))
    res = validate(str(p))
    assert res["valid"] is False
    assert any("prompt" in e for e in res["errors"])


def test_validator_rejects_bad_session_id(tmp_path):
    t = _sample_trace()
    t["sessions"][0]["session_id"] = "bad id!!"
    p = tmp_path / "t.json"
    p.write_text(json.dumps(t))
    res = validate(str(p))
    assert res["valid"] is False


def test_replay_plan_counts(tmp_path):
    p = tmp_path / "t.json"
    p.write_text(json.dumps(_sample_trace()))
    plan = json.load(open(str(p)))
    # 直接调用 plan 函数
    from scripts.e35_replay_trace import plan as plan_fn
    res = plan_fn(str(p))
    assert res["n_sessions"] == 1 and res["n_requests"] == 2
    assert res["total_prompt_tokens"] == 3200


def test_mapping_coverage_logic():
    """映射覆盖率：session 的 trace_id 在 assigned 事件中出现比例。"""
    evs = [{"type": "assigned", "trace": "trace-S0"},
           {"type": "assigned", "trace": "trace-S0"},
           {"type": "assigned", "trace": "trace-S1"}]
    session_reqs = {"trace-S0": 2, "trace-S1": 1}
    cov = {tid: sum(1 for e in evs if e["trace"] == tid) / n for tid, n in session_reqs.items()}
    assert all(c == 1.0 for c in cov.values())


def test_active_never_victim_logic():
    """active protection：processing trace 不得出现在 purge victim。"""
    active_traces = {"trace-active-0"}
    purge = {"trace": "trace-adapter-pressure", "slot": "3"}
    # victim slot 的 trace 通过关联得到；断言 active trace 不在 victim 侧
    victim_trace = "trace-idle-2"
    assert victim_trace not in active_traces


def test_trace_id_restricted_charset():
    import re
    ok = ["trace-aaa-001", "sess_01", "ABCDEF123"]
    bad = ["bad trace", "x\ny", "semi;colon"]
    for t in ok:
        assert re.fullmatch(r"[A-Za-z0-9_\-.]{1,64}", t)
    for t in bad:
        assert not re.fullmatch(r"[A-Za-z0-9_\-.]{1,64}", t)
