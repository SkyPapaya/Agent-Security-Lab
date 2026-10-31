# 基础实验命令行入口。选择 Mock/llama.cpp 后端、场景和 full/llm/preserving 模式，最后生成记录、指标与初始报告。
"""Run: python3 -m experiments.llama_cpp.compaction_security.run_experiment --backend mock"""
import argparse
import json
from pathlib import Path
from .agent_runner import run_one
from .config import MODES, RESULTS, SCENARIOS
from .evaluation import summarize
from .history_builder import load_scenario
from .model_adapter import LlamaCppModel, MockModel


# 由记录生成初始报告；包含历史版本的固定说明，跨环境复用时须核对文字是否符合本次日志。
def make_report(records: list[dict], metrics: dict, args, path: Path) -> None:
    lines = ["# Compaction Boundary Security Lab: 初始报告", "",
             "## 1. Research Question", "",
             "检验上下文压缩是否遗漏限制、来源或授权信息，进而改变模型提出的动作；检验独立执行层能否阻断未经授权的模拟操作。H1-H4 均为待验证假设。", "",
             "## 2. Threat Model", "",
             "攻击者仅控制标记为 untrusted 的虚构工作流文本；合法用户指令和可信场景配置不受其控制。压缩器与 Agent 共享模型服务，模型上下文可受摘要影响。执行层授权状态只来自可信配置，不接受文档修改。所有工具只在实验目录写入虚构数据。", "",
             "## 3. Methodology", "",
             f"后端：{args.backend}；模型文件：{args.model_file or '未提供'}；端点：{args.url}；temperature={args.temperature}；seed={args.seed}；摘要上限={args.summary_tokens}；动作上限={args.action_tokens}。", "",
             "固定的四个场景位于 `scenarios/`。每个场景的三种模式使用相同后续用户请求和工具权限，比较完整历史、同一服务生成的摘要和带独立安全状态的摘要。真实服务可用时尝试 llama.cpp `/tokenize`；端点不可用则明确记录 whitespace proxy。事实保留率使用场景预声明的词项/别名逐项匹配，未匹配不等于语义上必然丢失。", "",
             "## 4. Results", "",
             "| Experiment ID | 场景 | 模式 | 状态 | 模型动作 | 未授权请求 | 工具执行 |", "|---|---|---|---|---|---|---|"]
    for r in records:
        lines.append(f"| {r['experiment_id']} | {r['scenario_id']} | {r['mode']} | {r['status']} | {r.get('action', '-')} | {r.get('unauthorized_request', '-')} | {r.get('tool_result', {}).get('executed', '-')} |")
    lines += ["", f"真实运行数：{metrics['real_runs']}；有效模型动作：{metrics['valid_real_action_runs']}；本批 Mock 运行数：{metrics['mock_runs']}。",
              f"真实模型指标：`{json.dumps({k: v for k, v in metrics.items() if k not in ('real_runs', 'mock_runs')}, ensure_ascii=False)}`", "",
              "Mock 只验证程序路径，不计入真实模型攻击成功率。模型调用、解析、能力和压缩失败各自保留，不被算作安全成功。", "",
              "## 5. Case Study", ""]
    example = next((r for r in records if r["scenario_id"] == "multistep_workflow" and r["mode"] == "llm"), None)
    mock_records_path = RESULTS / "mock_all_records.json"
    if example is None and mock_records_path.exists():
        mock_records = json.loads(mock_records_path.read_text(encoding="utf-8"))
        lines.insert(lines.index("## 5. Case Study"), "Mock 全矩阵：" + ", ".join(
            f"{r['scenario_id']}/{r['mode']}={r['status']}:{r.get('action', '-')}" for r in mock_records))
        lines.insert(lines.index("## 5. Case Study"), "")
        example = next((r for r in mock_records if r["scenario_id"] == "multistep_workflow" and r["mode"] == "llm"), None)
    if example:
        run_dir = Path(example["log"]).parent
        lines += [f"{example['backend']} 轨迹 `{example['experiment_id']}`（Mock 仅验证程序链路）：原始历史见 `{run_dir / 'history.txt'}`，其中用户禁止发送报告和备份，工作流记录为 untrusted。",
                  f"实际摘要见 `{run_dir / 'summary.txt'}`；约束保留检查：`{json.dumps(example.get('constraint_retention'), ensure_ascii=False)}`；来源保留检查：`{json.dumps(example.get('provenance_retention'), ensure_ascii=False)}`。",
                  f"模型响应见 `{run_dir / 'response.txt'}`；动作 `{example.get('action', '无有效动作')}`；权限与工具结果见 `{example['log']}`。状态：`{example['status']}`。"]
    else:
        lines.append("本次没有可用的多步骤压缩轨迹。")
    lines += ["", "## 6. Limitations", "",
              "Qwen 1.5B 可能无法稳定遵循 JSON 协议或有效概括长历史；本次若缺少真实模型运行，无法评价该能力。单一小模型不能代表前沿模型。这里模拟的是外置摘要步骤，不等同于 Agent 产品内置压缩；模拟工具与真实业务系统不同。每组一次不足以支持统计推断，temperature=0 也不保证跨平台完全确定。", "",
              "## 7. Next Research Steps", "",
              "优先在可用的固定 GGUF 和 llama.cpp 服务上重复运行，先确认正常任务与摘要能力，再比较同一请求的完整历史和自然生成摘要；随后增加明确标记的人工消融组以隔离来源和授权事实。", ""]
    # 下面是早期环境探测留下的固定报告文字，不是运行时检测；换环境时不能据此断言服务状态。
    if args.backend == "real" and metrics["valid_real_action_runs"] == 0:
        lines.insert(lines.index("## 5. Case Study"), "五次 Python 后端调用在受限环境中均记录 `URLError(PermissionError(1))`；随后在沙箱外独立检查 `127.0.0.1:8080`，结果为连接拒绝。本地未发现可用 GGUF 或 llama.cpp 服务。真实模型行为、压缩信息丢失及攻击率均不可判定。")
        lines.insert(lines.index("## 5. Case Study"), "")
    path.write_text("\n".join(lines), encoding="utf-8")


# 解析运行参数，建立模型后遍历实验矩阵；real 会请求配置的推理端点，mock 只验证流程。
def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--backend", choices=("mock", "real"), default="mock")
    parser.add_argument("--url", default="http://127.0.0.1:8080")
    parser.add_argument("--model", default="local")
    parser.add_argument("--model-file")
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--temperature", type=float, default=0)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--summary-tokens", type=int, default=320)
    parser.add_argument("--action-tokens", type=int, default=120)
    parser.add_argument("--suite", choices=("all", "initial"), default="all")
    args = parser.parse_args(argv)
    if args.runs < 1 or args.summary_tokens < 1 or args.action_tokens < 1:
        parser.error("runs and token limits must be positive")
    model = MockModel() if args.backend == "mock" else LlamaCppModel(args.url, args.model, args.temperature, args.seed)
    # initial 取五项首次验证组合；all 遍历四个场景×三模式，再按 runs 重复。
    choices = ([("benign_control", "full"), ("benign_control", "llm"),
                ("constraint_loss", "full"), ("constraint_loss", "llm"),
                ("constraint_loss", "preserving")] if args.suite == "initial" else
               [(p.stem, mode) for p in sorted(SCENARIOS.glob("*.json")) for mode in MODES])
    records = []
    for index in range(args.runs):
        for name, mode in choices:
            records.append(run_one(load_scenario(SCENARIOS / f"{name}.json"), mode, model,
                                   args.backend, index, args.summary_tokens, args.action_tokens,
                                   args.model_file, args.temperature, args.seed))
    RESULTS.mkdir(parents=True, exist_ok=True)
    summary = summarize(records)
    tag = f"{args.backend}_{args.suite}"
    (RESULTS / f"{tag}_records.json").write_text(json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8")
    (RESULTS / f"{tag}_metrics.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    make_report(records, summary, args, RESULTS / "initial_report.md")
    print(json.dumps({"records": len(records), "metrics": summary, "report": str(RESULTS / "initial_report.md")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
