# 执行层独立授权判断。每次动作执行前检查可信配置，不从摘要或工具返回文本中接受新权限。
from .schemas import Action, SecurityState


# 按 no_action、工具权限、文件来源、接收方依次判断，返回是否允许及原因；未知来源的文件不能交给工具。
def authorize(action: Action, state: SecurityState) -> tuple[bool, str]:
    if action.action == "no_action":
        return True, "no action"
    # 拒绝列表优先：即使工具同时出现在允许列表，只要被禁止仍然拒绝。
    if action.action not in state.authorized_tools or action.action in state.forbidden_actions:
        return False, "tool not authorized"
    file_id = action.arguments.get("file_id")
    if file_id and file_id not in state.file_sources:
        return False, "file provenance missing"
    if action.action == "send_mock_email" and action.arguments["recipient"] not in state.recipients:
        return False, "recipient not authorized"
    return True, "authorized"
