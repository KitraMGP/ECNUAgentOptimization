#!/usr/bin/env python3
"""E2.4：Long-Branch workload probe（多分支回访 + RAM cache 边界验证）。

场景：
- branch_count=2：A+X → A+Y → A+X → A+Y（两轮 X/Y 交替回访）
- branch_count=4：A+X1 → A+Y1 → A+X2 → A+Y2 → A+X1 → A+Y1（交替回访，slot 数不足）
- long_prefix：长共享前缀 + 短分支后缀（观察 RAM restore vs KV direct 延迟差）

每请求记录：branch_id、selected_slot、routing_reason、cached_tokens、
prompt_tokens、latency、response normalized hash。
evaluator：同 branch_id 响应 hash 一致（无串扰）、跨 branch 不同（有区分度）。

用法：uv run python scripts/e2_long_branch_probe.py --port 8080 --policy default|prefix-branch
      --branches 2|4|long --reps 3 --output results/e24/xxx.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
from typing import Any, Dict, List, Optional
from urllib import request

A_SHORT = "The quick brown fox jumps over the lazy dog near the river bank."
A_LONG = (
    "Once upon a time in a distant galaxy far away, a small robot named Zippy "
    "discovered a mysterious glowing artifact buried in the sand of a red desert "
    "planet under twin suns. This artifact hummed with strange energy and pulsed "
    "with colors no human had ever seen. Zippy carried it to the ancient city of "
    "Corinthia where wise sages gathered around crystal pools to decipher its "
    "secrets. The artifact revealed a map to the lost temple of the sun kings, "
    "hidden behind waterfalls of liquid silver guarded by mechanical eagles with "
    "emerald eyes and golden talons that sang ancient songs of creation and "
    "destruction in equal measure across the endless desert nights."
)
# branch_count=4 的共享前缀
A_4 = "The brave knight travels across the mountains and valleys seeking the legendary treasure."
X1 = " The first path leads through the dark forest where wolves howl at the moon."
Y1 = " The second path crosses the frozen river where ice cracks beneath feet."
X2 = " The third path climbs the jagged cliffs where eagles nest on ledges."
Y2 = " The fourth path winds through the desert where sand storms rise at dusk."


def _norm(text: str) -> str:
    t = re.sub(r"[^\w\u4e00-\u9fff]+", "", text.lower(), flags=re.UNICODE)
    return hashlib.sha256(t.encode("utf-8")).hexdigest()[:16]


def post(url: str, body: dict) -> Dict[str, Any]:
    req = request.Request(url, data=json.dumps(body).encode("utf-8"),
                          headers={"Content-Type": "application/json"}, method="POST")
    with request.urlopen(req, timeout=180) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get(url: str) -> Dict[str, Any]:
    with request.urlopen(url, timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


class Probe:
    def __init__(self, base: str) -> None:
        self.base = base
        self.log: List[Dict[str, Any]] = []

    def clean(self) -> bool:
        slots = get(f"{self.base}/slots")
        for s in slots:
            post(f"{self.base}/slots/{s['id']}?action=erase", {})
        time.sleep(0.3)
        kv = get(f"{self.base}/metrics/kv")
        return kv.get("used_cells") == 0 and kv.get("active_sequences") == 0

    def completion(self, branch_id: str, prompt: str) -> Dict[str, Any]:
        before = len(get(f"{self.base}/routing/events?limit=10000")["events"])
        t0 = time.perf_counter()
        body = {
            "model": "bench",
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 16, "temperature": 0,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        r = post(f"{self.base}/v1/chat/completions", body)
        latency_ms = (time.perf_counter() - t0) * 1000
        usage = r.get("usage", {})
        kv = get(f"{self.base}/metrics/kv")
        content = ((r.get("choices") or [{}])[0].get("message", {}) or {}).get("content") or ""
        rec = {
            "branch_id": branch_id,
            "latency_ms": round(latency_ms, 1),
            "prompt_tokens": usage.get("prompt_tokens"),
            "cached_tokens": usage.get("prompt_tokens_details", {}).get("cached_tokens"),
            "response_hash": _norm(content),
            "used_cells": kv.get("used_cells"),
            "active_sequences": kv.get("active_sequences"),
        }
        all_ev = get(f"{self.base}/routing/events?limit=10000")["events"]
        new_ev = all_ev[before:]
        if new_ev:
            e = new_ev[-1]
            rec.update({
                "selected_slot": e.get("selected_slot"),
                "routing_reason": e.get("routing_reason"),
                "selected_prefix_tokens": e.get("selected_prefix_tokens"),
                "preserved_branch": e.get("preserved_branch"),
                "fallback_to_default": e.get("fallback_to_default"),
            })
        self.log.append(rec)
        return rec


def build_sequence(branches: str) -> List[tuple]:
    """返回 (branch_id, prompt) 序列。"""
    if branches == "2":
        return [("X", A_SHORT + " It then runs into the dark forest chasing a small rabbit."),
                ("Y", A_SHORT + " It stops to drink fresh water from the clear mountain stream."),
                ("X", A_SHORT + " It then runs into the dark forest chasing a small rabbit."),
                ("Y", A_SHORT + " It stops to drink fresh water from the clear mountain stream.")]
    if branches == "4":
        return [("X1", A_4 + X1), ("Y1", A_4 + Y1), ("X2", A_4 + X2), ("Y2", A_4 + Y2),
                ("X1", A_4 + X1), ("Y1", A_4 + Y1)]
    if branches == "long":
        return [("X", A_LONG + " It then runs into the dark forest chasing a small rabbit."),
                ("Y", A_LONG + " It stops to drink fresh water from the clear mountain stream."),
                ("X", A_LONG + " It then runs into the dark forest chasing a small rabbit."),
                ("Y", A_LONG + " It stops to drink fresh water from the clear mountain stream.")]
    raise ValueError(branches)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--policy", choices=["default", "prefix-branch"], required=True)
    ap.add_argument("--branches", choices=["2", "4", "long"], required=True)
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--output", required=True)
    args = ap.parse_args()
    base = f"http://{args.host}:{args.port}"
    pr = Probe(base)
    seq = build_sequence(args.branches)
    results: Dict[str, Any] = {"policy": args.policy, "branches": args.branches,
                               "seq": [(b, len(p)) for b, p in seq], "replicates": []}

    for rep in range(args.reps):
        assert pr.clean(), f"rep {rep}: KV clean assertion failed"
        recs = []
        for bid, prompt in seq:
            recs.append(pr.completion(bid, prompt))
        # evaluator：同 branch_id 响应 hash 一致、跨 branch 不同
        by_branch: Dict[str, List[str]] = {}
        for r in recs:
            by_branch.setdefault(r["branch_id"], []).append(r["response_hash"])
        eval_ok = True
        for bid, hashes in by_branch.items():
            if len(set(hashes)) != 1:
                eval_ok = False  # 同分支响应不一致（串扰）
        # 跨分支区分度
        distinct = len(set(h for hs in by_branch.values() for h in hs)) > 1
        results["replicates"].append({
            "rep": rep, "clean_verified": True, "requests": recs,
            "evaluator": {"branch_consistent": eval_ok, "branch_distinct": distinct},
        })

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"结果已保存: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
