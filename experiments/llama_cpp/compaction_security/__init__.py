# 代码阅读路线：
# 1. run_compaction_lab.sh：常用参数与一键启动；--watch 只查看进度。
# 2. native_retention/codex_runner.py：当前 Codex 原生压缩的实验矩阵和每条轨迹。
# 3. native_retention/codex_backend.py、responses_gateway.py：隔离会话与原始请求记录。
# 4. native_retention/relation_corpus.py：预定义的事项、来源、授权和合成材料。
# 5. native_retention/relation_measurement.py：逐项答案、原文引证与对应关系的检查。
# 6. native_retention/loss_ledger.py、reporting.py：逐轮变化记录和统计汇总。
# 7. attack_scenarios.py、attack_tools.py：第二阶段的合成载荷和本地工具。
#    attack_runner.py、causal_evaluation.py 保留探索版逻辑，已知问题写在相应分支旁；
#    正式结论以 phase1_design/phase2_pilot_audit.md 与修订协议为准。
# 8. run_experiment.py、agent_runner.py：最初的 Mock/llama.cpp 外置摘要实验，
#    它与 Codex 原生压缩是不同流程，不能将两者结果直接混合。
# 9. tests/：各测试旁解释了条件与预期；只验证本地行为，不构成真实模型攻击证据。
# runtime/ 中的源码副本是历史实验的冻结材料，不随当前代码的注释修改而更新。
# 基础实验包入口。可先阅读 run_experiment → agent_runner → compactor/policies/tools，再看 native_retention 中的原生压缩实验。
"""Local compaction boundary security experiment."""
