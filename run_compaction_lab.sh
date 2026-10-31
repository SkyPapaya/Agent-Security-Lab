#!/usr/bin/env bash

# Compaction Boundary Security Lab
#
# This wrapper forwards all arguments to the Python experiment runner. It does
# not modify the normal Codex configuration, auth.json, proxy, or CC Switch.
# Credentials are resolved by the runner from the configured sub2api provider.
#
# Basic commands:
#   ./run_compaction_lab.sh --rounds 6
#   ./run_compaction_lab.sh --watch
#   ./run_compaction_lab.sh --help 

#   ./run_compaction_lab.sh --model gpt-5.6-terra --reasoning-effort medium --histories 3 --source-roles user tool
#
# Experiment controls:
#   --rounds N
#       Real native compactions per trajectory; integer 1..50. Default: 3.
#   --runs N
#       Independent layout repetitions per history; integer 1..30. Default: 1.
#   --histories N
#       Number of synthetic history families; integer 1..8. Default: 1.
#   --thresholds N [N ...]
#       Automatic-compaction thresholds in tokens; each must be >= 10000.
#       Default: 24000. Multiple values create separate experiment cells.
#   --trigger auto|manual
#       auto uses the Codex automatic threshold; manual calls native compact.
#       Default: auto. Do not combine results from these conditions.
#   --source-roles user assistant tool [ ... ]
#       Source placement diagnostic. Default: user. assistant/tool are
#       artificial injected-history controls and do not execute real tools.
#   --chunk-chars N
#       Synthetic filler characters added before each boundary; minimum 256.
#       Default: 8000.
#   --max-chunks N
#       Maximum filler chunks allowed while waiting for a native boundary.
#       Default: 120.
#
# Model and request controls (only this run; no shared config modification):
#   --model NAME
#       Override the model name from the sub2api provider config.
#   --reasoning-effort minimal|low|medium|high|xhigh
#       Override model reasoning effort. Default comes from config.toml.
#   --service-tier NAME
#       Override service tier for this run.
#   --config-file PATH
#       Alternate Codex config.toml; default: /home/haomin_wang/.codex/config.toml.
#   --auth-file PATH
#       Credential file to inspect for OPENAI_API_KEY; default:
#       /home/haomin_wang/.codex/auth.json. OAuth tokens are never used as a
#       third-party provider key; the provider env_key is used when needed.
#   --timeout SECONDS
#       Per app-server operation timeout. Default: 600.
#
# Safety and budget controls:
#   --max-requests N
#       Maximum upstream requests for the whole run. Default: 200.
#   --max-input-tokens N
#       Cumulative input-token guard. Default: 2000000.
#   --max-output-tokens N
#       Cumulative output-token guard. Default: 200000.
#   --no-probes
#       Disable detached capability probes; retain mechanical context records.
#   --out PATH
#       New output directory. Existing run directories are never overwritten.
#   --quiet
#       Disable the foreground progress display; progress.json is still saved.
#
# Observation and offline commands:
#   --watch [PATH]
#       Watch the latest run, or a specified run directory, read-only.
#   --export-legacy [PATH]
#       Offline-export the old DeepSeek run loss ledger; makes no API calls.
#
# Exit codes:
#   0 completed, 1 failed trajectory/measurement, 3 budget stopped,
#   130 interrupted. Use `./run_compaction_lab.sh --help` for the generated
#   argparse help and the exact option syntax.

# 中文阅读说明：本脚本只定位仓库，再把参数原样交给 Python。
# 不带 --watch/--help 时会开始实验；真实推理使用已有 sub2api 线路，可能产生费用。
# 脚本不自行修改日常 Codex 配置；隔离与只读凭据加载由 Python 后端负责。
#
# 常用参数（完整帮助以 --help 对应的 argparse 定义为准）：
#   --rounds N                 每条轨迹希望观测的原生压缩次数，默认 3。
#   --histories N              构造几份不同的合成历史，默认 1。
#   --runs N                   retention 研究中的材料布局重复数，默认 1。
#   --thresholds N [N ...]     自动压缩阈值，单位 token；默认 24000，可比较多个阈值。
#   --trigger auto|manual     自动触发或手动请求原生压缩，两种条件不能混算。
#   --source-roles ...         材料放入 user/assistant/tool 的角色诊断；不代表真实工具读取。
#   --model NAME              本次模型别名，不写回共享配置；以线路实际支持的名字为准。
#   --reasoning-effort LEVEL   本次推理强度，支持列表及默认值见上方和 --help。
#   --service-tier NAME        本次服务档位，不承诺上游一定支持。
#   --chunk-chars N            每次填充的字符数，默认 8000；字符数不等于 token 数。
#   --max-chunks N             等待压缩时的最大填充块数，默认 120，防止无限循环。
#   --timeout SECONDS          一次原生操作的超时，默认 600 秒。
#   --max-requests N           全实验请求数上限，默认 200。
#   --max-input-tokens N       累计输入 token 上限，默认 2000000。
#   --max-output-tokens N      累计输出上限，默认 200000；不是单次回复长度限制。
#   --max-consecutive-failures 连续失败多少条后停止，默认 3。
#   --no-probes                只记录可见上下文，不询问信息恢复能力，不能据此下能力结论。
#   --out PATH                本次独立结果目录；不能覆盖已有实验协议所在的目录。
#   --quiet                   关闭终端进度条，仍写 progress.json。
#   --watch [PATH]            只读查看指定目录的进度；不填路径则查看最近一次实验。
#   --export-legacy [PATH]    离线整理旧实验的丢失内容记录，不调用模型。
#   --config-file/--auth-file 替换只读配置或凭据路径；不要在命令行直接写密钥。
#
# 专门比较“事项—来源—授权”对应关系时：
#   --study relations         选择关联信息实验；默认 study 是 retention。
#   --task-conditions ...     record_only=只记录，pending_review=稍后核对，两者默认都运行。
#   --layout N                固定材料布局编号，默认 0。
#   --generation-repeats N    同一布局重新建立独立会话的次数，默认 1。
#   relations 必须配 --source-roles tool，保持 --runs 1，不能加 --no-probes。
#
# 示例：六轮关系观测，只用一份历史、每种任务条件各一条独立会话：
#   ./run_compaction_lab.sh --study relations --source-roles tool --rounds 6 \
#     --histories 1 --generation-repeats 1 --model gpt-5.6-terra --reasoning-effort medium
#
# 建议阅读顺序：本脚本 → native_retention/codex_runner.py → codex_backend.py /
# responses_gateway.py → relation_measurement.py / loss_ledger.py。
# 主要结果：progress.json 看进度，lost_content.md 看每次变化，events.jsonl 查事件，
# gateway/ 和 checkpoints/ 查原始请求及摘要。字符串未命中并不等于语义消失。

# 遇错退出、拒绝未定义变量、管道任一环节失败都返回失败，避免静默继续。
set -euo pipefail
# 使用脚本本身所在目录，保证从其他工作目录启动也能找到 Python 包。
lab_repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd -- "$lab_repo_dir"
# exec 用 Python 替代当前 shell，便于 Ctrl+C 和退出码直接传递；"$@" 保留原始参数边界。
exec python3 -m experiments.llama_cpp.compaction_security.native_retention.codex_runner "$@"
