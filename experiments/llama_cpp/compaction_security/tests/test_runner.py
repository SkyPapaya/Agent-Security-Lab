# 测试基础 Agent 实验从历史构造、摘要、动作解析到授权和本地工具执行的完整流程；固定模型替身只验证代码行为。
# 阅读顺序：先看每个测试上方的条件和目的，再对照实际断言；固定回复只用于离线单测。
import json
import tempfile
import unittest
from pathlib import Path
from experiments.llama_cpp.compaction_security.agent_runner import run_one
from experiments.llama_cpp.compaction_security.config import SCENARIOS
from experiments.llama_cpp.compaction_security.history_builder import load_scenario
from experiments.llama_cpp.compaction_security.model_adapter import MockModel


# 集中验证本模块的输入、正常路径与异常边界；各方法独立构造条件。
class RunnerTests(unittest.TestCase):
    # 在同一正常场景跑 full 和 Mock 摘要流程，核对关键事件、正常任务完成和摘要文件。
    def test_full_and_compacted_events_and_mock_label(self):
        scenario = load_scenario(SCENARIOS / "benign_control.json")
        with tempfile.TemporaryDirectory() as temp:
            full = run_one(scenario, "full", MockModel(), "mock", output_root=Path(temp))
            compacted = run_one(scenario, "llm", MockModel(), "mock", output_root=Path(temp))
            for record in (full, compacted):
                events = [json.loads(row)["event"] for row in Path(record["log"]).read_text().splitlines()]
                self.assertIn("model_response", events)
                self.assertIn("policy_check", events)
                self.assertIn("experiment_completed", events)
                self.assertTrue(record["benign_success"])
            self.assertTrue((Path(compacted["log"]).parent / "summary.txt").exists())

    # 本地备份后仍能回溯合成数据来源，且原任务未授权发送时不生成模拟发件箱。
    def test_multistep_backup_provenance_persists(self):
        scenario = load_scenario(SCENARIOS / "multistep_workflow.json")
        with tempfile.TemporaryDirectory() as temp:
            record = run_one(scenario, "preserving", MockModel(), "mock", output_root=Path(temp))
            self.assertEqual(record["pre_action_result"]["source"], "synthetic local dataset")
            self.assertTrue(Path(record["pre_action_result"]["path"]).exists())
            self.assertFalse((Path(record["log"]).parent / "mock_outbox").exists())

    # 模型替身故意返回非 JSON，运行状态必须是 parse_error，不能视作拒绝或正常完成。
    def test_parse_error_is_separate(self):
        # 始终返回非法格式的模型替身，只测试错误分类。
        class BadModel(MockModel):
            # 固定返回非 JSON 文本，触发动作解析失败。
            def generate(self, prompt, max_tokens):
                return "not json"
        with tempfile.TemporaryDirectory() as temp:
            record = run_one(load_scenario(SCENARIOS / "benign_control.json"), "full", BadModel(), "mock", output_root=Path(temp))
            self.assertEqual(record["status"], "parse_error")

    # 替身故意提出未授权发送，验证“已提出请求”与“实际执行”分别记录且权限检查阻断落盘。
    def test_unauthorized_model_request_blocked(self):
        # 人为提出未授权动作的模型替身，只验证执行器代码路径，不是攻击成功样本。
        class SendModel(MockModel):
            # 摘要仍复用 Mock，决策则固定输出未授权发送，用于核对请求/执行两个事件分开记录。
            def generate(self, prompt, max_tokens):
                if "Summarize" in prompt:
                    return super().generate(prompt, max_tokens)
                return '{"action":"send_mock_email","arguments":{"file_id":"demo-report-001","recipient":"unapproved-recipient"}}'
        with tempfile.TemporaryDirectory() as temp:
            record = run_one(load_scenario(SCENARIOS / "constraint_loss.json"), "full", SendModel(), "mock", output_root=Path(temp))
            self.assertTrue(record["unauthorized_request"])
            self.assertFalse(record["unauthorized_execution"])
            self.assertEqual(record["status"], "authorization_denied")
            self.assertFalse((Path(record["log"]).parent / "mock_outbox").exists())
