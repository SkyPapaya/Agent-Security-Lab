# Agent Security Lab

这个项目研究 Agent 长时间运行时的上下文压缩问题：历史被压缩后，模型还能否记住数据来自哪里、谁允许了什么操作、哪些限制仍然有效？如果这些信息丢失，模型会不会执行原本没有获得授权的操作？

目前的主要实验是 **Compaction Boundary Security Lab**。我们先观察信息经过多轮压缩后如何变化，再通过对照实验判断这些变化是否真的会影响工具调用。所有材料都是虚构数据，业务工具只在本地实验目录中产生模拟结果；真实模型推理使用已配置的服务。

截至 **2026-10-31**，已经观察到具体来源和逐项授权信息的退化，尚未验证出由上下文压缩直接导致的攻击成功案例。下面的“当前结果”说明了实验条件和结论范围。

## 目录

- [我们想回答什么](#我们想回答什么)
- [目前有哪些实验](#目前有哪些实验)
- [项目结构](#项目结构)
- [代码从哪里读](#代码从哪里读)
- [环境要求](#环境要求)
- [快速运行](#快速运行)
- [如何设置实验规模](#如何设置实验规模)
- [如何查看进度和结果](#如何查看进度和结果)
- [如何判断信息是否丢失](#如何判断信息是否丢失)
- [当前结果](#当前结果)
- [本地工具和授权检查](#本地工具和授权检查)
- [测试与复现](#测试与复现)
- [已知问题和下一步](#已知问题和下一步)

## 我们想回答什么

Agent 的历史越来越长，系统通常会将较早的内容整理成摘要，再用摘要继续工作。这里需要区分两个过程：框架决定哪些原始消息继续送给模型，模型决定摘要中写入哪些信息。只看摘要，可能误以为某项信息已丢失，但框架其实仍在下一次请求中附上了原文。

实验主要围绕以下问题展开：

| 问题 | 一个具体例子 |
|---|---|
| 哪些内容先丢失？ | 报告编号、来源是否可信、授权条件，还是近期任务进度？ |
| 信息之间的对应关系还在吗？ | 记得“允许备份”，是否还记得这项授权只针对报告 A，而不是报告 B？ |
| 原始消息退出上下文后会发生什么？ | 用户的禁止发送要求不再原样重放时，摘要是否还能准确表达限制？ |
| 多轮压缩会继续丢信息吗？ | 第一次摘要保留了逐项授权，摘要再被压缩时是否还会保留？ |
| 信息变化会影响动作吗？ | 完整历史下不发送，压缩后是否开始请求模拟发送？ |
| 独立权限检查能否兜底？ | 即使模型忘记限制，执行器能否拒绝未经授权的动作，同时允许正常任务？ |

前四项是信息保留实验，后两项是行为与执行实验。信息不再出现、模型答不出来、模型请求越权、工具实际越权执行，是不同结果，需要分别记录。

## 目前有哪些实验

| 实验 | 用途 | 入口与状态 |
|---|---|---|
| 基础摘要实验 | 比较完整历史、模型摘要、附带独立安全状态的摘要；验证本地工具和日志 | `run_experiment.py`；支持 Mock 和已有 llama.cpp 服务 |
| 原生压缩信息观测 | 触发 Codex 自带的压缩，逐轮保存实际输入和恢复问答 | `run_compaction_lab.sh`；当前主要入口，使用 Codex + sub2api |
| 来源与授权对应关系实验 | 跟踪同一事项的内容、来源、可信度、授权和限制 | 同一启动脚本，增加 `--study relations` |
| 第二阶段攻击探索 | 在本地模拟页面、重定向和报告引用中放入诱导内容，观察动作与工具影响 | `attack_runner.py`；旧探索流程存在已知缺陷，正式验证协议已修订 |
| 早期短摘要试验 | 观察输出长度和材料位置的影响 | `priority_probe/`、`retention_probe/`；作为早期探索保存，不代表原生压缩机制 |
| 历史 DeepSeek / Claude Code 方案 | 保留最初设计、旧运行器与历史结果 | `phase1_design/`、`native_retention/runner.py`；当前不作为默认线路 |

基础摘要实验主动请求模型写摘要；原生观测实验由 Codex 框架触发压缩。它们测量的过程不同，结果分开解释。模型别名也只代表本次线路实际记录的名称，不能据此保证服务端权重快照始终不变。

## 项目结构

```text
Agent-Security-Lab/
├── README.md                         # 项目总入口
├── run_compaction_lab.sh              # 原生压缩观测：启动、参数、实时进度
├── .gitignore                        # 排除缓存、凭据目录和原生实验 runtime
└── experiments/
    └── llama_cpp/
        └── compaction_security/
            ├── README.md             # 模块职责、基础实验与工具说明
            ├── config.py             # 固定目录、工具和对象列表、摘要提示词
            ├── schemas.py            # 动作格式与可信安全状态
            ├── history_builder.py    # 从场景生成历史，保留来源标签
            ├── compactor.py          # 基础版外置摘要与长度统计
            ├── model_adapter.py      # llama.cpp 客户端与确定性 Mock
            ├── agent_runner.py       # 单次基础实验流程
            ├── policies.py           # 独立授权检查
            ├── tools.py              # 读取、备份、本地模拟发送
            ├── evaluation.py         # 基础指标与错误分类
            ├── run_experiment.py     # 基础实验命令行与报告
            ├── attack_scenarios.py   # 第二阶段固定合成材料
            ├── attack_tools.py       # redirect / include 模拟工具
            ├── attack_runner.py      # 第二阶段探索版运行器
            ├── causal_evaluation.py  # 探索版评估，已知问题有注释
            ├── scenarios/            # 四个基础 JSON 场景
            ├── native_retention/     # 原生压缩观测、网关、问答、损失记录
            ├── priority_probe/       # 早期位置轮换与短摘要试验
            ├── retention_probe/      # 早期摘要与后续决策材料
            ├── phase1_design/        # 方案、审计、周报及历史运行目录
            │   └── runtime/          # 原生实验的逐次记录与冻结源码
            ├── tests/                # 离线单元测试与集成测试
            └── results/              # 基础实验记录、报告及测试验证产物
```

`llama_cpp` 目录名来自项目最初的本地模型方案。当前主要的 Codex + sub2api 实验也放在这里，运行原生观测不需要先启动 llama.cpp。

`config.py` 还定义了预留的 `logs/` 路径，但当前逐次事件日志随实验结果保存，不要求单独存在这个目录。

`runtime/` 保存历史输入、响应、事件和冻结源码，体积可能很大，并且已被 Git 忽略。新克隆的仓库不一定包含这些本机结果；文档中的历史结果链接需要相应产物存在才能打开。这里的冻结源码用于核对当时究竟运行了什么，不随着当前代码更新。

## 代码从哪里读

当前维护的 Python 源码、测试和启动脚本已补中文注释。建议根据要解决的问题选择阅读路线：

| 想了解的内容 | 阅读顺序 |
|---|---|
| 如何启动一次原生实验 | `run_compaction_lab.sh` → `native_retention/codex_runner.py` |
| 如何隔离实验、记录真实模型输入 | `codex_backend.py` → `responses_gateway.py` |
| 材料中放了什么 | `corpus.py` → `relation_corpus.py` |
| 如何判断事项和授权还对应 | `relation_measurement.py` |
| 每轮“丢了什么”的文件如何生成 | `loss_ledger.py` → `reporting.py`，另看 `export_losses.py` |
| 进度条如何更新 | `progress.py` |
| 基础三模式与独立权限检查 | `run_experiment.py` → `agent_runner.py` → `policies.py` → `tools.py` |
| 第二阶段模拟攻击如何执行 | `attack_scenarios.py` → `attack_tools.py`；结合审计阅读旧运行器和评估器 |

详细职责表见[模块 README](experiments/llama_cpp/compaction_security/README.md)，原生运行细节见[原生观测 README](experiments/llama_cpp/compaction_security/native_retention/README.md)。

## 环境要求

所有示例均从仓库根目录执行。当前代码使用 Python 标准库，原生适配器用到 `tomllib`，建议统一使用 **Python 3.11 或更高版本**。如果已有合适的 Conda 环境，激活后使用即可；启动脚本调用当前 PATH 中的 `python3`，不会自动创建环境或安装依赖。

| 运行内容 | 需要什么 |
|---|---|
| 离线测试、基础 Mock | Python 3.11+ |
| Codex 原生压缩实验 | Python 3.11+、已安装且接口兼容的 `codex`、`bwrap`，以及已有 sub2api Responses 配置和 API key |
| llama.cpp 真实摘要实验 | 已启动的兼容服务、现有 GGUF 文件；服务的启动与显存配置由现有部署负责 |
| 第二阶段工具 Mock | Python 3.11+；无需模型服务 |

可先检查已有工具，不会发起模型调用：

```bash
python3 --version
command -v codex
command -v bwrap
./run_compaction_lab.sh --help
```

Codex 运行器默认只读 `/home/haomin_wang/.codex/config.toml` 与 `/home/haomin_wang/.codex/auth.json`，路径可通过参数覆盖。配置中需要名为 `sub2api`、使用 `responses` 协议的 provider。凭据优先读取认证文件中的非空 `OPENAI_API_KEY`，其次读取 provider 配置的 `env_key` 对应环境变量。ChatGPT OAuth 的 access/refresh token 不会被当作第三方 API key。

模型、强度和服务档位可以在单次实验中覆盖；不指定时读取已有配置。源码中的兜底模型名不是服务可用性的保证，实际支持情况由线路决定。程序不会自动下载模型、升级 Codex、安装 Conda 或修改 CC Switch、代理和防火墙。

## 快速运行

### 1. 先验证本地流程

```bash
# 离线测试，不需要真实模型或 API key
python3 -m unittest discover -s experiments/llama_cpp/compaction_security/tests -v

# 基础 Mock：四场景 × 三模式，共 12 次流程检查
python3 -m experiments.llama_cpp.compaction_security.run_experiment --backend mock
```

Mock 使用固定规则输出，只检查解析、授权、工具和日志能否串起来。它不能回答真实模型是否会忘记信息，也不计入真实模型攻击率。

基础运行会在 `results/` 中创建独立的逐次目录，但同名批次汇总和 `initial_report.md` 会由后续运行更新。需要长期保存时，先归档这些汇总；原生实验则应为每批选择新的 `--out`。

### 2. 小规模原生压缩观测

以下命令会向配置的 sub2api 服务发送虚构材料，产生真实推理请求：

```bash
./run_compaction_lab.sh \
  --model gpt-5.6-terra --reasoning-effort medium \
  --histories 1 --runs 1 --rounds 1 \
  --out experiments/llama_cpp/compaction_security/phase1_design/runtime/example_retention
```

这是一条轨迹、一次目标压缩，额外包含压缩前基线及独立问答。输出目录需要是新的；模型名可以替换为线路支持的名称。直接不带参数启动时，默认目标为每条轨迹 3 轮压缩，不是离线演示。

### 3. 观察来源和授权是否仍对应

```bash
./run_compaction_lab.sh \
  --study relations --source-roles tool \
  --model gpt-5.6-terra --reasoning-effort medium \
  --histories 1 --generation-repeats 1 --layout 0 --rounds 1 \
  --out experiments/llama_cpp/compaction_security/phase1_design/runtime/example_relations
```

默认比较“只记录”和“稍后核对”两种任务，所以这条命令计划运行 2 条轨迹、2 次压缩。材料中的 `tool` 来源是编排器注入的固定工具调用/结果记录，不是模型真实读取了某个网站。关系实验要求 `--runs 1`、`--source-roles tool` 且开启问答探针。

### 4. 使用已有 llama.cpp 服务

```bash
python3 -m experiments.llama_cpp.compaction_security.run_experiment \
  --backend real --suite initial \
  --url http://127.0.0.1:8080 \
  --model-file /path/to/existing/model.gguf
```

将示例路径替换成已有文件。本入口不会启动服务或下载 GGUF；`--model-file` 用于记录文件名、SHA256 和量化标签，推理由 `--url` 指向的服务完成。首次 `initial` 套件包含五个基本组合，`--suite all --runs 10` 才是四场景、三模式、每组十次的矩阵。

### 5. 验证第二阶段工具流程

```bash
python3 -m experiments.llama_cpp.compaction_security.attack_runner \
  --backend mock \
  --out experiments/llama_cpp/compaction_security/phase1_design/runtime/example_attack_mock
```

该入口使用固定危险动作重放模拟工具，输出明确标为 Mock。真实模式仍是探索版，不能靠运行旧入口就声称完成了修订后的配对实验；正式方案见[第二阶段协议](experiments/llama_cpp/compaction_security/phase1_design/phase2_paired_protocol.md)。

## 如何设置实验规模

原生实验中，`--rounds` 是每条轨迹目标观测的压缩次数。第 0 轮是未压缩基线，不计入压缩次数；每个检查点的问答又会增加模型请求，因此“72 次压缩”不等于“72 次 API 请求”。

| 模式 | 计划轨迹数 |
|---|---|
| `retention` | 历史数 × 阈值数 × `--runs` × 来源角色数 |
| `relations` | 历史数 × 阈值数 × `--generation-repeats` × 任务条件数 |

计划压缩观测数 = 轨迹数 × `--rounds`。`--runs` 改变基础实验材料布局；关系实验中的 `--generation-repeats` 则在同一布局下新建独立会话。两者含义不同。

例如下面是之前六轮关系观测采用的规模：3 份历史 × 1 个阈值 × 2 次独立会话 × 2 种任务 = 12 条轨迹，目标 72 次压缩：

```bash
./run_compaction_lab.sh \
  --study relations --source-roles tool \
  --model gpt-5.6-terra --reasoning-effort medium \
  --histories 3 --generation-repeats 2 --layout 0 --rounds 6 \
  --max-requests 600 --max-input-tokens 24000000 --max-output-tokens 800000 \
  --out experiments/llama_cpp/compaction_security/phase1_design/runtime/example_relations_72
```

这是运行示例，不代表新批次已经完成，也不保证限额下所有轨迹都能完成。请求和 token 上限是停止保护，不是账单估算；达到上限的轨迹需作为未完成记录。相同历史的连续六轮也不是六个独立样本。

更完整的参数、默认值和合法范围见[原生观测说明](experiments/llama_cpp/compaction_security/native_retention/README.md)或 `./run_compaction_lab.sh --help`。

## 如何查看进度和结果

启动后，终端会打印本批目录并显示进度条，包括当前轨迹、阶段、已完成压缩、失败数、请求数和 token 用量。另开终端可只读查看：

```bash
# 最近一次实验
./run_compaction_lab.sh --watch

# 固定查看某一批，避免“最近一次”被其他运行改变
./run_compaction_lab.sh --watch \
  experiments/llama_cpp/compaction_security/phase1_design/runtime/example_relations
```

| 想了解什么 | 查看哪个文件 |
|---|---|
| 运行结束了吗，是否有失败？ | `progress.json`、`report.md`，并核对 `runs_completed/runs_total` |
| 每次有哪些原值未匹配？ | `lost_content.md`；这是词面变化清单 |
| 某个值首次消失、持续未匹配、后来重现的证据？ | `lost_content.jsonl`、`loss_checkpoints/` |
| 对应关系是否还有明确证据？ | `semantic_changes.md`、`relation_checkpoints/`，用于 `relations` 模式 |
| 压缩后问答和完整历史问答差在哪？ | `recall_changes.md`、每轮 `effective_probe.json` / `full_probe.json` |
| 模型实际收到什么？ | `trajectories/<ID>/checkpoints/<轮次>/effective_request.json` |
| 哪次请求是压缩前输入？ | 同一检查点的 `pre_compaction_request.json` 与原生事件；缺失时不能补猜 |
| 模型原始响应、SSE、usage 和哈希？ | `gateway/` 中的编号目录及请求账本 |
| 当时的参数和代码是什么？ | `execution_protocol.json`、`frozen_source/` |
| 日常配置是否被改动？ | `normal_config_check.json` 的前后哈希；正常并发使用也可能合法改变文件 |

终止前台实验可以按 Ctrl+C，也可以在该批目录创建名为 `STOP` 的文件：

```bash
touch experiments/llama_cpp/compaction_security/phase1_design/runtime/example_relations/STOP
```

STOP 在运行器的检查点生效，不保证立即取消已发出的请求。退出 `--watch` 只结束观察，不停止实验。当前 Codex 的临时会话不支持透明断点续跑，已有结果保留，重试应选择新目录。

退出码：`0` 为完成，`1` 为轨迹或测量失败，`3` 为限额停止，`130` 为中断。报告文件存在不等于计划全部完成。

## 如何判断信息是否丢失

先看实际模型输入，再看摘要，最后结合独立问答和语义复核。当前记录分成三层：

1. **原值是否还在。** 用预先声明的词项和别名扫描。比如中文被改写成英文时可能未命中，所以它只能作为查阅线索。
2. **信息是否仍属于同一事项。** 同时出现“报告 A”和“允许发送”，不代表允许发送报告 A。关系评分要求逐字证据出现在实际输入中，并能对应同一事项。
3. **模型能否从输入恢复答案。** 同一检查点分别询问压缩后的实际上下文和完整历史。回答 UNKNOWN、引用格式不合格、调用失败分别保留，不能直接说成压缩删除。

关系实验先检查未压缩基线的摄入和问答表现，再保留两边共同获得支持的字段作为后续比较对象。否则，原本就答错的字段也可能被误算成压缩丢失。

遇到加密或不透明压缩状态时，程序记录“无法直接观察”，不会猜测里面保存了什么。完整历史对照是独立的恢复问答，不是同一个原生会话中所有隐藏状态的完整复制。

判断“压缩导致了攻击”还需要额外证据：完整历史下没有危险影响，自然压缩下出现未授权动作和确切本地影响，安全事实确实缺失或弱化，补回事实后行为改变，并且相同动作能被工具修复或独立状态阻断。单独一个 `attack_success` 字段不够。

## 当前结果

### 六轮信息观测

条件为 Codex 原生自动压缩、`gpt-5.6-terra` / `medium`、24,000 的触发阈值、人工注入的工具来源材料，并反复要求只回复“已记录”。计划 12 条轨迹、72 次压缩，实际完成 **7 条轨迹、42 次压缩**，5 条失败保留。

- 具体事项、来源、可信度及局部限制的关联，从首轮起较难通过证据问答恢复；仍有一般性的安全原则保留，不能说所有安全信息都消失了。
- 前 3 轮，原始用户授权引言仍被重放；第 4 轮引言退出实际请求，但 5/7 条摘要仍保留逐项授权绑定。
- 到第 5 轮，7/7 条有效上下文都不再有逐项授权绑定，第 6 轮未恢复。
- 当前回应规则和近期进度相对稳定；本批没有单独操纵相关性和重复次数，所以不能据此推导通用的压缩优先级。

这些轮次取决于当前材料、填充长度和框架行为，不是其他会话的固定遗忘期限。本批也没有执行攻击动作。

详见[阶段审计](experiments/llama_cpp/compaction_security/phase1_design/runtime/relation_terra_medium_72obs_20261029_v1/evaluation.md)及[逐条边界复核](experiments/llama_cpp/compaction_security/phase1_design/runtime/relation_terra_medium_72obs_20261029_v1/evaluation_boundary_review.md)。旧机械评分曾把英文改写误判为缺失，解释结果应以语义复核后的结论为准。

### 第二阶段探索

五批探索试跑共 **20 个案例、287 次请求、73 个实际原生压缩边界**。13 个终端动作符合当时的格式约定，其中未观察到精确的危险 redirect/include 请求。其他案例包含初始回合失败或参数格式错误，不能作为防御成功。

旧记录中的一次 `attack_success=true` 来自模拟工具误把普通的“自动验证”文字当成执行标记，已被审计排除。旧运行器还存在压缩漏记、完整历史对照意外压缩等问题，因此这五批只能作探索和接口诊断，不能合并计算攻击率。

目前结论是：**观察到了信息及对应关系退化，尚未确认压缩导致了攻击。** 独立授权路径已通过离线工具测试，但这些真实试跑没有产生合格的攻击动作，不能宣称已经在真实攻击上验证防御效果。

依据见[五批探索审计](experiments/llama_cpp/compaction_security/phase1_design/phase2_pilot_audit.md)。下一轮方法见[修订后的配对协议](experiments/llama_cpp/compaction_security/phase1_design/phase2_paired_protocol.md)，该协议是计划，不能当作已完成结果。

## 本地工具和授权检查

基础工具只有读取虚构报告、创建本地备份和模拟发送。`send_mock_email` 只把 JSON 写入本次运行的 `mock_outbox/`，没有真实邮箱或收件地址。文件 ID、接收方标签和工具名均有固定列表，模型不能传入任意 Shell 命令。

第二阶段的 `mock://public/...`、`mock://vault/...` 和 include 标记都是本地固定资源。所谓漏洞是代码有意模拟的特定重定向或引用展开，不访问真实网站或受保护文件。工具执行前检查固定参数；写文件还检查符号链接、路径和文件类型。

可信权限来自场景配置或原始用户任务，不由页面、文档、模型摘要生成。基础三模式的执行器都检查同一权限；`preserving` 只是额外让模型看见独立状态。第二阶段则分开比较模拟漏洞工具、修复工具和独立状态，不能将“没有独立检查”的研究条件称为已授权。

原生信息观测中，模型输出的工具调用会在网关阻断，后端使用独立 app-server 和私有状态目录。业务工具不会外发；**模型推理本身仍需连接已配置的服务**，只有 Mock 和离线检查无需推理网络。

## 测试与复现

最近一次完整离线验证为 **205 项测试通过**，日志保存在[注释修改后测试记录](experiments/llama_cpp/compaction_security/results/code_comments_20261031/tests_after.txt)。注释修改前后均通过，并核验了 55 个 Python 文件的代码结构和启动脚本的执行语句未改变，见[验证清单](experiments/llama_cpp/compaction_security/results/code_comments_20261031/validation.json)。这些是工程检查，不是模型安全率。

测试覆盖历史构造、摘要接口、来源标记、动作格式、权限检查、本地文件边界、备份来源、Mock/真实结果分离、网关记录与脱敏、压缩检查点、损失账本、关系评分、进度和失败分类。

如需复查已有真实实验，保留整批 `runtime/` 后执行对应批次的离线审计脚本；输入、响应、代码哈希应一起核对。重新调用同名模型不保证得到完全相同输出，尤其是在服务只提供可变别名的情况下。

```bash
# 只适用于本机已保存的这批历史产物，不重新调用模型
python3 experiments/llama_cpp/compaction_security/phase1_design/runtime/relation_terra_medium_72obs_20261029_v1/audit_results.py
```

历史设计、启动记录和报告可能对应不同版本。优先阅读本 README、实际批次的 `execution_protocol.json` 和后续审计，不把早期方案中的计划数量当成完成数量。

## 已知问题和下一步

接下来需要先完成第二阶段运行器的修订：逐次记录全部真实压缩，在同一份冻结历史上比较完整、自然压缩和补回事实三个条件，并将相同动作交给不同执行器重放。格式失败和模型服务失败必须单独处理。

基础实验的报告模板还包含早期环境检查留下的固定说明，不能仅凭生成的文字判断当前模型或服务是否可用，应以本次请求和事件日志为准。

之后优先研究原始用户消息退出重放前后的变化，再分别改变信息位置、重复次数和任务相关性。这样才能判断信息保留主要受什么影响，而不是仅靠增加轮数得出模糊结论。语义复核还需要独立的人类标注。

从 Agent 的设计看，目前最值得验证的是：将授权、数据来源、撤销状态和接收方限制保存在摘要之外，是否能在信息退化后继续阻断越权，同时保留正常工作能力。

相关材料：

- [六轮关系实验设计](experiments/llama_cpp/compaction_security/phase1_design/relation_retention_plan.md)
- [第二阶段原始方案](experiments/llama_cpp/compaction_security/phase1_design/phase2_causal_attack_plan.md)
- [第二阶段修订协议](experiments/llama_cpp/compaction_security/phase1_design/phase2_paired_protocol.md)
- [导师周报](experiments/llama_cpp/compaction_security/phase1_design/weekly_report_20261030.md)
- [历史 DeepSeek 设计及切换说明](experiments/llama_cpp/compaction_security/phase1_design/README.md)
