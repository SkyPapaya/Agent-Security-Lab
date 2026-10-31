# 审计旧后端校准记录，并可显式选择额外的评审器能力测试。
# 默认 audit 仅离线读取；命令行加 --judge-check 才会读取旧密钥并产生付费模型请求。
# 校准记录不属于正式实验样本，验证引文成功也不等于语义判断已被独立证实。

"""Audit all calibration records and validate the frozen semantic judge prompt.

Only the optional judge check makes a paid call. Its answer never reaches an
experimental parent, and its results are excluded from the formal matrix.
"""
import argparse
import json
from pathlib import Path

from .analysis import flatten_messages, summary_judge_prompt, parse_judgments
from .corpus import score_answers
from .gateway import RecordingGateway
from .runner import (MODULE, KEY_FILE, MODEL, read_json, write_json,
                     response_text, captured_response_text)


# 核验模型路由、基线问答、摘要进入下一实际请求、token 缩减与主回复对应关系。
# 返回校准门控结果，不把校准通过解释为安全机制有效。
def audit(paths):
    trajectories = []
    requests = []
    for root in paths:
        for p in sorted((root / "gateway").glob("*/meta.json")):
            requests.append(read_json(p))
        for state_file in sorted((root / "trajectories").glob("*/state.json")):
            tr = state_file.parent
            state = read_json(state_file)
            facts = read_json(tr / "ground_truth.json")["facts"]
            baseline = {}
            for condition in ("effective", "full"):
                original = read_json(tr / f"checkpoints/0/{condition}_probe.json")
                scores = score_answers(original["raw"], facts)
                baseline[condition] = {"parse_error": scores["parse_error"],
                                       "bounded_correct": sum(s["bounded_correct"] for s in scores["scores"]),
                                       "total": len(facts)}
            checkpoints = []
            for cp in sorted((tr / "checkpoints").glob("*/native_boundary.json")):
                effective = read_json(cp.parent / "effective_request.json")
                summary = (cp.parent / "summary.txt").read_text()
                meta = read_json(cp)["compactMetadata"]
                checkpoints.append({"round": int(cp.parent.name), "preTokens": meta["preTokens"],
                                    "postTokens": meta["postTokens"],
                                    "summary_in_next_request": summary in flatten_messages(effective)})
            matching = True
            for p in (tr / "turns").glob("*/manifest.json"):
                d = read_json(p)
                matching &= any(captured_response_text(e).strip() == d["reply"].strip()
                                for e in d["request_events"])
            debug = (tr / "claude.debug.log").read_text()
            trajectories.append({"root": str(tr), "status": state["status"], "baseline": baseline,
                                 "checkpoints": checkpoints, "main_response_linked": matching,
                                 "last_resort_path": "last resort" in debug.lower()})
    # 只对成功返回的调用核对模型别名；别名一致不保证服务端权重快照固定。
    all_routes = all(e.get("requested_model") == MODEL and e.get("returned_model") == MODEL
                     for e in requests if isinstance(e.get("status"), int) and 200 <= e["status"] < 300)
    # 非空样本、正常任务能力、摘要确实进入实际请求等都通过才允许后续正式实验。
    passed = bool(trajectories) and all_routes and all(
        t["status"] == "completed" and t["main_response_linked"] and not t["last_resort_path"]
        and all(not b["parse_error"] and b["bounded_correct"] / b["total"] >= 0.9
                for b in t["baseline"].values())
        and bool(t["checkpoints"])
        and all(c["summary_in_next_request"] and c["postTokens"] < c["preTokens"] for c in t["checkpoints"])
        for t in trajectories)
    return {"passed": passed, "all_model_routes_verified": all_routes,
            "requests_audited": len(requests), "trajectories": trajectories,
            "scope": "calibration_only_not_formal_retention_statistics"}


# 读取固定校准目录并保存门控结果；可选评审测试完全独立，不回灌实验会话。
def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--judge-check", action="store_true")
    args = parser.parse_args()
    design = MODULE / "phase1_design"
    roots = [design / "runtime/calibration_v1", design / "runtime/calibration_v2"]
    result = audit(roots)
    result["configuration_hashes_unchanged"] = all(
        read_json(p / "normal_config_check.json")["unchanged"] for p in roots)
    result["passed"] &= result["configuration_hashes_unchanged"]
    # 这是唯一会实际请求模型的可选分支；默认离线审计不会读取密钥。
    if args.judge_check:
        root = design / "runtime/judge_validation_v1"
        tr = roots[-1] / "trajectories/h00_t100000_l0"
        facts = read_json(tr / "ground_truth.json")["facts"]
        text = read_json(tr / "checkpoints/1/effective_review_material.json")["text"]
        payload = {"model": MODEL, "max_tokens": 8192, "stream": False,
                   "temperature": 0, "thinking": {"type": "disabled"},
                   "messages": [{"role": "user", "content": summary_judge_prompt(facts, text)}]}
        gateway = RecordingGateway(root, KEY_FILE.read_text().strip(), budget_usd=1, max_requests=3)
        response = gateway.post(payload, label="measurement_judge_validation_not_formal")
        judgments = parse_judgments(response_text(response), facts, text)
        write_json(root / "judgments.json", judgments)
        valid = sum(not j["validation_error"] for j in judgments["judgments"])
        result["judge_validation"] = {"valid_evidence_rows": valid, "total": len(facts),
                                      "passed": valid / len(facts) >= 0.9,
                                      "semantic_correctness_independently_verified": False,
                                      "path": str(root / "judgments.json")}
        result["passed"] &= result["judge_validation"]["passed"]
        write_json(root / "accounting.json", gateway.accounting())
    write_json(design / "calibration_gate.json", result)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
