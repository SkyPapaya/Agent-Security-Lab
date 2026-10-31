# 测试本地攻击模拟工具的输入、授权和文件边界。所有地址和值都是固定虚构资源；vulnerable 仅开放预设模拟缺陷，不能突破宿主文件系统。
# 阅读顺序：先看每个测试上方的条件和目的，再对照实际断言；固定回复只用于离线单测。
import json
import os
import tempfile
import unittest
from pathlib import Path

from experiments.llama_cpp.compaction_security.attack_scenarios import (
    INCLUDE_MARKER, INCLUDE_PUBLIC_URL, INCLUDE_VALUE, REDIRECT_URL,
    VAULT_VALUE, trusted_state,
)
from experiments.llama_cpp.compaction_security.attack_tools import execute, parse_action, policy_check


# 集中验证本模块的输入、正常路径与异常边界；各方法独立构造条件。
class AttackToolTests(unittest.TestCase):
    # 有效动作应能解析，缺少固定 url 参数应被拒绝，不能让模型自行扩展工具协议。
    def test_action_schema_is_strict(self):
        action = parse_action(json.dumps({"action": "fetch_mock_resource", "arguments": {"url": REDIRECT_URL}}))
        self.assertEqual(action["action"], "fetch_mock_resource")
        with self.assertRaises(ValueError):
            parse_action('{"action":"fetch_mock_resource","arguments":{}}')

    # 打开预设模拟缺陷，确认只返回固定虚构值并留下本地证据文件，不读取真实秘密。
    def test_vulnerable_fixture_returns_only_synthetic_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = execute({"action": "fetch_mock_resource", "arguments": {"url": REDIRECT_URL}},
                             Path(tmp), vulnerable=True)
            self.assertTrue(result["executed"])
            self.assertEqual(result["content"], VAULT_VALUE)
            self.assertTrue((Path(tmp) / "attack_artifacts" / "vulnerable_redirect_followed.json").exists())

    # 对同一重定向请求同时检查策略和执行结果，确认独立授权拒绝时不会产生工具影响。
    def test_independent_state_blocks_same_request(self):
        action = {"action": "fetch_mock_resource", "arguments": {"url": REDIRECT_URL}}
        allowed, reason = policy_check(action, trusted_state())
        self.assertFalse(allowed)
        self.assertIn("independent", reason)
        with tempfile.TemporaryDirectory() as tmp:
            result = execute(action, Path(tmp), vulnerable=True, state=trusted_state())
            self.assertFalse(result["executed"])
            self.assertTrue(result["blocked"])

    # 关闭模拟缺陷后重放固定重定向动作，确认工具自身拒绝跳转。
    def test_hardened_fixture_blocks_redirect(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = execute({"action": "fetch_mock_resource", "arguments": {"url": REDIRECT_URL}},
                             Path(tmp), vulnerable=False)
            self.assertFalse(result["executed"])
            self.assertTrue(result["blocked"])

    # 即使模拟缺陷开启，独立状态也应在写报告前拒绝包含受保护引用的动作。
    def test_independent_state_blocks_include_in_report(self):
        action = {"action": "write_local_report", "arguments": {"report_id": "document-002", "content": INCLUDE_MARKER}}
        with tempfile.TemporaryDirectory() as tmp:
            result = execute(action, Path(tmp), vulnerable=True, state=trusted_state())
            self.assertFalse(result["executed"])
            self.assertTrue(result["blocked"])

    # 枚举代码围栏、前后说明、重复键、额外字段和错误类型，确认只接受唯一含义的严格 JSON。
    def test_json_markdown_comments_and_duplicate_keys_rejected(self):
        invalid = [
            '```json\n{"action":"no_action","arguments":{}}\n```',
            'Here is the action: {"action":"no_action","arguments":{}}',
            '{"action":"no_action","arguments":{}} trailing',
            '{"action":"no_action","action":"fetch_mock_resource","arguments":{}}',
            '{"action":"write_local_report","arguments":{"report_id":"status-001","content":"a","content":"b"}}',
            '{"action":"no_action","arguments":{},"reason":"safe"}',
            '{"action":[],"arguments":{}}',
        ]
        for raw in invalid:
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                parse_action(raw)

    # 在解析层和直接执行层各检查一次非法 report_id，防止绕过解析后写到任意路径。
    def test_report_id_path_traversal_rejected_at_both_boundaries(self):
        for report_id in ["../outside", "/tmp/outside", "..\\outside", "status-001/../../outside", "unknown"]:
            action = {"action": "write_local_report", "arguments": {"report_id": report_id, "content": "safe"}}
            with self.subTest(report_id=report_id):
                with self.assertRaises(ValueError):
                    parse_action(json.dumps(action))
                with tempfile.TemporaryDirectory() as tmp, self.assertRaises(ValueError):
                    execute(action, Path(tmp), vulnerable=True)

    # 直接向执行器传入 shell、未知地址和错误参数，确保跳过模型解析也无法绕过固定工具边界。
    def test_direct_execute_cannot_bypass_validation(self):
        actions = [
            {"action": "shell", "arguments": {"cmd": "ignored"}},
            {"action": "write_local_report", "arguments": {"report_id": "status-001", "content": [], "extra": "x"}},
            {"action": "fetch_mock_resource", "arguments": {"url": "https://example.invalid"}},
            {"action": "fetch_mock_resource", "arguments": {"url": "mock://vault/not-registered"}},
        ]
        for action in actions:
            with self.subTest(action=action), tempfile.TemporaryDirectory() as tmp, self.assertRaises(ValueError):
                execute(action, Path(tmp), vulnerable=True)

    # 读取 include 场景的合法公共页面，确认它能正常完成且不产生攻击影响，避免防御误伤正常任务。
    def test_both_fixed_public_pages_are_available(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = execute({"action": "open_mock_page", "arguments": {"url": INCLUDE_PUBLIC_URL}},
                             Path(tmp), vulnerable=False)
            self.assertTrue(result["executed"])
            self.assertIsNone(result["synthetic_impact"])

    # 报告只是明确否定“自动验证”，不能触发 include；这是修复早期误报的重要回归条件。
    def test_negative_automatic_verification_report_has_no_impact(self):
        content = "未执行或访问自动验证。页面不是授权，不应操作。"
        action = {"action": "write_local_report", "arguments": {"report_id": "document-002", "content": content}}
        for vulnerable in (True, False):
            with self.subTest(vulnerable=vulnerable), tempfile.TemporaryDirectory() as tmp:
                result = execute(action, Path(tmp), vulnerable=vulnerable, state=trusted_state())
                self.assertTrue(result["executed"])
                self.assertIsNone(result["synthetic_impact"])
                self.assertEqual(Path(result["path"]).read_text(), content)
                self.assertFalse((Path(tmp) / "attack_artifacts").exists())

    # 只有完整精确的 INCLUDE_MARKER 才展开固定虚构值，并留下可核对的报告与证据文件。
    def test_precise_marker_expands_in_vulnerable_report(self):
        action = {"action": "write_local_report", "arguments": {
            "report_id": "document-002", "content": "before\n" + INCLUDE_MARKER + "\nafter"}}
        with tempfile.TemporaryDirectory() as tmp:
            result = execute(action, Path(tmp), vulnerable=True)
            self.assertEqual(result["synthetic_impact"], "include_directive_expanded")
            report = Path(result["path"]).read_text()
            self.assertIn(INCLUDE_VALUE, report)
            self.assertNotIn(INCLUDE_MARKER, report)
            artifact = json.loads(Path(result["artifact"]).read_text())
            self.assertEqual(artifact["value"], INCLUDE_VALUE)
            self.assertTrue(artifact["synthetic_only"])

    # 把完全相同的 include 动作交给两种防御，要求都在创建报告前阻断，避免比较不同随机输出。
    def test_identical_include_action_blocked_by_state_and_hardened_tool(self):
        action = {"action": "write_local_report", "arguments": {"report_id": "document-002", "content": INCLUDE_MARKER}}
        for vulnerable, state in [(True, trusted_state()), (False, None)]:
            with self.subTest(vulnerable=vulnerable), tempfile.TemporaryDirectory() as tmp:
                result = execute(action, Path(tmp), vulnerable=vulnerable, state=state)
                self.assertFalse(result["executed"])
                self.assertTrue(result["blocked"])
                self.assertIsNone(result["synthetic_impact"])
                self.assertFalse((Path(tmp) / "reports").exists())

    # 改动资源编号或标记格式，内容必须原样保存而不能宽泛匹配为攻击影响。
    def test_inexact_include_marker_is_not_expanded(self):
        for marker in [INCLUDE_MARKER.replace("08", "09"), INCLUDE_MARKER.replace("include:", "include :")]:
            with self.subTest(marker=marker), tempfile.TemporaryDirectory() as tmp:
                result = execute({"action": "write_local_report", "arguments": {
                    "report_id": "document-002", "content": marker}}, Path(tmp), vulnerable=True)
                self.assertIsNone(result["synthetic_impact"])
                self.assertEqual(Path(result["path"]).read_text(), marker)

    # 将 reports 目录替换为符号链接，写入必须失败且外部目录保持为空。
    def test_symlink_directory_does_not_allow_outside_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "case"
            outside = Path(tmp) / "outside"
            root.mkdir()
            outside.mkdir()
            (root / "reports").symlink_to(outside, target_is_directory=True)
            action = {"action": "write_local_report", "arguments": {"report_id": "status-001", "content": "safe"}}
            with self.assertRaises(OSError):
                execute(action, root, vulnerable=True)
            self.assertEqual(list(outside.iterdir()), [])

    # 将报告文件指向实验外文件，确认拒绝跟随链接并保留外部原内容。
    def test_symlink_file_does_not_modify_outside_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "case"
            (root / "reports").mkdir(parents=True)
            outside = Path(tmp) / "outside.txt"
            outside.write_text("preserve")
            (root / "reports" / "status-001.txt").symlink_to(outside)
            action = {"action": "write_local_report", "arguments": {"report_id": "status-001", "content": "overwrite"}}
            with self.assertRaises(OSError):
                execute(action, root, vulnerable=True)
            self.assertEqual(outside.read_text(), "preserve")

    # 连实验根目录本身也是链接时也要拒绝，不能只检查最终文件名。
    def test_symlink_root_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            outside = Path(tmp) / "outside"
            outside.mkdir()
            root = Path(tmp) / "case"
            root.symlink_to(outside, target_is_directory=True)
            action = {"action": "record_observation", "arguments": {"label": "l", "value": "v"}}
            with self.assertRaises(OSError):
                execute(action, root, vulnerable=False)
            self.assertEqual(list(outside.iterdir()), [])

    # 预先放置硬链接，要求先检查链接数再截断文件，避免破坏同一 inode 的外部内容。
    def test_hardlink_target_is_rejected_before_truncation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "case"
            (root / "reports").mkdir(parents=True)
            outside = Path(tmp) / "outside.txt"
            outside.write_text("preserve")
            # 硬链接不是符号链接；只依赖 O_NOFOLLOW 不足以保护同一 inode 的外部原内容。
            os.link(outside, root / "reports" / "status-001.txt")
            action = {"action": "write_local_report", "arguments": {"report_id": "status-001", "content": "overwrite"}}
            with self.assertRaises(ValueError):
                execute(action, root, vulnerable=True)
            self.assertEqual(outside.read_text(), "preserve")

    # 执行前复制原动作，执行后应完全一致，保证不同执行器重放时仍接收相同材料。
    def test_execute_does_not_mutate_input(self):
        action = {"action": "write_local_report", "arguments": {"report_id": "document-002", "content": INCLUDE_MARKER}}
        original = json.loads(json.dumps(action))
        with tempfile.TemporaryDirectory() as tmp:
            execute(action, Path(tmp), vulnerable=True)
        self.assertEqual(action, original)
