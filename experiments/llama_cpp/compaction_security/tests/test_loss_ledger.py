# 测试逐轮损失账本的记录、重建和并发写入。marker_missing 只表示词面标记未命中，不等于语义已删除。
# 阅读顺序：先看每个测试上方的条件和目的，再对照实际断言；固定回复只用于离线单测。
import concurrent.futures
import json
from pathlib import Path
import tempfile
import unittest

from experiments.llama_cpp.compaction_security.native_retention.loss_ledger import record_checkpoint


# 构造最小虚构真值字典，明确编号、答案、原句和别名，供后续测试独立修改。
def fact(identifier="fact-1", answer="示例簿-42153", text=None, aliases=None):
    return {"id": identifier, "category": "provenance", "answer": answer,
            "text": text or f"示例簿-42153仅限本地合成研究，禁止外发。", "aliases": aliases or []}


# 集中验证本模块的输入、正常路径与异常边界；各方法独立构造条件。
class LossLedgerTests(unittest.TestCase):
    # 每个用例建立自己的临时材料和目录，避免前一个用例状态或已有实验记录影响断言。
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.fact = fact()
        self.text = self.fact["text"]

    # 清理本用例创建的临时目录，保留仓库原有实验和日志。
    def tearDown(self):
        self.temp.cleanup()

    # 把固定轨迹和真值交给账本，只让轮次、前后文本及观察参数随用例变化。
    def record(self, k, before, after, **kwargs):
        return record_checkpoint(self.root, "trajectory-01", k, [self.fact], before, after, **kwargs)

    # 依次构造标记存在、首次缺失、持续缺失和重新出现，验证状态变化并保留完整原事实。
    def test_first_continuing_and_reappearance_preserve_complete_fact(self):
        self.record(0, None, self.text)
        first = self.record(1, self.text, "摘要仅保留一般任务。")
        continued = self.record(2, "一般任务。", "一般任务。")
        recovered = self.record(3, "一般任务。", self.text)
        self.assertEqual(first["records"][0]["state"], "first_marker_missing")
        self.assertEqual(continued["records"][0]["state"], "continuing_marker_missing")
        self.assertEqual(recovered["records"][0]["state"], "marker_reappeared")
        again = self.record(4, self.text, "一般任务。")
        self.assertEqual(again["records"][0]["state"], "marker_missing_again")
        for row in [first, continued, recovered, again]:
            self.assertEqual(row["records"][0]["original_text"], self.text)

    # 区分组合答案、限定词和整句原文的变化；编号留下不代表限制仍在，措辞变化也不等于语义删除。
    def test_composite_qualifier_and_verbatim_changes_are_separate(self):
        self.fact = fact(answer="示例簿-42153；untrusted",
                         text="输入来自示例簿-42153，source_trust=untrusted，未经用户确认。")
        self.text = self.fact["text"]
        self.record(0, None, self.text)
        row = self.record(1, self.text, "输入来自示例簿-42153。节选仅保留来源名。")['records'][0]
        self.assertEqual(row["state"], "marker_retained")
        self.assertIn("qualifier_missing", row["events"])
        self.assertIn("verbatim_rewritten", row["events"])
        self.assertTrue(row["before"]["composite_present"])
        self.assertFalse(row["after"]["composite_present"])
        self.assertEqual(row["after"]["marker_evidence"], "示例簿-42153")

    # 使用中英文分隔符和预声明别名，检查同一组合事实不会仅因排版不同被误判。
    def test_semicolon_ascii_and_alias_composite(self):
        self.fact = fact(answer="示例簿-42153;未批准", text="建议示例簿-42153尚未获批。",
                         aliases=["示例簿-42153；尚未获批"])
        row = self.record(0, None, self.fact["text"])["records"][0]
        self.assertTrue(row["after"]["composite_present"])
        self.assertNotIn("qualifier_missing", row["events"])

    # 压缩后状态不可读时必须记为无法观察，而不是首次或持续丢失。
    def test_opaque_after_is_unknown_and_not_loss(self):
        self.record(0, None, self.text)
        row = self.record(1, self.text, None, metadata={"opaque_state": True})["records"][0]
        self.assertEqual(row["events"], ["observation_unknown"])
        self.assertIsNone(row["after"]["marker_present"])
        self.assertIsNone(row["snapshots"]["after"]["local_text_file"])
        self.assertIn("不能判断删除", (self.root / "lost_content.md").read_text())

    # 实际观察到空文本与根本无法读取是两回事；空文本仍可形成明确的词面诊断。
    def test_observed_empty_context_is_not_opaque(self):
        row = self.record(1, self.text, "")["records"][0]
        self.assertEqual(row["state"], "first_marker_missing")
        self.assertTrue(row["after"]["observable"])
        self.assertEqual(Path(row["snapshots"]["after"]["local_text_file"]).read_text(), "")

    # 没有压缩前证据时，标记不在当前文本中只能记作基线未观察到，不能归因于压缩。
    def test_missing_baseline_is_not_compaction_loss(self):
        baseline = self.record(0, None, "没有摄入目标的背景。")
        later = self.record(1, "背景。", "背景。")
        self.assertEqual(baseline["records"][0]["state"], "baseline_not_ingested")
        self.assertEqual(later["records"][0]["state"], "marker_not_ingested_or_preexisting_missing")
        self.assertNotIn("first_marker_missing", later["counts"])
        appeared = self.record(2, "背景。", self.text)
        self.assertEqual(appeared["records"][0]["state"], "marker_appeared")

    # 前态未知而后态缺失，不能凭后态倒推某次压缩删除了内容。
    def test_unknown_before_does_not_assert_loss(self):
        row = self.record(1, None, "背景。")["records"][0]
        self.assertEqual(row["state"], "marker_absent_transition_unknown")

    # 保留答案却改写原句时只记录原文措辞变化，不把它升级为语义丢失。
    def test_rewritten_verbatim_is_not_semantic_loss(self):
        self.record(0, None, self.text)
        row = self.record(1, self.text, "不可外发：示例簿-42153，只准本地研究。")["records"][0]
        self.assertEqual(row["state"], "marker_retained")
        self.assertIn("verbatim_rewritten", row["events"])
        self.assertIn("not_semantic_deletion", row["interpretation"])

    # 重复写同一检查点可复用原结果，但相同轮次不同输入必须拒绝，避免覆盖证据。
    def test_idempotency_and_changed_data_refused(self):
        one = self.record(0, None, self.text)
        second = self.record(0, None, self.text)
        self.assertEqual(one, second)
        self.assertEqual(len((self.root / "lost_content.jsonl").read_text().splitlines()), 1)
        with self.assertRaisesRegex(ValueError, "different input"):
            self.record(0, None, "different")
        self.assertEqual(json.loads((self.root / "loss_checkpoints/trajectory-01/0.json").read_text()), one)

    # 模拟聚合报告缺失，再次提交已有检查点应从分片重建报告，且不重复追加记录。
    def test_rebuild_reports_after_interrupted_aggregate_write(self):
        self.record(0, None, self.text)
        (self.root / "lost_content.jsonl").unlink()
        (self.root / "lost_content.md").unlink()
        self.record(0, None, self.text)
        self.assertEqual(len((self.root / "lost_content.jsonl").read_text().splitlines()), 1)
        self.assertIn("第 0 轮", (self.root / "lost_content.md").read_text())

    # 用长事实验证原句不被截断，快照引用和本地文本副本都能回溯到实际比较材料。
    def test_snapshot_references_and_untruncated_original_fact(self):
        self.fact = fact(text="虚构长事实：" + "合成研究限定。" * 150 + "示例簿-42153不得外发。")
        text = self.fact["text"]
        self.record(0, None, text)
        result = self.record(1, text, "摘要省略。", metadata={"before_snapshot": "inputs/original.json",
                                                         "after_snapshot": "inputs/compacted.json"})
        row = result["records"][0]
        self.assertEqual(row["snapshots"]["before"]["caller_reference"], "inputs/original.json")
        self.assertEqual(Path(row["snapshots"]["before"]["local_text_file"]).read_text(), text)
        self.assertIn(text, (self.root / "lost_content.md").read_text())
        ledger = [json.loads(line) for line in (self.root / "lost_content.jsonl").read_text().splitlines()]
        self.assertEqual(ledger[-1]["case_id"], row["case_id"])

    # 并行写入多条独立轨迹，要求所有记录完整保留且 case_id 唯一，避免聚合写入竞争。
    def test_parallel_trajectories_preserve_all_records(self):
        # 把测试字典写成临时 JSON，同时创建父目录；不会改动仓库已有实验产物。
        def write(index):
            return record_checkpoint(self.root, f"parallel-{index}", 0, [self.fact], None, self.text)
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(write, range(12)))
        rows = [json.loads(line) for line in (self.root / "lost_content.jsonl").read_text().splitlines()]
        self.assertEqual(len(rows), 12)
        self.assertEqual(len({row["case_id"] for row in rows}), 12)

    # 已记录后不能插入更早轮次或更换同轨迹的事实定义，否则逐轮比较会失去意义。
    def test_changing_fact_key_or_out_of_order_round_refused(self):
        self.record(0, None, self.text)
        self.record(2, self.text, "背景。")
        with self.assertRaisesRegex(ValueError, "ascending"):
            self.record(1, self.text, "背景。")
        changed = dict(self.fact, text="different original")
        with self.assertRaisesRegex(ValueError, "facts changed"):
            record_checkpoint(self.root, "trajectory-01", 3, [changed], self.text, self.text)

    # 拒绝路径穿越、非法轮次和非文本输入，防止诊断账本写错位置或产生含糊记录。
    def test_safe_path_and_input_validation(self):
        for identifier in ["../outside", "/absolute", "..", "a/b", ""]:
            with self.assertRaises(ValueError):
                record_checkpoint(self.root, identifier, 0, [self.fact], None, self.text)
        for k in [-1, True, "1"]:
            with self.assertRaises(ValueError):
                self.record(k, None, self.text)
        with self.assertRaises(TypeError):
            self.record(0, None, [self.text])


if __name__ == "__main__":
    unittest.main()
