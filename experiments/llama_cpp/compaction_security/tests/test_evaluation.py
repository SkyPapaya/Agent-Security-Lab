# 测试汇总指标的分母和配对方式，确保 Mock、模型错误与真实模型的有效行为不会混算。
# 阅读顺序：先看每个测试上方的条件和目的，再对照实际断言；固定回复只用于离线单测。
import unittest
from experiments.llama_cpp.compaction_security.evaluation import summarize


# 集中验证本模块的输入、正常路径与异常边界；各方法独立构造条件。
class EvaluationTests(unittest.TestCase):
    # 把一个 Mock 越权请求与一个真实后端标签的正常记录混合，确认真实指标分母不包含 Mock。
    def test_mock_excluded_from_real_metrics(self):
        mock = {"backend": "mock", "attack": True, "unauthorized_request": True, "mode": "full",
                "scenario_id": "a", "run_index": 0, "status": "completed"}
        real = {"backend": "real", "attack": True, "unauthorized_request": False,
                "unauthorized_execution": False, "mode": "full", "scenario_id": "a", "run_index": 0,
                "status": "completed", "action": "no_action"}
        result = summarize([mock, real])
        self.assertEqual(result["real_runs"], 1)
        self.assertEqual(result["mock_runs"], 1)
        self.assertEqual(result["unauthorized_action_request_rate"], 0)

    # 相同场景和轮次的 full/llm 行为不同应列入配对差异；差异本身不自动等于漏洞。
    def test_decision_change_is_paired(self):
        rows = [{"backend": "real", "attack": True, "scenario_id": "x", "run_index": 0,
                 "mode": mode, "action": action, "status": "completed"}
                for mode, action in (("full", "no_action"), ("llm", "send_mock_email"))]
        self.assertEqual(len(summarize(rows)["compaction_induced_decision_changes"]), 1)

    # 唯一记录是模型调用失败时，未授权请求率应未知，不能自动算作 0 或安全成功。
    def test_model_error_not_counted_as_safety_success(self):
        row = {"backend": "real", "attack": True, "scenario_id": "x", "run_index": 0,
               "mode": "full", "status": "model_error"}
        self.assertIsNone(summarize([row])["unauthorized_action_request_rate"])
