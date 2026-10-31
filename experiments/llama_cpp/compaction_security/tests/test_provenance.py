# 测试不可信来源标记、可信配置中的授权主体，以及预声明事实的词面保留率。
# 阅读顺序：先看每个测试上方的条件和目的，再对照实际断言；固定回复只用于离线单测。
import unittest
from experiments.llama_cpp.compaction_security.config import SCENARIOS
from experiments.llama_cpp.compaction_security.history_builder import build_history, load_scenario, trusted_state
from experiments.llama_cpp.compaction_security.evaluation import retention


# 集中验证本模块的输入、正常路径与异常边界；各方法独立构造条件。
class ProvenanceTests(unittest.TestCase):
    # 原始历史保留 untrusted 标记，文档内容不能改变可信用户状态或偷偷授权发送。
    def test_untrusted_mark_retained_in_history(self):
        scenario = load_scenario(SCENARIOS / "provenance_loss.json")
        history = build_history(scenario)
        self.assertTrue(any(row.get("source_trust") == "untrusted" for row in history))
        state = trusted_state(scenario)
        self.assertEqual(state.authorizer, "user")
        self.assertNotIn("send_mock_email", state.authorized_tools)

    # 用两个预声明词面事实校验一半命中的保留率；这不是模型自评，也不等于语义保留率。
    def test_predeclared_fact_checks(self):
        facts = [{"id": "trust", "terms": ["untrusted"]}, {"id": "authorizer", "terms": ["user authorized"]}]
        result = retention("source is untrusted", facts)
        self.assertEqual(result["rate"], 0.5)
