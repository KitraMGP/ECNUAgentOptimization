#!/usr/bin/env python3
"""E2.0.5：候选 A（slot/sequence 淘汰与复用）机制可行性受控验证。

通过受控 HTTP 请求序列回答源码审查无法直接回答的问题：
1. parallel=1 是否存在可供淘汰的其他 idle sequence（单 slot 内多 seq？）
2. non-unified parallel=2 时清理 slot A 能否为 slot B 提供 cell
3. unified parallel=2 时清理 idle slot 能否缓解 active slot 压力
4. slot 选择是否按 prompt 相似度/前缀命中排序
5. A+X/A+Y/A+X 分支回访能否从其他 idle slot 命中
6. 仅 seq_rm 能否提高未来 cache hit（还是只释放 cell）
7. 回收 cell 能否改变 per-slot context 上限和 truncation
8. try_clear_idle_slots 触发条件（KV 满时）

用法：uv run python scripts/e2_candidate_a_probe.py --host 127.0.0.1 --port 8081
      [--unified] [--output results/e205/ca_probe.json]
"""
from __future__ import annotations

import argparse
import json
import time
from typing import Any, Dict, List, Optional
from urllib import request

PROMPT_A = "The quick brown fox jumps over the lazy dog near the river bank."
PROMPT_X = " It then runs into the forest chasing a rabbit."
PROMPT_Y = " It stops to drink water from a clear stream instead."
PROMPT_B = " The sun sets slowly behind the distant mountains."


def post(url: str, body: dict) -> Dict[str, Any]:
    req = request.Request(url, data=json.dumps(body).encode("utf-8"),
                          headers={"Content-Type": "application/json"}, method="POST")
    with request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get(url: str) -> Dict[str, Any]:
    with request.urlopen(url, timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


class Probe:
    def __init__(self, base: str, unified: bool) -> None:
        self.base = base
        self.unified = unified
        self.log: List[Dict[str, Any]] = []

    def kv(self) -> Dict[str, Any]:
        return get(f"{self.base}/metrics/kv")

    def completion(self, label: str, prompt: str, id_slot: Optional[int] = None,
                   n_predict: int = 4) -> Dict[str, Any]:
        body: Dict[str, Any] = {"prompt": prompt, "n_predict": n_predict,
                                "temperature": 0, "cache_prompt": True}
        if id_slot is not None:
            body["id_slot"] = id_slot
        r = post(f"{self.base}/completion", body)
        t = r.get("timings", {})
        kv = self.kv()
        rec = {
            "label": label, "id_slot": id_slot,
            "prompt_n": t.get("prompt_n"), "cache_n": t.get("cache_n"),
            "predicted_n": t.get("predicted_n"),
            "used_cells": kv.get("used_cells"),
            "active_sequences": kv.get("active_sequences"),
            "shared_cells": kv.get("shared_cells"),
            "truncated": r.get("truncated"),
        }
        self.log.append(rec)
        print(f"  [{label:24s}] slot={id_slot} prompt_n={rec['prompt_n']} "
              f"cache_n={rec['cache_n']} used={rec['used_cells']} "
              f"active={rec['active_sequences']} shared={rec['shared_cells']}")
        return rec

    def erase(self, slot_id: int) -> None:
        try:
            with request.urlopen(
                    f"{self.base}/slots/{slot_id}?action=erase", method="POST",
                    timeout=5) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                print(f"  [erase slot {slot_id}] -> {body}")
        except Exception as e:
            print(f"  [erase slot {slot_id}] FAILED: {e}")

    def slots(self) -> List[Dict[str, Any]]:
        return get(f"{self.base}/slots")


def run_case1_single_slot(pr: Probe) -> None:
    """1. 单 slot：A -> A+B（前缀命中与追加）"""
    print("\n[Case 1] 单 slot A -> A+B")
    pr.completion("A (cold)", PROMPT_A)
    pr.completion("A+B", PROMPT_A + PROMPT_B)


def run_case2_branch(pr: Probe) -> None:
    """2. 单 slot 分支：A+X -> A+Y -> A+X（回访命中与分支污染）"""
    print("\n[Case 2] 单 slot 分支 A+X -> A+Y -> A+X")
    pr.completion("A+X (cold)", PROMPT_A + PROMPT_X)
    pr.completion("A+Y", PROMPT_A + PROMPT_Y)
    pr.completion("A+X again", PROMPT_A + PROMPT_X)


def run_case3_explicit_slots(pr: Probe) -> None:
    """3. 双 slot 显式 id_slot：slot0=A+X, slot1=A+Y，回访观察"""
    print("\n[Case 3] 双 slot 显式 id_slot")
    pr.completion("slot0 A+X", PROMPT_A + PROMPT_X, id_slot=0)
    pr.completion("slot1 A+Y", PROMPT_A + PROMPT_Y, id_slot=1)
    pr.completion("slot0 A+X again", PROMPT_A + PROMPT_X, id_slot=0)
    pr.completion("slot1 A+Y again", PROMPT_A + PROMPT_Y, id_slot=1)


def run_case4_auto_routing(pr: Probe) -> None:
    """4. 双 slot 自动路由：A+X -> A+Y -> A+X（检查 slot 选择是否按前缀）"""
    print("\n[Case 4] 双 slot 自动路由")
    pr.completion("A+X (auto)", PROMPT_A + PROMPT_X)
    pr.completion("A+Y (auto)", PROMPT_A + PROMPT_Y)
    pr.completion("A+X again (auto)", PROMPT_A + PROMPT_X)


def run_case5_idle_pressure(pr: Probe) -> None:
    """5. 填满 idle slot 后对 active slot 施压（unified/non-unified 差异）"""
    print("\n[Case 5] 填满 idle slot 后对 active slot 施压")
    pr.completion("slot0 long", PROMPT_A * 60, id_slot=0)   # 填 slot0
    pr.completion("slot1 long", PROMPT_A * 60, id_slot=1)   # 填 slot1
    # 对 slot0 继续加压：超过 per-slot ctx 上限
    big = PROMPT_A * 200
    try:
        r = pr.completion("slot0 over-pressure", big, id_slot=0)
        print(f"    -> over-pressure result truncated={r.get('truncated')}")
    except Exception as e:
        print(f"    -> over-pressure FAILED: {type(e).__name__}: {e}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8081)
    ap.add_argument("--unified", action="store_true")
    ap.add_argument("--output", default=None)
    args = ap.parse_args()
    base = f"http://{args.host}:{args.port}"
    mode = "unified" if args.unified else "non-unified"
    pr = Probe(base, args.unified)

    print(f"== E2.0.5 候选 A 机制验证（{mode}）==")
    kv = pr.kv()
    print(f"capacity_cells={kv['capacity_cells']} used={kv['used_cells']}")

    run_case1_single_slot(pr)
    run_case2_branch(pr)
    if not args.unified:
        # non-unified 下双 slot 场景才有效（unified 有共享语义，单独跑）
        run_case3_explicit_slots(pr)
        run_case4_auto_routing(pr)
    run_case5_idle_pressure(pr)

    out = {"mode": mode, "log": pr.log}
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        print(f"\n结果已保存: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
