# 定义可信安全状态和模型动作格式。解析只检查格式及固定对象范围，不代表用户已经授权；授权检查在 policies 中单独进行。
from dataclasses import dataclass, field, asdict
from typing import Any


# 可信权限与数据来源的快照。frozen 阻止字段重新赋值，但 file_sources 仍是可变字典，不能误认为深度不可变。
@dataclass(frozen=True)
class SecurityState:
    # 原始用户目标，作为独立于摘要的任务依据。
    goal: str
    # 可信配置允许的工具名；出现在协议白名单中并不自动出现在这里。
    authorized_tools: tuple[str, ...]
    # 显式禁止的动作；与允许列表冲突时由拒绝优先。
    forbidden_actions: tuple[str, ...]
    # 数据敏感等级，用于保存与展示；当前简单授权器没有按等级再细分规则。
    sensitivity: str
    # 原始数据来源描述。
    data_source: str
    # 来源可信度标签，供记录和模型理解；不能由文档自称提升。
    source_trust: str
    # 谁给出的合法授权，与文档里的“已经批准”声明区分。
    authorizer: str
    # 这次任务允许的模拟接收方列表。
    recipients: tuple[str, ...]
    # 授权/撤销历史记录；当前授权器使用现行列表，不自动解释这段历史。
    authorization_history: tuple[str, ...]
    # 文件 ID → 数据来源。当前是已登记来源检查，不是完整文件内容或派生链验证。
    file_sources: dict[str, str] = field(default_factory=dict)

    # 转为普通字典，供日志和 preserving 模式展示；不会从模型输出反向更新权限。
    def json(self):
        return asdict(self)


# 模型提出的单个工具动作；arguments 内容仍需 parse_action 验证。
@dataclass(frozen=True)
class Action:
    action: str
    arguments: dict[str, Any]


# 输入原始模型文本，输出 Action；拒绝额外字段、未知动作、非字符串参数及未注册对象。格式不符抛错，不自动修复。
def parse_action(raw: str) -> Action:
    import json
    from .config import TOOLS, ALLOWED_FILES, RECIPIENTS
    value = json.loads(raw)
    if not isinstance(value, dict) or set(value) != {"action", "arguments"}:
        raise ValueError("action must have exactly action and arguments")
    name, args = value["action"], value["arguments"]
    # 这里的 TOOLS 是协议白名单；是否获用户批准还要检查 authorized_tools。
    if name not in TOOLS or not isinstance(args, dict):
        raise ValueError("unknown action or invalid arguments")
    expected = {"no_action": set(), "read_demo_report": {"file_id"},
                "write_local_backup": {"file_id"},
                "send_mock_email": {"file_id", "recipient"}}[name]
    if set(args) != expected or any(not isinstance(v, str) for v in args.values()):
        raise ValueError("invalid argument schema")
    if "file_id" in args and args["file_id"] not in ALLOWED_FILES:
        raise ValueError("unknown file id")
    # unapproved-recipient 也在固定模拟列表中，用来测试“对象合法但未获授权”的情况。
    if "recipient" in args and args["recipient"] not in RECIPIENTS:
        raise ValueError("unknown recipient")
    return Action(name, args)
