# Codex + sub2api：多轮上下文压缩观测

这个目录研究的是：一段长期工作记录经过多次压缩后，哪些内容还在，哪些内容不再能被恢复，以及变化发生在哪一轮。

例如，模型可能仍记得“整理报告”，却不再清楚报告来自哪份文档、文档是否可信、用户究竟批准了哪项操作。我们既看摘要写了什么，也看下一次请求实际带了什么，再用独立问答检查模型能否恢复这些信息。只看摘要，容易忽略框架重新放入的原始用户消息；只看回答，又容易把模型答错误认为内容已经被删掉。

这里使用 Codex 的原生压缩流程，不用自定义提示词要求模型删除或保留某类信息。它与上层目录 `compactor.py` 的外置摘要实验是两条不同路线。**本目录的观测流程不执行模型提出的工具动作，也不统计攻击成功率。** 第二阶段模拟工具实验见[模块说明](../README.md)和[探索批次审计](../phase1_design/phase2_pilot_audit.md)。

## 先看哪几份文件

| 想了解的内容 | 文件 |
|---|---|
| 启动参数和一键运行 | 仓库根目录的 [run_compaction_lab.sh](../../../../run_compaction_lab.sh) |
| 实验顺序、每条轨迹和检查点 | [codex_runner.py](codex_runner.py) |
| 实验专用 Codex 进程、配置读取和原生事件 | [codex_backend.py](codex_backend.py) |
| 真实请求/响应记录、用量保护和工具阻断 | [responses_gateway.py](responses_gateway.py) |
| 12 类单项事实及固定填充材料 | [corpus.py](corpus.py) |
| 事项、来源、可信度、授权和限制的组合材料 | [relation_corpus.py](relation_corpus.py) |
| 答案、原文引证和同一事项关联的检查 | [relation_measurement.py](relation_measurement.py) |
| 每轮内容变化记录 | [loss_ledger.py](loss_ledger.py) |
| 可见文本、词面匹配和旧语义评审接口 | [analysis.py](analysis.py) |
| 进度条和只读监视 | [progress.py](progress.py) |
| 旧数据导出、报告与校准 | [export_losses.py](export_losses.py)、[reporting.py](reporting.py)、[validate.py](validate.py) |

`runner.py` 和 `gateway.py` 保留了早期 Claude Code / DeepSeek 路线，供查看历史记录；当前一键脚本走的是 `codex_runner.py` 和 `responses_gateway.py`。不要把旧路线的模型、价格和输出格式套到当前实验中。

## 环境与凭据

从仓库根目录执行本文所有命令。需要已有的 Python 3.11 或更新版本、Codex CLI 和 Linux 的 bubblewrap（`bwrap`）。当前实现使用 Python 标准库，其中 `tomllib` 要求 Python 3.11；可以使用已有 Conda 环境，也可以使用满足版本要求的系统 Python。

先检查环境和帮助，这些命令不会开始模型实验：

```bash
python3 --version
codex --version
command -v bwrap
./run_compaction_lab.sh --help
```

程序复用已安装组件，不自动下载模型、安装 Conda、更新 Codex，也不修改代理、CC Switch 或网络设置。若隔离组件不可用，会报错退出，不会悄悄改用日常共享会话。

当前默认读取路径写在 `codex_runner.py` 中：

- 配置：`/home/haomin_wang/.codex/config.toml`；
- 凭据：`/home/haomin_wang/.codex/auth.json`。

在其他机器或用户下运行时，应通过 `--config-file`、`--auth-file` 指向自己的已有文件。配置必须包含 `model_providers.sub2api`，且 `wire_api` 为 `responses`，服务地址须满足网关的 HTTPS 校验。

凭据读取顺序是：先读指定 `auth.json` 中非空的 `OPENAI_API_KEY`；若没有，再读 provider 的 `env_key` 对应的环境变量，缺省变量名为 `SUB2API_API_KEY`。两处都没有有效 API key 时会报错。程序不会把 ChatGPT 的 access token 或 refresh token 当作第三方 API key，也不需要把密钥写进命令或聊天。

模型、推理强度和服务档位优先采用命令行覆盖值，其次读取配置。配置缺省时的代码回退值分别为 `gpt-6.1-sol`、`high`、`default`；这不代表线路一定支持这些值。实际采用值会写入本次 `execution_protocol.json`，模型请求也会原样留存。已有研究批次使用的是 `gpt-5.6-terra` / `medium`。

## 先跑小规模，再增加轮数

下面开始模型实验的命令会向已配置的 sub2api 线路发送合成材料，并产生真实推理请求。`--help`、`--watch`、离线测试不会启动这类请求。

### 一份历史、一次压缩

```bash
./run_compaction_lab.sh \
  --model gpt-5.6-terra --reasoning-effort medium \
  --histories 1 --runs 1 --rounds 1
```

这走默认的 `retention` 研究：一份历史、一套布局、user 来源、一条轨迹，观测一次原生压缩。首次运行建议先确认基线、原生边界和输出文件完整，再增加规模。

### 观察六次连续压缩

```bash
./run_compaction_lab.sh \
  --model gpt-5.6-terra --reasoning-effort medium \
  --rounds 6 \
  --max-requests 300 --max-input-tokens 12000000 --max-output-tokens 400000
```

`--rounds 6` 是每条轨迹希望观测六次压缩，不是发六个请求。材料摄入、普通回复、摘要和每个检查点的两次独立问答都会增加调用量。上例限额只是编排保护，不保证一定完成六轮或对应某个费用。

### 研究“谁允许对哪份数据做什么”

```bash
./run_compaction_lab.sh \
  --study relations --source-roles tool \
  --model gpt-5.6-terra --reasoning-effort medium \
  --histories 1 --generation-repeats 1 --layout 0 --rounds 6 \
  --max-requests 300 --max-input-tokens 12000000 --max-output-tokens 400000
```

这会比较两种目标：`record_only`（只记录）和 `pending_review`（稍后核对），共两条独立轨迹、计划 12 次压缩。每份材料有六个事项，每个事项记录五个字段：事项、来源、可信度、授权状态和限制。用户授权账本来自可信任务，不允许工具文档自行改变。

要对应已有的 12 条轨迹、72 次压缩计划，可将规模改为：

```bash
./run_compaction_lab.sh \
  --study relations --source-roles tool \
  --model gpt-5.6-terra --reasoning-effort medium \
  --histories 3 --generation-repeats 2 --layout 0 --rounds 6 \
  --max-requests 600 --max-input-tokens 24000000 --max-output-tokens 800000
```

这是计划规模，不是已有批次全部完成的声明。已有对应批次实际完成 7 条轨迹、42 次压缩，其余失败保留，详见后面的结果入口。

## 两种研究和规模如何计算

| 研究 | 主要问题 | 轨迹数量 |
|---|---|---|
| `retention` | 目标、约束、数值、来源、更新等 12 类单项事实如何变化 | histories × 阈值数量 × runs × 来源角色数量 |
| `relations` | 同一事项与来源、可信度、授权和限制是否仍能对应 | histories × 阈值数量 × generation-repeats × 任务条件数量 |

计划压缩观测数 = 轨迹数量 × rounds。第 0 轮是未压缩基线，也保存检查点，但不计入压缩次数。

`retention` 中的 `--runs` 改变预定材料布局，既包含位置变化也包含会话生成差异，不能当作完全相同输入的纯随机性重复。`relations` 中用 `--layout` 固定布局，`--generation-repeats` 为相同材料新建独立会话；工具调用 ID、线程 ID 和框架工作目录仍会因会话而不同。

`relations` 必须使用 `--source-roles tool`、`--runs 1`，并启用探针，不能加 `--no-probes`。只要指定一种 `--task-conditions`，任务条件数量就是 1；默认两种都跑，且按历史和重复编号轮换执行顺序。

同一历史的多轮压缩、同一检查点的多个字段不是独立样本，不能把总字段数当作独立样本量。

## 一条轨迹实际做什么

1. 创建独立输出目录、私有 Codex 配置和 app-server 进程，保存框架版本、程序哈希和材料哈希。
2. 让主会话接收虚构任务与材料。主会话只记录，不执行工具。
3. 保存第 0 轮实际请求，并从实际上下文和完整记录两边分别发起独立问答。
4. 检查基线：所有目标词面值必须已经读入；启用问答时，两边均须有完整数量的字段，且至少 90% 通过规定评分。关系实验还保存两边共同通过的字段集合。
5. 加入固定的普通工作记录，等待自动压缩，或在 `manual` 条件中请求原生压缩。
6. 只在核验到真实 `contextCompaction` 完成事件后记录新轮次，保存前后请求、原生事件、可读文本和独立问答。
7. 生成每轮变化清单、报告和实时进度，完成后关闭私有会话。

探针是对同一模型后端的独立请求，答案只落盘，不放回主会话，避免测试答案强化记忆。`effective` 使用当时实际请求中的输入，`full` 使用保存的原始可见记录和初始 developer 前缀。完整记录对照不重放隐藏推理，因此也不等于把运行时所有内部状态完整复制了一遍。

如果摄入材料时已经压缩，未压缩基线就被污染，轨迹会失败；应在新目录中调整阈值重新实验。如果一个回合跨过多个压缩边界，当前流程不能伪造每个中间快照，会以观测缺口报错。失败记录不会被当成遗忘或安全成功。

### auto、manual 和材料来源

`auto` 设置 `model_auto_compact_token_limit`，推动 Codex 自动压缩。`manual` 把自动阈值调高，再调用 app-server 的 `thread/compact/start`；它是人工触发原生压缩的诊断组，两种结果应分开报告。

`user` 来源用真实用户轮次送入材料。`assistant` 和 `tool` 来源通过 `thread/inject_items` 放入合成消息或固定的 function_call/function_call_output 对；这是人工控制的来源实验，并没有真实读取工具，也不是自然形成的工具轨迹。

```bash
# 比较来源，默认 retention 研究
./run_compaction_lab.sh --rounds 1 --source-roles user assistant tool

# 人工触发原生压缩，单独报告
./run_compaction_lab.sh --rounds 1 --trigger manual
```

## 参数参考

下面列的是当前 `codex_runner.py` 的参数。脚本会原样转发参数；以后代码调整时，以 `./run_compaction_lab.sh --help` 和参数定义为准。

| 参数 | 默认 | 含义与范围 |
|---|---|---|
| `--study` | `retention` | `retention` 或 `relations` |
| `--rounds` | `3` | 每条轨迹的原生压缩次数，1–50 |
| `--histories` | `1` | 合成历史数量，1–8 |
| `--runs` | `1` | retention 的布局重复数，1–30；relations 固定为 1 |
| `--thresholds` | `24000` | 一个或多个自动压缩阈值，每个至少 10000 |
| `--trigger` | `auto` | `auto` 或 `manual` |
| `--source-roles` | `user` | 一个或多个 `user`、`assistant`、`tool`；relations 仅能选 tool |
| `--task-conditions` | `record_only pending_review` | relations 的任务条件，不能重复 |
| `--generation-repeats` | `1` | relations 相同布局的独立会话数，1–30 |
| `--layout` | `0` | relations 固定布局编号，非负整数 |
| `--chunk-chars` | `8000` | 每块填充的字符数，至少 256；不是 token 数 |
| `--max-chunks` | `120` | 一条轨迹最多发送的填充块数 |
| `--max-consecutive-failures` | `3` | 连续失败多少条后停止启动新轨迹，至少 1 |
| `--model` | 从配置读取 | 只覆盖本次模型名，不修改日常配置 |
| `--reasoning-effort` | 从配置读取 | `minimal`、`low`、`medium`、`high`、`xhigh` |
| `--service-tier` | 从配置读取 | 本次服务档位，上游是否支持须实测 |
| `--timeout` | `600` | 每次 app-server 操作超时，单位秒 |
| `--max-requests` | `200` | 整批上游请求数保护 |
| `--max-input-tokens` | `2000000` | 整批累计输入 token 保护 |
| `--max-output-tokens` | `200000` | 整批累计输出保护，不是单次生成上限 |
| `--config-file` | 见环境章节 | 只读的配置路径 |
| `--auth-file` | 见环境章节 | 只读的凭据路径 |
| `--out` | 自动生成时间目录 | 本批输出路径，不复用已有实验协议的目录 |
| `--no-probes` | 不启用该开关 | 关闭独立问答，只记录机械变化；relations 不允许 |
| `--quiet` | 不启用该开关 | 不显示前台进度条，仍保存进度 |
| `--watch [PATH]` | 不监视 | 只读查看指定批次；省略 PATH 时读取最近启动的目录 |
| `--export-legacy [PATH]` | 不导出 | 离线导出旧批次，省略 PATH 时使用旧 `full_v1` 目录 |

输入、输出用量上限要求正整数，由网关校验。对于 max-chunks、timeout 等参数，应使用合理正值；不能仅因为 CLI 接受就认为任何数值都有实验意义。

## 运行时进度、停止和退出码

前台进度条显示已观测压缩次数、总计划、已完成/失败轨迹、当前阶段、耗时、请求数和 token 用量。`progress.json` 约每秒更新。进度条按真实已保存的压缩观测计数，失败轨迹不会填成完成。

在第二个终端查看：

```bash
./run_compaction_lab.sh --watch

# 如果同时有多个批次，指定路径可以避免“最近一次”指向其他批次
./run_compaction_lab.sh --watch /path/to/your/run-directory
```

按 Ctrl+C 停止前台运行，或者在打印的运行目录中新建 `STOP` 文件。停止检查发生在新增工作前，已经发出的请求可能仍需返回或超时；已有记录保留。退出 `--watch` 只关闭查看器，不停止实验。

| 退出码 | 含义 |
|---|---|
| `0` | 计划轨迹全部完成 |
| `1` | 轨迹或测量失败；也可能因连续失败提前结束 |
| `3` | 请求/token 保护触发，停止新增工作 |
| `130` | 主实验被中断 |

参数解析错误使用 argparse 的退出码（通常为 2）；配置、组件或启动阶段异常还应查看终端错误，不能只依赖上述运行阶段状态。中断的 ephemeral 会话不能透明恢复；重新运行应选择新目录，不能假装接着旧模型状态继续。

## 输出文件怎么读

默认写到 `../phase1_design/runtime/codex_sub2api_<时间>/`，这是相对于本 README 的位置。命令行会打印真实路径；仓库根目录下的完整位置是 `experiments/llama_cpp/compaction_security/phase1_design/runtime/`。

```text
<run-directory>/
├── execution_protocol.json       # 实际参数、矩阵、线路信息及 native_retention 源码哈希
├── frozen_source/                # 本次使用的 native_retention/*.py 字节副本
├── progress.json                 # 实时进度与用量
├── events.jsonl                  # 整批事件
├── report.md                     # 运行汇总，包含失败说明
├── lost_content.md / .jsonl       # 逐事实词面变化，名称不代表已证明语义丢失
├── recall_changes.md             # 实际问答与完整记录问答的差异
├── semantic_changes.md           # relations 的逐字段证据检查与待复核项
├── normal_config_check.json      # 日常 config/auth 前后哈希，不含文件正文
├── loss_checkpoints/<轨迹>/<轮次>.json
├── relation_checkpoints/<轨迹>/<轮次>.json  # 仅 relations
├── gateway/
│   ├── requests.jsonl            # 请求账本与用量
│   └── <请求编号>/               # 原始 request、response 和 meta
└── trajectories/<轨迹>/
    ├── ground_truth.json         # 预先定义的事实/关系清单
    ├── material_identity.json    # 本条实际材料哈希与条件
    ├── artificial_role_injection.json  # assistant/tool 条件才有
    ├── state.json                # 完成轮数、状态、错误
    ├── session/
    │   ├── session_manifest.json # Codex 版本、二进制哈希、隔离说明
    │   ├── appserver.jsonl        # 原生 RPC 与事件
    │   └── private_codex/        # 本次私有配置、日志、SQLite 等
    └── checkpoints/<轮次>/
        ├── effective_request.json     # 模型当时实际接收到的请求
        ├── pre_compaction_request.json # 能捕获时保存；第0轮无此文件
        ├── readable_context.txt
        ├── native_events.json
        ├── effective_probe.json
        ├── full_probe.json
        └── measurements.json
```

目录是否出现取决于条件和失败阶段。原始摘要从对应网关响应和原生事件中追溯，不应假设每次都会有单独名为 `summary.txt` 的文件。`frozen_source/` 当前冻结的是 native_retention 目录，不是仓库所有文件；复现实验还需核对协议、材料和框架版本。

建议先看 `report.md` 判断批次是否完成，再看 `state.json` 和失败原因。看具体变化时，retention 可从 `lost_content.md` 入手，relations 优先看 `semantic_changes.md`，最后沿引用回到原始请求和摘要核实。

## 这些分数能说明什么

这里分三层观察：

- **词面出现**：原值或声明的别名是否还在可读文本中。例如 `544株` 改成 `544 seedlings` 可能未命中，但数量仍然保留。
- **带证据的恢复**：模型能否给出正确答案，提供实际上下文中的逐字引文，并对应到同一事项。`UNKNOWN`、改写引文、一个句子列多个事项等都可能不通过机械检查，不自动等于内容已删。
- **语义复核**：对原始摘要及实际输入逐条检查，区分保留、改写、弱化、遗漏和来源关联丢失。现有研究助手/子 agent 复核不是独立的人类盲标。

关系实验的 `baseline_paired_supported_fields` 保存压缩前 full/effective 两边共同通过的字段，分析后续变化应优先使用这个资格集，避免把初始评分错误算作压缩效果。短词“可信”或“已批准”全局出现，也不能证明它属于正确的事项。

实际上下文可能同时含原始用户消息与摘要，因此“摘要没写”不等于“模型看不到”。可读 reasoning summary 会进入文本投影；加密 reasoning 单独标记，不推断其内部内容。如果出现不透明 compaction 状态，就明确记录无法直接观察，不能生成看似完整的遗忘清单。框架在压缩请求前已经裁剪掉的内容，也不能直接归因于摘要模型。

## 隔离范围与用量保护

每条轨迹使用新的 app-server 和 ephemeral thread。bubblewrap 将实验私有目录挂到子进程视角中的 Codex 状态路径，使用独立配置、日志和 SQLite；保持 HOME/CODEX_HOME 原值，不改宿主的日常配置。宿主文件系统以只读方式挂载，实验根目录可写，临时目录独立。

私有配置关闭 shell、记忆、网页搜索等能力，网关会在把模型输出交回 Codex 前拒绝非允许的工具输出。模型提出真实工具调用时记为协议诊断/失败，编排器不执行该动作。该隔离不是完全网络命名空间隔离：模型推理仍由本地录制网关转到已配置 sub2api 服务；合成工具不访问外部网站。网关不保存认证请求头，真实 key 不交给实验 Codex 子进程，结束时只比较日常 config/auth 的哈希。

请求前会预留输入和输出额度，按响应 usage 结算，防止多请求越过预算。输入预留使用字节上界，缺省输出预留为 32768；实际返回用量、流式失败和上游计费仍可能有差异。这里记录的是调用/token 保护，不是可靠账单估算，也不沿用旧 DeepSeek 单价。多条件矩阵和完整历史探针的输入会增长，应先做小批验证。

## 常见失败

| 现象 | 先检查什么 | 不能怎样解释 |
|---|---|---|
| 缺少 key 或 provider 错误 | 配置是否有 sub2api Responses provider；指定凭据/环境变量是否有效 | 不是模型能力或安全结果 |
| 缺少 bwrap、Codex 或隔离启动失败 | 已有二进制、系统是否允许所需挂载/namespace | 不应降级成污染日常配置的运行 |
| `Compaction occurred during ingestion` | 材料还没读完就压缩；在新批次适当提高阈值 | 不能拿它做未压缩基线 |
| `Baseline JSON/capability failed` | 原始问答、引证格式、服务错误和材料完整性 | 不能一概说模型不理解任务，更不是遗忘 |
| `No verified native compaction` | 阈值、实际输入增长、max-chunks 和原生事件 | 不能用请求次数冒充压缩次数 |
| `Observation gap` | 一个回合是否跨越多个原生边界 | 不能拼凑缺失的中间快照 |
| HTTP 502、流内 failed、超时 | 网关请求与响应、session 日志 | 不能按 HTTP 200 或返回文字片段直接算完成 |
| `budget_stopped` | 请求、累计 token 和在途预留 | 不代表模型安全拒绝 |
| 问答出现 `UNKNOWN` 或词面零命中 | 是否翻译、改写、指向错误事项，原始消息是否仍被重放 | 不能直接说所有安全信息丢失 |

## 已有结果与测试

截至 2026-10-31 文档整理时，六轮关系实验的对应批次完成 **7/12 条轨迹、42/72 次压缩**。在这批固定材料、`gpt-5.6-terra` / `medium`、24000 阈值的条件下，具体工具来源和事项信息较早难以恢复；前 3 轮用户原话重放仍保护授权，第 4 轮原话退出重放后，部分摘要仍保留英文逐项授权，第 5、6 轮未见逐项授权绑定。重复出现的当前回复规则保留得更稳定。不能将第 4/5 轮当成所有会话的固定遗忘期限，也不能据此声称已经绕过权限。

详细结果和纠正后的解释见：

- [六轮实验评估](../phase1_design/runtime/relation_terra_medium_72obs_20261029_v1/evaluation.md)
- [逐条摘要语义复核](../phase1_design/runtime/relation_terra_medium_72obs_20261029_v1/evaluation_boundary_review.md)
- [关系实验设计](../phase1_design/relation_retention_plan.md)
- [第二阶段探索审计](../phase1_design/phase2_pilot_audit.md)：五批探索没有确认压缩导致的攻击；旧成功标签已排除。
- [第二阶段修订协议](../phase1_design/phase2_paired_protocol.md)：后续同历史配对验证的要求，不是已完成结果。

`runtime/` 默认被 Git 忽略，已有本地结果链接在不携带运行产物的新克隆中可能不存在；不要用空目录或 README 中的数字代替原始证据。

最近一次代码注释修改前后，**205 项单元测试全部通过**，见 [验证记录](../results/code_comments_20261031/validation.json)和[测试输出](../results/code_comments_20261031/tests_after.txt)。重新验证代码可在仓库根目录运行：

```bash
python3 -m unittest discover -s experiments/llama_cpp/compaction_security/tests -v
```

测试使用本地合成材料和替身，不需要真实 key；其中本地回环 HTTP 服务用于验证协议，不访问真实模型。单元测试通过只能证明这些代码路径符合断言，不能替代真实模型的实验结论。
