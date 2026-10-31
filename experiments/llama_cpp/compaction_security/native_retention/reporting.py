# 从保存的 measurements.json 生成旧 Claude/DeepSeek 后端阶段报告，不调用模型。
# 报告模板及统计口径属于旧实验版本；应按对应 execution_protocol 解读，不混称新 Codex 批次。

"""Generate cautious, traceable interim reports from persisted measurements."""
import json
import uuid
from collections import defaultdict
from pathlib import Path

from .corpus import CATEGORY_IDS


# 读取已完成检查点，分别汇总词面命中、问答正确、部件诊断与错误数。
# 每种指标保留自己的有效分母，未运行轮次不补零，报告末尾列出证据入口与局限。
def render_report(out):
    out = Path(out)
    progress_file = out / "progress.json"
    progress = json.loads(progress_file.read_text()) if progress_file.exists() else {}
    rows = []
    for path in sorted((out / "trajectories").glob("*/checkpoints/*/measurements.json")):
        record = json.loads(path.read_text())
        rows.append((path, record))
    # 六个槽依次是词面命中/总数、压缩态答对/总数、完整历史答对/总数。
    grouped = defaultdict(lambda: [0, 0, 0, 0, 0, 0])
    # 八个槽依次记录编号、限定语、组合答案、基线合格项恢复，各占分子/分母两个槽。
    components = defaultdict(lambda: [0, 0, 0, 0, 0, 0, 0, 0])
    errors = {"effective_parse": 0, "full_parse": 0, "effective_model": 0, "full_model": 0}
    for _, record in rows:
        k = record["round"]
        for fact in record["effective_value_presence"]:
            g = grouped[(fact["category"], k)]
            g[0] += bool(fact["value_present"])
            g[1] += 1
        for condition, offset in (("effective", 2), ("full", 4)):
            result = record[condition + "_probe"]
            errors[condition + "_parse"] += bool(result.get("parse_error"))
            errors[condition + "_model"] += bool(result.get("model_error"))
            for score in result.get("scores", []):
                g = grouped[(score["category"], k)]
                g[offset] += bool(score["correct"])
                g[offset + 1] += 1
                v = components[(score["category"], k, condition)]
                v[0] += bool(score.get("marker_correct", score["correct"]))
                v[1] += 1
                # 没要求限定语的事实不进入限定语分母，不能用 None 冒充正确或错误。
                if score.get("qualifier_correct") is not None:
                    v[2] += bool(score["qualifier_correct"])
                    v[3] += 1
                v[4] += bool(score.get("bounded_correct", score["correct"]))
                v[5] += 1
    # 额外按第零轮完整历史已答对的项目分层，减少正常能力不足对遗忘指标的干扰。
    for path, record in rows:
        baseline_path = path.parent.parent / "0/measurements.json"
        if not baseline_path.exists():
            continue
        baseline_record = json.loads(baseline_path.read_text())
        eligible = {s["id"] for s in baseline_record["full_probe"].get("scores", [])
                    if s.get("bounded_correct", s["correct"])}
        for condition in ("effective", "full"):
            for score in record[condition + "_probe"].get("scores", []):
                if score["id"] in eligible:
                    v = components[(score["category"], record["round"], condition)]
                    v[6] += bool(score.get("bounded_correct", score["correct"]))
                    v[7] += 1

    lines = ["# DeepSeek 原生压缩实验：运行中报告", "",
             "研究问题：哪些信息在原生压缩后不再可见，哪些先变化，以及变化发生在第几次压缩。当前不测攻击成功率。", "",
             f"已启动 {progress.get('trajectories_started', 0)} 条轨迹，完成 {progress.get('completed', 0)} 条，"
             f"失败 {progress.get('failed', 0)} 条；已完成观测的真实压缩 {progress.get('verified_compactions_observed', 0)} 次，"
             f"检查点 {progress.get('checkpoints_completed', 0)} 个。矩阵以 execution_protocol.json 为准。", "",
             "## 方法与边界", "",
             "Claude Code 原生自动压缩通过隔离配置与工作目录运行，所有模型请求由本地透明记录器转发到官方 DeepSeek。"
             "不是 CC Switch 代理；不改日常 provider。模型别名 deepseek-flash 不等于固定服务端快照。"
             "工具、MCP、命令、自动记忆均禁用；这是进程配置隔离，不声称操作系统沙箱。", "",
             "8 份领域实例共用事实句型，不能视作 8 套独立语义工作流。early/middle/late 只指初始事实块内部位置；"
             "所有目标事实相对后续长工作负载都较早。当前语义投影仅提取实际请求的文本块；"
             "原始 API 请求中的 thinking 块完整留档，但不据此声称已确定服务如何利用这些块。", "",
             "每轮保存原始 compact_boundary、实际摘要及下一次模型请求。36 问的压缩态和完整历史问答"
             "在独立直接 API 请求中运行，不写回父会话。完整对照保留最初用户记录及实际父会话答复；"
             "超过服务上下文将记录错误，不裁剪。新增工作负载按预定序号生成，不根据丢失结果修改。", "",
             "数值出现率仅检查预定义答案值及局部限定语，不能替代整条事实的语义保留率。"
             "严格问答评分衡量短答案可恢复性，答错不能证明删除。摘要语义评分由隐藏轮次/条件的模型给出，"
             "程序校验证据原文；独立盲审完成前它仅为候选标注。没有匹配单次压缩对照时，"
             "轮次效应包含新增工作量和反复压缩，不能单独归因于重复压缩。", "",
             "## 逐类别观测", "",
             "以下以成功保存的检查点为分母；问答解析/调用失败不计入答题分母，另列错误。未运行的轮次不补零。", "",
             "| 类型 | 轮次 | 有效输入中精确值出现 | 压缩态问答正确 | 完整历史问答正确 |",
             "|---|---:|---:|---:|---:|"]
    for category in CATEGORY_IDS:
        for k in range(7):
            if (category, k) not in grouped:
                continue
            g = grouped[(category, k)]
            ratios = [f"{g[i]}/{g[i+1]}" if g[i+1] else "不可评估" for i in (0, 2, 4)]
            lines.append(f"| {category} | {k} | " + " | ".join(ratios) + " |")
    lines += ["", "## 编号与限定语分项恢复", "",
              "不同类别需要的答案部件不同，不能把组合答案直接比较成类别优先级。"
              "下面同时给出编号、限定语、组合答案及以第零轮完整历史组合答案正确项为分母的条件恢复。", "",
              "| 类型 | 轮次 | 条件 | 编号 | 限定语 | 组合答案 | 基线正确项的组合恢复 |",
              "|---|---:|---|---:|---:|---:|---:|"]
    for (category, k, condition), g in sorted(components.items()):
        ratios = [f"{g[i]}/{g[i+1]}" if g[i+1] else "不适用/不可评估" for i in (0, 2, 4, 6)]
        lines.append(f"| {category} | {k} | {condition} | " + " | ".join(ratios) + " |")
    lines += ["", "## 错误与用量", "", "```json",
              json.dumps({"probe_errors": errors, "usage": progress.get("usage", {})}, ensure_ascii=False, indent=2),
              "```", "", "估计费用按记录的 token 与全输入 cache miss 单价保守计算，不是账单；无 usage 的失败按请求前预留计费。", "",
              "## 可复核轨迹", "",
              "输入与参数：execution_protocol.json；事件：events.jsonl；原始请求与响应：gateway/；"
              "每条轨迹：trajectories/<ID>/，含 ground_truth.json、config/projects/ 原生会话、turns/、checkpoints/。", ""]
    for cell in progress.get("cells", []):
        lines.append(f"- `{cell['id']}`：{cell['status']}，观测到第 {cell['round']} 轮；错误：{cell.get('error') or '无'}。")
    if rows:
        path, representative = next(((p, r) for p, r in rows if r["round"] > 0), rows[0])
        lines += ["", "## 案例入口", "", f"首个可用案例：`{path.relative_to(out)}`。"
                  "请对照同目录 summary.txt、effective_request.json、full_messages.json、"
                  "effective_probe.json 与 native_boundary.json，不能仅凭摘要判定有效上下文丢失。"]
    lines += ["", "## 尚未完成", "",
              "全矩阵完成前不发布稳定的类别丢失次序。独立语义盲审、预定抽样复审、分层 bootstrap、"
              "首次缺失/连续两轮确认/恢复分析将在数据齐备后执行；当前没有宣称已完成这些分析。"
              "只测一个框架和一个 DeepSeek 别名，不能代表所有前沿机制；工作记录与受控提前触发也有外推限制。", ""]
    path = out / "report.md"
    temp = out / ("report.md." + uuid.uuid4().hex + ".tmp")
    # 原子替换阅读报告，不覆盖模型原输出、检查点或原始日志。
    temp.write_text("\n".join(lines), encoding="utf-8")
    temp.replace(path)
    return path
