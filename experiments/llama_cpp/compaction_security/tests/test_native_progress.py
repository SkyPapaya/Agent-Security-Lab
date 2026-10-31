# 测试进度渲染和只读观察器，防止失败轨迹充当完成量、未知分母变成 100%，或观察进度时改写实验。
# 阅读顺序：先看每个测试上方的条件和目的，再对照实际断言；固定回复只用于离线单测。
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from experiments.llama_cpp.compaction_security.native_retention.progress import (
    ProgressDisplay, render_progress, watch,
)


# 在内存中模拟交互输出流，捕获回车与换行供断言检查。
class TTYStream(io.StringIO):
    # 让内存流表现为交互终端，便于验证单行刷新而不依赖真实终端。
    def isatty(self):
        return True


# 集中验证本模块的输入、正常路径与异常边界；各方法独立构造条件。
class ProgressRenderingTests(unittest.TestCase):
    # 使用 10 轮配置和失败轨迹核对分母，进度只能按实际压缩边界增长，不能因运行结束填满。
    def test_rounds_are_configurable_and_failed_runs_do_not_fill_bar(self):
        state = {"status": "failed", "runs_total": 2, "runs_completed": 0,
                 "runs_failed": 2, "rounds_per_run": 10,
                 "compactions_completed": 3, "compactions_total": 20,
                 "current_trajectory": "h01", "phase": "probe"}
        line = render_progress(state, width=20)
        self.assertIn("[###-----------------]", line)
        self.assertIn("15.0%", line)
        self.assertIn("压缩 3/20", line)
        self.assertIn("每轨迹 10 轮", line)
        self.assertIn("失败 2", line)
        self.assertIn("阶段 probe", line)
        self.assertNotIn("100.0%", line)

    # 兼容旧进度字段，输入 token 包括缓存读取量，且完成数和失败数分开显示。
    def test_legacy_state_and_cached_token_accounting(self):
        state = {"trajectories_planned": 4, "completed": 1, "failed": 1,
                 "verified_compactions_observed": 6, "rounds_per_run": 3,
                 "usage": {"requests": 15, "input_tokens": 100,
                           "cache_read_input_tokens": 1000, "output_tokens": 50}}
        line = render_progress(state, width=10)
        self.assertIn("50.0%", line)
        self.assertIn("轨迹完成 1/4 失败 1", line)
        self.assertIn("tokens 输入 1,100 / 输出 50", line)

    # 没有计划总量时应显示问号和未知百分比，不能因已观测到若干轮就显示完成。
    def test_missing_denominator_stays_unknown(self):
        line = render_progress({"verified_compactions_observed": 6})
        self.assertIn("压缩 6/?", line)
        self.assertIn("-- %", line)
        self.assertNotIn("100.0%", line)

    # 负数、非数字和无穷值只在显示投影中处理，不能修改原实验状态；非法条宽应拒绝。
    def test_invalid_numbers_are_zero_and_state_is_not_mutated(self):
        state = {"compactions_completed": -1, "compactions_total": "invalid",
                 "runs_failed": float("nan"), "elapsed_seconds": float("inf")}
        before = copy.deepcopy(state)
        line = render_progress(state)
        self.assertIn("压缩 0/?", line)
        self.assertIn("耗时 00:00:00", line)
        self.assertEqual(state.keys(), before.keys())
        self.assertEqual(state["compactions_completed"], -1)
        for width in (0, -1, 201, True, 2.5):
            with self.assertRaises(ValueError):
                render_progress({}, width=width)

    # 阶段和轨迹名含换行、回车或控制符时要清洗，避免输出破坏终端显示。
    def test_labels_do_not_inject_terminal_controls(self):
        line = render_progress({"phase": "probe\n\x1b[2J", "current_trajectory": "test\rnew"})
        self.assertNotIn("\n", line)
        self.assertNotIn("\r", line)
        self.assertNotIn("\x1b", line)

    # 真实观测超过计划时条形图最多填满，但原始 12/10 计数仍显示以暴露异常。
    def test_overrun_does_not_overflow_bar_and_keeps_raw_count(self):
        line = render_progress({"compactions_completed": 12, "compactions_total": 10}, width=5)
        self.assertIn("[#####]", line)
        self.assertIn("压缩 12/10", line)


# 集中验证本模块的输入、正常路径与异常边界；各方法独立构造条件。
class ProgressDisplayTests(unittest.TestCase):
    # 交互终端用回车刷新，结束仅换行一次，并且不能关闭调用者提供的输出流。
    def test_tty_updates_single_line_and_close_once(self):
        stream = TTYStream()
        display = ProgressDisplay(stream=stream, min_interval=0)
        display.update({"compactions_completed": 1, "compactions_total": 2})
        display.update({"compactions_completed": 2, "compactions_total": 2})
        display.close()
        display.close()
        self.assertEqual(stream.getvalue().count("\r"), 2)
        self.assertEqual(stream.getvalue().count("\n"), 1)
        self.assertFalse(stream.closed)

    # 重定向到日志时忽略重复和仅耗时变化，避免每次刷新产生无意义新行。
    def test_non_tty_skips_elapsed_only_changes_and_unchanged_metrics(self):
        stream = io.StringIO()
        display = ProgressDisplay(stream=stream, min_interval=0)
        state = {"compactions_completed": 1, "compactions_total": 4, "elapsed_seconds": 1}
        display.update(state)
        display.update(state)
        display.update(dict(state, elapsed_seconds=9))
        display.update(dict(state, compactions_completed=2, elapsed_seconds=10))
        display.close()
        self.assertEqual(len(stream.getvalue().splitlines()), 2)
        self.assertNotIn("\r", stream.getvalue())

    # 用固定时钟验证刷新节流不会丢掉待显示变化，结束状态不能被节流延迟。
    def test_throttling_keeps_pending_change_and_terminal_state_is_immediate(self):
        stream = io.StringIO()
        display = ProgressDisplay(stream=stream, min_interval=1)
        with patch("experiments.llama_cpp.compaction_security.native_retention.progress.time.monotonic",
                   side_effect=[0, 0.1, 1.1, 1.2]):
            display.update({"phase": "start"})
            display.update({"phase": "probe"})
            display.update({"phase": "probe"})
            display.update({"phase": "stopped", "status": "stopped"})
        self.assertEqual(len(stream.getvalue().splitlines()), 3)
        self.assertIn("已停止", stream.getvalue())

    # 关闭显示时 update 和 close 都应无输出，不干扰安静模式。
    def test_disabled_display_has_no_output_or_close_effect(self):
        stream = TTYStream()
        display = ProgressDisplay(stream=stream, enabled=False)
        display.update({"status": "completed"})
        display.close()
        self.assertEqual(stream.getvalue(), "")


# 集中验证本模块的输入、正常路径与异常边界；各方法独立构造条件。
class WatchTests(unittest.TestCase):
    # 单次观察不存在的目录只返回 waiting，不得自行创建实验或输出文件。
    def test_once_missing_run_does_not_create_any_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "not-created"
            with patch("sys.stderr", new=io.StringIO()):
                result = watch(root, once=True)
            self.assertEqual(result["status"], "waiting")
            self.assertFalse(root.exists())

    # 完成、失败、中断等终态应立即退出观察，且不改写进度文件或新增目录内容。
    def test_terminal_states_return_without_sleeping_or_modifying_run(self):
        for status in ("completed", "failed", "stopped", "interrupted", "budget_stopped"):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                path = root / "progress.json"
                original = json.dumps({"status": status, "phase": "end"})
                path.write_text(original)
                with patch("sys.stderr", new=io.StringIO()), patch(
                    "experiments.llama_cpp.compaction_security.native_retention.progress.time.sleep"
                ) as sleep:
                    result = watch(root)
                self.assertEqual(result["status"], status)
                sleep.assert_not_called()
                self.assertEqual(path.read_text(), original)
                self.assertEqual([p.name for p in root.iterdir()], ["progress.json"])

    # 中断观察器只停止显示，不创建 STOP，也不改变正在运行实验的状态。
    def test_keyboard_interrupt_only_exits_watcher(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "progress.json"
            path.write_text('{"status":"running"}')
            original = path.read_bytes()
            with patch("sys.stderr", new=io.StringIO()), patch(
                "experiments.llama_cpp.compaction_security.native_retention.progress.time.sleep",
                side_effect=KeyboardInterrupt,
            ):
                result = watch(root)
            self.assertEqual(result["status"], "running")
            self.assertFalse((root / "STOP").exists())
            self.assertEqual(path.read_bytes(), original)

    # 旧进度缺少轮数时可只读协议补全显示分母，原始文件必须逐字保持不变。
    def test_legacy_protocol_rounds_used_without_rewriting_files(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "progress.json").write_text(json.dumps({
                "trajectories_planned": 2, "completed": 2, "failed": 0,
                "verified_compactions_observed": 10}))
            (root / "execution_protocol.json").write_text('{"rounds":5}')
            before = {p.name: p.read_bytes() for p in root.iterdir()}
            stream = io.StringIO()
            with patch("sys.stderr", new=stream):
                result = watch(root)
            self.assertEqual(result["rounds_per_run"], 5)
            self.assertIn("压缩 10/10", stream.getvalue())
            self.assertEqual({p.name: p.read_bytes() for p in root.iterdir()}, before)

    # 非法刷新间隔立即拒绝，读到尚未写完的 JSON 时返回 waiting 而非破坏实验。
    def test_interval_is_bounded_and_invalid_json_is_observable_wait(self):
        for value in (0, -1, True, float("nan"), 61, "1"):
            with self.subTest(interval=value), self.assertRaises(ValueError):
                watch(Path("unused"), interval=value, once=True)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "progress.json").write_text("{unfinished")
            with patch("sys.stderr", new=io.StringIO()):
                self.assertEqual(watch(root, once=True)["status"], "waiting")


if __name__ == "__main__":
    unittest.main()
