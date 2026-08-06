"""Config 系统单元测试。"""
from __future__ import annotations

import json

import pytest

from framework.config import BenchmarkConfig


def test_defaults():
    cfg = BenchmarkConfig()
    assert cfg.host == "127.0.0.1"
    assert cfg.port == 8080
    assert cfg.scenario == "all"
    assert cfg.repeat == 1
    assert cfg.warmup == 0
    assert cfg.temperature == 0.0
    assert cfg.base_url == "http://127.0.0.1:8080/v1"


def test_server_url_override():
    cfg = BenchmarkConfig(server_url="http://10.0.0.1:9000/v1")
    assert cfg.base_url == "http://10.0.0.1:9000/v1"


def test_from_dict_known_and_extra():
    cfg = BenchmarkConfig.from_dict(
        {"host": "1.2.3.4", "scenario": "multi_turn", "future_key": 123})
    assert cfg.host == "1.2.3.4"
    assert cfg.scenario == "multi_turn"
    assert cfg.extra == {"future_key": 123}


def test_invalid_scenario():
    with pytest.raises(ValueError):
        BenchmarkConfig(scenario="unknown_scene")


def test_invalid_repeat():
    with pytest.raises(ValueError):
        BenchmarkConfig(repeat=0)


def test_invalid_ctx():
    with pytest.raises(ValueError):
        BenchmarkConfig(ctx_size=0)


def test_load_json(tmp_path):
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({"scenario": "long_life", "long_rounds": 12}), encoding="utf-8")
    cfg = BenchmarkConfig.load(str(p))
    assert cfg.scenario == "long_life"
    assert cfg.long_rounds == 12


def test_load_yaml(tmp_path):
    yaml = pytest.importorskip("yaml")
    p = tmp_path / "cfg.yaml"
    p.write_text("scenario: branch\nbranch_rounds: 3\n", encoding="utf-8")
    cfg = BenchmarkConfig.load(str(p))
    assert cfg.scenario == "branch"
    assert cfg.branch_rounds == 3


def test_load_unsupported_ext(tmp_path):
    p = tmp_path / "cfg.toml"
    p.write_text("a=1", encoding="utf-8")
    with pytest.raises(ValueError):
        BenchmarkConfig.load(str(p))


def test_merge_cli_overrides():
    cfg = BenchmarkConfig(scenario="all", rounds=20)
    merged = cfg.merge_cli({"scenario": "multi_turn", "rounds": 5, "port": None})
    assert merged.scenario == "multi_turn"
    assert merged.rounds == 5
    assert merged.port == 8080  # None 不覆盖


def test_legacy_keys():
    cfg = BenchmarkConfig()
    legacy = cfg.legacy_keys()
    assert set(legacy) == {
        "host", "port", "scenario", "rounds", "tool_steps",
        "branch_rounds", "long_rounds", "long_secret", "ctx_size",
    }


def test_to_dict_has_base_url():
    cfg = BenchmarkConfig()
    d = cfg.to_dict()
    # server_url 保留原始值（空 = 由 host:port 推导）；base_url 为派生实际值
    assert d["server_url"] == ""
    assert d["base_url"] == "http://127.0.0.1:8080/v1"
    assert "extra" not in d
