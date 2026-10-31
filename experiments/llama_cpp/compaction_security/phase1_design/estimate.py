# 只做离线规模和预算估算，读取 protocol.json；不启动模型、不发送请求，估计金额也不是实际账单。
"""Offline planning arithmetic. Reads the protocol; never calls a model."""
import json
from pathlib import Path


# 用固定矩阵相乘估计轨迹、压缩、探针与审阅数量；同一历史的多次检查不是独立样本，费用基于预设 token 区间。
def estimate(protocol: dict) -> dict:
    corpus, matrix = protocol["corpus"], protocol["matrix"]
    histories = corpus["independent_histories"]
    facts = len(protocol["category_ids"]) * corpus["atomic_facts_per_category_per_history"]
    trajectories = (histories * matrix["primary_framework_count"]
                    * len(matrix["threshold_tokens_candidates"]) * matrix["independent_repeats"])
    compactions = trajectories * matrix["verified_compaction_rounds"]
    snapshots = trajectories * len(matrix["observe_rounds"])
    probes = snapshots * (2 if matrix["full_history_probe_at_each_round"] else 1)
    semantic_reviews = compactions if matrix["semantic_evaluation_at_each_compacted_snapshot"] else 0
    price = protocol["budget"]["planning_flash_usd_per_million_peak"]
    costs = {}
    for name in ("scenario_low", "scenario_high"):
        tokens = protocol["budget"][name]
        costs[name] = round((tokens["input_cache_miss_tokens"] * price["input_cache_miss"]
                            + tokens["output_tokens"] * price["output"]) / 1_000_000, 2)
    return {
        "status": "planning_only_no_model_calls",
        "facts_per_history": facts,
        "unique_ground_truth_facts": facts * histories,
        "primary_trajectories": trajectories,
        "verified_compactions_planned": compactions,
        "checkpoint_snapshots": snapshots,
        "compressed_and_full_probe_batches": probes,
        "primary_semantic_review_batches": semantic_reviews,
        "scheduled_batches_excluding_workload_pilot_retries_and_audit": compactions + probes + semantic_reviews,
        "fact_checkpoint_observations_not_independent_samples": snapshots * facts,
        "planning_usd_by_token_envelope": costs,
        "cost_warning": "not a quote; thinking/helper calls and uncompressed controls need calibration",
    }


if __name__ == "__main__":
    config = json.loads(Path(__file__).with_name("protocol.json").read_text(encoding="utf-8"))
    print(json.dumps(estimate(config), ensure_ascii=False, indent=2))
