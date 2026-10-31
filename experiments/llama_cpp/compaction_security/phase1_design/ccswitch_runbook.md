# DeepSeek 接入与隔离运行手册

本文件记录第一阶段实验的启动准备，不会修改日常 Codex、Claude Code 或 CC Switch 配置。

## 当前诊断

- WSL 中 Claude Code 已为 `2.1.312`，`claude doctor` 报告安装正常。
- 本机 Codex CLI 为 `0.165.0`。
- Windows 侧 CC Switch 为 `3.14.1`，当前 Claude provider 指向 DeepSeek Anthropic endpoint；CC Switch 本地代理关闭，配置端口为 `127.0.0.1:15721`。
- Windows `~/.claude` 与 WSL `/home/haomin_wang/.claude` 是两套目录。CC Switch 不会自动桥接 WSL，除非显式设置 Claude Directory。
- WSL 直接访问 `https://api.deepseek.com/anthropic` 可达；没有认证时返回 HTTP 401。当前 shell 中的旧认证尝试 `GET /models` 也返回 401。没有输出或保存旧 token 内容。
- 2026-10-28 最小 Python Anthropic 请求使用 `x-api-key` 成功返回 `deepseek-flash` 和 `OK`。同一 key 通过隔离 Claude Code 的 `ANTHROPIC_API_KEY` 直连仍返回 401；改用隔离进程的 `ANTHROPIC_AUTH_TOKEN` 后，Claude Code 2.1.312 成功返回 `OK`。因此本机 Claude→DeepSeek 的可用变量是 `ANTHROPIC_AUTH_TOKEN`，而 `--bare` 的 help 对该变量存在表述差异，必须在每次版本升级后复验。
- 当前进程有指向 `127.0.0.1:7892` 的代理变量；该代理未作为本实验的修改对象。验证直连时仅使用命令级 `--noproxy '*'`，不改变 shell 或系统代理。

## 密钥交接

将 DeepSeek key 单独写入：

`/home/haomin_wang/.local/share/compaction-security-lab/secrets/deepseek_api_key`

文件已经创建，权限为 `600`，当前为空。只写一行 key，不要写 `export`、JSON、引号或空格。key 不进入 Git、命令行参数、CC Switch deeplink、调试日志、JSONL 事件或聊天消息。运行器只从文件读取到子进程内存，并在退出时清除引用；日志只记录 key 是否存在和脱敏的 endpoint/model 信息。

用户保存后只需在聊天中回复“已保存”。在此之前不发送付费模型请求。

已完成一次低成本连接验证：密钥文件非空、权限 600；Python 兼容请求成功；Claude Code 隔离 smoke 成功。该 smoke 没有使用 CC Switch 本地代理，不能冒充 CC Switch 路由验证。CC Switch 代理仍保持关闭，避免影响日常使用。

## 不影响日常使用的启动原则

CC Switch 全局 provider 不切换。实验先使用自己的子进程环境和工作目录。优先方案是实验专用 Claude 配置目录；若需要 CC Switch 管理它，使用 CC Switch 的 Claude Directory 指向实验专用 WSL 目录，而不是覆盖当前 Windows 或 WSL 目录。CC Switch 的本地代理只有在确认“仅 Claude 实验进程”路由、WSL 可访问 Windows 监听、并能恢复原开关后才考虑启用；当前不启用。

Claude Code 的官方文档支持用 `CLAUDE_CONFIG_DIR` 为设置、会话历史和插件指定独立目录；本机 help 还支持 `--bare`、`--setting-sources`、`--settings`、`--strict-mcp-config`、`--tools ""`、`--debug-file` 和会话 ID。正式压缩实验会使用独立目录、空 MCP、无工具或仅 fixture 工具、固定 session id、独立 debug 文件和结果目录。`--bare` 的认证行为要先做冒烟验证：本机 help 指明它读取 `ANTHROPIC_API_KEY` 或显式 helper，不应直接假定 CC Switch 的 `ANTHROPIC_AUTH_TOKEN` 变量在 bare 模式有效。

Codex 实验单独使用独立配置/home 或容器，关闭共享 daemon；不执行 DeepSeek 官方全局安装脚本，因为该脚本会写入用户级 Codex 配置。Codex 与 Claude 的 provider 结果分别记录。

## 兼容性闸门

密钥写入后只做一条低成本请求，确认：

1. DeepSeek 实际返回的 model 名称、协议和版本字段；
2. Claude Code 的 provider 变量和模型映射是否被 CC Switch/实验 wrapper 正确应用；
3. 请求是否经过当前代理、CC Switch 本地代理或直连；
4. 空工具、短会话和隔离日志是否生效；
5. Claude `PreCompact`/`PostCompact` 事件、实际 compact summary 和下一轮有效输入能否捕获。

当前连接闸门已通过前三项中的 endpoint、认证和最小 Claude 请求；CC Switch 代理路径与原生 compact 观测仍未通过，进入正式实验前单独验证。

如果只得到普通回答而没有原生 compact 事件，结果只能进入“连接/能力试跑”，不能进入第一阶段压缩统计。若 DeepSeek provider 不支持框架需要的 compact 行为，则改为记录不兼容，并保留已有离线摘要实验作为方法预演，不伪称原生压缩。

## 恢复

实验结束后删除专用密钥文件和临时配置目录，恢复 CC Switch 原有 Claude Directory 与本地代理开关。恢复前后只核对配置文件 hash、provider 名称、endpoint 主机和开关状态，不打印凭据或完整配置。

参考文档：

- [DeepSeek Claude Code integration](https://api-docs.deepseek.com/quick_start/agent_integrations/claude_code/)
- [DeepSeek Codex integration](https://api-docs.deepseek.com/quick_start/agent_integrations/codex/)
- [Claude Code environment variables](https://code.claude.com/docs/en/env-vars)
- [Claude Code settings](https://code.claude.com/docs/en/settings)
- [Claude Code context window](https://code.claude.com/docs/en/context-window)
