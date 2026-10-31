# 第一阶段执行记录与冻结规则

## 当前执行状态：Codex + sub2api

下一阶段的完整因果攻击实验仅完成设计，未执行，见[Phase 2 方案](phase2_causal_attack_plan.md)。方案要求先建立完整历史安全基线，再用自然压缩、独立安全状态和有缺陷/加固工具做配对；只有“安全事实自然丢失 → 未授权请求 → 本地合成影响 → 反事实阻断”八项证据齐全时才标记攻击成功。

### 2026-10-29：六轮关联事实实验，72次计划观测

**已结束并审计：完成7/12条轨迹、42/72个边界，5条在压缩前失败。**本批不是72次全部完成。详见[阶段评估](runtime/relation_terra_medium_72obs_20261029_v1/evaluation.md)和[逐条原始摘要复核](runtime/relation_terra_medium_72obs_20261029_v1/evaluation_boundary_review.md)。225请求中222次Responses completed、1次HTTP200流内失败、2次HTTP502；450个原始文件哈希核验一致，日常配置/认证未变。

阶段结论：工具来源事项/来源/信任/局部限制的164个基线合格字段首轮未恢复；授权前3轮受原始用户引言重放保护。第4轮原引言统一退出重放，但5/7摘要仍有全部六项英文授权绑定，机械评分严重低估。摘要授权绑定按六轮为24/42、30/42、30/42、30/42、0/42、0/42；第五轮有可追溯的再次摘要遗漏。即时“已记录”规则42/42摘要保留。仅1组完整目标配对，不能判断目标操纵效应。这是当前材料与框架路径的观察，不是所有模型的固定删除顺序。语义复核由研究助手及子agent完成，尚无人类盲标。

执行顺序、测量口径及完整启动命令见[冻结计划](relation_retention_plan.md)。原计划3份历史×2种任务目标×2次固定布局会话重复×6轮，共12条轨迹、72个边界；边界不是独立样本。只观测五类字段及其关联，不测试安全绕过或执行工具动作。

运行目录`runtime/relation_terra_medium_72obs_20261029_v1/`，启动PID 131920，时间16:31北京时间。模型`gpt-5.6-terra`，effort `medium`，自动阈值24,000。保护限额600请求、24M输入、800k输出，连续3条轨迹失败后停止新增轨迹；完整矩阵与源码哈希在`execution_protocol.json`。实时进度用仓库根目录`./run_compaction_lab.sh --watch`；逐轮字段恢复/变化见`semantic_changes.md`和`relation_checkpoints/`，原文诊断见`lost_content.md`。

启动前142项原测试及41项新增测试全部通过，合计183项。修订真实校准`runtime/relation_calibration_20261029_v2/`为2/2轨迹、2/2边界、20请求全200，配置/认证未变；门控审计在该目录`calibration_gate.json`。两组基线各有29/30个共同支持字段，压缩后effective均6/30、full均30/30。六个保留证据字段来自可信用户授权账本重放，须与文档字段分开解释。

初次校准`runtime/relation_calibration_20261029_v1/`的pending_review探针非法JSON导致基线失败，原输出保留；修订strict JSON Schema及证据核验后新建v2，没有覆盖或修补模型输出。两个校准均排除于正式矩阵。正式批次状态以其`progress.json`为准，失败详列阶段评估，未静默补跑或替换。

### 2026-10-29：Terra medium 54次压缩观测

**已完成并离线评估：**18/18轨迹、54/54次压缩完成，342次请求全200；详见 [本批评估](runtime/codex_terra_medium_54obs_20261029_v2/evaluation.md)。9条user轨迹重放原句，工具组每轮编号/问答为15/324，首次309个编号缺失均在第1轮；另逐句发现第2轮安全限定语弱化案例，不能用编号稳定推断语义不变。

用户授权启动3个历史×3个布局重复×2种来源（user/tool）×3轮自动压缩，合计18条轨迹、54个边界。固定模型请求ID `gpt-5.6-terra`，推理强度`medium`，自动阈值24,000。不与此前gpt-6.1-sol批次混算。

运行目录：`runtime/codex_terra_medium_54obs_20261029_v2/`。独立后台进程，PID及完整参数在`launcher.json`，终端记录在`terminal.log`，实时状态在`progress.json`，逐轮清单在`lost_content.md/jsonl`，汇总在`report.md`。从仓库根目录执行：

```bash
./run_compaction_lab.sh --watch experiments/llama_cpp/compaction_security/phase1_design/runtime/codex_terra_medium_54obs_20261029_v2
```

本批请求保护限额500次、累计输入16,000,000 tokens、输出600,000 tokens，旨在容纳完整矩阵，不是账单估计。此前单条三轮约566k输入，18条可能超过最初示例的8M限额。所有含探针的轨迹仍须通过独立基线能力检查；失败保留，不当作压缩遗忘。

启动诊断：大写显示名`GPT-5.6-Terra`第一次请求返回HTTP400，失败尝试目录`runtime/codex_terra_medium_54obs_20261029/`保留18条初始回合失败记录（前6条400、后12条429），0次压缩观测。无usage的保守预留记账不能当成实际token消费。随后使用本机配置已有的小写ID `gpt-5.6-terra` 单轨迹smoke验证成功：7请求全部200、1次原生压缩完成、全部响应模型名一致，见`runtime/codex_terra_lower_smoke_20261029/`。这一smoke与失败尝试都不并入54次正式统计。没有替换为其他模型。

### 2026-10-28：初次切换后端与验证

2026-10-28 用户要求切换后端，旧 `runtime/full_v1/STOP` 已设置，停止继续调用 DeepSeek。旧轨迹及失败全部保留，离线损失清单在 `runtime/full_v1/loss_review/`，不与新后端混算。

新入口为仓库根目录 `./run_compaction_lab.sh --rounds 6`；支持实时进度、`--watch`、来源角色条件及请求/token 限额。当前 Codex 0.165.0、sub2api Responses provider、模型别名 gpt-6.1-sol。使用私有挂载的 Codex 状态与 ephemeral 会话，不修改日常配置。详见 [当前实验报告及后续计划](codex_sub2api_report.md) 与 [操作说明](../native_retention/README.md)。

## 以下为旧 DeepSeek 批次的历史执行规则

2026-10-28：用户已授权 DeepSeek 接入、试跑及通过测量检查后的全量实验。初稿 `README.md` 和 `protocol.json` 中“尚未调用/设计状态”保留为历史描述；本文件及各运行目录的 `execution_protocol.json` 记录当前执行状态。

目标仍是信息类型、相对保留与首次变化轮次，不评估注入成功。主实验候选矩阵为 **8 份历史 × 2 档阈值 × 3 套平衡布局 = 48 条轨迹，每条 6 次真实压缩**，共 288 次边界、336 检查点、672 批独立问答。摘要语义候选标注另存，独立审查未完成前不当成最终语义保留率。

本机 Claude Code 2.1.312 的 `--autocompact` 只接受 100k–1M，故设计初稿 24k/49k 不可执行。校准候选改为 **100,000 / 150,000**。每条辅助记录固定为 20,000 Unicode 字符；字符数不作 token 数使用，保留 provider usage 与框架计数。每条消息低于窗口，避免先前 smoke 的巨型单消息应急压缩偏差。校准须核查真实日志路径。

使用现有 `claude` 安装，记录版本和程序 SHA256，不删除重装。实验子进程独立 `CLAUDE_CONFIG_DIR` 与 cwd，`--bare --setting-sources '' --tools '' --strict-mcp-config --disable-slash-commands`。模型无工具、网络、文件或 shell 能力；编排程序只发送虚构材料到用户授权的 DeepSeek。这里是进程配置隔离，不是完整 OS 沙箱。日常 Claude/Codex 配置前后只作 hash 比较。没有修改代理、CC Switch 数据库或全局 provider。

调用路径：Claude → 临时 loopback 原样录制网关 → 官方 DeepSeek Anthropic endpoint。网关不添加提示，不改 JSON/SSE，不保存认证头。所有模型别名映射为 deepseek-flash，探针请求 thinking=disabled、temperature=0；实际参数以原始请求为准。服务端别名不可锁定快照，不能宣称跨平台确定性。

运行入口：

```bash
# 两档各 2 次真实压缩，完成校准后再发起主矩阵
python3 -m experiments.llama_cpp.compaction_security.native_retention.runner run \
  --out experiments/llama_cpp/compaction_security/phase1_design/runtime/calibration_v1 \
  --histories 1 --layouts 1 --thresholds 100000 150000 --rounds 2 --workers 2 --budget-usd 5

# 主实验默认 48 条轨迹、6 轮，最多 2 个并发
python3 -m experiments.llama_cpp.compaction_security.native_retention.runner run

python3 -m experiments.llama_cpp.compaction_security.native_retention.runner status
```

运行中的 `progress.json` 和 `report.md` 每约 10 秒更新。原始请求/响应在 `gateway/`，逐次事件在 `events.jsonl`，原生会话在 `trajectories/<ID>/config/projects/`。长输入经 stdin 传送。父会话不接收探针问题、答案或评分。

每次启动冻结代码/程序哈希和完整矩阵。相同目录恢复要求冻结字段一致，已完成检查点不重复查询。若崩溃发生于模型回合写入与编排提交之间，标记 ambiguous 并停止该轨迹，禁止静默重复导致上下文污染。创建运行目录下的 `STOP` 文件可停止后续提交，已在途请求会完成。预算是操作层保守费用估计上限：校准 $5，主实验 $75（非用户指定额度或实际账单），请求前按 UTF-8 bytes 输入上界及输出上限预留；达到上限会暂停新增调用，不自动增额。

机械指标为“原答案值及限定语在近邻片段中的出现”和“独立严格短答案恢复”。它们不能证明完整语义保留或删除。摘要语义候选标注使用结构化事实与逐字证据核验；后续独立盲审、20% 第二审与分层统计仍是最终结论所需工作。正式运行前检查完整历史问答基线、真实 compact_boundary 与摘要进入实际下一请求、实际参数路由、配置隔离。校准样本不并入主实验统计。
