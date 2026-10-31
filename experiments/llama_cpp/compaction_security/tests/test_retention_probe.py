# 核对早期保留探针的固定输入和失败标记，防止把无法读取材料或非原生摘要冒充压缩实验。
# 阅读顺序：先看每个测试上方的条件和目的，再对照实际断言；固定回复只用于离线单测。
import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1] / "retention_probe"


# 集中验证本模块的输入、正常路径与异常边界；各方法独立构造条件。
class RetentionProbeTests(unittest.TestCase):
    # 早中晚三个输入只改变排列，不增删记录；每个目标、授权和模拟攻击锚点恰好出现一次。
    def test_variants_have_same_records_and_anchors(self):
        lines = [(ROOT / "inputs" / f"authority_{position}.txt").read_text(encoding="utf-8").splitlines()
                 for position in ("early", "middle", "late")]
        self.assertEqual(sorted(lines[0]), sorted(lines[1]))
        self.assertEqual(sorted(lines[0]), sorted(lines[2]))
        for block in lines:
            text = "\n".join(block)
            for anchor in ("GOAL-2", "AUTH-7", "SRC-4", "ATK-9", "STEP-6", "NOISE-3"):
                self.assertEqual(text.count(anchor), 1)

    # 检查早期输入读取失败独立保留，且结果明确未启用原生压缩和窗口配置。
    def test_pilot_records_failure_separately(self):
        data = json.loads((ROOT / "pilot_results.json").read_text(encoding="utf-8"))
        self.assertFalse(data["native_compaction"])
        self.assertFalse(data["context_window_configured"])
        self.assertEqual(len(data["runs"]), 6)
        self.assertEqual(sum(run.get("status") == "input_read_failure" for run in data["runs"]), 1)
