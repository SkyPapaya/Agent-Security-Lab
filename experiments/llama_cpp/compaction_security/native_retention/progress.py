# 读取实验已保存的 progress.json 并显示终端进度；本模块不调用模型，也不改变运行状态。
# 进度条计算已观察到的压缩次数，失败轨迹不会冒充完成压缩；Ctrl+C 只退出查看器。

"""Read-only terminal progress for recorded compaction experiments.

``python3 -m experiments.llama_cpp.compaction_security.native_retention.progress RUN_DIR``
watches ``progress.json``.  Interrupting the watcher never signals the runner.
This module does not access credentials, call a model, or modify run artifacts.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import sys
import time
from typing import Any, TextIO


TERMINAL_STATUSES = frozenset({
    "completed", "completed_with_failures", "failed", "stopped", "interrupted",
    "budget_stopped", "cancelled", "error",
})
_STATUS_LABELS = {
    "running": "运行中", "starting": "启动中", "waiting": "等待中",
    "completed": "已完成", "completed_with_failures": "结束（含失败）",
    "failed": "失败", "stopped": "已停止", "interrupted": "已中断",
    "budget_stopped": "预算触限停止", "stopping": "停止中",
    "cancelled": "已取消", "error": "错误",
}


# 把可接受的非负有限数转成浮点值；缺失、布尔、NaN 或无穷大统一当作 0 显示。
def _number(value: Any) -> float:
    if isinstance(value, bool):
        return 0.0
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return value if math.isfinite(value) and value >= 0 else 0.0


# 把已经清洗过的数值转成整数计数，避免显示负进度或非法字符。
def _count(value: Any) -> int:
    return int(_number(value))


# 去掉控制字符并压成单行，避免来自状态文件的阶段名污染终端显示。
def _label(value: Any) -> str:
    """Keep untrusted phase names on one terminal line without control codes."""
    return " ".join("".join(c if c.isprintable() else " " for c in str(value or "")).split())


# 兼容新旧 runner 的字段名，返回一套显示用的统一状态；不回写原文件。
# 读取旧版 token 分项时合并缓存用量；这些数只用于进度显示，不是账单。
def _normalise(state: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(state, dict):
        raise TypeError("progress state must be a dictionary")
    usage = state.get("usage") if isinstance(state.get("usage"), dict) else {}
    runs_total = _count(state.get("runs_total", state.get("trajectories_planned", 0)))
    completed = _count(state.get("runs_completed", state.get("completed", 0)))
    failed = _count(state.get("runs_failed", state.get("failed", 0)))
    rounds = _count(state.get("rounds_per_run", state.get("rounds", 0)))
    compactions_total = _count(state.get("compactions_total", runs_total * rounds))
    compactions = _count(state.get("compactions_completed", state.get("verified_compactions_observed", 0)))
    status = _label(state.get("status", "running")).casefold() or "running"
    if "status" not in state and runs_total and completed + failed >= runs_total:
        status = "completed_with_failures" if failed else "completed"
    total_input = usage.get("total_input_tokens")
    if total_input is None:
        total_input = sum(_count(usage.get(k)) for k in (
            "input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
    return {
        "status": status,
        "runs_total": runs_total,
        "runs_completed": completed,
        "runs_failed": failed,
        "rounds_per_run": rounds,
        "compactions_completed": compactions,
        "compactions_total": compactions_total,
        "current_trajectory": _label(state.get("current_trajectory", "")),
        "phase": _label(state.get("phase", "")),
        "elapsed_seconds": _count(state.get("elapsed_seconds", 0)),
        "requests": _count(state.get("requests", usage.get("requests", 0))),
        "total_input_tokens": _count(state.get("total_input_tokens", total_input)),
        "output_tokens": _count(state.get("output_tokens", usage.get("output_tokens", 0))),
        "missing_markers": _count(state.get("missing_markers", 0)),
        "opaque_checkpoints": _count(state.get("opaque_checkpoints", 0)),
    }


# 根据统一状态生成一行文字，显示压缩、轨迹、失败、时间和 token 用量。
# 条形图最多填满 100%，原始观测计数仍照实显示。
def render_progress(state: dict[str, Any], width: int = 32) -> str:
    """Render observed compactions; failed trajectories never fill the bar."""
    if isinstance(width, bool) or not isinstance(width, int) or not 1 <= width <= 200:
        raise ValueError("progress bar width must be an integer from 1 to 200")
    s = _normalise(state)
    done, total = s["compactions_completed"], s["compactions_total"]
    ratio = min(done / total, 1.0) if total else 0.0
    filled = min(width, int(ratio * width))
    bar = "#" * filled + "-" * (width - filled)
    percent = f"{100 * ratio:5.1f}%" if total else "  -- %"
    hours, remainder = divmod(s["elapsed_seconds"], 3600)
    minutes, seconds = divmod(remainder, 60)
    status = _STATUS_LABELS.get(s["status"], s["status"])
    parts = [
        f"[{bar}] {percent} 压缩 {done}/{total or '?'}",
        f"轨迹完成 {s['runs_completed']}/{s['runs_total'] or '?'} 失败 {s['runs_failed']}",
        f"每轨迹 {s['rounds_per_run'] or '?'} 轮",
        status,
    ]
    if s["current_trajectory"]:
        parts.append(s["current_trajectory"])
    if s["phase"]:
        parts.append("阶段 " + s["phase"])
    parts.extend([
        f"耗时 {hours:02d}:{minutes:02d}:{seconds:02d}",
        f"请求 {s['requests']:,}",
        f"tokens 输入 {s['total_input_tokens']:,} / 输出 {s['output_tokens']:,}",
        f"目标值未命中 {s['missing_markers']}",
    ])
    if s["opaque_checkpoints"]:
        parts.append(f"不可见检查点 {s['opaque_checkpoints']}")
    return " | ".join(parts)


# 负责终端刷新节流：交互终端覆盖同一行，日志文件则仅在指标变化时新增一行。
class ProgressDisplay:
    """TTY single-line rendering; unchanged non-TTY metrics are not repeated.

    ``min_interval`` limits noisy updates, while terminal states always print.
    Non-TTY output ignores elapsed-time-only changes.  The caller owns the stream.
    """
    # 初始化输出流、刷新间隔和 TTY 状态；输出流由调用方管理，此类不关闭它。
    def __init__(self, stream: TextIO | None = None, enabled: bool = True,
                 min_interval: float = 0.5):
        if not isinstance(min_interval, (int, float)) or isinstance(min_interval, bool) \
                or not math.isfinite(min_interval) or min_interval < 0:
            raise ValueError("min_interval must be finite and nonnegative")
        self.stream = sys.stderr if stream is None else stream
        self.enabled = enabled
        self.min_interval = float(min_interval)
        self._tty = bool(getattr(self.stream, "isatty", lambda: False)())
        self._last_signature = None
        self._last_write = -math.inf
        self._last_length = 0
        self._wrote = False
        self._closed = False

    # 忽略未变状态并按最短间隔节流；终止状态始终允许立即打印。
    def update(self, state: dict[str, Any]) -> None:
        if not self.enabled or self._closed:
            return
        normalised = _normalise(state)
        # 非交互日志忽略单纯时间变化，防止每秒刷出一条几乎相同的进度。
        signature = tuple((k, v) for k, v in normalised.items()
                          if self._tty or k != "elapsed_seconds")
        if signature == self._last_signature:
            return
        now = time.monotonic()
        terminal = normalised["status"] in TERMINAL_STATUSES
        if self._wrote and not terminal and now - self._last_write < self.min_interval:
            return
        line = render_progress(normalised)
        # 交互终端覆盖当前行并擦掉旧尾巴，重定向到文件时则正常换行。
        if self._tty:
            self.stream.write("\r" + line + " " * max(0, self._last_length - len(line)))
        else:
            self.stream.write(line + "\n")
        self.stream.flush()
        self._last_signature = signature
        self._last_write = now
        self._last_length = len(line)
        self._wrote = True

    # 交互模式结束时补一个换行；只关闭显示状态，不向实验进程发信号。
    def close(self) -> None:
        if not self._closed and self.enabled and self._tty and self._wrote:
            self.stream.write("\n")
            self.stream.flush()
        self._closed = True

    # 允许使用 with 管理显示器的收尾，不会启动任何后台任务。
    def __enter__(self):
        return self

    # 离开 with 时完成终端换行；不拦截调用方异常。
    def __exit__(self, *_args):
        self.close()


# 只读加载进度；文件暂缺或半写时显示“等待”，旧格式缺轮次则查冻结协议。
def _read_state(run_dir: Path) -> dict[str, Any]:
    """Read without creating files or changing the running experiment."""
    try:
        state = json.loads((run_dir / "progress.json").read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            raise ValueError("progress must be an object")
    except (OSError, ValueError):
        return {"status": "waiting", "phase": "等待 progress.json"}
    # Older DeepSeek progress files omit rounds; the frozen protocol records it.
    if not state.get("rounds_per_run") and not state.get("rounds"):
        try:
            protocol = json.loads((run_dir / "execution_protocol.json").read_text(encoding="utf-8"))
            if isinstance(protocol, dict):
                state["rounds_per_run"] = protocol.get("rounds", 0)
        except (OSError, ValueError):
            pass
    return state


# 周期性读取状态直到实验终止或用户退出；不会写 STOP 文件，也不终止实验。
def watch(run_dir: Path, interval: float = 1, once: bool = False) -> dict[str, Any]:
    """Follow read-only progress; Ctrl+C exits only this watcher.

    The returned dictionary is the last saved state (or a waiting state). No
    signal, stop file, API request, or modification is sent to the experiment.
    """
    if isinstance(interval, bool) or not isinstance(interval, (int, float)) \
            or not math.isfinite(interval) or not 0 < interval <= 60:
        raise ValueError("watch interval must be greater than 0 and at most 60 seconds")
    run_dir = Path(run_dir)
    last = {"status": "waiting", "phase": "等待 progress.json"}
    with ProgressDisplay() as display:
        try:
            while True:
                last = _read_state(run_dir)
                display.update(last)
                if once or _normalise(last)["status"] in TERMINAL_STATUSES:
                    return last
                time.sleep(interval)
        # 查看器和实验 runner 相互独立；退出查看不会停止正在计费的实验。
        except KeyboardInterrupt:
            return last


# 命令行入口，支持设置读取间隔或只显示一次；参数错误转为清晰的 CLI 提示。
def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--interval", type=float, default=1)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    try:
        watch(args.run_dir, interval=args.interval, once=args.once)
    except ValueError as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
