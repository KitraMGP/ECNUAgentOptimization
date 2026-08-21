"""Deterministic long-context needle recall workload.

The workload deliberately asks the running server to render and tokenize the
whole chat prompt.  It therefore records actual token counts rather than using
character-count approximations when a model's template/tokenizer changes.
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

from framework.config import BenchmarkConfig
from framework.driver import Driver
from framework.workload import Workload, WorkloadSpec, register


SYSTEM = "You are a precise retrieval agent. Answer only with the requested record value."
NEEDLE = "NEEDLE_RECORD=QWEN-HYBRID-7291"
FILLER = " Archived operational context: routine status was verified."
POSITIONS = ("early", "middle", "late")


def _messages(content: str) -> List[dict]:
    return [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": content + "\nWhat is the exact NEEDLE_RECORD value?"},
    ]


def _rendered_token_count(driver: Driver, content: str) -> int:
    prompt = driver.apply_template(_messages(content))
    return driver.count_tokens(prompt)


def build_exact_token_context(driver: Driver, target_tokens: int, position: str) -> Tuple[str, int]:
    """Build a deterministic context at or below ``target_tokens``.

    BPE merges make character padding invalid.  We binary-search repeated
    records against ``/apply-template`` + ``/tokenize`` and return the exact
    count that the server will receive.  If the target falls between reachable
    token counts, the largest safe value is recorded rather than overflowing
    context silently.
    """
    if position not in POSITIONS:
        raise ValueError(f"invalid needle position: {position!r}")
    if target_tokens <= 0:
        raise ValueError("target_tokens must be positive")

    def content(records: int) -> str:
        before = records if position == "late" else (0 if position == "early" else records // 2)
        after = records - before
        return FILLER * before + "\n" + NEEDLE + "\n" + FILLER * after

    baseline = _rendered_token_count(driver, content(0))
    if baseline > target_tokens:
        raise ValueError(f"target_tokens={target_tokens} is smaller than mandatory prompt ({baseline})")

    low, high = 0, 1
    while _rendered_token_count(driver, content(high)) <= target_tokens:
        low, high = high, high * 2
    while low + 1 < high:
        mid = (low + high) // 2
        if _rendered_token_count(driver, content(mid)) <= target_tokens:
            low = mid
        else:
            high = mid
    final = content(low)
    return final, _rendered_token_count(driver, final)


@register
class NeedleWorkload(Workload):
    name = "needle"
    version = "1.0"
    description = "精确 token 长上下文：early/middle/late needle recall。"

    def params_from_config(self, config: BenchmarkConfig) -> Dict[str, Any]:
        return {"target_tokens": config.needle_target_tokens}

    def generate(self, params: Dict[str, Any]) -> WorkloadSpec:
        target = int(params["target_tokens"])
        return WorkloadSpec(
            name=self.name,
            params={"target_tokens": target},
            prompts=list(POSITIONS),
            expected={"needle": "QWEN-HYBRID-7291"},
            meta={"version": self.version, "description": self.description,
                  "token_count_method": "apply-template+tokenize"},
        )

    def run(self, driver: Driver, spec: WorkloadSpec) -> Dict[str, Any]:
        rows = []
        target = int(spec.params["target_tokens"])
        for position in POSITIONS:
            content, actual_tokens = build_exact_token_context(driver, target, position)
            result = driver.chat(_messages(content), temperature=0, seed=42, max_tokens=16)
            rows.append({"position": position, "target_tokens": target,
                         "actual_prompt_tokens": actual_tokens, **result})
        return {"rows": rows, "meta": {"token_count_method": "apply-template+tokenize"}}

    def evaluate(self, results: Dict[str, Any], spec: WorkloadSpec) -> Dict[str, Any]:
        rows = results.get("rows", [])
        expected = spec.expected["needle"]
        recalls = {row["position"]: expected in (row.get("text") or "") for row in rows}
        return {
            "task_success": len(recalls) == len(POSITIONS) and all(recalls.values()),
            "early_recall": recalls.get("early", False),
            "middle_recall": recalls.get("middle", False),
            "late_recall": recalls.get("late", False),
            "token_count_method": results.get("meta", {}).get("token_count_method"),
        }
