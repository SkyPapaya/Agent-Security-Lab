# 保存每次压缩前后目标词面的变化，生成 lost_content.jsonl 和 lost_content.md。
# 这里的“丢失”标签只描述可读输入中的词面诊断；None 是不可观察，空字符串才是可见但为空。
# 所有输入必须是合成材料；标准答案、前后原文、引用和哈希一起保存，便于人工核对。

"""Offline, replayable records of lexical changes at native compact boundaries.

This module never infers semantic deletion from a missing value.  A caller must
supply the actual before/after model-input text, or ``None`` when that text is
unobservable (for example encrypted native compaction state).  Model answers are
not substitutes for those inputs.  All text and facts must be synthetic fixtures.
"""
from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import threading
from typing import Any
import uuid

from .analysis import value_presence


_WRITE_LOCK = threading.RLock()
_TRAJECTORY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
# 这些名称仅用于词面变化时间线，不是语义删除或攻击成功标签。
_LOSS_EVENTS = {"first_marker_missing", "marker_missing_again", "continuing_marker_missing"}
_LABELS = {
    "first_marker_missing": "本轮首次观察到目标值消失",
    "marker_missing_again": "恢复或重新加入后再次消失",
    "continuing_marker_missing": "本轮前后目标值均未出现",
    "marker_reappeared": "此前出现过的目标值重新出现",
    "marker_appeared": "目标值首次进入可观察输入",
    "qualifier_missing": "目标值仍在，但规定限定语未匹配",
    "verbatim_rewritten": "原句未逐字出现，可能改写或部分保留",
    "observation_unknown": "压缩后文本不可观察，无法判断丢失",
    "baseline_not_ingested": "第零轮未检测到目标值，不计后续压缩丢失",
    "marker_not_ingested_or_preexisting_missing": "本轮之前已缺失，尚无已摄入证据",
    "marker_absent_transition_unknown": "压缩后未匹配目标值，但压缩前不可观察",
    "baseline_present": "第零轮目标值可见",
    "marker_retained": "本轮前后目标值可见",
    "marker_present_before_unknown": "压缩后目标值可见，压缩前不可观察",
}


# 计算 UTF-8 文本哈希，用于输入指纹、快照校验与稳定 case_id。
def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# 使用固定键序与紧凑 JSON 编码，确保同一逻辑输入得到相同指纹；禁止 NaN。
def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


# 先写唯一临时文件再替换目标，避免进度查看器读到写了一半的账本。
def _atomic_text(path: Path, text: str) -> None:
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


# 同时使用线程锁和文件锁，协调实验进程与离线导出进程对同一账本的写入。
@contextmanager
def _locked(root: Path):
    # Thread locks alone are insufficient when an offline exporter and a live
    # runner use the same ledger. flock is available in the supported WSL host.
    import fcntl

    with _WRITE_LOCK:
        root.mkdir(parents=True, exist_ok=True)
        with (root / ".loss_ledger.lock").open("a", encoding="utf-8") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)


# 验证安全路径片段、轮次、事实字段与文本类型；重复事实 ID 会破坏跨轮对齐，因此拒绝。
def _validate(trajectory_id, round_index, facts, before_text, after_text, metadata):
    if not isinstance(trajectory_id, str) or not _TRAJECTORY.fullmatch(trajectory_id):
        raise ValueError("trajectory_id must be a safe single path component")
    if isinstance(round_index, bool) or not isinstance(round_index, int) or round_index < 0:
        raise ValueError("round_index must be a nonnegative integer")
    if not isinstance(facts, list) or not facts:
        raise ValueError("facts must be a nonempty list")
    ids = []
    for fact in facts:
        if not isinstance(fact, dict):
            raise ValueError("every fact must be a dictionary")
        for key in ("id", "category", "text", "answer"):
            if not isinstance(fact.get(key), str) or not fact[key].strip():
                raise ValueError(f"fact {key} must be a nonempty string")
        if not isinstance(fact.get("aliases", []), list):
            raise ValueError("fact aliases must be a list")
        ids.append(fact["id"])
    if len(ids) != len(set(ids)):
        raise ValueError("fact IDs must be unique")
    for name, text in (("before_text", before_text), ("after_text", after_text)):
        if text is not None and not isinstance(text, str):
            raise TypeError(f"{name} must be a string or None")
    if metadata is not None and not isinstance(metadata, dict):
        raise TypeError("metadata must be a dictionary or None")
    _canonical({"facts": facts, "metadata": metadata or {}})


# 把一段可读文本转成逐事实观测：标记、组合答案和完整原句分别检测。
# 文本不可观察时返回 None 状态，不能将其替换成“目标缺失”。
def _observations(text, facts):
    if text is None:
        return {fact["id"]: {"observable": False, "marker_present": None,
                              "composite_present": None, "verbatim_present": None,
                              "marker_evidence": "", "composite_evidence": "",
                              "context_excerpt": ""} for fact in facts}
    # 单独查第一个标记与完整组合：编号存在并不意味着“未批准”等限定语仍在。
    marker_facts = [dict(fact, answer=re.split(r"[；;]", fact["answer"])[0], aliases=[])
                    for fact in facts]
    markers = {row["id"]: row for row in value_presence(text, marker_facts)}
    composites = {row["id"]: row for row in value_presence(text, facts)}
    result = {}
    for fact in facts:
        marker, composite = markers[fact["id"]], composites[fact["id"]]
        evidence = marker["evidence"]
        position = text.find(evidence) if evidence else -1
        result[fact["id"]] = {
            "observable": True,
            "marker_present": marker["value_present"],
            "composite_present": composite["value_present"],
            "verbatim_present": fact["text"] in text,
            "marker_evidence": evidence,
            "composite_evidence": composite["evidence"],
            "context_excerpt": text[max(0, position - 100):position + len(evidence) + 140]
            if position >= 0 else "",
        }
    return result


# 从逐轨迹不可变检查点加载记录，并按轨迹和轮次排序，作为重建账本的来源。
def _load_checkpoints(root: Path) -> list[dict]:
    records = [json.loads(path.read_text(encoding="utf-8"))
               for path in (root / "loss_checkpoints").glob("*/*.json")]
    return sorted(records, key=lambda row: (row["trajectory_id"], row["round"]))


# 从全部检查点重建机器账本与可读 Markdown，原始逐轮证据保持不变。
# 可读文件只重点展示异常/变化项，JSONL 仍保留全部记录。
def _render(root: Path) -> None:
    checkpoints = _load_checkpoints(root)
    all_records = [row for checkpoint in checkpoints for row in checkpoint["records"]]
    _atomic_text(root / "lost_content.jsonl", "".join(_canonical(row) + "\n" for row in all_records))
    lines = ["# 每次压缩变化：精确值未出现，不自动等于语义丢失", "",
             "这里只比较实际模型输入的可观察文本。原句未逐字出现可能是正常改写；"
             "目标值未匹配不证明整条语义消失。不可观察的加密／不透明状态标为 unknown。", "",
             "每个条目保留完整原始事实和前后证据，可在相同目录的 JSONL 中按 case_id 查询。"
             "第零轮未摄入项单列，不计作后续压缩丢失。", ""]
    total_events = Counter(event for row in all_records for event in row["events"])
    lines.extend(["事件计数（一个事实可同时有多个标签）：", ""])
    for event, count in sorted(total_events.items()):
        lines.append(f"- {_LABELS.get(event, event)}：{count}")
    lines.append("")
    for checkpoint in checkpoints:
        lines += [f"## {checkpoint['trajectory_id']} · 第 {checkpoint['round']} 轮", "",
                  f"检查点：`loss_checkpoints/{checkpoint['trajectory_id']}/{checkpoint['round']}.json`。"
                  f"前文本可观察：{checkpoint['before_observable']}；"
                  f"后文本可观察：{checkpoint['after_observable']}。", ""]
        selected = [row for row in checkpoint["records"]
                    if any(event not in {"baseline_present", "marker_retained", "marker_present_before_unknown"}
                           for event in row["events"])]
        if not selected:
            lines += ["本轮未检测到目标值或限定语的词面变化；这不是独立的完整语义审核。", ""]
        for row in selected:
            labels = "；".join(_LABELS.get(event, event) for event in row["events"])
            lines += [f"### {row['fact_id']}（{row['category']}）", "",
                      f"变化：{labels}。case_id：`{row['case_id']}`。", "",
                      "原始完整事实：", "", "```text", row["original_text"], "```", "",
                      f"原始规定答案：`{row['original_answer']}`", "",
                      "压缩前证据：", "", "```text",
                      row["before"]["context_excerpt"] or (
                          "文本不可观察" if not row["before"]["observable"] else "目标值未匹配；不代表完整语义缺失"),
                      "```", "", "压缩后证据：", "", "```text",
                      row["after"]["context_excerpt"] or (
                          "文本不可观察，不能判断删除" if not row["after"]["observable"] else "目标值未匹配；请对照完整后文本"),
                      "```", ""]
            for side in ("before", "after"):
                ref = row["snapshots"][side]
                if ref.get("local_text_file"):
                    relative = Path(ref["local_text_file"]).relative_to(root)
                    lines.append(f"- {side} 完整文本：[{relative}]({relative.as_posix()})")
                if ref.get("caller_reference") is not None:
                    lines.append(f"- {side} 调用方快照引用：`{_canonical(ref['caller_reference'])}`")
            lines.append("")
    _atomic_text(root / "lost_content.md", "\n".join(lines))


# 记录一个轨迹的一次压缩边界，并刷新派生账本；相同输入重复调用是幂等的。
# 相同轮次但不同指纹会拒绝覆盖，事实集变化和逆序写入也会报错，保护证据一致性。
def record_checkpoint(root: Path, trajectory_id: str, round_index: int,
                      facts: list[dict], before_text: str | None,
                      after_text: str | None, metadata: dict | None = None) -> dict:
    """Persist one checkpoint and regenerate the readable and machine ledgers.

    Repeating the same trajectory/round and identical input is idempotent. A
    changed input at an already recorded checkpoint raises ValueError. None
    denotes unobservable text, whereas an empty string is an observed empty
    context. Evidence is lexical only; there are no model/API calls here.
    """
    _validate(trajectory_id, round_index, facts, before_text, after_text, metadata)
    root = Path(root).resolve()
    metadata = json.loads(_canonical(metadata or {}))
    facts = json.loads(_canonical(facts))
    facts_sha256 = _sha(_canonical(facts))
    # 指纹覆盖事实、前后文本和元数据，阻止相同检查点被另一份证据悄悄覆盖。
    input_fingerprint = _sha(_canonical({"trajectory_id": trajectory_id, "round": round_index,
                                        "facts_sha256": facts_sha256,
                                        "before_sha256": _sha(before_text) if before_text is not None else None,
                                        "after_sha256": _sha(after_text) if after_text is not None else None,
                                        "metadata": metadata}))
    with _locked(root):
        folder = root / "loss_checkpoints" / trajectory_id
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f"{round_index}.json"
        if target.exists():
            existing = json.loads(target.read_text(encoding="utf-8"))
            if existing["input_fingerprint"] != input_fingerprint:
                raise ValueError("checkpoint already exists with different input; refusing to overwrite evidence")
            _render(root)
            return existing
        history = [checkpoint for checkpoint in _load_checkpoints(root)
                   if checkpoint["trajectory_id"] == trajectory_id]
        # 同一轨迹必须使用固定标准答案，不能跑到一半换靶标后再计算遗忘次序。
        if any(checkpoint["facts_sha256"] != facts_sha256 for checkpoint in history):
            raise ValueError("facts changed within a trajectory")
        if any(checkpoint["round"] > round_index for checkpoint in history):
            raise ValueError("checkpoints must be recorded in ascending round order")
        before = _observations(before_text, facts)
        after = _observations(after_text, facts)
        snapshots = {}
        # 原文另存文件并记录哈希；JSON 中的短摘录只是阅读方便，不能代替完整快照。
        for side, text in (("before", before_text), ("after", after_text)):
            local_file = folder / f"{round_index}.{side}.txt" if text is not None else None
            if local_file is not None:
                _atomic_text(local_file, text)
            snapshots[side] = {"observable": text is not None,
                               "characters": len(text) if text is not None else None,
                               "sha256": _sha(text) if text is not None else None,
                               "local_text_file": str(local_file) if local_file is not None else None,
                               "caller_reference": metadata.get(f"{side}_snapshot", metadata.get(f"{side}_ref"))}
        historical = {fact["id"]: [row for checkpoint in history for row in checkpoint["records"]
                                    if row["fact_id"] == fact["id"]] for fact in facts}
        records = []
        for fact in facts:
            identifier = fact["id"]
            prior = historical[identifier]
            b, a = before[identifier], after[identifier]
            ever_present = b["marker_present"] is True or any(
                row["before"]["marker_present"] is True or row["after"]["marker_present"] is True for row in prior)
            ever_lost = any(_LOSS_EVENTS.intersection(row["events"]) for row in prior)
            # 分类优先处理不可见和零轮未摄入，再谈首次缺失、持续缺失与重新出现。
            if not a["observable"]:
                primary = "observation_unknown"
            elif round_index == 0:
                primary = "baseline_present" if a["marker_present"] else "baseline_not_ingested"
            elif not a["marker_present"]:
                # 只有压缩前可见、压缩后未命中，才有本轮词面消失的直接证据。
                if b["marker_present"] is True:
                    primary = "marker_missing_again" if ever_lost else "first_marker_missing"
                elif b["marker_present"] is None:
                    primary = "marker_absent_transition_unknown"
                elif ever_lost:
                    primary = "continuing_marker_missing"
                else:
                    primary = "marker_not_ingested_or_preexisting_missing"
            elif b["marker_present"] is False:
                primary = "marker_reappeared" if ever_present else "marker_appeared"
            elif b["marker_present"] is None:
                primary = "marker_present_before_unknown"
            else:
                primary = "marker_retained"
            events = [primary]
            # 标记仍在时继续检查限定语和原句；原句改写不自动算成事实丢失。
            if a["marker_present"] is True:
                if not a["composite_present"]:
                    events.append("qualifier_missing")
                if not a["verbatim_present"]:
                    events.append("verbatim_rewritten")
            record = {
                "case_id": _sha(f"{trajectory_id}\n{round_index}\n{identifier}")[:20],
                "trajectory_id": trajectory_id, "round": round_index, "fact_id": identifier,
                "category": fact["category"], "original_text": fact["text"],
                "original_answer": fact["answer"], "events": events, "state": primary,
                "before": b, "after": a, "snapshots": snapshots,
                "metadata": metadata, "previous_recorded_round": history[-1]["round"] if history else None,
                "history_gap": bool(history and history[-1]["round"] != round_index - 1),
                "previously_observed_present": ever_present,
                "before_reintroduction": bool(prior and prior[-1]["after"]["marker_present"] is False
                                               and b["marker_present"] is True),
                "interpretation": "lexical_observation_not_semantic_deletion_or_attack_success",
            }
            records.append(record)
        result = {"trajectory_id": trajectory_id, "round": round_index,
                  "created_utc": datetime.now(timezone.utc).isoformat(),
                  "input_fingerprint": input_fingerprint, "facts_sha256": facts_sha256,
                  "before_observable": before_text is not None, "after_observable": after_text is not None,
                  "metadata": metadata, "snapshots": snapshots,
                  "counts": dict(Counter(event for row in records for event in row["events"])),
                  "records": records}
        # 先保存单轮原始检查点，再重建可读索引；读者可由索引返回原证据。
        _atomic_text(target, json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        _render(root)
        return result
