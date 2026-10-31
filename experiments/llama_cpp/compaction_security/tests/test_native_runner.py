# 测试早期 Claude 原生压缩编排的隔离、边界识别和离线测量流程。不会启动真实 Claude 或调用真实模型。
# 阅读顺序：先看每个测试上方的条件和目的，再对照实际断言；固定回复只用于离线单测。
import copy
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from experiments.llama_cpp.compaction_security.native_retention import runner


# 集中验证本模块的输入、正常路径与异常边界；各方法独立构造条件。
class NativeRunnerTests(unittest.TestCase):
    # 构造带旧认证和代理的环境，实验副本应移除它们并将各模型档位统一路由，父环境不变。
    def test_isolation_removes_old_auth_and_routes_all_models(self):
        parent = {"PATH": "/bin", "HOME": "/normal", "ANTHROPIC_AUTH_TOKEN": "stale",
                  "ANTHROPIC_API_KEY": "old", "HTTPS_PROXY": "bad", "CLAUDECODE": "1"}
        with patch.dict(os.environ, parent, clear=True):
            env = runner.isolated_env("http://127.0.0.1:123", Path("/private/config"), "test/turn")
            self.assertEqual(dict(os.environ), parent)
        self.assertNotIn("ANTHROPIC_AUTH_TOKEN", env)
        self.assertNotIn("HTTPS_PROXY", env)
        self.assertNotIn("CLAUDECODE", env)
        self.assertEqual(env["HOME"], "/normal")
        self.assertEqual(env["ANTHROPIC_DEFAULT_HAIKU_MODEL"], runner.MODEL)
        self.assertEqual(env["ANTHROPIC_CUSTOM_HEADERS"], "X-Lab-Label: test/turn")

    # 检查启动参数关闭工具和日常配置来源，不添加跳过权限参数，也不替换框架系统提示。
    def test_native_command_disables_tools_and_original_config(self):
        args = runner.claude_args("claude", Path("/lab"), 100000, "sid", True)
        self.assertEqual(args[args.index("--tools") + 1], "")
        self.assertEqual(args[args.index("--setting-sources") + 1], "")
        self.assertIn("--strict-mcp-config", args)
        self.assertIn("--session-id", args)
        self.assertNotIn("--system-prompt", args)
        self.assertNotIn("--dangerously-skip-permissions", args)

    # 探针只在深拷贝历史上追加问题，禁用工具且不修改主会话请求。
    def test_probe_has_no_parent_mutations_or_tools(self):
        effective = {"system": [{"type": "text", "text": "system"}],
                     "tools": [{"name": "Read"}],
                     "messages": [{"role": "user", "content": "history"}]}
        original = copy.deepcopy(effective)
        probe = runner.probe_payload(effective, effective["messages"], "question")
        self.assertEqual(original, effective)
        self.assertEqual(len(probe["messages"]), 2)
        self.assertNotIn("tools", probe)
        self.assertFalse(probe["stream"])

    # compact_boundary 才计为压缩事件；摘要正文与普通 retry 事件要单独识别。
    def test_summary_and_boundaries_distinguished(self):
        records = [{"type": "system", "subtype": "compact_boundary"},
                   {"isCompactSummary": True, "message": {"content": "summary"}},
                   {"type": "system", "subtype": "retry"}]
        self.assertEqual(len(runner.boundaries(records)), 1)
        self.assertEqual(runner.latest_summary(records), "summary")

    # 用交错流事件验证只拼接文本回复，不混入 thinking 或依赖事件固定排列。
    def test_response_link_uses_text_not_thinking_or_event_order(self):
        import json
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "response.sse"
            events = [
                {"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "not answer"}},
                {"type": "content_block_start", "content_block": {"type": "text", "text": ""}},
                {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "已"}},
                {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "记录"}},
            ]
            path.write_text("\n".join("data: " + json.dumps(e) for e in events))
            self.assertEqual(runner.captured_response_text({"response_path": str(path),
                                                          "response_content_type": "text/event-stream"}), "已记录")

    # 完成和失败轨迹分开计数，实际观测轮数只来自有记录的完成边界。
    def test_progress_keeps_failure_separate(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            runner.write_json(root / "trajectories/a/state.json", {
                "status": "completed", "turn": 10, "round": 2, "checkpoints": [0, 1, 2]})
            runner.write_json(root / "trajectories/b/state.json", {
                "status": "failed", "turn": 1, "round": 0, "checkpoints": [], "error": "API"})
            s = runner.status(root)
            self.assertEqual(s["completed"], 1)
            self.assertEqual(s["failed"], 1)
            self.assertEqual(s["verified_compactions_observed"], 2)

    # 通过离线网关走基线和压缩后测量，检查快照、语义评审和轮数保存且探针不污染父历史。
    def test_recorded_checkpoint_pipeline_keeps_probes_out_of_parent(self):
        import json

        # 本用例的离线网关替身，按探针标签返回固定答案或判断，不访问真实服务。
        class OfflineGateway:
            # 初始化本用例替身的记录容器和可选响应，不建立真实连接。
            def __init__(self):
                self.calls = []

            # 按 judge 标签返回固定评审，其余返回固定问答，仅供检查点管线测试。
            def post(self, payload, label):
                self.calls.append((payload, label))
                if "judge" in label:
                    raw = {"judgments": [{"id": f["id"], "state": "semantic",
                                          "evidence": f["text"]} for f in facts]}
                else:
                    raw = {"answers": [{"id": f["id"], "answer": f["answer"]} for f in facts]}
                return {"content": [{"type": "text", "text": json.dumps(raw, ensure_ascii=False)}]}

        with tempfile.TemporaryDirectory() as temp:
            gateway = OfflineGateway()
            tr = runner.Trajectory(Path(temp), {"id": "offline_only", "history": 0,
                                                "layout": 0, "threshold": 100000}, gateway, "unused", 1)
            facts = tr.corpus["facts"]
            original_messages = [{"role": "user", "content": tr.corpus["intro"]}]
            for m in tr.corpus["messages"]:
                original_messages.extend([m, {"role": "assistant", "content": "已记录"}])
            # 先测未压缩基线，保留原始全历史的深拷贝，用于稍后核对探针没有污染它。
            tr.state["full_messages"] = copy.deepcopy(original_messages)
            effective_path = tr.root / "input.json"
            tr.state["effective_request"] = str(effective_path)
            runner.write_json(effective_path, {"messages": original_messages[:-1]})
            before = copy.deepcopy(tr.state["full_messages"])
            with patch.object(runner, "transcript_records", return_value=[]):
                measured = tr.checkpoint(0)
            self.assertTrue(measured["baseline_capability"]["passed"])
            self.assertEqual(before, tr.state["full_messages"])
            saved_full = runner.read_json(tr.root / "checkpoints/0/full_messages.json")
            self.assertEqual(saved_full, original_messages[:-1])
            # 再人工准备包含全部事实的摘要及边界事件，只测试检查点管线能否正确记账。
            summary = "\n".join(f["text"] for f in facts)
            runner.write_json(effective_path, {"messages": [{"role": "user", "content": summary}]})
            records = [{"type": "system", "subtype": "compact_boundary", "compactMetadata": {}},
                       {"isCompactSummary": True, "message": {"content": summary}}]
            with patch.object(runner, "transcript_records", return_value=records):
                measured = tr.checkpoint(1)
            self.assertEqual(len(gateway.calls), 6)
            self.assertEqual(tr.state["round"], 1)
            self.assertEqual(tr.state["checkpoints"], [0, 1])
            self.assertFalse(measured["summary_semantics"]["validation_error"])
            self.assertFalse(measured["effective_semantics"]["validation_error"])
            self.assertEqual(before, tr.state["full_messages"])


if __name__ == "__main__":
    unittest.main()
