# 将既有 Claude/DeepSeek 运行记录离线导出为可读的内容变化账本。
# 只读取原始证据，不加载密钥、不请求模型；输出明确标作旧后端数据，不能混入 Codex 新实验。

"""Export an existing Claude/DeepSeek run into a readable local loss ledger.

This is an offline reader. It does not run a model, load authentication, or
modify the source experiment's original snapshots. The generated ledger is
explicitly labelled legacy data rather than a new Codex/sub2api experiment.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Callable
import uuid

from .analysis import flatten_messages
from .loss_ledger import record_checkpoint


# 以 UTF-8 读取一个已保存的 JSON 对象，不对原文件进行回写。
def _read(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


# 计算源文件字节的 SHA256，用来确认离线分析对应的原始证据。
def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# 先写临时文件再替换目标 JSON，避免读者在中途读到半份导出清单。
def _write(path: Path, value) -> None:
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


# 递归识别加密或不透明压缩状态；只标注不可观察，不尝试解密或猜测内容。
def _opaque(value) -> bool:
    """Conservatively recognise recorded opaque state without interpreting it."""
    if isinstance(value, dict):
        if any(value.get(key) for key in ("compaction", "encrypted_content")):
            return True
        if value.get("type") == "compaction":
            return True
        return any(_opaque(item) for item in value.values())
    if isinstance(value, list):
        return any(_opaque(item) for item in value)
    return False


# 遍历轨迹和检查点，读取实际输入并调用共享账本记录函数。
# 缺文件或坏数据单列失败并继续；下一轮的压缩前状态设为未知，不能跨缺口推断丢失。
def export_legacy(run_dir: Path, output_dir: Path | None = None, *,
                  progress: Callable[[dict], None] | None = None,
                  trajectory_id: str | None = None) -> dict:
    """Read saved effective requests and record per-round lexical changes.

    ``progress`` receives a dictionary after each attempted checkpoint. A bad or
    missing source checkpoint is retained as an export failure; the next round
    receives an unknown before-state so the exporter cannot bridge that gap.
    Re-running with unchanged inputs is safe and does not duplicate ledger rows.
    """
    run_dir = Path(run_dir).resolve()
    if not run_dir.is_dir():
        raise FileNotFoundError(f"Experiment directory does not exist: {run_dir}")
    trajectories_dir = run_dir / "trajectories"
    if not trajectories_dir.is_dir():
        raise FileNotFoundError("Source experiment has no trajectories directory")
    if trajectory_id is not None and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", trajectory_id):
        raise ValueError("trajectory_id must be one safe directory name")
    output_dir = Path(output_dir).resolve() if output_dir is not None else run_dir / "loss_review"
    output_dir.mkdir(parents=True, exist_ok=True)
    source_protocol = run_dir / "execution_protocol.json"
    protocol = {}
    failures = []
    if source_protocol.exists():
        try:
            protocol = _read(source_protocol)
            if not isinstance(protocol, dict):
                raise ValueError("protocol is not an object")
        except (OSError, ValueError) as exc:
            failures.append({"scope": "protocol", "path": str(source_protocol),
                             "error_type": type(exc).__name__, "error": str(exc)[:250]})
    cells = {cell["id"]: cell for cell in protocol.get("cells", [])
             if isinstance(cell, dict) and isinstance(cell.get("id"), str)}
    paths = sorted(path for path in trajectories_dir.iterdir() if path.is_dir()
                   and (trajectory_id is None or path.name == trajectory_id))
    if trajectory_id is not None and not paths:
        raise FileNotFoundError(f"Requested trajectory does not exist: {trajectory_id}")
    checkpoint_paths = {}
    for trajectory in paths:
        candidates = [path for path in (trajectory / "checkpoints").glob("*")
                      if path.is_dir() and path.name.isdecimal()]
        checkpoint_paths[trajectory] = sorted(candidates, key=lambda path: (int(path.name), path.name))
    # 导出清单同时记录来源后端与哈希，避免把旧数据导出误报为新模型实验。
    manifest = {
        "status": "running", "source_backend": "claude_deepseek_legacy",
        "is_new_codex_experiment": False,
        "source_run_dir": str(run_dir), "output_dir": str(output_dir),
        "source_protocol": str(source_protocol) if source_protocol.exists() else None,
        "source_protocol_sha256": _sha(source_protocol) if source_protocol.exists() else None,
        "source_model": protocol.get("model"), "source_framework": protocol.get("framework"),
        "started_utc": datetime.now(timezone.utc).isoformat(),
        "trajectories_discovered": len(paths), "trajectories_exported": 0,
        "checkpoints_seen": sum(len(checkpoint_paths[path]) for path in paths),
        "checkpoints_attempted": 0, "checkpoints_exported": 0, "checkpoints_skipped": 0,
        "counts": {}, "failures": failures,
        "interpretation": "lexical_values_not_semantic_deletion; original_records_read_only; legacy_not_codex_results",
    }
    manifest_path = output_dir / "export_manifest.json"
    if manifest_path.exists():
        previous = _read(manifest_path)
        if previous.get("source_run_dir") != str(run_dir):
            raise ValueError("Output directory already belongs to another source run")
    _write(manifest_path, manifest)
    events = Counter()
    # 每条轨迹独立处理；某条 ground truth 损坏不会让其他轨迹的失败被隐藏。
    for trajectory in paths:
        truth = trajectory / "ground_truth.json"
        try:
            data = _read(truth)
            facts = data.get("facts") if isinstance(data, dict) else None
            if not isinstance(facts, list) or not facts:
                raise ValueError("ground truth must contain a nonempty facts list")
            truth_sha256 = _sha(truth)
        except (OSError, ValueError) as exc:
            failures.append({"scope": "trajectory", "trajectory_id": trajectory.name,
                             "path": str(truth), "error_type": type(exc).__name__,
                             "error": str(exc)[:250]})
            manifest["checkpoints_skipped"] += len(checkpoint_paths[trajectory])
            manifest["checkpoints_attempted"] += len(checkpoint_paths[trajectory])
            _write(manifest_path, manifest)
            if progress:
                progress({"event": "trajectory_skipped", "trajectory_id": trajectory.name,
                          "completed": manifest["checkpoints_attempted"], "total": manifest["checkpoints_seen"]})
            continue
        prior_text = None
        prior_snapshot = None
        prior_round = None
        exported_this_trajectory = 0
        seen_rounds = set()
        cell = cells.get(trajectory.name, {})
        threshold_match = re.search(r"(?:^|_)t([0-9]+)(?:_|$)", trajectory.name)
        threshold = cell.get("threshold", int(threshold_match.group(1)) if threshold_match else None)
        for checkpoint in checkpoint_paths[trajectory]:
            k = int(checkpoint.name)
            snapshot = checkpoint / "effective_request.json"
            manifest["checkpoints_attempted"] += 1
            status = "exported"
            try:
                if k in seen_rounds:
                    raise ValueError("duplicate numeric checkpoint round")
                seen_rounds.add(k)
                payload = _read(snapshot)
                if not isinstance(payload, dict):
                    raise ValueError("effective request must be a JSON object")
                opaque = _opaque(payload)
                if not opaque and not isinstance(payload.get("messages"), list):
                    raise ValueError("legacy effective request must contain a messages list")
                # 只要存在不透明压缩态，就保守记为不可观察，不扫描外壳后宣称目标已丢失。
                after_text = None if opaque else flatten_messages(payload)
                adjacent = prior_round is not None and prior_round == k - 1
                # 只有相邻轮次可使用前轮快照作比较；第零轮没有“压缩前”状态。
                before_text = prior_text if k > 0 and adjacent else None
                metadata = {
                    "source_backend": "claude_deepseek_legacy", "is_new_codex_experiment": False,
                    "context_mode": "recorded_native_compaction_effective_input",
                    "source_run_dir": str(run_dir), "source_trajectory_id": trajectory.name,
                    "source_model": payload.get("model", protocol.get("model")),
                    "source_framework": protocol.get("framework"), "threshold_tokens": threshold,
                    "source_protocol": str(source_protocol) if source_protocol.exists() else None,
                    "source_protocol_sha256": manifest["source_protocol_sha256"],
                    "ground_truth_snapshot": str(truth), "ground_truth_sha256": truth_sha256,
                    "before_snapshot": str(prior_snapshot) if adjacent and prior_snapshot is not None else None,
                    "after_snapshot": str(snapshot), "after_snapshot_sha256": _sha(snapshot),
                    "source_previous_round": prior_round,
                    "observation_gap": bool(k > 0 and not adjacent), "opaque_state": opaque,
                    "native_boundary_snapshot": str(checkpoint / "native_boundary.json")
                    if (checkpoint / "native_boundary.json").exists() else None,
                    "native_boundary_record_present": (checkpoint / "native_boundary.json").exists(),
                    "measurement_channel": "visible_system_and_message_text; opaque_state_unknown; thinking_blocks_not_measured",
                }
                result = record_checkpoint(output_dir, trajectory.name, k, facts,
                                           before_text, after_text, metadata)
                events.update(result["counts"])
                manifest["counts"] = dict(events)
                manifest["checkpoints_exported"] += 1
                exported_this_trajectory += 1
                prior_text, prior_snapshot, prior_round = after_text, snapshot, k
            except (OSError, ValueError, TypeError, KeyError) as exc:
                status = "skipped"
                manifest["checkpoints_skipped"] += 1
                failures.append({"scope": "checkpoint", "trajectory_id": trajectory.name,
                                 "round": k, "path": str(snapshot), "error_type": type(exc).__name__,
                                 "error": str(exc)[:250]})
                # 导出失败切断前后链，下一轮不能跨过坏记录推断本轮发生了删除。
                prior_text, prior_snapshot, prior_round = None, None, k
            _write(manifest_path, manifest)
            if progress:
                progress({"event": "checkpoint_" + status, "trajectory_id": trajectory.name,
                          "round": k, "completed": manifest["checkpoints_attempted"],
                          "total": manifest["checkpoints_seen"], "counts": dict(events)})
        manifest["trajectories_exported"] += bool(exported_this_trajectory)
    manifest["status"] = "complete_with_errors" if failures else "complete"
    manifest["finished_utc"] = datetime.now(timezone.utc).isoformat()
    manifest["ledger_jsonl"] = str(output_dir / "lost_content.jsonl")
    manifest["ledger_markdown"] = str(output_dir / "lost_content.md")
    _write(manifest_path, manifest)
    return manifest


# 命令行入口：指定旧实验目录、输出位置及可选轨迹过滤；只导出，不启动实验。
def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--trajectory")
    parser.add_argument("--quiet", action="store_true", help="Suppress per-checkpoint progress")
    args = parser.parse_args()

    # 每个已尝试检查点向 stderr 打印一次进度，不影响 stdout 的最终 JSON。
    def progress(row):
        print(f"[{row['completed']}/{row['total']}] {row['trajectory_id']} "
              f"round={row.get('round', '-')} {row['event']}", file=sys.stderr, flush=True)

    result = export_legacy(args.run_dir, args.out, progress=None if args.quiet else progress,
                           trajectory_id=args.trajectory)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
