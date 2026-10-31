# 测试普通摘要接口能否接收构造历史、保存提示词并拒绝空摘要。这里使用 Mock 后端，不测量真实模型遗忘。
# 阅读顺序：先看每个测试上方的条件和目的，再对照实际断言；固定回复只用于离线单测。
import unittest
from experiments.llama_cpp.compaction_security.compactor import compact
from experiments.llama_cpp.compaction_security.history_builder import build_history, load_scenario
from experiments.llama_cpp.compaction_security.config import SCENARIOS
from experiments.llama_cpp.compaction_security.model_adapter import MockModel


# 集中验证本模块的输入、正常路径与异常边界；各方法独立构造条件。
class CompactorTests(unittest.TestCase):
    # 读取固定场景后构造长历史，验证约束仍在原文、摘要接口保存提示词且 Mock 文本确实缩短。
    def test_history_and_compaction_interface(self):
        scenario = load_scenario(SCENARIOS / "constraint_loss.json")
        history = build_history(scenario)
        self.assertGreater(len(history), 12)
        self.assertIn("no-send", str(history))
        result = compact(history, MockModel(), 100)
        self.assertIn("Summarize", result["prompt"])
        self.assertGreater(result["before_tokens"], result["after_tokens"])

    # 人工让摘要接口返回空字符串，要求显式报错而不是把空摘要当成安全压缩结果。
    def test_empty_summary_fails(self):
        # 始终返回空摘要的接口替身，用于覆盖显式失败路径。
        class Empty:
            # 固定返回空字符串，触发摘要接口的空输出校验。
            def generate(self, prompt, max_tokens):
                return ""
        with self.assertRaises(ValueError):
            compact([{"role": "user", "content": "hello"}], Empty(), 10)
