# 测试关系研究的矩阵、连续失败停止和离线多轮流程。人工会话替身用于验证程序记录，不计入真实模型实验。
# 阅读顺序：先看每个测试上方的条件和目的，再对照实际断言；固定回复只用于离线单测。
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from experiments.llama_cpp.compaction_security.native_retention import codex_runner as runner
from experiments.llama_cpp.compaction_security.native_retention.codex_backend import context_view
from experiments.llama_cpp.compaction_security.native_retention.relation_corpus import build_relation_corpus


# 构造关系研究的默认参数，允许单测只覆盖关心的字段，不读取命令行或真实配置。
def arguments(**overrides):
    values = dict(study="relations", histories=3, runs=1, thresholds=[24000],
                  source_roles=["tool"], generation_repeats=2, layout=0,
                  task_conditions=["record_only", "pending_review"], rounds=6,
                  quiet=True, trigger="manual", timeout=10, probes=True,
                  max_chunks=10, chunk_chars=256, max_consecutive_failures=3)
    values.update(overrides)
    return SimpleNamespace(**values)


# 集中验证本模块的输入、正常路径与异常边界；各方法独立构造条件。
class RelationRunnerTests(unittest.TestCase):
    # 三历史、两次会话重复、两目标和六轮组成 72 个预定边界，目标顺序交替以减少顺序偏差。
    def test_seventy_two_boundaries_and_balanced_task_order(self):
        args = arguments()
        cells = runner.build_cells(args)
        self.assertEqual(len(cells), 12)
        self.assertEqual(len({cell["id"] for cell in cells}), 12)
        self.assertEqual(len(cells) * args.rounds, 72)
        first = [cells[i]["task_condition"] for i in range(0, len(cells), 2)]
        self.assertEqual(first.count("record_only"), 3)
        self.assertEqual(first.count("pending_review"), 3)

    # 同条件的独立会话重复必须使用相同布局材料，generation_repeat 仅标记重复而不换输入。
    def test_generation_repeat_reuses_material_and_workload(self):
        cells = runner.build_cells(arguments())
        for history in range(3):
            for condition in ("record_only", "pending_review"):
                pair = [c for c in cells if c["history"] == history and c["task_condition"] == condition]
                materials = [build_relation_corpus(c["history"], c["repeat"], c["task_condition"]) for c in pair]
                self.assertEqual(materials[0], materials[1])
                self.assertNotEqual(pair[0]["generation_repeat"], pair[1]["generation_repeat"])

    # 连续失败达到阈值后停止新增轨迹，保留计划总量和未启动条目，不能用失败数填充完成数。
    def test_fail_fast_preserves_unstarted_cells(self):
        # 本用例的离线网关替身，记录临时快照或返回固定答案，不访问真实服务。
        class Gateway:
            # 返回离线替身的请求计数或固定用量，供真实编排代码更新进度，不表示真实付费消耗。
            def accounting(self):
                return {}

        # 故意失败的编排器子类，用于确认提前停止仍保留未启动计划。
        class Failing(runner.Experiment):
            # 故意把每条轨迹计为失败，以验证连续失败保护，而不是运行真实轨迹。
            def one(self, cell):
                self.progress["runs_failed"] += 1

        with tempfile.TemporaryDirectory() as temp:
            args = arguments(max_consecutive_failures=2)
            experiment = Failing(args, Path(temp), {}, Gateway())
            experiment.run(runner.build_cells(args))
            self.assertEqual(experiment.progress["status"], "failed")
            self.assertEqual(experiment.progress["runs_failed"], 2)
            self.assertEqual(experiment.progress["runs_total"], 12)
            self.assertIn("consecutive_trajectory_failures", (Path(temp) / "events.jsonl").read_text())

    # 离线替身故意只保留可信引言，验证授权因重放可恢复而文档细节消失；这是流程测试，不计自然遗忘。
    def test_two_round_pipeline_separates_user_authority_from_document_loss(self):
        corpus = build_relation_corpus(0, 0, "record_only")
        parent_histories = []
        with tempfile.TemporaryDirectory() as temp:
            out = Path(temp)

            # 本用例的离线网关替身，记录临时快照或返回固定答案，不访问真实服务。
            class Gateway:
                local_url = "http://127.0.0.1:0/v1"
                label = "offline"

                # 初始化本用例替身的记录容器和可选响应，不建立真实连接。
                def __init__(self):
                    self.rows = []
                    self.probes = []

                # 返回离线替身的请求计数或固定用量，供真实编排代码更新进度，不表示真实付费消耗。
                def accounting(self):
                    return {"requests": len(self.rows), "guard_exhausted": False}

                # 返回已记录事件的副本，模拟真实网关的审计接口，避免调用者改动内部账本。
                def events(self):
                    return copy.deepcopy(self.rows)

                # 把伪请求和伪响应写入临时快照，并登记路径和序号，供真实检查点逻辑追溯。
                def capture(self, payload, response_text):
                    n = len(self.rows) + 1
                    folder = out / "fake_gateway" / str(n)
                    folder.mkdir(parents=True)
                    request = folder / "request.json"
                    response = folder / "response.json"
                    request.write_text(json.dumps(payload, ensure_ascii=False))
                    response.write_text(json.dumps({"output": [{"type": "message", "content": [
                        {"type": "output_text", "text": response_text}]}]}, ensure_ascii=False))
                    self.rows.append({"idx": n, "label": self.label, "status": 200,
                                      "request_path": str(request), "response_path": str(response),
                                      "response_content_type": "application/json"})

                # 根据实际可见文本生成固定证据回答；没有证据的字段回 UNKNOWN，模拟探针但不向主历史回灌答案。
                def post(self, request, label):
                    self.probes.append(copy.deepcopy(request))
                    text = context_view({"input": request["input"][:-1]})["text"]
                    records = []
                    for relation in corpus["relations"]:
                        row = {"id": relation["id"], "evidence": {}}
                        for field, expected in relation["expected"].items():
                            evidence = relation["statement"] if relation["statement"] in text else ""
                            if not evidence and field == "authorization":
                                evidence = next((line for line in text.splitlines()
                                                 if relation["id"] in line and expected in line), "")
                            row[field] = expected if evidence else "UNKNOWN"
                            row["evidence"][field] = evidence or "UNKNOWN"
                        records.append(row)
                    return {"output": [{"type": "message", "content": [
                        {"type": "output_text", "text": json.dumps({"records": records}, ensure_ascii=False)}]}]}

            gateway = Gateway()

            # 本用例的离线会话替身，用列表模拟主历史和边界，验证编排而不启动 Codex。
            class Session:
                # 为每个离线会话新建历史和边界列表，防止不同轨迹共享模拟状态。
                def __init__(self, *args, **kwargs):
                    self.history = []
                    self.boundaries = []

                # 追加用户输入和“已记录”回复，保存伪请求并收集主历史，以检查探针是否污染主会话。
                def send(self, text):
                    self.history.append(runner.input_message("user", text))
                    gateway.capture({"input": copy.deepcopy(self.history)}, "已记录")
                    self.history.append(runner.input_message("assistant", "已记录"))
                    parent_histories.append(copy.deepcopy(self.history))
                    return {"events": [{"method": "item/completed", "params": {
                        "item": {"type": "agentMessage", "text": "已记录"}}}]}

                # 深拷贝追加固定角色材料，只模拟编排器注入，不执行其中任何工具。
                def inject(self, items):
                    self.history.extend(copy.deepcopy(items))

                # 人工记录一次压缩边界或替换为固定摘要，仅用于验证保存流程，不能作为真实压缩结果。
                def compact(self):
                    gateway.capture({"input": copy.deepcopy(self.history)}, "offline summary")
                    # 故意仅保留用户引言：模拟“授权仍因原文重放可见、文档字段已缺失”的测量条件。
                    self.history = self.history[:1] + [runner.input_message("user", "离线测试摘要，只记录。")]
                    self.boundaries.append({"type": "contextCompaction"})

                # 返回离线替身已经创建的压缩边界列表，供编排器对比新增次数。
                def compactions(self):
                    return list(self.boundaries)

                # 离线替身没有真实子进程或连接需要释放，保留接口便于编排器统一清理。
                def close(self):
                    pass

            args = arguments(histories=1, generation_repeats=1, task_conditions=["record_only"], rounds=2)
            experiment = runner.Experiment(args, out, {"model": "offline", "reasoning_effort": "medium"}, gateway)
            with patch.object(runner, "CodexSession", Session):
                experiment.run(runner.build_cells(args))
            self.assertEqual(experiment.progress["status"], "completed")
            self.assertEqual(experiment.progress["compactions_completed"], 2)
            # 轮次 0 是压缩前基线，1/2 是压缩后，因此两轮应有三个测量文件。
            checkpoints = sorted(out.glob("trajectories/*/checkpoints/*/measurements.json"))
            self.assertEqual(len(checkpoints), 3)
            for checkpoint in checkpoints:
                measurement = json.loads(checkpoint.read_text())
                effective = measurement["relation_effective_probe"]["scores"]
                full = measurement["relation_full_probe"]["scores"]
                self.assertEqual(sum(s["supported_correct"] for s in full), 30)
                # 基线 30 字段全在；替身压缩后只剩用户引言中的六项授权，不能把它算成摘要记忆。
                expected = 30 if measurement["round"] == 0 else 6
                self.assertEqual(sum(s["supported_correct"] for s in effective), expected)
            self.assertEqual(len(gateway.probes), 6)  # two detached probes, not two duplicated suites
            self.assertTrue(all(p["text"]["format"]["type"] == "json_schema" for p in gateway.probes))
            self.assertTrue((out / "semantic_changes.md").exists())
            self.assertTrue(all('"records"' not in json.dumps(history) for history in parent_histories))


if __name__ == "__main__":
    unittest.main()
