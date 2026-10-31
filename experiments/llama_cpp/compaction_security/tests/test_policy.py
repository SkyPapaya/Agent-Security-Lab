# 测试基础执行器的授权边界：未授权发送不能落盘，合法发送也只写入本地 mock_outbox。
# 阅读顺序：先看每个测试上方的条件和目的，再对照实际断言；固定回复只用于离线单测。
import tempfile
import unittest
from pathlib import Path
from experiments.llama_cpp.compaction_security.config import SCENARIOS
from experiments.llama_cpp.compaction_security.history_builder import load_scenario, trusted_state
from experiments.llama_cpp.compaction_security.policies import authorize
from experiments.llama_cpp.compaction_security.schemas import Action, parse_action
from experiments.llama_cpp.compaction_security.tools import execute


# 集中验证本模块的输入、正常路径与异常边界；各方法独立构造条件。
class PolicyTests(unittest.TestCase):
    # 未批准接收方同时在策略层和执行层被拒绝，mock_outbox 不应创建。
    def test_denies_send_and_keeps_outbox_empty(self):
        state = trusted_state(load_scenario(SCENARIOS / "multistep_workflow.json"))
        action = Action("send_mock_email", {"file_id": "demo-report-001", "recipient": "unapproved-recipient"})
        self.assertFalse(authorize(action, state)[0])
        with tempfile.TemporaryDirectory() as temp:
            result = execute(action, state, Path(temp))
            self.assertFalse(result["executed"])
            self.assertFalse((Path(temp) / "mock_outbox").exists())

    # 正常授权发送必须可完成，但结果仅是临时目录内的 message.json，不涉及 SMTP。
    def test_authorized_send_is_local(self):
        state = trusted_state(load_scenario(SCENARIOS / "benign_control.json"))
        action = Action("send_mock_email", {"file_id": "demo-report-001", "recipient": "approved-recipient"})
        with tempfile.TemporaryDirectory() as temp:
            result = execute(action, state, Path(temp))
            self.assertTrue(result["executed"])
            self.assertTrue((Path(temp) / "mock_outbox" / "message.json").exists())

    # 可信状态要记录合成文件来源，同时拒绝路径穿越、未知工具和额外动作字段。
    def test_file_provenance_and_schema(self):
        state = trusted_state(load_scenario(SCENARIOS / "multistep_workflow.json"))
        self.assertEqual(state.file_sources["demo-report-001"], "synthetic local dataset")
        with self.assertRaises(ValueError):
            parse_action('{"action":"send_mock_email","arguments":{"file_id":"../../etc/passwd","recipient":"approved-recipient"}}')
        with self.assertRaises(ValueError):
            parse_action('{"action":"shell","arguments":{}}')
        with self.assertRaises(ValueError):
            parse_action('{"action":"no_action","arguments":{},"extra":1}')
