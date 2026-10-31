# 测试早期人工摘要探针的位置轮换和结果矩阵。这些固定材料实验不属于框架原生自动压缩。
# 阅读顺序：先看每个测试上方的条件和目的，再对照实际断言；固定回复只用于离线单测。
import unittest
import json
from pathlib import Path
from experiments.llama_cpp.compaction_security.priority_probe.generate import BLOCKS, render
from experiments.llama_cpp.compaction_security.priority_probe.analyze import analyze


# 集中验证本模块的输入、正常路径与异常边界；各方法独立构造条件。
class PriorityProbeTests(unittest.TestCase):
    # 六次轮换应让每类事实在每个位置各出现一次，避免只比较一个固定顺序。
    def test_every_fact_appears_at_each_position(self):
        orders = [render(i).splitlines()[:-1] for i in range(len(BLOCKS))]
        self.assertEqual(len(orders), 6)
        for position in range(6):
            self.assertEqual(set(row[position] for row in orders), set(BLOCKS))
        self.assertEqual({len(set(row)) for row in orders}, {6})

    # 已保存结果应覆盖 6 个位置轮换与 2 个摘要预算，并明确标记其非原生压缩性质。
    def test_results_cover_all_rotations_and_budgets(self):
        path = Path(__file__).resolve().parents[1] / "priority_probe" / "results.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual({(row["rotation"], row["budget"]) for row in data["runs"]},
                         {(rotation, budget) for rotation in range(6) for budget in (60, 120)})
        self.assertFalse(data["native_compaction"])
        self.assertEqual(analyze(data)["by_budget"]["60"]["retention"]["A"], 6)
