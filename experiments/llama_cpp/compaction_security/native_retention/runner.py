# 旧 Claude Code + DeepSeek 原生压缩运行器，保留历史协议与断点恢复能力。
# 它使用私有配置目录和无工具启动参数，并非 Codex/bubblewrap 的操作系统级隔离。
# 下面所有主会话、探针和辅助评审均会调用模型；本次加注释不会执行这些入口。
"""Isolated, resumable native-compaction trajectories (standard library only).

Run with ``python3 -m experiments.llama_cpp.compaction_security.native_retention.runner``.
The experiment uses a recording gateway, never an action-executing agent.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
import time
import uuid

from .corpus import build_corpus, make_workload, probe_prompt, score_answers
from .gateway import RecordingGateway, BudgetExceeded
from .analysis import flatten_messages, value_presence, summary_judge_prompt, parse_judgments, review_material

MODULE = Path(__file__).resolve().parents[1]
DEFAULT_OUT = MODULE / "phase1_design/runtime/full_v1"
KEY_FILE = Path("/home/haomin_wang/.local/share/compaction-security-lab/secrets/deepseek_api_key")
MODEL = "deepseek-flash"
WRITE_LOCK = threading.RLock()


# 唯一临时文件写入后原子替换，供状态和检查点持久化。
def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


# 按 UTF-8 读取本地 JSON 产物；格式错误向上抛出，不把坏文件当空结果。
def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


# 计算文件 SHA256，只保存指纹以核验源码、二进制及配置是否发生变化。
def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


# 使用共享写锁追加带时间戳的全局 JSONL 事件，支持多条轨迹并行。
def event(root, kind, **fields):
    with WRITE_LOCK:
        with (Path(root) / "events.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps({"time": time.time(), "event": kind, **fields}, ensure_ascii=False) + "\n")


# 为子进程清除继承的 Claude/Anthropic 线路和代理变量，指定实验私有配置与回环网关。
# 不改父进程环境或 HOME；子进程只得到占位凭据，真实 key 留在网关内。
def isolated_env(base_url, config_dir, label):
    # Do not change HOME, CODEX_HOME, the invoking shell, or global providers.
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("ANTHROPIC_", "CLAUDE_", "CLAUDECODE"))
           and k.lower() not in ("http_proxy", "https_proxy", "all_proxy")}
    env.update({
        "ANTHROPIC_BASE_URL": base_url,
        "ANTHROPIC_API_KEY": "local-gateway-placeholder-not-a-provider-key",
        "ANTHROPIC_MODEL": MODEL,
        "ANTHROPIC_DEFAULT_OPUS_MODEL": MODEL,
        "ANTHROPIC_DEFAULT_SONNET_MODEL": MODEL,
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": MODEL,
        "ANTHROPIC_SMALL_FAST_MODEL": MODEL,
        "ANTHROPIC_CUSTOM_HEADERS": "X-Lab-Label: " + label,
        "CLAUDE_CONFIG_DIR": str(config_dir),
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "CLAUDE_CODE_DISABLE_AUTO_UPDATE": "1",
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
        "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "8192",
        "DISABLE_NON_ESSENTIAL_MODEL_CALLS": "1",
        "MAX_THINKING_TOKENS": "0",
        "API_TIMEOUT_MS": "600000",
        "NO_PROXY": "127.0.0.1,localhost",
    })
    return env


# 构造无工具、无 MCP、无浏览器、无用户设置来源的 Claude Code 启动参数。
# 首回合新建会话，后续按同一 session_id 恢复；阈值交给原生压缩机制处理。
def claude_args(binary, root, threshold, session_id, first):
    args = [binary, "--bare", "--setting-sources", "", "--tools", "",
            "--strict-mcp-config", "--disable-slash-commands", "--no-chrome",
            "--permission-mode", "dontAsk", "--permission-prompts", "none",
            "--model", MODEL, "--autocompact", str(threshold),
            "--output-format", "json", "--debug-file", str(root / "claude.debug.log")]
    args += ["--session-id" if first else "--resume", session_id, "-p"]
    return args


# 读取本轨迹的原生 JSONL 会话记录，排除 sidechain。
# 并发追加时末行可能不完整，解析失败行暂时跳过，不伪造边界事件。
def transcript_records(root):
    paths = list((root / "config/projects").rglob("*.jsonl"))
    records = []
    for path in paths:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue  # a concurrently appended final line is not a complete event
            if not d.get("isSidechain"):
                records.append(d)
    return records


# 仅提取原生 compact_boundary 系统事件，用它确认实际发生的压缩次数。
def boundaries(records):
    return [d for d in records if d.get("type") == "system" and d.get("subtype") == "compact_boundary"]


# 取得原生记录中最后一份压缩摘要；没有摘要时返回空串，后续检查会识别异常。
def latest_summary(records):
    summaries = [d for d in records if d.get("isCompactSummary")]
    return summaries[-1]["message"]["content"] if summaries else ""


# 只抽取 Anthropic 响应的 text 内容块，不把工具或其他块当成最终回答。
def response_text(response):
    return "".join(b.get("text", "") for b in response.get("content", []) if b.get("type") == "text")


# 复用分析层的展开规则，将完整账本转成可核对的文字。
def messages_text(messages):
    return flatten_messages({"messages": messages})


# 为独立问答复制待测消息与原始 system，追加问题，显式关闭 thinking。
# 不提供工具或原始答案；temperature=0 也不意味着跨服务完全确定。
def probe_payload(effective, messages, question):
    payload = {"model": MODEL, "max_tokens": 6144, "temperature": 0,
               "stream": False, "thinking": {"type": "disabled"},
               "messages": copy.deepcopy(messages)}
    if "system" in effective:
        payload["system"] = copy.deepcopy(effective["system"])
    # No tools, retrieval, original transcript reading, or original fact answers.
    payload["messages"].append({"role": "user", "content": question})
    return payload


# 对日常 Claude/Codex 配置取哈希，运行前后比较，不把配置内容写入报告。
def config_fingerprints():
    paths = [Path("/home/haomin_wang/.claude.json"),
             Path("/home/haomin_wang/.claude/settings.json"),
             Path("/home/haomin_wang/.codex/config.toml")]
    return {str(p): digest(p) if p.is_file() else None for p in paths}


# 筛选同一轨迹回合标签下的成功 HTTP 请求；之后仍需与最终答案文本匹配。
def main_event_requests(gateway, label):
    return [e for e in gateway.events() if e.get("label") == label
            and isinstance(e.get("status"), int) and 200 <= e["status"] < 300]


# 根据元数据读取实际请求，兼容历史记录中的相对和绝对路径。
def saved_request(gateway_root, request_event):
    # Gateway publishes relative request_path; tolerate an absolute recorded path.
    path = Path(request_event["request_path"])
    return read_json(path if path.is_absolute() else gateway_root / path)


# 从保存的 JSON 或 SSE 还原可见回复，用于将 CLI 最终答案关联到真实请求。
# 不能简单取“最后一个请求”，因为一个回合还可能有压缩或其他请求。
def captured_response_text(request_event):
    """Link a captured native response to the CLI final answer, not event order."""
    body = Path(request_event["response_path"]).read_text(encoding="utf-8")
    if "event-stream" not in request_event.get("response_content_type", ""):
        return response_text(json.loads(body))
    parts = []
    for line in body.splitlines():
        if not line.startswith("data:"):
            continue
        try:
            d = json.loads(line[5:])
        except json.JSONDecodeError:
            continue
        block = d.get("content_block", {})
        if d.get("type") == "content_block_start" and block.get("type") == "text":
            parts.append(block.get("text", ""))
        delta = d.get("delta", {})
        if d.get("type") == "content_block_delta" and delta.get("type") == "text_delta":
            parts.append(delta.get("text", ""))
    return "".join(parts)


# 一条固定历史/阈值/布局组合的持久化状态机，保存完整可见账本和原生检查点。
class Trajectory:
    # 创建私有目录和固定材料；若已有材料与本次构造不同，拒绝恢复。
    # 恢复 state.json 或创建新 session_id，避免把不同条件的实验混在一起。
    def __init__(self, out, cell, gateway, binary, rounds, chunk_chars=20000, max_turns=120):
        self.out, self.cell, self.gateway, self.binary = Path(out), cell, gateway, binary
        self.rounds, self.chunk_chars, self.max_turns = rounds, chunk_chars, max_turns
        self.root = self.out / "trajectories" / cell["id"]
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "config").mkdir(exist_ok=True)
        (self.root / "work").mkdir(exist_ok=True)
        self.corpus = build_corpus(cell["history"], cell["layout"])
        corpus_file = self.root / "ground_truth.json"
        if corpus_file.exists() and read_json(corpus_file) != self.corpus:
            raise RuntimeError("frozen corpus changed")
        write_json(corpus_file, self.corpus)
        state_path = self.root / "state.json"
        self.state = read_json(state_path) if state_path.exists() else {
            "session_id": str(uuid.uuid4()), "turn": 0, "round": 0,
            "full_messages": [], "status": "running", "checkpoints": [],
        }
        self.save()

    # 保存状态，包括已完成回合、压缩轮次、完整消息和检查点编号。
    def save(self):
        write_json(self.root / "state.json", self.state)

    # 执行或恢复一个回合，并将 CLI 答案关联到网关原始请求后才提交状态。
    # 只有 started.json 而无 manifest 的回合属于不确定在途状态，不能悄悄重发。
    def turn(self, prompt, phase):
        if (self.out / "STOP").exists():
            raise RuntimeError("operator_stop_file")
        n = self.state["turn"]
        label = self.cell["id"] + f"/turn_{n:03d}"
        turn_root = self.root / "turns" / f"{n:03d}"
        turn_root.mkdir(parents=True, exist_ok=True)
        manifest_path = turn_root / "manifest.json"
        if manifest_path.exists():
            manifest = read_json(manifest_path)
            if manifest["prompt_sha256"] != hashlib.sha256(prompt.encode()).hexdigest():
                raise RuntimeError("resume_prompt_changed")
            effective = read_json(turn_root / "effective_request.json")
            reply = manifest["reply"]
        else:
            # A crash with an uncommitted persisted turn is ambiguous; never repeat silently.
            if (turn_root / "started.json").exists():
                raise RuntimeError("ambiguous_inflight_turn; inspect transcript before resuming")
            (turn_root / "prompt.txt").write_text(prompt, encoding="utf-8")
            write_json(turn_root / "started.json", {"label": label, "phase": phase, "time": time.time()})
            event(self.out, "turn_started", cell=self.cell["id"], turn=n, phase=phase)
            args = claude_args(self.binary, self.root, self.cell["threshold"], self.state["session_id"], n == 0)
            env = isolated_env(self.gateway.base_url, self.root / "config", label)
            proc = subprocess.run(args, input=prompt, text=True, capture_output=True,
                                  cwd=self.root / "work", env=env, timeout=660)
            (turn_root / "stdout.json").write_text(proc.stdout, encoding="utf-8")
            (turn_root / "stderr.txt").write_text(proc.stderr, encoding="utf-8")
            output = json.loads(proc.stdout)
            if proc.returncode or output.get("is_error"):
                raise RuntimeError(f"native_turn_failed: exit={proc.returncode}, subtype={output.get('subtype')}")
            requests = main_event_requests(self.gateway, label)
            if not requests:
                raise RuntimeError("missing_actual_request_capture")
            reply = str(output.get("result", ""))
            matched = [e for e in requests if captured_response_text(e).strip() == reply.strip()]
            if not matched:
                raise RuntimeError("CLI_response_not_linked_to_recorded_request")
            selected = matched[-1]
            effective = saved_request(self.out / "gateway", selected)
            if effective.get("model") != MODEL or effective.get("tools"):
                raise RuntimeError("unexpected_model_or_tools")
            manifest = {"prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                        "reply": reply, "request_events": requests, "phase": phase,
                        "matched_main_request_idx": selected["idx"],
                        "native_boundary_count": len(boundaries(transcript_records(self.root)))}
            write_json(turn_root / "effective_request.json", effective)
            write_json(manifest_path, manifest)
        self.state["full_messages"].extend([{"role": "user", "content": prompt},
                                            {"role": "assistant", "content": reply}])
        self.state["turn"] += 1
        self.state["effective_request"] = str(turn_root / "effective_request.json")
        self.save()
        event(self.out, "turn_completed", cell=self.cell["id"], turn=n,
              boundaries=manifest["native_boundary_count"], captured_requests=len(manifest["request_events"]))
        return effective, reply

    # 同时保存实际上下文、原生摘要和完整账本，再分别发送独立问答与辅助语义评审。
    # 先核验边界次数和摘要确实进入下个请求；辅助评审不是独立人工定论。
    def checkpoint(self, k):
        root = self.root / "checkpoints" / str(k)
        root.mkdir(parents=True, exist_ok=True)
        effective = read_json(self.state["effective_request"])
        records = transcript_records(self.root)
        native_boundaries = boundaries(records)
        summary = latest_summary(records) if k else ""
        text = flatten_messages(effective)
        if k:
            if len(native_boundaries) != k:
                raise RuntimeError("unexpected_boundary_count_at_checkpoint")
            if not summary or summary not in text:
                raise RuntimeError("summary_not_in_actual_next_request")
            write_json(root / "native_boundary.json", native_boundaries[-1])
        (root / "summary.txt").write_text(summary, encoding="utf-8")
        write_json(root / "effective_request.json", effective)
        # Match the main-request observation instant: both exclude its future reply.
        # 完整对照与实际请求取同一观测时刻：都排除该请求之后才生成的当前回复。
        full = self.state["full_messages"][:-1]
        write_json(root / "full_messages.json", full)
        facts = self.corpus["facts"]
        measured = {
            "round": k, "cell": self.cell, "native_boundary_count": len(native_boundaries),
            "effective_value_presence": value_presence(text, facts),
            "summary_value_presence": value_presence(summary, facts),
            "full_value_presence": value_presence(messages_text(full), facts),
            "summary_chars": len(summary), "effective_chars": len(text),
            "full_chars": len(messages_text(full)),
            "availability_caveat": "value presence alone does not establish full semantic fact retention",
        }
        if k == 0 and not all(x["value_present"] for x in measured["effective_value_presence"]):
            write_json(root / "ingestion_failure.json", measured)
            raise RuntimeError("baseline_ingestion_missing_values")
        # Each probe is a direct detached request, never appended to the native session.
        for condition in ("effective", "full"):
            result_file = root / f"{condition}_probe.json"
            if result_file.exists():
                result = read_json(result_file)
            else:
                msgs = effective["messages"] if condition == "effective" else full
                payload = probe_payload(effective, msgs, probe_prompt(facts))
                try:
                    response = self.gateway.post(payload, label=f"{self.cell['id']}/checkpoint_{k}/{condition}")
                    raw = response_text(response)
                    result = {"response": response, "raw": raw, "scoring": score_answers(raw, facts)}
                except BudgetExceeded:
                    (self.out / "STOP").touch()
                    raise
                except Exception as exc:
                    result = {"error_type": type(exc).__name__, "error": str(exc)[:300],
                              "scoring": {"parse_error": False, "model_error": True, "scores": []}}
                write_json(result_file, result)
            measured[condition + "_probe"] = result["scoring"]
        if k == 0:
            baseline = {}
            for condition in ("effective", "full"):
                scores = measured[condition + "_probe"].get("scores", [])
                rate = sum(s.get("bounded_correct", s["correct"]) for s in scores) / len(facts)
                baseline[condition] = {"bounded_correct_rate": rate,
                                       "valid_complete_json": len(scores) == len(facts),
                                       "passed": len(scores) == len(facts) and rate >= 0.9}
            baseline["passed"] = all(baseline[c]["passed"] for c in ("effective", "full"))
            measured["baseline_capability"] = baseline
        if k:
            workload_texts = [(p.parent / "prompt.txt").read_text(encoding="utf-8")
                              for p in sorted((self.root / "turns").glob("*/manifest.json"))
                              if read_json(p).get("phase") == "workload"]
            projected = review_material(effective, workload_texts)
            write_json(root / "effective_review_material.json", projected)
        # 模型辅助评审读取摘要或投影文本，仅提供带证据的初步标注，仍需独立复核。
        for judge_condition in (("summary", "effective") if k else ()):
            judged_text = summary if judge_condition == "summary" else projected["text"]
            judge_file = root / f"{judge_condition}_judge.json"
            if judge_file.exists():
                judge = read_json(judge_file)
            else:
                payload = {"model": MODEL, "max_tokens": 8192, "stream": False,
                           "temperature": 0, "thinking": {"type": "disabled"},
                           "messages": [{"role": "user", "content": summary_judge_prompt(facts, judged_text)}]}
                try:
                    response = self.gateway.post(payload, label=f"{self.cell['id']}/checkpoint_{k}/blinded_{judge_condition}_judge")
                    raw = response_text(response)
                    judge = {"response": response, "raw": raw,
                             "judgments": parse_judgments(raw, facts, judged_text),
                             "status": "model_assisted_evidence_checked_pending_independent_review"}
                except BudgetExceeded:
                    (self.out / "STOP").touch()
                    raise
                except Exception as exc:
                    judge = {"error_type": type(exc).__name__, "error": str(exc)[:300],
                             "judgments": {"validation_error": True, "judgments": []},
                             "status": "model_error"}
                write_json(judge_file, judge)
            measured[judge_condition + "_semantics"] = judge["judgments"]
        write_json(root / "measurements.json", measured)
        if k not in self.state["checkpoints"]:
            self.state["checkpoints"].append(k)
        self.state["round"] = k
        self.save()
        event(self.out, "checkpoint_completed", cell=self.cell["id"], round=k,
              path=str(root / "measurements.json"))
        return measured

    # 先完成材料摄入与第零轮能力门控，再按固定填充序列推进压缩。
    # 多次压缩跳过观测、能力不合格、服务故障均记为失败，不算遗忘。
    def run(self):
        if self.state.get("status") == "completed":
            return
        self.state["status"] = "running"
        self.save()
        event(self.out, "trajectory_started", cell=self.cell["id"])
        try:
            initial = [self.corpus["intro"]] + [m["content"] for m in self.corpus["messages"]]
            while self.state["turn"] < len(initial):
                self.turn(initial[self.state["turn"]], "ingestion")
            # 先检查第零轮确实没有发生压缩，再验证模型能正常回答材料问题。
            if 0 not in self.state["checkpoints"]:
                if boundaries(transcript_records(self.root)):
                    raise RuntimeError("compacted_during_ingestion")
                self.checkpoint(0)
            if not read_json(self.root / "checkpoints/0/measurements.json")["baseline_capability"]["passed"]:
                raise RuntimeError("baseline_capability_failure_not_compaction_loss")
            while self.state["round"] < self.rounds:
                count = len(boundaries(transcript_records(self.root)))
                if count > self.state["round"]:
                    if count != self.state["round"] + 1:
                        raise RuntimeError("observation_gap_multiple_compactions_in_one_turn")
                    self.checkpoint(count)
                    continue
                if self.state["turn"] >= self.max_turns:
                    raise RuntimeError("compaction_not_triggered_within_max_turns")
                chunk = self.state["turn"] - len(initial)
                # Counter is exposure index, independent of observations and model output.
                prompt = make_workload(self.cell["history"], 0, chunk, size=self.chunk_chars)
                self.turn(prompt, "workload")
            self.state["status"] = "completed"
        except Exception as exc:
            self.state["status"] = "failed"
            # The gateway never embeds authentication in errors; no raw environment is logged.
            self.state["error_type"] = type(exc).__name__
            self.state["error"] = str(exc)[:500]
            event(self.out, "trajectory_failed", cell=self.cell["id"], error=self.state["error"])
        finally:
            self.save()
            event(self.out, "trajectory_finished", cell=self.cell["id"], status=self.state["status"])


# 汇总状态文件和请求账本，生成进度与报告；没有调用模型，可用于运行中查看。
# 使用 Anthropic 的输入/缓存字段口径，不能直接套用到 Responses 账本。
def status(out):
    out = Path(out)
    cells = []
    for path in sorted((out / "trajectories").glob("*/state.json")):
        s = read_json(path)
        cells.append({"id": path.parent.name, "status": s["status"], "turn": s["turn"],
                      "round": s["round"], "checkpoints": s["checkpoints"], "error": s.get("error")})
    usage = {"requests": 0, "input_tokens": 0, "cache_read_input_tokens": 0,
             "cache_creation_input_tokens": 0, "total_input_tokens": 0,
             "output_tokens": 0, "conservative_estimated_usd": 0.0}
    ledger = out / "gateway/requests.jsonl"
    if ledger.exists():
        for line in ledger.read_text().splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            usage["requests"] += 1
            u = row.get("usage", {})
            for key in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
                usage[key] += u.get(key, 0) or 0
            usage["conservative_estimated_usd"] += row.get("estimated_usd", 0) or 0
    usage["total_input_tokens"] = sum(usage[k] for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
    protocol_file = out / "execution_protocol.json"
    planned = len(read_json(protocol_file)["cells"]) if protocol_file.exists() else len(cells)
    result = {"time": time.time(), "trajectories_planned": planned,
              "trajectories_not_started": planned - len(cells), "trajectories_started": len(cells),
              "completed": sum(c["status"] == "completed" for c in cells),
              "failed": sum(c["status"] == "failed" for c in cells),
              "verified_compactions_observed": sum(c["round"] for c in cells),
              "checkpoints_completed": sum(len(c["checkpoints"]) for c in cells),
              "usage": usage, "cells": cells}
    write_json(out / "progress.json", result)
    from .reporting import render_report
    render_report(out)
    return result


# 本批编排入口：加非阻塞文件锁、冻结协议与源码、读取密钥、启动网关和工作池。
# 恢复时必须核对源码/材料/轮数/二进制；收尾关闭网关并检查日常配置指纹。
def run(args):
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    lock_path = out / "runner.lock"
    import fcntl
    with lock_path.open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lock.write(str(os.getpid()))
        lock.flush()
        binary = shutil.which("claude")
        if binary is None:
            raise RuntimeError("Claude Code executable not found")
        binary = str(Path(binary).resolve())
        version = subprocess.check_output([binary, "--version"], text=True).strip()
        cells = [{"id": f"h{h:02d}_t{t}_l{layout}", "history": h, "threshold": t, "layout": layout}
                 for h in range(args.histories) for t in args.thresholds for layout in range(args.layouts)]
        sources = {str(p.relative_to(MODULE)): digest(p)
                   for p in sorted((MODULE / "native_retention").glob("*.py"))}
        protocol = {"protocol_id": "native-retention-execution-v1", "created_at": time.time(),
                    "user_authorized": True, "route": "isolated_loopback_recorder_to_official_deepseek_not_ccswitch",
                    "model": MODEL, "snapshot_pinning_verified": False,
                    "corpus_scope": "8 synthetic domain instances sharing 36 sentence templates; not independent semantic workflows",
                    "position_scope": "early_middle_late_within_initial_fact_block; all target facts early in cumulative workload",
                    "framework": version, "framework_binary": binary, "framework_sha256": digest(binary),
                    "source_sha256": sources, "cells": cells, "rounds": args.rounds,
                    "chunk_characters": args.chunk_chars, "max_turns": args.max_turns,
                    "budget_guard_usd": args.budget_usd, "concurrency": args.workers,
                    "guard_is_operational_not_user_specified_cap": True,
                    "launch_flags": claude_args(binary, Path("<trajectory>"), args.thresholds[0], "<session>", True),
                    "thinking": {"main_and_compactor": "native_default_no_explicit_thinking_field_effort_high_in_calibration",
                                 "detached_probes_and_judges": "explicitly_disabled"},
                    "probe_temperature": 0, "probe_max_tokens": 6144,
                    "judge_max_tokens": 8192,
                    "isolation": "private_config_and_cwd_bare_no_tools_no_mcp_no_commands_not_OS_sandbox",
                    "full_control": "direct_API_original_user_messages_and_visible_parent_replies_excluding_current_reply_no_compaction; prior_thinking_not_replayed",
                    "primary_mechanical_metric": "exact_answer_value_presence_not_full_fact_semantics",
                    "baseline_capability_gate": "complete_JSON_and_bounded_correct_rate_at_least_0.90_in_both_detached_conditions",
                    "answer_metrics": "strict_exact,marker_only,qualifier_only,bounded_components; none_is_full_fact_semantics",
                    "semantic_metric": "summary_and_effective_text_projection_blinded_to_round_and_cell_model_assisted_evidence_checked_pending_independent_review",
                    "workload_schedule": "fixed_chunk_index_independent_of_observed_loss",
                    "stop_rule": "six_verified_compactions_or_recorded_failure_or_budget_or_STOP_file"}
        frozen_path = out / "execution_protocol.json"
        # 断点恢复必须沿用冻结条件；不允许改材料或源码后沿用原批次结果目录。
        if frozen_path.exists():
            old = read_json(frozen_path)
            for field in ("source_sha256", "cells", "rounds", "chunk_characters", "framework_sha256"):
                if old[field] != protocol[field]:
                    raise RuntimeError("resume protocol mismatch: " + field)
            write_json(out / f"resume_{int(time.time())}.json", {"budget_usd": args.budget_usd,
                       "max_turns": args.max_turns, "workers": args.workers, "time": time.time()})
        else:
            for source in sorted((MODULE / "native_retention").glob("*.py")):
                target = out / "frozen_source" / source.name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target)
            write_json(frozen_path, protocol)
        before = config_fingerprints()
        write_json(out / "normal_config_fingerprints_before.json", before)
        key = KEY_FILE.read_text(encoding="utf-8").strip()
        if not key:
            raise RuntimeError("empty credential file")
        gateway = RecordingGateway(out / "gateway", key, budget_usd=args.budget_usd, max_requests=6000)
        gateway.base_url = gateway.start()
        event(out, "run_started", cells=len(cells), pid=os.getpid(), framework=version)
        try:
            # 每个工作线程只操作自己的轨迹，网关账本和预算共享并受锁保护。
            with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
                # 调度单条轨迹前检查 STOP；已开始的轨迹还有自身的停止与预算检查。
                def run_cell(cell):
                    if (out / "STOP").exists():
                        return
                    Trajectory(out, cell, gateway, binary, args.rounds,
                               args.chunk_chars, args.max_turns).run()
                futures = [pool.submit(run_cell, c) for c in cells]
                pending = set(futures)
                while pending:
                    done, pending = concurrent.futures.wait(pending, timeout=10,
                                                           return_when=concurrent.futures.FIRST_COMPLETED)
                    for f in done:
                        f.result()
                    budget = gateway.accounting()
                    write_json(out / "budget_accounting.json", budget)
                    if budget.get("guard_exhausted") or budget["estimated_usd"] + budget["reserved_usd"] >= args.budget_usd:
                        (out / "STOP").touch()
                    status(out)
        finally:
            gateway.close()
            after = config_fingerprints()
            write_json(out / "normal_config_fingerprints_after.json", after)
            write_json(out / "normal_config_check.json", {"unchanged": before == after,
                                                           "caveat": "hash change may also originate from normal concurrent usage"})
            event(out, "run_finished", **{k: v for k, v in status(out).items() if k != "cells"})


# 解析 run/status 子命令与实验规模，限制矩阵范围后才允许启动真实实验。
# status 只读取产物，run 会调用 Claude Code 与已配置的推理服务。
def cli():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("run", "status"))
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    # histories/layouts/thresholds 构成组合矩阵；每个组合是一条独立轨迹。
    parser.add_argument("--histories", type=int, default=8)
    parser.add_argument("--layouts", type=int, default=3)
    parser.add_argument("--thresholds", type=int, nargs="+", default=[100000, 150000])
    # rounds 是希望观察的压缩次数；max-turns 是等待压缩的停止上限，不是压缩次数。
    parser.add_argument("--rounds", type=int, default=6)
    parser.add_argument("--chunk-chars", type=int, default=20000)
    parser.add_argument("--max-turns", type=int, default=120)
    # 并发上限只影响运行调度；共享网关会为所有在途请求预留预算。
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--budget-usd", type=float, default=75.0)
    args = parser.parse_args()
    if args.command == "status":
        print(json.dumps(status(args.out), ensure_ascii=False, indent=2))
    else:
        if not (1 <= args.histories <= 8 and 1 <= args.layouts <= 3 and 1 <= args.workers <= 2
                and 1 <= args.rounds <= 6 and all(100000 <= t <= 1000000 for t in args.thresholds)):
            parser.error("matrix bounds invalid")
        run(args)


if __name__ == "__main__":
    cli()
