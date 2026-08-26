from __future__ import annotations

from scripts.branch_cow_memory_eval import aggregate


def _row(arm: str, used: int, shared: int, capacity: int = 100, recurrent: int = 200,
         allocated: int = 80, blocks: int = 8):
    return {
        "arm": arm,
        "pass": True,
        "metrics": {
            "used_cells": used,
            "shared_cells": shared,
            "capacity_bytes": capacity,
            "allocator": {
                "allocated_bytes": allocated,
                "allocated_blocks": blocks,
            },
            "recurrent": {"capacity_bytes": recurrent},
        },
        "rss_mb": 10.0,
        "gpu_mb": 20.0,
    }


def test_aggregate_reports_logical_savings_and_fixed_capacity():
    result = aggregate({
        "control": [_row("control", 100, 0)],
        "fork": [_row("fork", 40, 60)],
    })

    assert result["comparison"]["used_cells_reduction_ratio"] == 0.6
    assert result["comparison"]["allocated_bytes_reduction_ratio"] == 0.0
    assert result["comparison"]["capacity_bytes_equal"] is True
    assert result["comparison"]["recurrent_capacity_bytes_equal"] is True
    assert result["fork"]["shared_cells_mean"] == 60


def test_aggregate_reports_physical_paged_savings():
    result = aggregate({
        "control": [_row("control", 100, 0, allocated=80, blocks=8)],
        "fork": [_row("fork", 40, 60, allocated=32, blocks=3)],
    })

    assert result["comparison"]["allocated_bytes_reduction_ratio"] == 0.6
    assert result["control"]["allocated_blocks_mean"] == 8
    assert result["fork"]["allocated_blocks_mean"] == 3


def test_aggregate_ignores_failed_runs():
    failed = _row("fork", 999, 0)
    failed["pass"] = False
    result = aggregate({"fork": [failed, _row("fork", 40, 60)]})

    assert result["fork"]["runs"] == 2
    assert result["fork"]["passed_runs"] == 1
    assert result["fork"]["used_cells_mean"] == 40
