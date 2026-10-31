# 测试旧实验产物的离线损失记录导出。导出只生成复核材料，不重跑模型，也不修改旧输入和原始日志。
# 阅读顺序：先看每个测试上方的条件和目的，再对照实际断言；固定回复只用于离线单测。
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from experiments.llama_cpp.compaction_security.native_retention.export_losses import export_legacy


# 集中验证本模块的输入、正常路径与异常边界；各方法独立构造条件。
class LegacyLossExportTests(unittest.TestCase):
    # 每个用例建立自己的临时材料和目录，避免前一个用例状态或已有实验记录影响断言。
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "old_run"
        self.root.mkdir()
        self.tr = self.root / "trajectories/h00_t12000_l0"
        self.tr.mkdir(parents=True)
        self.fact = {"id": "f-1", "category": "exact_value", "text": "虚构批次编号是例簿-31415。",
                     "answer": "例簿-31415", "aliases": []}
        self.write(self.tr / "ground_truth.json", {"facts": [self.fact]})
        self.write(self.root / "execution_protocol.json", {
            "model": "fictional-legacy-model", "framework": "fictional-framework-v1",
            "cells": [{"id": self.tr.name, "threshold": 12000}]})

    # 清理本用例创建的临时目录，保留仓库原有实验和日志。
    def tearDown(self):
        self.temp.cleanup()

    # 把测试字典写成临时 JSON，同时创建父目录；不会改动仓库已有实验产物。
    @staticmethod
    def write(path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value, ensure_ascii=False))

    # 在临时旧轨迹中生成有效请求快照，并为非零轮增加模拟边界记录。
    def checkpoint(self, k, text=None, **extra):
        payload = {"model": "fictional-legacy-model", "messages": [{"role": "user", "content": text or ""}], **extra}
        path = self.tr / "checkpoints" / str(k) / "effective_request.json"
        self.write(path, payload)
        if k:
            self.write(path.parent / "native_boundary.json", {"type": "system", "subtype": "compact_boundary"})
        return path

    # 离线导出两轮快照，核对原句、引用、阈值和来源标记，并用哈希确认原始 JSON 未改写。
    def test_two_round_export_records_exact_lost_content_and_source_evidence(self):
        before = self.checkpoint(0, self.fact["text"])
        after = self.checkpoint(1, "后续仅剩一般任务。")
        hashes = {str(path): hashlib.sha256(path.read_bytes()).hexdigest()
                  for path in self.root.rglob("*.json")}
        progress = []
        result = export_legacy(self.root, progress=progress.append)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["checkpoints_exported"], 2)
        self.assertEqual(result["counts"]["first_marker_missing"], 1)
        self.assertFalse(result["is_new_codex_experiment"])
        cp = json.loads((self.root / "loss_review/loss_checkpoints" / self.tr.name / "1.json").read_text())
        self.assertEqual(cp["metadata"]["before_snapshot"], str(before))
        self.assertEqual(cp["metadata"]["after_snapshot"], str(after))
        self.assertEqual(cp["metadata"]["threshold_tokens"], 12000)
        self.assertEqual(cp["metadata"]["source_backend"], "claude_deepseek_legacy")
        self.assertIn(self.fact["text"], (self.root / "loss_review/lost_content.md").read_text())
        self.assertEqual(len(progress), 2)
        self.assertTrue(all(hashlib.sha256(Path(p).read_bytes()).hexdigest() == digest for p, digest in hashes.items()))

    # 同一旧目录导出两次，统计和 case_id 必须保持一致，防止重复导出放大样本数。
    def test_repeat_export_has_no_duplicate_rows(self):
        self.checkpoint(0, self.fact["text"])
        self.checkpoint(1, "省略。")
        one = export_legacy(self.root)
        two = export_legacy(self.root)
        self.assertEqual(one["counts"], two["counts"])
        lines = (self.root / "loss_review/lost_content.jsonl").read_text().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(len({json.loads(line)["case_id"] for line in lines}), 2)

    # 中间 JSON 损坏应单独记录失败；后一轮不能跨过缺口声称第一次丢失发生在该轮。
    def test_corrupt_checkpoint_is_separate_failure_and_does_not_bridge(self):
        self.checkpoint(0, self.fact["text"])
        broken = self.tr / "checkpoints/1/effective_request.json"
        broken.parent.mkdir(parents=True)
        broken.write_text("{bad JSON")
        self.checkpoint(2, "省略。")
        result = export_legacy(self.root)
        self.assertEqual(result["status"], "complete_with_errors")
        self.assertEqual(result["checkpoints_skipped"], 1)
        self.assertEqual(result["failures"][0]["round"], 1)
        cp = json.loads((self.root / "loss_review/loss_checkpoints" / self.tr.name / "2.json").read_text())
        self.assertEqual(cp["records"][0]["state"], "marker_absent_transition_unknown")
        self.assertNotIn("first_marker_missing", result["counts"])

    # 分别构造缺快照和缺真值的目录，确认按检查点/轨迹层级报告错误而非静默跳过。
    def test_missing_snapshot_and_ground_truth_are_reported(self):
        self.checkpoint(0, self.fact["text"])
        (self.tr / "checkpoints/1").mkdir()
        missing_tr = self.root / "trajectories/missing-truth/checkpoints/0"
        missing_tr.mkdir(parents=True)
        result = export_legacy(self.root)
        self.assertEqual(result["checkpoints_skipped"], 2)
        self.assertEqual(len(result["failures"]), 2)
        self.assertEqual({row["scope"] for row in result["failures"]}, {"checkpoint", "trajectory"})

    # 快照含不可读压缩状态时，词面观察应记为 unknown，不能认定内容已丢失。
    def test_opaque_payload_never_becomes_lexical_loss(self):
        self.checkpoint(0, self.fact["text"])
        self.checkpoint(1, "可见摘要不含目标。", input=[{"type": "compaction", "encrypted_content": "fictional-opaque-state"}])
        result = export_legacy(self.root)
        self.assertEqual(result["counts"]["observation_unknown"], 1)
        self.assertNotIn("first_marker_missing", result["counts"])

    # 空元数据或空加密字段不等于不可观测，仍应对实际可见文本进行词面比较。
    def test_empty_compaction_metadata_does_not_hide_visible_input(self):
        self.checkpoint(0, self.fact["text"], compaction=None)
        self.checkpoint(1, "省略。", encrypted_content="")
        result = export_legacy(self.root)
        self.assertEqual(result["counts"]["first_marker_missing"], 1)

    # 第 0 轮后直接出现第 3 轮，缺少相邻前态，必须标注观察缺口而不定位首次丢失。
    def test_nonadjacent_round_without_snapshot_uses_unknown_before(self):
        self.checkpoint(0, self.fact["text"])
        self.checkpoint(3, "省略。")
        result = export_legacy(self.root)
        self.assertNotIn("first_marker_missing", result["counts"])
        cp = json.loads((self.root / "loss_review/loss_checkpoints" / self.tr.name / "3.json").read_text())
        self.assertTrue(cp["metadata"]["observation_gap"])

    # 检查单轨迹筛选、自定义输出和非法路径；导出范围应明确，不能跨到目录外。
    def test_optional_trajectory_selection_and_custom_output(self):
        self.checkpoint(0, self.fact["text"])
        another = self.root / "trajectories/other"
        self.write(another / "ground_truth.json", {"facts": [self.fact]})
        self.write(another / "checkpoints/0/effective_request.json", {"messages": []})
        custom = Path(self.temp.name) / "custom_review"
        result = export_legacy(self.root, custom, trajectory_id=self.tr.name)
        self.assertEqual(result["trajectories_discovered"], 1)
        self.assertEqual(result["checkpoints_exported"], 1)
        self.assertTrue((custom / "lost_content.md").is_file())
        with self.assertRaises(FileNotFoundError):
            export_legacy(self.root, custom, trajectory_id="no-such-trajectory")
        with self.assertRaises(ValueError):
            export_legacy(self.root, custom, trajectory_id="../outside")


if __name__ == "__main__":
    unittest.main()
